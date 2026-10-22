"""
others/carica_master_wifi.py — WAKEUPeSTALTA
---------------------------------------------
Carica arduino/master/master.ino sul Mega del master VIA WIFI (modulo ESP con
esp-link collegato alla seriale e al RESET del Mega), senza cavo USB.

Uso (la Shelly deve essere ACCESA: l'ESP prende corrente dal Mega):
    python others/carica_master_wifi.py            (IP dell'ESP di default)
    python others/carica_master_wifi.py 10.27.239.225

Cosa fa, da solo:
  1. compila master.ino con arduino-cli (quello dell'Arduino IDE)
  2. lo carica attraverso l'ESP e rilegge OGNI pagina per verificarla
  3. aspetta e fa ripartire il master finche' non e' in MODALITA 0
     (produzione): ogni caricamento lo fa ripartire, e due ripartenze a meno
     di 2.5s di distanza lo farebbero passare alla modalita' successiva
     (selettore di modalita' di master.ino)
  4. stampa cosa dice il master all'avvio: frequenze, tempi, nodi

Perche' non avrdude: la versione per Windows non riesce a usare una porta di
rete ("ser_drain(): read error"). Qui si parla direttamente il protocollo
del bootloader del Mega (STK500v2) sulla porta 23 di esp-link.
Se qualcosa va storto il bootloader resta intatto: basta rilanciare.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ESP_DEFAULT = "10.27.239.225"
SKETCH = Path(__file__).resolve().parent.parent / "arduino" / "master"
FQBN = "arduino:avr:mega:cpu=atmega2560"
BAUD_BOOTLOADER = 115200
BAUD_SKETCH = 9600        # Serial.begin(9600) in master.ino
PAGINA = 256              # byte per pagina flash dell'ATmega2560
ATTESA_SESSIONE_S = 4.0   # > 2.5s (SOGLIA_SESSIONE_LUNGA_MS in master.ino)


# ----------------------------------------------------------------------
def trova_arduino_cli() -> str:
    candidati = [shutil.which("arduino-cli"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"),
                 r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"]
    for c in candidati:
        if c and Path(c).exists():
            return c
    raise SystemExit("ERRORE: arduino-cli non trovato (installa l'Arduino IDE 2).")


def compila() -> Path:
    uscita = Path(tempfile.mkdtemp(prefix="master_build_"))
    print(f"1. Compilo {SKETCH / 'master.ino'} ...")
    r = subprocess.run([trova_arduino_cli(), "compile", "--fqbn", FQBN, "--output-dir", str(uscita), str(SKETCH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        raise SystemExit("ERRORE: compilazione fallita (vedi sopra).")
    riga = next((l for l in r.stdout.splitlines() if "Sketch uses" in l), "")
    print(f"   ok. {riga.strip()}")
    return uscita / "master.ino.hex"


def leggi_hex(percorso: Path) -> bytes:
    mem, base = {}, 0
    for riga in open(percorso):
        riga = riga.strip()
        if not riga.startswith(":"):
            continue
        b = bytes.fromhex(riga[1:])
        indirizzo, tipo, dati = (b[1] << 8) | b[2], b[3], b[4:4 + b[0]]
        if tipo == 0:
            for i, v in enumerate(dati):
                mem[base + indirizzo + i] = v
        elif tipo == 2:
            base = ((dati[0] << 8) | dati[1]) << 4
        elif tipo == 4:
            base = ((dati[0] << 8) | dati[1]) << 16
        elif tipo == 1:
            break
    dati = bytes(mem.get(i, 0xFF) for i in range(max(mem) + 1))
    return dati + b"\xff" * (-len(dati) % PAGINA)


# ----------------------------------------------------------------------
def http(esp: str, percorso: str) -> None:
    urllib.request.urlopen(urllib.request.Request(f"http://{esp}{percorso}", method="POST"), timeout=5)


def imposta_baud(esp: str, baud: int) -> None:
    http(esp, f"/console/baud?rate={baud}")


class Stk500v2:
    """Il minimo del protocollo del bootloader del Mega (STK500v2)."""

    def __init__(self, sock: socket.socket):
        self.s, self.seq = sock, 1

    def comando(self, corpo: list[int], attesa: float = 1.0) -> bytes | None:
        pacchetto = bytes([0x1B, self.seq, len(corpo) >> 8, len(corpo) & 0xFF, 0x0E]) + bytes(corpo)
        ck = 0
        for x in pacchetto:
            ck ^= x
        self.s.sendall(pacchetto + bytes([ck]))
        buf, t0 = b"", time.time()
        while time.time() - t0 < attesa:
            try:
                buf += self.s.recv(4096)
            except socket.timeout:
                pass
            i = buf.find(bytes([0x1B, self.seq]))
            if i >= 0 and len(buf) >= i + 5:
                n = (buf[i + 2] << 8) | buf[i + 3]
                if len(buf) >= i + 6 + n:
                    self.seq = (self.seq + 1) & 0xFF
                    return buf[i + 5:i + 5 + n]
        return None

    @staticmethod
    def indirizzo(byte_addr: int) -> list[int]:
        w = byte_addr >> 1  # il bootloader vuole l'indirizzo in parole da 2 byte
        return [0x06, 0x80 | ((w >> 24) & 0x7F), (w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF]


def carica(esp: str, dati: bytes) -> None:
    pagine = len(dati) // PAGINA
    print(f"2. Carico via WiFi ({pagine} pagine da {PAGINA} byte) ...")
    imposta_baud(esp, BAUD_BOOTLOADER)
    try:
        s = socket.create_connection((esp, 23), timeout=5)
        s.settimeout(0.05)
        p = Stk500v2(s)
        http(esp, "/console/reset")
        t0, firma = time.time(), None
        while time.time() - t0 < 3 and firma is None:  # il bootloader ascolta ~1s dopo il reset
            r = p.comando([0x01], attesa=0.08)
            if r and len(r) > 2 and r[0] == 0x01 and r[1] == 0x00:
                firma = r[3:3 + r[2]].decode("latin-1")
        if firma is None:
            raise SystemExit("ERRORE: il bootloader del Mega non risponde (diodo sul RESET? cavi seriali?).")
        s.settimeout(0.2)
        r = p.comando([0x10, 0xC8, 0x64, 0x19, 0x20, 0x00, 0x53, 0x03, 0xAC, 0x53, 0x00, 0x00])
        if not r or r[1] != 0x00:
            raise SystemExit("ERRORE: il bootloader rifiuta la programmazione.")
        for n in range(pagine):
            a = n * PAGINA
            for _tentativo in range(3):  # sul WiFi un pacchetto ogni tanto si perde: si riprova la pagina
                p.comando(Stk500v2.indirizzo(a))
                r = p.comando([0x13, PAGINA >> 8, PAGINA & 0xFF, 0xC1, 0x0A, 0x40, 0x4C, 0x20, 0x00, 0x00]
                              + list(dati[a:a + PAGINA]), attesa=2)
                if r and r[1] == 0x00:
                    break
            else:
                raise SystemExit(f"ERRORE: scrittura della pagina {n} fallita 3 volte. Rilancia lo strumento.")
        sbagliate = 0
        for n in range(pagine):
            a = n * PAGINA
            p.comando(Stk500v2.indirizzo(a))
            r = p.comando([0x14, PAGINA >> 8, PAGINA & 0xFF, 0x20], attesa=2)
            if not r or r[1] != 0x00 or bytes(r[2:2 + PAGINA]) != dati[a:a + PAGINA]:
                sbagliate += 1
        p.comando([0x11, 0x01, 0x01])  # fine programmazione: parte il programma
        s.close()
    finally:
        imposta_baud(esp, BAUD_SKETCH)
    if sbagliate:
        raise SystemExit(f"ERRORE: {sbagliate} pagine diverse da quelle inviate. Rilancia lo strumento.")
    print(f"   ok. Verifica: {pagine}/{pagine} pagine identiche.")


def riparti_e_leggi(esp: str) -> str:
    s = socket.create_connection((esp, 23), timeout=5)
    s.settimeout(0.2)
    http(esp, "/console/reset")
    testo, t0 = b"", time.time()
    while time.time() - t0 < 4:
        try:
            testo += s.recv(4096)
        except socket.timeout:
            pass
    s.close()
    return testo.decode("latin-1", "replace")


def porta_in_produzione(esp: str) -> None:
    print("3. Controllo che il master riparta in produzione (MODALITA 0) ...")
    for tentativo in range(4):
        time.sleep(ATTESA_SESSIONE_S)  # sessione "lunga": la prossima ripartenza torna in modalita' 0
        testo = riparti_e_leggi(esp)
        if "MODALITA 0" in testo:
            print("   ok.\n4. Il master dice:")
            for riga in testo.strip().splitlines():
                if not riga.startswith("Pronto"):
                    print("   " + riga)
            return
        modo = next((l for l in testo.splitlines() if "MODALITA" in l), "nessun messaggio di avvio")
        print(f"   ripartito in: {modo.strip()} - riprovo")
    raise SystemExit("ERRORE: il master non torna in MODALITA 0. Spegni e riaccendi la Shelly (lasciala accesa >5s).")


def main() -> None:
    esp = sys.argv[1] if len(sys.argv) > 1 else ESP_DEFAULT
    try:
        socket.create_connection((esp, 23), timeout=4).close()
    except OSError:
        raise SystemExit(f"ERRORE: ESP {esp} non raggiungibile. La Shelly e' accesa? Il PC e' sulla stessa rete?")
    dati = leggi_hex(compila())
    carica(esp, dati)
    porta_in_produzione(esp)
    print("\nFATTO: codice nuovo caricato e master in produzione.")


if __name__ == "__main__":
    main()
