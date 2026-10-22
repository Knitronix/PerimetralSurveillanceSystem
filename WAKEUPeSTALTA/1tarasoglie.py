"""
1tarasoglie.py — WAKEUPeSTALTA
-------------------------------
Calcola le soglie Goertzel che 3main.py usa in ARMATO (SOGLIA_ON/OFF_F1/F2),
in base alla potenza con cui sente davvero f1 e f2 sulla daisy chain.

Uso (3main.py deve essere CHIUSO: stessa porta UDP):
  1. AVVIA: ~1s di fibra muta (non toccare niente) = il RUMORE
  2. via WiFi (ESP) la daisy chain parla: per 30s PREMI i tappeti,
     ognuno almeno 5 volte (senza pressioni f2 non suona)
  3. report: le soglie vanno bene / da cambiare (con i valori da copiare)

Regola, volutamente semplice:
  - rumore = picchi del segnale a f1/f2 con la fibra muta
  - tono   = la parte BASSA dei blocchi in cui il tono c'e' (cosi' le
             soglie reggono anche quando il tono si indebolisce)
  - soglia ON a meta' strada, in proporzione, tra rumore e tono; OFF = 65%
  - verifica: la decodifica di 3main.py sulla registrazione, con le soglie
    attuali e con quelle calcolate - le nuove si consigliano solo se non
    perdono niente

Volume: la riga "Volume f1/f2" imposta il volume del master (comando VOL,
salvato nel master). La potenza va col quadrato del volume (50% = 1/4):
dopo ogni cambio rifare la misura. Il report dice con che volume e' fatta.

Frequenze, soglie attuali e decodificatore sono letti da 3main.py stesso.
Registrazione + report in archivio/AAAA-MM-GG/taratura/.
"""

from __future__ import annotations

import socket
import importlib
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

# 3main.py inizia con un numero (ordine d'uso degli script): "import 3main"
# non e' sintassi valida, si importa per nome.
M = importlib.import_module("3main")
from others.comandi_daisy import ComandiDaisy
from others.archivio import CARTELLA_ARCHIVIO, cartella_taratura
from others.tema import COLORI, FONT_MONO, STYLE_QSS, applica_palette, crea_card, etichetta, stile_plot

DURATA_S = 30.0
PRIMA_ACCENSIONE_S = 1.0  # fibra muta registrata prima di mandare PARLA
INTERVALLO_PARLA_S = 2.0  # PARLA ripetuto: senza, il master si zittisce dopo 10s
FRAZIONE_SOGLIA_OFF = 0.65
TONO_SU_RUMORE = 30.0     # un blocco conta come "tono" se supera di tanto il rumore
PERCENTILE_TONO = 10      # "tono" = 10° percentile dei picchi dei singoli toni (la parte bassa)
MARGINE_OK = 5.0          # soglia ok se almeno 5x sopra il rumore e 5x sotto il tono
MIN_TONI_F2 = 5           # sotto: troppe poche pressioni per misurare f2
SECONDI_GRAFICO = 10.0

B = M.BLOCCO_GOERTZEL_CAMPIONI
FS = M.SAMPLE_RATE


# ======================================================================
# Analisi (pura)
# ======================================================================
class TaraturaErrore(Exception):
    pass


def potenze(campioni: np.ndarray, freq: float) -> np.ndarray:
    """Potenza per blocco da 20ms, stessa scala del Goertzel di 3main.py."""
    n = len(campioni) // B
    blocchi = campioni[: n * B].astype(np.float64).reshape(n, B)
    k = int(round(B * freq / FS))
    return np.abs(np.fft.rfft(blocchi, axis=1)[:, k]) ** 2 / (B / 2.0) ** 2 / 32768.0 ** 2


def picchi_dei_toni(p: np.ndarray, sopra: float) -> list[float]:
    """Per ogni tono suonato (tratto continuo sopra 'sopra'), il suo blocco
    piu' forte: e' un blocco pieno, non un bordo in cui il tono c'e' solo
    per meta' (uno slot da 50ms e' fatto quasi solo di bordi)."""
    picchi, inizio = [], None
    for i, v in enumerate(np.append(p > sopra, False)):
        if v and inizio is None:
            inizio = i
        elif not v and inizio is not None:
            picchi.append(float(p[inizio:i].max()))
            inizio = None
    return picchi


def decodifica(x: np.ndarray, soglie: tuple) -> dict:
    """La decodifica di 3main.py, identica: quanti marcatori, quante
    pressioni per tappeto, quanti tappeti inesistenti (errori)."""
    p = M.RilevatoreProtocolloF1F2(FS, B, M.F1_HZ, M.F2_HZ, M.T0_MS, M.T1_MS, M.TOLLERANZA_MS, *soglie)
    marcatori, slot = 0, []
    for i in range(len(x) // B):
        for e in p.elabora_blocco(x[i * B:(i + 1) * B]):
            if e["tipo"] == "zero":
                marcatori += 1
            elif e["tipo"] == "slot":
                slot.append(e)
    # numero di tappeti = il contatore piu' alto presente in almeno meta' dei cicli
    contatori = [e["counter"] for e in slot]
    n_tappeti = max((k for k in set(contatori) if contatori.count(k) >= marcatori / 2), default=0)
    pressioni = {k: sum(1 for e in slot if e["counter"] == k and e["stato"] == "chiuso") for k in range(1, n_tappeti + 1)}
    inesistenti = sum(1 for e in slot if e["counter"] > n_tappeti)
    return {"marcatori": marcatori, "pressioni": pressioni, "inesistenti": inesistenti}


def analizza(x: np.ndarray, n_on: int) -> dict:
    if n_on < B * 10:
        raise TaraturaErrore("Manca la registrazione della fibra muta prima dell'accensione.")
    p1, p2 = potenze(x, M.F1_HZ), potenze(x, M.F2_HZ)
    muta = slice(0, max(1, n_on // B - 1))
    dopo = slice(n_on // B, None)
    rumore = [max(float(np.percentile(p[muta], 99)), 1e-12) for p in (p1, p2)]

    toni, n_toni = [], []
    for p, r in zip((p1, p2), rumore):
        picchi = picchi_dei_toni(p[dopo], TONO_SU_RUMORE * r)
        toni.append(float(np.percentile(picchi, PERCENTILE_TONO)) if picchi else None)
        n_toni.append(len(picchi))
    if toni[0] is None:
        raise TaraturaErrore(f"Nessun tono a {M.F1_HZ:.0f}Hz: la daisy chain non e' partita? "
                             "(ESP/WiFi, alimentazione, fibra staccata dal trasduttore)")

    on1 = float(np.sqrt(rumore[0] * toni[0]))
    calcolate = [on1, on1 * FRAZIONE_SOGLIA_OFF, M.SOGLIA_ON_F2, M.SOGLIA_OFF_F2]
    f2_ok = toni[1] is not None and n_toni[1] >= MIN_TONI_F2
    if f2_ok:
        on2 = float(np.sqrt(rumore[1] * toni[1]))
        calcolate[2:] = [on2, on2 * FRAZIONE_SOGLIA_OFF]

    attuali = (M.SOGLIA_ON_F1, M.SOGLIA_OFF_F1, M.SOGLIA_ON_F2, M.SOGLIA_OFF_F2)
    dec_att, dec_calc = decodifica(x, attuali), decodifica(x, tuple(calcolate))

    def soglia_ok(on, i):  # almeno MARGINE_OK volte sopra il rumore e sotto il tono
        return on >= MARGINE_OK * rumore[i] and toni[i] >= MARGINE_OK * on
    attuali_ok = soglia_ok(attuali[0], 0) and (not f2_ok or soglia_ok(attuali[2], 1))
    peggiori = (dec_calc["marcatori"] < dec_att["marcatori"]
                or sum(dec_calc["pressioni"].values()) < sum(dec_att["pressioni"].values()))
    return {"rumore": rumore, "toni": toni, "f2_ok": f2_ok, "attuali": attuali, "proposte": tuple(calcolate),
            "dec_attuali": dec_att, "dec_calcolate": dec_calc, "attuali_ok": attuali_ok, "peggiori": peggiori}


def esito(r: dict) -> str:
    if not r["f2_ok"]:
        return "✗ RIFAI LA MISURA: premi i tappeti più volte (almeno 5 volte ciascuno)"
    if r["attuali_ok"]:
        return "✓ SOGLIE OK: NON CAMBIARE NIENTE"
    if r["peggiori"]:
        return "⚠ TIENI LE SOGLIE ATTUALI: quelle calcolate funzionerebbero peggio. Rifai la misura"
    return "⚠ CAMBIA LE SOGLIE: copia i valori qui sotto in 3main.py"


def _giudizio(on: float, rumore: float, tono: float) -> str:
    if on < MARGINE_OK * rumore:
        return "TROPPO BASSA (vicina al rumore: rischio di impulsi falsi)"
    if tono < MARGINE_OK * on:
        return "TROPPO ALTA (vicina al tono: rischio di non sentire)"
    return "ok"


def componi_report(r: dict) -> str:
    a, c = r["attuali"], r["proposte"]
    righe = [esito(r), ""]
    for i, nome in ((0, "f1"), (1, "f2")):
        tono = r["toni"][i]
        if tono is None or (i == 1 and not r["f2_ok"]):
            righe.append(f"{nome}: tono non misurabile (pochi tappeti premuti)")
            continue
        righe.append(f"{nome}: tono {tono:.6f} · rumore {r['rumore'][i]:.7f} · "
                     f"soglia attuale {a[2 * i]:.6f} → {_giudizio(a[2 * i], r['rumore'][i], tono)}")
    d = r["dec_attuali"]
    pressioni = " · ".join(f"tappeto {k}: {n}" for k, n in d["pressioni"].items()) or "nessuna"
    righe.append(f"Pressioni sentite: {pressioni}")
    if d["inesistenti"]:
        righe.append(f"Durante passi/botte 3main.py ha visto {d['inesistenti']} volte un tappeto inesistente "
                     "(problema noto, non dipende dalle soglie)")
    if esito(r).startswith("⚠ CAMBIA"):
        righe += ["", f"SOGLIA_ON_F1 = {c[0]:.6f}", f"SOGLIA_OFF_F1 = {c[1]:.6f}",
                  f"SOGLIA_ON_F2 = {c[2]:.6f}", f"SOGLIA_OFF_F2 = {c[3]:.6f}"]
    return "\n".join(righe)


# ======================================================================
# GUI
# ======================================================================
class Segnali(QtCore.QObject):
    fase = QtCore.Signal(str)
    errore = QtCore.Signal(str)
    risultato = QtCore.Signal(object)


class FinestraTaraSoglie(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tara soglie Goertzel — WAKEUPeSTALTA")
        self.resize(1300, 820)
        self.setStyleSheet(STYLE_QSS)
        self.segnali = Segnali()
        self.segnali.fase.connect(self._cambia_fase)
        self.segnali.errore.connect(self._errore)
        self.segnali.risultato.connect(self._mostra_risultato)
        self.fase = "inattivo"
        self.sock: socket.socket | None = None
        self.buf: list[np.ndarray] = []
        self.n_on = 0
        self.t_fase = 0.0
        self.t_parla = 0.0
        self.volume: tuple[int, int] | None = None  # come lo riporta il master
        self.stato_chiesto = False
        self.coda = np.zeros(0, dtype=np.int16)
        n_grafico = int(SECONDI_GRAFICO * FS / B)
        self.storico_f1 = np.full(n_grafico, 1e-9)
        self.storico_f2 = np.full(n_grafico, 1e-9)
        self._costruisci_ui()
        # Collegamento all'ESP aperto per tutta la vita della finestra: serve
        # alla misura (PARLA/ZITTO) e al volume (VOL, STATO).
        self.daisy = ComandiDaisy(self.campo_ip.text().strip())
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)

    def _costruisci_ui(self):
        radice = QtWidgets.QWidget()
        radice.setObjectName("Radice")
        self.setCentralWidget(radice)
        layout = QtWidgets.QHBoxLayout(radice)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        sinistra = QtWidgets.QVBoxLayout()
        card, lay, _ = crea_card("Misura", colore_titolo=COLORI["accento"])
        riga = QtWidgets.QHBoxLayout()
        riga.addWidget(etichetta("IP ESP"))
        self.campo_ip = QtWidgets.QLineEdit(M.Config().ESP_HOST)
        self.campo_ip.editingFinished.connect(self._cambia_ip)
        riga.addWidget(self.campo_ip, 1)
        lay.addLayout(riga)
        riga_vol = QtWidgets.QHBoxLayout()
        riga_vol.addWidget(etichetta("Volume f1 %"))
        self.spin_vol_f1 = QtWidgets.QSpinBox()
        self.spin_vol_f1.setRange(0, 100)
        self.spin_vol_f1.setValue(100)
        riga_vol.addWidget(self.spin_vol_f1)
        riga_vol.addWidget(etichetta("f2 %"))
        self.spin_vol_f2 = QtWidgets.QSpinBox()
        self.spin_vol_f2.setRange(0, 100)
        self.spin_vol_f2.setValue(100)
        riga_vol.addWidget(self.spin_vol_f2)
        self.btn_volume = QtWidgets.QPushButton("Imposta volume")
        self.btn_volume.setObjectName("Piatto")
        self.btn_volume.clicked.connect(self._imposta_volume)
        riga_vol.addWidget(self.btn_volume)
        self.label_volume = QtWidgets.QLabel("master: …")
        riga_vol.addWidget(self.label_volume, 1)
        lay.addLayout(riga_vol)
        riga_nota = QtWidgets.QHBoxLayout()
        riga_nota.addWidget(etichetta("Nota"))
        self.campo_nota = QtWidgets.QLineEdit()
        self.campo_nota.setPlaceholderText("luogo, fibra, mezzo, meteo… (salvata con la misura)")
        riga_nota.addWidget(self.campo_nota, 1)
        lay.addLayout(riga_nota)
        bottoni = QtWidgets.QHBoxLayout()
        self.btn_avvia = QtWidgets.QPushButton("AVVIA MISURA")
        self.btn_avvia.setObjectName("Primario")
        self.btn_avvia.clicked.connect(self._avvia)
        self.btn_apri = QtWidgets.QPushButton("Apri registrazione…")
        self.btn_apri.setObjectName("Piatto")
        self.btn_apri.clicked.connect(self._apri)
        bottoni.addWidget(self.btn_avvia)
        bottoni.addWidget(self.btn_apri)
        lay.addLayout(bottoni)
        self.label_fase = QtWidgets.QLabel(
            f"Chiudi 3main.py prima di avviare. Durante i {DURATA_S:.0f}s di misura PREMI i tappeti "
            "(uno alla volta e insieme).")
        self.label_fase.setWordWrap(True)
        lay.addWidget(self.label_fase)
        self.barra = QtWidgets.QProgressBar()
        self.barra.setRange(0, 1000)
        self.barra.setTextVisible(False)
        lay.addWidget(self.barra)
        sinistra.addWidget(card)

        card, lay, _ = crea_card("Potenza f1 / f2 dal vivo · linee tratteggiate = soglie ON attuali di 3main.py",
                                 colore_titolo=COLORI["blu"])
        self.plot = pg.PlotWidget()
        stile_plot(self.plot, "Potenza Goertzel", "Tempo (s)")
        self.plot.setLogMode(y=True)
        self.plot.addLegend(offset=(10, 10))
        asse = np.linspace(-SECONDI_GRAFICO, 0, len(self.storico_f1))
        self.asse = asse
        self.curva_f1 = self.plot.plot(asse, self.storico_f1, pen=pg.mkPen(COLORI["verde"], width=1.4),
                                       name=f"f1 {M.F1_HZ:.0f}Hz")
        self.curva_f2 = self.plot.plot(asse, self.storico_f2, pen=pg.mkPen(COLORI["ambra"], width=1.4),
                                       name=f"f2 {M.F2_HZ:.0f}Hz")
        for valore, colore in ((M.SOGLIA_ON_F1, COLORI["verde"]), (M.SOGLIA_ON_F2, COLORI["ambra"])):
            self.plot.addItem(pg.InfiniteLine(np.log10(valore), angle=0,
                                              pen=pg.mkPen(colore, width=1, style=QtCore.Qt.DashLine)))
        self.linee_proposte = []
        lay.addWidget(self.plot)
        sinistra.addWidget(card, 1)
        layout.addLayout(sinistra, 5)

        card, lay, _ = crea_card("Report", colore_titolo=COLORI["verde"])
        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setStyleSheet(f"font-family: {FONT_MONO}; font-size: 12px;")
        lay.addWidget(self.report)
        layout.addWidget(card, 5)

    # --- acquisizione ---------------------------------------------------
    def _avvia(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.sock.bind((M.UDP_IP, M.UDP_PORT))
            self.sock.setblocking(False)
        except OSError as e:
            self.sock = None
            self._errore(f"Porta UDP {M.UDP_PORT} occupata - 3main.py o un altro strumento e' aperto? ({e})")
            return
        self.buf = []
        self.n_on = 0
        self.report.clear()
        self.btn_avvia.setEnabled(False)
        self.btn_apri.setEnabled(False)
        self.fase = "muta"  # impostata PRIMA del thread: _cambia_fase ignora i cambi da "inattivo"
        self.t_fase = time.monotonic()
        self.label_fase.setText("Registro la fibra muta…")
        threading.Thread(target=self._sequenza, args=(self.campo_ip.text().strip(),), daemon=True).start()

    def _sequenza(self, host: str):
        inizio = time.monotonic()
        if not self.daisy.aspetta_connessione(5.0):
            self.segnali.errore.emit(f"ESP {host} non raggiungibile ({self.daisy.ultimo_errore}).")
            return
        self.daisy.comando("MODO 0")  # daisy chain normale (uno strumento potrebbe aver lasciato 1 o 2)
        time.sleep(max(0.0, PRIMA_ACCENSIONE_S - (time.monotonic() - inizio)))
        self.daisy.parla()
        self.segnali.fase.emit("misura")

    def _ferma_daisy(self):
        self.daisy.zitto()

    # --- volume (comando VOL del master) ---------------------------------
    def _cambia_ip(self):
        if self.campo_ip.text().strip() != self.daisy.host:
            self.daisy.imposta_host(self.campo_ip.text())
            self.volume, self.stato_chiesto = None, False
            self.label_volume.setText("master: …")

    def _imposta_volume(self):
        if self.fase != "inattivo":
            self.label_volume.setText("non durante la misura")
            return
        self.daisy.comando(f"VOL {self.spin_vol_f1.value()} {self.spin_vol_f2.value()}")

    def _leggi_master(self):
        if not self.daisy.connesso:
            if self.volume is None:
                self.label_volume.setText("master: ESP non raggiungibile")
            self.stato_chiesto = False
            return
        if not self.stato_chiesto:
            self.daisy.comando("STATO")  # risponde anche con "VOL=a b"
            self.stato_chiesto = True
        for riga in self.daisy.righe_ricevute():
            m = re.search(r"VOL=(\d+) (\d+)", riga) or re.search(r"VOL f1=(\d+)% f2=(\d+)%", riga)
            if m:
                self.volume = (int(m.group(1)), int(m.group(2)))
                self.spin_vol_f1.setValue(self.volume[0])
                self.spin_vol_f2.setValue(self.volume[1])
                self.label_volume.setText(f"master: f1 {self.volume[0]}%  f2 {self.volume[1]}%")

    def _cambia_fase(self, fase: str):
        if self.fase == "inattivo":
            return
        self.fase = fase
        self.t_fase = time.monotonic()
        if fase == "misura":
            self.n_on = sum(len(b) for b in self.buf)
            self.t_parla = self.t_fase

    def _tick(self):
        self._leggi_master()
        if self.sock is None:
            return
        pacchetti = []
        while True:
            try:
                dati = self.sock.recv(4096)
            except (BlockingIOError, OSError):
                break
            if len(dati) == M.CAMPIONI_PER_PACCHETTO * 2:
                pacchetti.append(np.frombuffer(dati, dtype=np.int16))
        if pacchetti:
            nuovi = np.concatenate(pacchetti)
            if self.fase in ("muta", "misura"):
                self.buf.append(nuovi)
            self._aggiorna_grafico(nuovi)
        if self.fase == "misura":
            trascorso = time.monotonic() - self.t_fase
            if time.monotonic() - self.t_parla >= INTERVALLO_PARLA_S:
                self.daisy.parla()
                self.t_parla = time.monotonic()
            self.barra.setValue(int(1000 * min(1.0, trascorso / DURATA_S)))
            self.label_fase.setText(f"Daisy chain accesa — PREMI i tappeti! {trascorso:.0f}/{DURATA_S:.0f}s")
            if trascorso >= DURATA_S:
                self._fine_misura()

    def _aggiorna_grafico(self, nuovi: np.ndarray):
        self.coda = np.concatenate((self.coda, nuovi))
        n = len(self.coda) // B
        if n == 0:
            return
        blocchi, self.coda = self.coda[: n * B], self.coda[n * B:]
        p1 = np.maximum(potenze(blocchi, M.F1_HZ), 1e-9)
        p2 = np.maximum(potenze(blocchi, M.F2_HZ), 1e-9)
        self.storico_f1 = np.concatenate((self.storico_f1, p1))[-len(self.asse):]
        self.storico_f2 = np.concatenate((self.storico_f2, p2))[-len(self.asse):]
        self.curva_f1.setData(self.asse, self.storico_f1)
        self.curva_f2.setData(self.asse, self.storico_f2)

    def _fine_misura(self):
        self.fase = "analisi"
        self._ferma_daisy()
        self.sock.close()
        self.sock = None
        x = np.concatenate(self.buf)
        nota = self.campo_nota.text().strip()
        volume = f"f1 {self.volume[0]}%, f2 {self.volume[1]}%" if self.volume else "non letto dal master"
        base = cartella_taratura() / time.strftime("soglie_%H%M%S")
        np.savez_compressed(base.with_suffix(".npz"), x=x, n_on=self.n_on, nota=nota, volume=volume)
        self._lancia_analisi(x, self.n_on, base, nota, volume)

    def _apri(self):
        percorso, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Apri registrazione", str(CARTELLA_ARCHIVIO), "Registrazioni (*.npz)")
        if not percorso:
            return
        d = np.load(percorso)
        self.fase = "analisi"
        self.btn_avvia.setEnabled(False)
        self.btn_apri.setEnabled(False)
        nota = str(d["nota"]) if "nota" in d.files else ""
        volume = str(d["volume"]) if "volume" in d.files else "non registrato"
        self._lancia_analisi(d["x"], int(d["n_on"]), Path(percorso).with_suffix(""), nota, volume)

    def _lancia_analisi(self, x, n_on, base: Path, nota: str = "", volume: str = ""):
        self.label_fase.setText("Analisi in corso…")

        def lavoro():
            try:
                r = analizza(x, n_on)
                r["report"] = f"Nota: {nota or '(nessuna)'}\nVolume: {volume}\n\n" + componi_report(r)
                base.with_suffix(".txt").write_text(r["report"], encoding="utf-8")
                r["file"] = str(base.with_suffix(".txt"))
                self.segnali.risultato.emit(r)
            except TaraturaErrore as e:
                self.segnali.errore.emit(str(e))
            except Exception as e:
                self.segnali.errore.emit(f"Errore inatteso nell'analisi: {e!r}")

        threading.Thread(target=lavoro, daemon=True).start()

    def _mostra_risultato(self, r: dict):
        self.report.setPlainText(r["report"])
        for linea in self.linee_proposte:
            self.plot.removeItem(linea)
        self.linee_proposte = []
        for valore, colore in ((r["proposte"][0], COLORI["verde"]), (r["proposte"][2], COLORI["ambra"])):
            linea = pg.InfiniteLine(np.log10(valore), angle=0, pen=pg.mkPen(colore, width=2))
            self.plot.addItem(linea)
            self.linee_proposte.append(linea)
        self._fine(f"{esito(r)}  ·  report: {r['file']}")

    def _errore(self, messaggio: str):
        self._ferma_daisy()
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self._fine("ERRORE: " + messaggio)

    def _fine(self, testo: str):
        self.fase = "inattivo"
        self.label_fase.setText(testo)
        self.btn_avvia.setEnabled(True)
        self.btn_apri.setEnabled(True)

    def closeEvent(self, evento):
        self.daisy.chiudi()  # ZITTO prima di uscire
        if self.sock is not None:
            self.sock.close()
        super().closeEvent(evento)


def main() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    applica_palette(app)
    finestra = FinestraTaraSoglie()
    finestra.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
