"""
others/comandi_daisy.py — WAKEUPeSTALTA
----------------------------------------
Il "telecomando" della daisy chain: collegamento al modulo ESP (esp-link,
porta 23 = seriale del master) per mandare PARLA / ZITTO al master e
leggere quello che il master stampa (avvio, "Nodo 2: NON RISPONDE",
"INT: chiuso", "ZITTO (nessun PARLA da 10s)"...).

Tutto il lavoro di rete sta in un thread separato: se l'ESP non risponde
(WiFi caduto, IP cambiato) la finestra di 3main.py non si blocca mai. Il
thread si ricollega da solo; chi lo usa legge `connesso` e `ultimo_errore`.

Comandi capiti dal master: vedi l'intestazione di arduino/master/master.ino.
"""

from __future__ import annotations

import queue
import socket
import threading
import time

PORTA_ESP = 23
TIMEOUT_CONNESSIONE_S = 2.0
PAUSA_RICONNESSIONE_S = 1.0


class ComandiDaisy:
    def __init__(self, host: str, porta: int = PORTA_ESP):
        self.host = host.strip()
        self.porta = porta
        self.connesso = False
        self.ultimo_errore: str | None = None
        self._comandi: queue.Queue[str] = queue.Queue()
        self._righe: queue.Queue[str] = queue.Queue()
        self._riconnetti = False
        self._attivo = True
        self._thread = threading.Thread(target=self._lavora, name="comandi_daisy", daemon=True)
        self._thread.start()

    # --- API per 3main.py ---------------------------------------------
    def parla(self) -> None:
        """Inizia a parlare, o rinnova il "sono ancora qui" (da mandare
        ogni ~2s: senza PARLA per 10s il master si zittisce da solo)."""
        self._comandi.put("PARLA")

    def zitto(self) -> None:
        self._comandi.put("ZITTO")

    def imposta_host(self, host: str) -> None:
        self.host = host.strip()
        self._riconnetti = True

    def righe_ricevute(self) -> list[str]:
        """Le righe stampate dal master da l'ultima chiamata."""
        righe = []
        while True:
            try:
                righe.append(self._righe.get_nowait())
            except queue.Empty:
                return righe

    def chiudi(self) -> None:
        """Manda ZITTO (aspettando un attimo che parta) e ferma il thread."""
        self.zitto()
        fine = time.monotonic() + 1.5
        while not self._comandi.empty() and time.monotonic() < fine and self.connesso:
            time.sleep(0.05)
        self._attivo = False
        self._thread.join(timeout=2.5)

    # --- thread di rete -------------------------------------------------
    def _lavora(self) -> None:
        sock: socket.socket | None = None
        avanzo = b""
        while self._attivo:
            if sock is None or self._riconnetti:
                if sock is not None:
                    sock.close()
                    sock = None
                self._riconnetti = False
                try:
                    sock = socket.create_connection((self.host, self.porta), timeout=TIMEOUT_CONNESSIONE_S)
                    sock.settimeout(0.1)
                    self.connesso, self.ultimo_errore = True, None
                except OSError as e:
                    self.connesso, self.ultimo_errore = False, str(e)
                    time.sleep(PAUSA_RICONNESSIONE_S)
                    continue
            try:
                while True:  # tutti i comandi in attesa, nell'ordine
                    comando = self._comandi.get_nowait()
                    try:
                        sock.sendall((comando + "\r\n").encode())
                    except OSError:
                        self._comandi.put(comando)  # ritenta dopo la riconnessione
                        raise
            except queue.Empty:
                pass
            except OSError as e:
                sock.close()
                sock, self.connesso, self.ultimo_errore = None, False, str(e)
                continue
            try:
                dati = sock.recv(4096)
                if not dati:
                    raise OSError("connessione chiusa dall'ESP")
                avanzo += dati
                *righe, avanzo = avanzo.split(b"\n")
                for riga in righe:
                    testo = riga.decode("latin-1", "replace").strip()
                    if testo:
                        self._righe.put(testo)
            except socket.timeout:
                pass
            except OSError as e:
                sock.close()
                sock, self.connesso, self.ultimo_errore = None, False, str(e)
        if sock is not None:
            sock.close()
        self.connesso = False
