"""
analizza_tempi_minimi.py
--------------------------
Analisi automatica del delta t minimo affidabile, pensata per lavorare
insieme a `sweep_tempi/sweep_tempi.ino` (nella stessa cartella TEST TEMPI/ -
SOLO quello sketch, non trasmettitore.ino: qui serve un tono singolo a
impulsi di durata variabile, non il protocollo f1/f2 completo).

Idea: invece di ricostruire finestre temporali allineate a ogni singolo
impulso (fragile, dipende da una sincronizzazione precisa tra Arduino e
Python che la sola seriale non garantisce), si ricalcola la potenza
Goertzel blocco per blocco (20ms, come BLOCCO_GOERTZEL_MS in main.py) su
tutto il gradino, e ci si fa passare sopra la STESSA macchina a stati a
isteresi (ON/OFF) usata davvero in main.py (MacchinaStatoFrequenza). Si
conta quante transizioni a ON vengono rilevate: se il canale riesce a
seguire il pattern comandato, il conteggio deve combaciare con
RIPETIZIONI_PER_GRADINO (una transizione per ogni impulso). Se le
transizioni rilevate sono meno:
- in FASE A (ON che si accorcia) → gli impulsi più corti probabilmente non
  fanno più in tempo a superare SOGLIA_ON: si sono "persi" (mai contati).
- in FASE B (gap che si accorcia) → i gap più corti probabilmente non fanno
  più in tempo a scendere sotto SOGLIA_OFF: due impulsi consecutivi si
  fondono in una sola transizione invece di due.
Questo approccio non richiede nessun allineamento preciso tra i cicli reali
sull'Arduino e i blocchi calcolati qui: conta solo il numero totale di
transizioni nell'intero gradino.

IMPORTANTE: FREQ_TEST_HZ, SOGLIA_ON, SOGLIA_OFF qui sotto vanno tenuti
allineati a mano con F1_HZ_DEFAULT/SOGLIA_ON_F1_DEFAULT/SOGLIA_OFF_F1_DEFAULT
in ../main.py (e con FREQ_TEST_HZ nello sketch .ino) - il risultato vale
solo per la combinazione frequenza+soglie con cui è stato misurato.

Uso: identico ad analizza_armoniche_sweep.py in TEST ARMONICHE/ (stessa
gestione seriale/UDP/ON-OFF automatico). A differenza di quello script, qui
lo sweep è finito (non gira in loop): lo script si ferma da solo quando
vede "SWEEP_FINITO" dall'Arduino e stampa subito il riepilogo.
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import time
import socket
import json
import threading
import queue
from pathlib import Path
from dataclasses import dataclass

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Manca il pacchetto pyserial. Installa con: pip install pyserial")
    sys.exit(1)

# config_condivisa.py sta due cartelle sopra (FREQUENCY DETECTION/), non
# nel path di ricerca di default per uno script lanciato da TEST TEMPI/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config_condivisa

# --- Configurazione: modifica qui la porta seriale dell'Arduino ---
SERIAL_PORT = "COM4"  # <-- CAMBIA QUI con la porta giusta (vedi elenco stampato se sbagliata)
SERIAL_BAUD = 9600

UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000

# FREQ_TEST_HZ da config_condivisa.py (F1_HZ di default - cambia in
# config_condivisa.F2_HZ se in sweep_tempi.ino hai messo FREQ_TEST_HZ =
# F2_HZ per caratterizzare f2 invece di f1: i due vanno cambiati insieme).
# SOGLIA_ON/OFF invece restano locali: vanno prese a mano dai valori
# SOGLIA_ON_F1_DEFAULT/SOGLIA_OFF_F1_DEFAULT attuali in ../main.py (non
# cascano da config_condivisa perché richiedono una nuova taratura ad ogni
# cambio di frequenza, non sono derivabili in automatico).
FREQ_TEST_HZ = config_condivisa.F2_HZ
SOGLIA_ON = 0.0055   # SOGLIA_ON_F2_DEFAULT in main.py (f2, non f1: FREQ_TEST_HZ è F2_HZ qui sopra)
SOGLIA_OFF = 0.0036  # SOGLIA_OFF_F2_DEFAULT in main.py

BLOCCO_MS = 20.0
BLOCCO_CAMPIONI = max(1, round(SAMPLE_RATE * BLOCCO_MS / 1000.0))

RIPETIZIONI_PER_GRADINO = 20  # deve combaciare con lo sketch

FILE_LOG = Path(__file__).resolve().parent / "tempi_minimi_log.jsonl"


class GoertzelSingolo:
    """Stesso identico calcolo di RilevatoreGoertzel in main.py, riscritto
    qui per non dover importare main.py (che all'import avvia un'app Qt)."""

    def __init__(self, frequenza_hz, sample_rate, dimensione_blocco):
        self.dimensione_blocco = dimensione_blocco
        k = round(dimensione_blocco * frequenza_hz / sample_rate)
        omega = 2.0 * np.pi * k / dimensione_blocco
        self.coeff = 2.0 * np.cos(omega)

    def potenza(self, blocco):
        coeff = self.coeff
        q1 = 0.0
        q2 = 0.0
        for x in blocco.tolist():
            q0 = coeff * q1 - q2 + x
            q2 = q1
            q1 = q0
        magnitudine_quadra = q1 * q1 + q2 * q2 - coeff * q1 * q2
        n = self.dimensione_blocco
        return magnitudine_quadra / ((n / 2.0) ** 2) / (32768.0 ** 2)


def potenze_a_blocchi(campioni, goertzel):
    n_blocchi = len(campioni) // BLOCCO_CAMPIONI
    potenze = np.empty(n_blocchi, dtype=np.float64)
    for i in range(n_blocchi):
        blocco = campioni[i * BLOCCO_CAMPIONI: (i + 1) * BLOCCO_CAMPIONI]
        potenze[i] = goertzel.potenza(blocco)
    return potenze


def conta_transizioni_on(potenze, soglia_on, soglia_off):
    """Stessa macchina a stati a isteresi di MacchinaStatoFrequenza in
    main.py: conta quante volte si passa da OFF ad ON."""
    attivo = False
    conteggio = 0
    for p in potenze:
        if not attivo:
            if p > soglia_on:
                attivo = True
                conteggio += 1
        else:
            if p <= soglia_off:
                attivo = False
    return conteggio


@dataclass
class RisultatoGradino:
    fase: str
    on_ms: float
    gap_ms: float
    transizioni_rilevate: int
    transizioni_attese: int
    n_blocchi: int


def analizza_gradino(campioni, fase, on_ms, gap_ms, goertzel):
    potenze = potenze_a_blocchi(campioni, goertzel)
    rilevate = conta_transizioni_on(potenze, SOGLIA_ON, SOGLIA_OFF)
    return RisultatoGradino(
        fase=fase,
        on_ms=on_ms,
        gap_ms=gap_ms,
        transizioni_rilevate=rilevate,
        transizioni_attese=RIPETIZIONI_PER_GRADINO,
        n_blocchi=len(potenze),
    )


def stampa_risultato(r):
    ok = r.transizioni_rilevate >= r.transizioni_attese
    simbolo = "OK " if ok else "PERSE"
    print(
        f"[fase {r.fase}] ON={r.on_ms:5.0f}ms GAP={r.gap_ms:5.0f}ms  "
        f"transizioni {r.transizioni_rilevate:2d}/{r.transizioni_attese:2d}  {simbolo}"
    )


def scrivi_log(r):
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fase": r.fase,
        "on_ms": r.on_ms,
        "gap_ms": r.gap_ms,
        "transizioni_rilevate": r.transizioni_rilevate,
        "transizioni_attese": r.transizioni_attese,
    }
    with open(FILE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def riepilogo_finale(risultati):
    print("\n" + "=" * 70)
    print("RIEPILOGO FINALE")
    print("=" * 70)

    for fase, etichetta, variabile in (("A", "durata ON minima (gap largo fisso)", "on_ms"),
                                        ("B", "durata GAP minima (ON sicuro fisso)", "gap_ms")):
        gradini_fase = [r for r in risultati if r.fase == fase]
        if not gradini_fase:
            continue
        gradini_fase.sort(key=lambda r: getattr(r, variabile))
        buoni = [r for r in gradini_fase if r.transizioni_rilevate >= r.transizioni_attese]
        print(f"\n[Fase {fase}] {etichetta}:")
        for r in gradini_fase:
            stampa_risultato(r)
        if buoni:
            minimo = min(getattr(r, variabile) for r in buoni)
            print(f"  --> minimo affidabile trovato in questo sweep: {minimo:.0f}ms")
            print(f"      (consiglio: usa almeno {minimo * 1.5:.0f}ms in produzione, margine di sicurezza)")
        else:
            print("  --> nessun gradino testato ha funzionato pienamente: prova valori anche più larghi")

    print(f"\nDettaglio completo salvato in: {FILE_LOG}")


class LettoreSeriale(threading.Thread):
    def __init__(self, porta, baud, coda_eventi):
        super().__init__(daemon=True)
        self.coda_eventi = coda_eventi
        self.running = True
        self.pronto = threading.Event()
        self.finito = threading.Event()
        try:
            self.ser = serial.Serial(porta, baud, timeout=1)
        except serial.SerialException as e:
            porte = [p.device for p in serial.tools.list_ports.comports()]
            print(f"Impossibile aprire {porta}: {e}")
            print(f"Porte seriali disponibili: {porte or 'nessuna trovata'}")
            print("Modifica SERIAL_PORT in cima allo script e riprova.")
            sys.exit(1)

    def run(self):
        while self.running:
            try:
                riga = self.ser.readline().decode("utf-8", errors="ignore").strip()
            except serial.SerialException:
                break
            if not riga:
                continue
            print(f"[arduino] {riga}")
            if "Pronto" in riga:
                self.pronto.set()
            if riga.startswith("SWEEP_FINITO"):
                self.finito.set()
            if riga.startswith("STEP;"):
                try:
                    _, _millis_arduino, fase, on_str, gap_str = riga.split(";")
                    on_ms = float(on_str)
                    gap_ms = float(gap_str)
                except ValueError:
                    continue
                self.coda_eventi.put((time.time(), fase, on_ms, gap_ms))

    def invia_comando(self, comando):
        try:
            self.ser.write((comando + "\n").encode("utf-8"))
        except Exception as e:
            print(f"Errore inviando '{comando}' sulla seriale: {e}")

    def stop(self):
        self.running = False
        try:
            self.ser.close()
        except Exception:
            pass


def archivia_log_precedente():
    if not FILE_LOG.exists():
        return
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    destinazione = FILE_LOG.with_name(f"{FILE_LOG.stem}_{timestamp}{FILE_LOG.suffix}")
    FILE_LOG.rename(destinazione)
    print(f"Log precedente archiviato in: {destinazione}")


def main():
    archivia_log_precedente()
    goertzel = GoertzelSingolo(FREQ_TEST_HZ, SAMPLE_RATE, BLOCCO_CAMPIONI)

    coda_eventi = queue.Queue()
    lettore = LettoreSeriale(SERIAL_PORT, SERIAL_BAUD, coda_eventi)
    lettore.start()

    print("Aspetto che l'Arduino finisca il riavvio (riga 'Pronto')...")
    if not lettore.pronto.wait(timeout=15.0):
        print("ATTENZIONE: non ho visto 'Pronto' entro 15s, mando 'ON' comunque ma verifica la porta/il cavo.")
    lettore.invia_comando("ON")
    print("Comando 'ON' inviato: lo sweep sui tempi dovrebbe partire adesso.\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(0.5)
    bytes_attesi = CAMPIONI_PER_PACCHETTO * 2

    gradino_corrente = None  # (fase, on_ms, gap_ms)
    buffer_corrente = []
    risultati = []

    def chiudi_gradino_corrente():
        nonlocal buffer_corrente
        if gradino_corrente is None or not buffer_corrente:
            buffer_corrente = []
            return
        fase, on_ms, gap_ms = gradino_corrente
        campioni = np.concatenate(buffer_corrente)
        durata_attesa_ms = (on_ms + gap_ms) * RIPETIZIONI_PER_GRADINO
        if len(campioni) < SAMPLE_RATE * (durata_attesa_ms / 1000.0) * 0.5:
            print(f"[fase {fase}] ON={on_ms:.0f} GAP={gap_ms:.0f}: troppo pochi campioni, saltato")
        else:
            r = analizza_gradino(campioni, fase, on_ms, gap_ms, goertzel)
            stampa_risultato(r)
            scrivi_log(r)
            risultati.append(r)
        buffer_corrente = []

    try:
        while not lettore.finito.is_set():
            try:
                nuovo_evento = coda_eventi.get_nowait()
            except queue.Empty:
                nuovo_evento = None

            if nuovo_evento is not None:
                _t_evento, fase, on_ms, gap_ms = nuovo_evento
                chiudi_gradino_corrente()
                gradino_corrente = (fase, on_ms, gap_ms)

            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                continue

            if len(data) != bytes_attesi or gradino_corrente is None:
                continue
            buffer_corrente.append(np.frombuffer(data, dtype=np.int16))

        chiudi_gradino_corrente()

    except KeyboardInterrupt:
        print("\n\nInterrotto dall'utente, chiudo l'ultimo gradino in corso...")
        chiudi_gradino_corrente()
    finally:
        lettore.invia_comando("OFF")
        time.sleep(0.2)
        lettore.stop()
        sock.close()

    riepilogo_finale(risultati)


if __name__ == "__main__":
    main()
