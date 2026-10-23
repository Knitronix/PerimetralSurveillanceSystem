"""
verificacatena.py — WAKEUPeSTALTA
----------------------------------
Verifica di funzionamento di TUTTA la catena, prima di una sessione
operativa (KPI 4.3, "Sensor integrity verification" - KPI/PROTOCOLLOkpi.MD).
Finestra separata da 3main.py, che deve essere CHIUSO (stessa porta UDP).

Uso: premi AVVIA e, quando la finestra lo chiede, calpesta i tappeti uno
alla volta.

Controlli, in ordine (se uno fallisce ci si ferma: i successivi non
avrebbero senso):
  1. Fibra non sorda   - arrivano pacchetti UDP dall'interrogatore
  2. ESP               - il modulo WiFi del master risponde
  3. Master            - risponde ai comandi (MODO 0: daisy chain, zitto)
  4. Fibra             - con il master zitto: pacchetti continui, rumore
                         plausibile, niente clipping
  5. Daisy chain       - il master parla: cicli completi (marcatore + tutti
                         i tappeti) decodificati come in 3main.py, lo slave
                         risponde, nessun tappeto sempre chiuso
  6. Walk test         - "CALPESTA IL TAPPETO n": ogni tappeto deve essere
                         visto chiuso sulla fibra per CICLI_CHIUSO_RICHIESTI
                         cicli di fila (una pressione vera resta chiusa
                         ~10s grazie alla memoria; un colpo di rumore dei
                         passi ne sporca uno solo) E confermato dal master
                         ("INT: chiuso" per il suo tappeto, risposta dello
                         slave sul bus con DEBUG per gli altri). Fibra
                         "chiuso" senza conferma del master = falso.
Il tempo totale (dentro anche il walk test) e' il numero del KPI: va in
KPI/kpi_selftest_log.jsonl, che calcola_kpi.py mission-ready (KPI 4.5)
somma da solo. Report in archivio/AAAA-MM-GG/taratura/.

Da aggiungere con le resistenze di fine linea sui tappeti: "nessun tappeto
in guasto" (cavo tagliato) - vedi others/TODO_ESP.md punto 5.
"""

from __future__ import annotations

import importlib
import json
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

CARTELLA_WAKEUP = Path(__file__).resolve().parent

M = importlib.import_module("3main")  # "import 3main" non e' sintassi valida
from others.archivio import cartella_taratura
from others.comandi_daisy import ComandiDaisy
from others.tema import COLORI, FONT_MONO, STYLE_QSS, applica_palette, crea_card, etichetta

FILE_LOG_KPI = CARTELLA_WAKEUP.parent / "KPI" / "kpi_selftest_log.jsonl"

FS = M.SAMPLE_RATE
B = M.BLOCCO_GOERTZEL_CAMPIONI

TIMEOUT_PRIMO_PACCHETTO_S = 3.0
TIMEOUT_ESP_S = 5.0
TIMEOUT_MASTER_S = 3.0
DURATA_FIBRA_S = 5.0
CICLI_RICHIESTI = 5
TIMEOUT_DAISY_S = 10.0
TIMEOUT_TAPPETO_S = 60.0
CICLI_CHIUSO_RICHIESTI = 3  # ~2.4s, ben dentro i 10s di memoria del tappeto

# Fibra (con il master zitto). Misurato: fibra collegata rms ~200-600,
# fibra staccata dal trasduttore ~10000-12000.
FRAZIONE_MINIMA_CAMPIONI = 0.85  # campioni ricevuti / attesi
BUCO_MASSIMO_S = 0.6             # le pause tra raffiche (~169ms) sono normali
RMS_MINIMO = 5.0                 # sotto: segnale piatto, sospetto
RMS_MASSIMO = 5000.0             # sopra: fibra staccata o saturata
SOGLIA_CLIPPING = 32000

IN_CORSO, PASS, FAIL = "IN CORSO", "PASS", "FAIL"

PASSI = [
    ("udp", "Fibra non sorda (pacchetti UDP)"),
    ("esp", "ESP raggiungibile"),
    ("master", "Master risponde"),
    ("fibra", "Fibra: continuita', rumore, clipping"),
    ("daisy", "Daisy chain: cicli completi"),
]


@dataclass
class Passo:
    chiave: str
    nome: str
    esito: str = ""
    dettaglio: str = ""
    durata_s: float = 0.0


@dataclass
class Risultato:
    passi: list[Passo] = field(default_factory=list)
    durata_automatica_s: float = 0.0
    durata_walk_test_s: float = 0.0
    durata_totale_s: float = 0.0
    esito: str = FAIL
    file_report: str = ""


class VerificaCatena:
    """Esegue la verifica in un thread. Comunica con la finestra con tre
    callback, chiamate DAL THREAD della verifica (la finestra le passa a Qt
    tramite segnali):
      on_passo(Passo)      - un passo e' iniziato (IN CORSO) o finito
      on_istruzione(str)   - cosa deve fare l'operatore ("CALPESTA...")
      on_fine(Risultato)
    """

    def __init__(self, esp_host: str,
                 on_passo: Callable[[Passo], None],
                 on_istruzione: Callable[[str], None],
                 on_fine: Callable[[Risultato], None]):
        self.esp_host = esp_host.strip()
        self.on_passo = on_passo
        self.on_istruzione = on_istruzione
        self.on_fine = on_fine
        self._ferma = threading.Event()
        self._thread: threading.Thread | None = None
        self.sock: socket.socket | None = None
        self.daisy: ComandiDaisy | None = None
        self.righe_master: list[str] = []
        self.numero_nodi = 0
        # Stato dei tappeti secondo il MASTER (non la fibra): tappeto -> True
        # se e' stato visto chiuso. Il suo: righe "INT: chiuso/aperto"; gli
        # altri: "RISPOSTA nodo n: ... -> chiuso" (solo con DEBUG acceso).
        self.master_chiuso: dict[int, bool] = {}
        self.debug_acceso = False
        self._coda = np.zeros(0, dtype=np.int16)
        self._parla = False
        self._t_parla = 0.0
        self.decoder = self._nuovo_decoder()

    @staticmethod
    def _nuovo_decoder():
        return M.RilevatoreProtocolloF1F2(FS, B, M.F1_HZ, M.F2_HZ, M.T0_MS, M.T1_MS, M.TOLLERANZA_MS,
                                          M.SOGLIA_ON_F1, M.SOGLIA_OFF_F1, M.SOGLIA_ON_F2, M.SOGLIA_OFF_F2)

    def avvia(self) -> None:
        self._thread = threading.Thread(target=self._esegui, name="verifica_catena", daemon=True)
        self._thread.start()

    def ferma(self) -> None:
        """Interrompe la verifica (esito FAIL, "interrotta")."""
        self._ferma.set()

    def attendi(self) -> None:
        if self._thread is not None:
            self._thread.join()

    # --- ascolto ---------------------------------------------------------
    def _ascolta(self, secondi: float, su_pacchetto=None, su_evento=None, finito=None) -> bool:
        """Legge UDP per al massimo `secondi`: passa i pacchetti a
        su_pacchetto(campioni, ora), gli eventi f1/f2 decodificati a
        su_evento(evento), raccoglie le righe del master, rinnova PARLA.
        Ritorna True se finito() e' diventato vero prima della scadenza."""
        fine = time.monotonic() + secondi
        while time.monotonic() < fine:
            if self._ferma.is_set():
                return False
            ora = time.monotonic()
            if self._parla and ora - self._t_parla >= M.INTERVALLO_PARLA_S:
                self.daisy.parla()
                self._t_parla = ora
            if self.daisy is not None:
                for riga in self.daisy.righe_ricevute():
                    self.righe_master.append(riga)
                    self._leggi_riga_master(riga)
            try:
                dati = self.sock.recv(4096)
            except socket.timeout:
                dati = None
            if dati is not None and len(dati) == M.CAMPIONI_PER_PACCHETTO * 2:
                campioni = np.frombuffer(dati, dtype=np.int16)
                if su_pacchetto is not None:
                    su_pacchetto(campioni, time.monotonic())
                if su_evento is not None:
                    self._coda = np.concatenate((self._coda, campioni))
                    n = len(self._coda) // B
                    for i in range(n):
                        for evento in self.decoder.elabora_blocco(self._coda[i * B:(i + 1) * B]):
                            su_evento(evento)
                    self._coda = self._coda[n * B:]
            if finito is not None and finito():
                return True
        return False

    # --- i passi ----------------------------------------------------------
    def _esegui(self) -> None:
        r = Risultato()
        t_inizio = time.monotonic()
        t_walk = None
        try:
            ok = self._passo(r, "udp", self._verifica_udp)
            ok = ok and self._passo(r, "esp", self._verifica_esp)
            ok = ok and self._passo(r, "master", self._verifica_master)
            ok = ok and self._passo(r, "fibra", self._verifica_fibra)
            ok = ok and self._passo(r, "daisy", self._verifica_daisy)
            r.durata_automatica_s = time.monotonic() - t_inizio
            if ok:
                t_walk = time.monotonic()
                self._imposta_debug(True)  # risposte dello slave visibili
                for n in range(1, self.numero_nodi + 1):
                    ok = self._passo(r, f"tappeto{n}", lambda n=n: self._verifica_tappeto(n),
                                     nome=f"Walk test: tappeto {n}") and ok
                r.durata_walk_test_s = time.monotonic() - t_walk
        except Exception as e:  # mai lasciare il master a parlare per un errore
            r.passi.append(Passo("errore", "Errore inatteso", FAIL, repr(e)))
            ok = False
        finally:
            self._parla = False
            if self.daisy is not None:
                if self.debug_acceso:
                    self._imposta_debug(False)
                self.daisy.chiudi()  # ZITTO
            if self.sock is not None:
                self.sock.close()
        if self._ferma.is_set():
            r.passi.append(Passo("stop", "Verifica interrotta dall'operatore", FAIL))
            ok = False
        r.durata_totale_s = time.monotonic() - t_inizio
        if t_walk is None:
            r.durata_automatica_s = r.durata_totale_s
        r.esito = PASS if ok and all(p.esito == PASS for p in r.passi) else FAIL
        self.on_istruzione("")
        self._salva(r)
        self.on_fine(r)

    def _passo(self, r: Risultato, chiave: str, funzione, nome: str | None = None) -> bool:
        passo = Passo(chiave, nome or dict(PASSI)[chiave], IN_CORSO)
        r.passi.append(passo)
        self.on_passo(passo)
        t = time.monotonic()
        ok, passo.dettaglio = funzione()
        passo.durata_s = time.monotonic() - t
        passo.esito = PASS if ok else FAIL
        self.on_passo(passo)
        return ok

    def _verifica_udp(self) -> tuple[bool, str]:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.sock.bind((M.UDP_IP, M.UDP_PORT))
            self.sock.settimeout(0.1)
        except OSError as e:
            self.sock = None
            return False, f"porta UDP {M.UDP_PORT} occupata: 3main.py o uno strumento e' aperto? ({e})"
        visti = []
        self._ascolta(TIMEOUT_PRIMO_PACCHETTO_S, su_pacchetto=lambda c, t: visti.append(1),
                      finito=lambda: bool(visti))
        if not visti:
            return False, (f"nessun pacchetto in {TIMEOUT_PRIMO_PACCHETTO_S:.0f}s: interrogatore spento, "
                           "cavo di rete, o IP del PC sbagliato")
        return True, "l'interrogatore trasmette"

    def _verifica_esp(self) -> tuple[bool, str]:
        self.daisy = ComandiDaisy(self.esp_host)
        if not self.daisy.aspetta_connessione(TIMEOUT_ESP_S):
            return False, (f"{self.esp_host} non risponde ({self.daisy.ultimo_errore}): ESP acceso? "
                           "PC sulla stessa rete WiFi? IP giusto?")
        return True, self.esp_host

    def _verifica_master(self) -> tuple[bool, str]:
        self.righe_master.clear()
        self.daisy.comando("MODO 0")  # daisy chain, zitto: ristampa l'intestazione
        self.daisy.comando("STATO")
        self._ascolta(TIMEOUT_MASTER_S, finito=lambda: any(r.startswith("STATO:") for r in self.righe_master))
        stato = next((r for r in self.righe_master if r.startswith("STATO:")), None)
        nodi = next((re.search(r"NUMERO_NODI=(\d+)", r) for r in self.righe_master if "NUMERO_NODI=" in r), None)
        if stato is None or nodi is None:
            return False, ("l'ESP risponde ma il master no: fili ESP-Mega (TX/RX), master alimentato?"
                           if not self.righe_master else f"risposta inattesa: {self.righe_master[-1]}")
        self.numero_nodi = int(nodi.group(1))
        vol = re.search(r"VOL=(\d+) (\d+)", stato)
        testo_vol = f", volume f1 {vol.group(1)}% f2 {vol.group(2)}%" if vol else ""
        return True, f"modalita' 0, {self.numero_nodi} tappeti{testo_vol}"

    def _verifica_fibra(self) -> tuple[bool, str]:
        pacchetti: list[np.ndarray] = []
        arrivi: list[float] = []

        def su_pacchetto(c, t):
            pacchetti.append(c)
            arrivi.append(t)

        t0 = time.monotonic()
        self._ascolta(DURATA_FIBRA_S, su_pacchetto=su_pacchetto)
        durata = time.monotonic() - t0
        if not pacchetti:
            return False, "nessun pacchetto: la fibra e' diventata sorda"
        x = np.concatenate(pacchetti).astype(np.float64)
        frazione = len(x) / (durata * FS)
        buco = max(np.diff([t0] + arrivi + [time.monotonic()]))
        rms = float(np.sqrt(np.mean(x ** 2)))
        picco = int(np.max(np.abs(x)))
        problemi = []
        if frazione < FRAZIONE_MINIMA_CAMPIONI:
            problemi.append(f"arriva solo il {frazione:.0%} dei campioni")
        if buco > BUCO_MASSIMO_S:
            problemi.append(f"buco di {buco:.1f}s nel flusso")
        if rms < RMS_MINIMO:
            problemi.append(f"segnale piatto (rms {rms:.0f})")
        if rms > RMS_MASSIMO:
            problemi.append(f"rms {rms:.0f} troppo alto: fibra staccata dal trasduttore?")
        if picco >= SOGLIA_CLIPPING:
            problemi.append(f"clipping (picco {picco})")
        misure = f"campioni {frazione:.0%}, buco max {buco:.2f}s, rms {rms:.0f}, picco {picco}"
        return (False, "; ".join(problemi) + f"  [{misure}]") if problemi else (True, misure)

    def _verifica_daisy(self) -> tuple[bool, str]:
        self.decoder = self._nuovo_decoder()
        self._coda = np.zeros(0, dtype=np.int16)
        self.righe_master.clear()
        cicli: list[dict[int, str]] = []  # per ogni ciclo: tappeto -> stato
        completi = []

        def su_evento(e):
            if e["tipo"] == "zero":
                if cicli and len(cicli[-1]) == self.numero_nodi:
                    completi.append(cicli[-1])
                cicli.append({})
            elif e["tipo"] == "slot" and cicli:
                cicli[-1][e["counter"]] = e["stato"]

        self.daisy.parla()
        self._parla, self._t_parla = True, time.monotonic()
        self._ascolta(TIMEOUT_DAISY_S, su_evento=su_evento, finito=lambda: len(completi) >= CICLI_RICHIESTI)
        muti = sorted({r for r in self.righe_master if "NON RISPONDE" in r})
        if muti:
            return False, "; ".join(muti) + ": slave spento, bus D8-D2 o GND tra i Mega?"
        if len(completi) < CICLI_RICHIESTI:
            visti = f"{len(cicli)} marcatori, {len(completi)} cicli completi"
            return False, (f"{visti} in {TIMEOUT_DAISY_S:.0f}s: tono non sentito (trasduttore staccato "
                           "dalla fibra? volume?) o tappeti mancanti")
        sempre_chiusi = [n for n in range(1, self.numero_nodi + 1) if all(c[n] == "chiuso" for c in completi)]
        if sempre_chiusi:
            return False, (f"tappeto {', '.join(map(str, sempre_chiusi))} sempre chiuso: "
                           "qualcuno sopra o tappeto bloccato")
        return True, f"{len(completi)} cicli completi, {self.numero_nodi} tappeti, slave risponde"

    def _leggi_riga_master(self, riga: str) -> None:
        if riga == "INT: chiuso":
            self.master_chiuso[1] = True
        m = re.match(r"RISPOSTA nodo (\d+): .*-> chiuso", riga)
        if m:
            self.master_chiuso[int(m.group(1))] = True
        if riga.startswith("DEBUG risposte:"):
            self.debug_acceso = riga.endswith("ACCESO")

    def _imposta_debug(self, acceso: bool) -> None:
        """DEBUG nel master e' un interruttore (ACCESO/SPENTO): lo si manda
        finche' la risposta non e' quella voluta (al massimo 2 volte)."""
        for _ in range(2):
            if self.debug_acceso == acceso:
                return
            prima = len(self.righe_master)
            self.daisy.comando("DEBUG")
            self._ascolta(2.0, finito=lambda: any(r.startswith("DEBUG risposte:")
                                                  for r in self.righe_master[prima:]))

    def _verifica_tappeto(self, n: int) -> tuple[bool, str]:
        self.on_istruzione(f"CALPESTA IL TAPPETO {n}")
        self.master_chiuso[n] = False
        di_fila = [0]
        isolati = [0]  # "chiuso" sulla fibra non confermati dal ciclo dopo

        def su_evento(e):
            if e["tipo"] == "slot" and e["counter"] == n:
                if e["stato"] == "chiuso":
                    di_fila[0] += 1
                else:
                    if di_fila[0] > 0:
                        isolati[0] += 1
                    di_fila[0] = 0

        t = time.monotonic()
        fibra_ok = self._ascolta(TIMEOUT_TAPPETO_S, su_evento=su_evento,
                                 finito=lambda: di_fila[0] >= CICLI_CHIUSO_RICHIESTI)
        if fibra_ok:  # la riga del master puo' arrivare un attimo dopo
            self._ascolta(1.0, finito=lambda: self.master_chiuso.get(n, False))
        durata = time.monotonic() - t
        nota_rumore = f" ({isolati[0]} 'chiuso' isolati scartati: rumore)" if isolati[0] else ""
        if self._ferma.is_set():
            return False, "interrotto"
        if fibra_ok and self.master_chiuso.get(n):
            return True, f"chiuso per {CICLI_CHIUSO_RICHIESTI} cicli dopo {durata:.1f}s, confermato dal master{nota_rumore}"
        if fibra_ok:
            return False, ("FALSO: la fibra lo vede chiuso ma il master no - rumore sulla fibra "
                           f"(passi, vibrazioni){nota_rumore}")
        if self.master_chiuso.get(n):
            return False, ("il master lo vede premuto ma la fibra non sente f2: trasduttore, "
                           f"volume o soglie{nota_rumore}")
        return False, f"non premuto in {TIMEOUT_TAPPETO_S:.0f}s: tappeto, cavo o slave{nota_rumore}"

    # --- report e log KPI ------------------------------------------------------
    def _salva(self, r: Risultato) -> None:
        righe = [f"Verifica catena WAKEUPeSTALTA - {time.strftime('%d/%m/%Y %H:%M:%S')}",
                 f"ESITO: {r.esito}", ""]
        for p in r.passi:
            righe.append(f"{p.esito:<5} {p.nome:<40} {p.durata_s:5.1f}s  {p.dettaglio}")
        righe += ["", f"Parte automatica: {r.durata_automatica_s:6.1f}s",
                  f"Walk test:        {r.durata_walk_test_s:6.1f}s",
                  f"TOTALE (KPI 4.3): {r.durata_totale_s:6.1f}s"]
        try:
            file = cartella_taratura() / time.strftime("verifica_%H%M%S.txt")
            file.write_text("\n".join(righe) + "\n", encoding="utf-8")
            r.file_report = str(file)
        except OSError:
            pass
        try:
            with open(FILE_LOG_KPI, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "origine": "WAKEUPeSTALTA/verificacatena.py",
                    "esito_globale": r.esito,
                    "durata_verifica_sec": round(r.durata_totale_s, 2),
                    "durata_automatica_sec": round(r.durata_automatica_s, 2),
                    "durata_walk_test_sec": round(r.durata_walk_test_s, 2),
                    "passi": {p.chiave: p.esito for p in r.passi},
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ======================================================================
# GUI
# ======================================================================
COLORE_ESITO = {IN_CORSO: COLORI["ambra"], PASS: COLORI["verde"], FAIL: COLORI["pericolo"]}


class Segnali(QtCore.QObject):
    passo = QtCore.Signal(object)
    istruzione = QtCore.Signal(str)
    fine = QtCore.Signal(object)


class FinestraVerifica(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Verifica catena — WAKEUPeSTALTA")
        self.resize(980, 640)
        self.setStyleSheet(STYLE_QSS)
        self.segnali = Segnali()
        self.segnali.passo.connect(self._mostra_passo)
        self.segnali.istruzione.connect(self._mostra_istruzione)
        self.segnali.fine.connect(self._mostra_fine)
        self.verifica: VerificaCatena | None = None
        self.righe: dict[str, int] = {}
        self._costruisci_ui()

    def _costruisci_ui(self):
        radice = QtWidgets.QWidget()
        radice.setObjectName("Radice")
        self.setCentralWidget(radice)
        layout = QtWidgets.QVBoxLayout(radice)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        card, lay, _ = crea_card("Verifica catena", colore_titolo=COLORI["accento"])
        riga = QtWidgets.QHBoxLayout()
        riga.addWidget(etichetta("IP ESP"))
        self.campo_ip = QtWidgets.QLineEdit(M.Config().ESP_HOST)
        riga.addWidget(self.campo_ip, 1)
        self.btn_avvia = QtWidgets.QPushButton("AVVIA VERIFICA")
        self.btn_avvia.setObjectName("Primario")
        self.btn_avvia.clicked.connect(self._avvia)
        riga.addWidget(self.btn_avvia)
        self.btn_ferma = QtWidgets.QPushButton("Ferma")
        self.btn_ferma.setObjectName("Piatto")
        self.btn_ferma.setEnabled(False)
        self.btn_ferma.clicked.connect(self._ferma)
        riga.addWidget(self.btn_ferma)
        lay.addLayout(riga)
        self.label_istruzione = QtWidgets.QLabel("Chiudi 3main.py prima di avviare.")
        self.label_istruzione.setAlignment(QtCore.Qt.AlignCenter)
        self.label_istruzione.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {COLORI['testo_tenue']};")
        self.label_istruzione.setMinimumHeight(56)
        lay.addWidget(self.label_istruzione)
        layout.addWidget(card)

        card, lay, _ = crea_card("Controlli", colore_titolo=COLORI["blu"])
        self.tabella = QtWidgets.QTableWidget(0, 4)
        self.tabella.setHorizontalHeaderLabels(["Esito", "Controllo", "Tempo", "Dettaglio"])
        self.tabella.verticalHeader().setVisible(False)
        self.tabella.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabella.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        intestazione = self.tabella.horizontalHeader()
        intestazione.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        intestazione.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        intestazione.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        intestazione.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.tabella.setWordWrap(True)
        lay.addWidget(self.tabella)
        layout.addWidget(card, 1)

        card, lay, _ = crea_card("Tempo di verifica (KPI 4.3)", colore_titolo=COLORI["verde"])
        self.label_tempi = QtWidgets.QLabel("—")
        self.label_tempi.setStyleSheet(f"font-family: {FONT_MONO}; font-size: 15px;")
        self.label_tempi.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(self.label_tempi)
        layout.addWidget(card)

        self.timer = QtCore.QTimer(self)  # cronometro dal vivo
        self.timer.timeout.connect(self._aggiorna_cronometro)
        self.t_avvio = QtCore.QElapsedTimer()

    # --- azioni --------------------------------------------------------
    def _avvia(self):
        self.tabella.setRowCount(0)
        self.righe.clear()
        self.btn_avvia.setEnabled(False)
        self.btn_ferma.setEnabled(True)
        self.campo_ip.setEnabled(False)
        self._mostra_istruzione("")
        self.verifica = VerificaCatena(self.campo_ip.text(), self.segnali.passo.emit,
                                       self.segnali.istruzione.emit, self.segnali.fine.emit)
        self.t_avvio.start()
        self.timer.start(100)
        self.verifica.avvia()

    def _ferma(self):
        if self.verifica is not None:
            self.verifica.ferma()
            self.btn_ferma.setEnabled(False)

    def _aggiorna_cronometro(self):
        self.label_tempi.setText(f"In corso… {self.t_avvio.elapsed() / 1000:.1f}s")

    # --- dal thread della verifica (via segnali) ------------------------------
    def _mostra_passo(self, p: Passo):
        if p.chiave not in self.righe:
            self.righe[p.chiave] = self.tabella.rowCount()
            self.tabella.insertRow(self.tabella.rowCount())
        riga = self.righe[p.chiave]
        valori = (p.esito, p.nome, "" if p.esito == IN_CORSO else f"{p.durata_s:.1f}s", p.dettaglio)
        for colonna, testo in enumerate(valori):
            item = QtWidgets.QTableWidgetItem(testo)
            if colonna == 0:
                item.setForeground(QtGui.QColor(COLORE_ESITO.get(p.esito, COLORI["testo"])))
            self.tabella.setItem(riga, colonna, item)
        self.tabella.resizeRowsToContents()

    def _mostra_istruzione(self, testo: str):
        if testo:
            self.label_istruzione.setText(testo)
            self.label_istruzione.setStyleSheet(f"font-size: 30px; font-weight: 700; color: {COLORI['ambra']};")
        else:
            self.label_istruzione.setText("")
            self.label_istruzione.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {COLORI['testo_tenue']};")

    def _mostra_fine(self, r: Risultato):
        self.timer.stop()
        for p in r.passi:  # anche le righe aggiunte alla fine (interrotta, errore)
            self._mostra_passo(p)
        colore = COLORI["verde"] if r.esito == PASS else COLORI["pericolo"]
        self.label_istruzione.setText(f"ESITO: {r.esito}")
        self.label_istruzione.setStyleSheet(f"font-size: 30px; font-weight: 700; color: {colore};")
        testo = (f"Parte automatica  {r.durata_automatica_s:6.1f}s\n"
                 f"Walk test         {r.durata_walk_test_s:6.1f}s\n"
                 f"TOTALE            {r.durata_totale_s:6.1f}s")
        if r.file_report:
            testo += f"\n\nReport: {r.file_report}"
        self.label_tempi.setText(testo)
        self.btn_avvia.setEnabled(True)
        self.btn_ferma.setEnabled(False)
        self.campo_ip.setEnabled(True)
        self.verifica = None

    def closeEvent(self, evento):
        if self.verifica is not None:  # ZITTO al master prima di uscire
            self.verifica.ferma()
            self.verifica.attendi()
        super().closeEvent(evento)


def main() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    applica_palette(app)
    finestra = FinestraVerifica()
    finestra.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
