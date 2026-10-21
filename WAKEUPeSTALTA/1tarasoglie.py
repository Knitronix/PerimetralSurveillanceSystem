"""
1tarasoglie.py — WAKEUPeSTALTA
-------------------------------
Misura le soglie Goertzel di 3main.py (SOGLIA_ON/OFF_F1/F2) sulla daisy chain
VERA, in produzione - non piu' stimate da uno sweep (0trovafrequenze.py
serve solo a scegliere le frequenze).

Cosa fa, premendo AVVIA (3main.py deve essere CHIUSO: stessa porta UDP):
  1. registra ~1s di fibra muta (rumore di riferimento)
  2. accende la Shelly: la daisy chain parte in produzione (modalita' 0)
  3. registra DURATA_S secondi - in questo tempo PREMI i tappeti, uno alla
     volta e anche insieme: senza pressioni f2 non suona e non si puo' tarare
  4. spegne la Shelly e analizza:
     - livello tipico del tono f1 e f2, rumore, quanto f1 "sporca" su f2
     - soglie giuste con la stessa regola di sempre (ON = 20% del tono,
       OFF = 65% dell'ON)
     - fa girare la decodifica di 3main.py sulla registrazione due volte, con
       le soglie ATTUALI e con quelle PROPOSTE, e dice se vanno cambiate
  5. salva registrazione + report in misure_soglie/

Frequenze, soglie attuali e decodificatore sono letti da 3main.py stesso (non
copiati): lo strumento prova esattamente quello che gira in produzione.
"""

from __future__ import annotations

import socket
import importlib
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
from others import shelly_control
from others.archivio import CARTELLA_ARCHIVIO, cartella_taratura
from others.tema import COLORI, FONT_MONO, STYLE_QSS, applica_palette, crea_card, etichetta, stile_plot

DURATA_S = 30.0
PRIMA_ACCENSIONE_S = 1.0  # fibra muta registrata prima di accendere la Shelly
FRAZIONE_SOGLIA_ON = 0.20
FRAZIONE_SOGLIA_OFF = 0.65
# Margine minimo richiesto tra soglia ON e il peggior rumore/cross-talk.
MARGINE_MINIMO = 10.0
# Le soglie attuali si considerano ancora buone se distano meno di questo
# fattore da quelle proposte (il tono sta 5x sopra la ON: uno scarto 2x e'
# assorbito) E la decodifica con le soglie attuali e' perfetta.
FATTORE_TOLLERANZA = 2.0
SECONDI_GRAFICO = 10.0

# --- Vibrazioni (passi, botte): il mondo fa rumore anche mentre il master
# suona. Un blocco e' "vibrazione" se l'energia alle frequenze di confronto
# (dove il master non suona mai: f1/f2 +/-400Hz) supera di FATTORE_VIBRAZIONE
# volte il loro livello tipico. Misurato il 19/10/2026: fibra tranquilla
# ~3e-7, botta 5e-5..2e-3 (100-1000x) - 20x separa bene i due casi.
FREQ_CONFRONTO = (M.F1_HZ - 400, M.F1_HZ + 400, M.F2_HZ - 400, M.F2_HZ + 400)
FATTORE_VIBRAZIONE = 20.0
ALLARGA_BLOCCHI = 2  # +/-40ms attorno a ogni blocco di vibrazione (le code)
MARGINE_VIBRAZIONE_S = 0.2  # un errore cosi' vicino a una vibrazione e' attribuito a lei
QUOTA_MAX_VIBRAZIONE = 0.5  # oltre meta' misura con vibrazioni: da rifare

B = M.BLOCCO_GOERTZEL_CAMPIONI
FS = M.SAMPLE_RATE
# Le misure si salvano nell'archivio della giornata: archivio/AAAA-MM-GG/taratura/
# (others/archivio.py). Le vecchie misure_soglie/ restano dove sono.


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


def livello_tono(p: np.ndarray, rumore: float) -> float | None:
    """Potenza tipica dei blocchi in cui il tono suona: mediana dei blocchi
    ben sopra il rumore e vicini al massimo (esclude i blocchi a cavallo
    dell'inizio/fine del tono). None se il tono non c'e'. p va passato GIA'
    senza i blocchi di vibrazione."""
    if len(p) == 0 or p.max() < 30 * rumore:
        return None
    pieni = p[p > 0.3 * np.percentile(p, 99.5)]
    return float(np.median(pieni))


def blocchi_vibrazione(x: np.ndarray) -> np.ndarray:
    """True per i blocchi in cui la fibra riceve una vibrazione a banda larga
    (passo, botta, oggetto che cade): energia alta anche alle frequenze di
    confronto, dove il master non suona mai. Un tono vero e' stretto e lascia
    le frequenze di confronto al livello del rumore; una botta le alza tutte.
    Il riferimento e' la mediana su tutta la registrazione (le botte sono la
    minoranza), quindi si adatta da solo al rumore del giorno."""
    rif = np.median(np.vstack([potenze(x, f) for f in FREQ_CONFRONTO]), axis=0)
    vib = rif > FATTORE_VIBRAZIONE * max(float(np.median(rif)), 1e-12)
    # code della vibrazione: allarga di qualche blocco per lato
    allargato = vib.copy()
    for s in range(1, ALLARGA_BLOCCHI + 1):
        allargato[s:] |= vib[:-s]
        allargato[:-s] |= vib[s:]
    return allargato


def intervalli(maschera: np.ndarray, n_on: int) -> list[tuple[float, float]]:
    """Tratti contigui True della maschera, in secondi dall'accensione."""
    out, inizio = [], None
    for i, v in enumerate(np.append(maschera, False)):
        if v and inizio is None:
            inizio = i
        elif not v and inizio is not None:
            out.append(((inizio * B - n_on) / FS, (i * B - n_on) / FS))
            inizio = None
    return out


def decodifica(x: np.ndarray, n_on: int, soglie: tuple, vib_intervalli: list) -> dict:
    """La decodifica di 3main.py, identica, su tutta la registrazione. Gli
    errori sono separati tra tratti tranquilli e tratti con vibrazioni."""
    p = M.RilevatoreProtocolloF1F2(FS, B, M.F1_HZ, M.F2_HZ, M.T0_MS, M.T1_MS, M.TOLLERANZA_MS, *soglie)
    eventi = []
    for i in range(len(x) // B):
        for e in p.elabora_blocco(x[i * B:(i + 1) * B]):
            e["t1"] = ((i + 1) * B - n_on) / FS
            e["t0"] = e["t1"] - e["durata_s"]
            eventi.append(e)
    zeri = [e for e in eventi if e["tipo"] == "zero"]
    slot = [e for e in eventi if e["tipo"] == "slot"]

    # Numero di tappeti: quanti slot ci sono di solito tra due marcatori.
    per_ciclo = [sum(1 for s in slot if a["t1"] < s["t1"] < b["t1"]) for a, b in zip(zeri, zeri[1:])]
    n_tappeti = int(np.bincount(per_ciclo).argmax()) if per_ciclo else 0

    errori = []  # (t0, t1, descrizione)
    for e in eventi:
        if e["tipo"] == "rumore":
            errori.append((e["t0"], e["t1"], "impulso scartato"))
        elif e["tipo"] == "slot" and e["counter"] > n_tappeti:
            errori.append((e["t0"], e["t1"], f"tappeto inesistente {e['counter']}"))
    if len(zeri) > 2:
        tipico = float(np.median(np.diff([e["t1"] for e in zeri])))
        for a, b in zip(zeri, zeri[1:]):
            if b["t1"] - a["t1"] > 1.5 * tipico:
                errori.append((a["t1"], b["t1"], "marcatore perso"))

    def in_vibrazione(t0, t1):
        return any(t0 < v1 + MARGINE_VIBRAZIONE_S and t1 > v0 - MARGINE_VIBRAZIONE_S for v0, v1 in vib_intervalli)

    tranquilli = [err for err in errori if not in_vibrazione(err[0], err[1])]
    con_vibrazione = [err for err in errori if in_vibrazione(err[0], err[1])]
    chiusure = {}
    for e in slot:
        if e["stato"] == "chiuso" and e["counter"] <= n_tappeti and not in_vibrazione(e["t0"], e["t1"]):
            chiusure[e["counter"]] = chiusure.get(e["counter"], 0) + 1
    return {
        "marcatori": len(zeri), "slot": len(slot), "n_tappeti": n_tappeti,
        "errori_tranquilli": tranquilli, "errori_vibrazione": con_vibrazione,
        "chiusure": chiusure,
        "primo_marcatore_s": zeri[0]["t0"] if zeri else None,
    }


def decodifica_ok(d: dict) -> bool:
    """Giudica le SOGLIE: contano solo gli errori nei tratti tranquilli."""
    return d["marcatori"] > 0 and not d["errori_tranquilli"]


def analizza(x: np.ndarray, n_on: int) -> dict:
    if n_on < B * 10:
        raise TaraturaErrore("Manca la registrazione della fibra muta prima dell'accensione.")
    p1 = potenze(x, M.F1_HZ)
    p2 = potenze(x, M.F2_HZ)
    blocco_on = n_on // B
    vib = blocchi_vibrazione(x)
    vib_intervalli = intervalli(vib, n_on)
    quota_vibrazione = float(vib[blocco_on:].mean())
    tranquillo = ~vib

    # Rumore: fibra muta prima dell'accensione + pause tra i toni (lontano
    # >= 60ms da qualunque blocco con energia), solo blocchi tranquilli.
    muta = np.zeros(len(p1), bool)
    muta[: max(1, blocco_on - 1)] = True
    muta &= tranquillo
    if not muta.any():
        raise TaraturaErrore("Vibrazioni anche durante la fibra muta iniziale: rifai la misura senza toccare la fibra all'inizio.")
    rif = max(np.percentile(p1[muta], 99), np.percentile(p2[muta], 99), 1e-12)
    attivo = (p1 > 20 * rif) | (p2 > 20 * rif)
    lontano = tranquillo.copy()
    for i in np.where(attivo)[0]:
        lontano[max(0, i - 3): i + 4] = False
    lontano[: blocco_on + 5] = False
    rumore_f1 = max(np.percentile(p1[muta], 99), np.percentile(p1[lontano], 99) if lontano.any() else 0)
    rumore_f2 = max(np.percentile(p2[muta], 99), np.percentile(p2[lontano], 99) if lontano.any() else 0)

    acceso = tranquillo.copy()
    acceso[:blocco_on] = False
    tono_f1 = livello_tono(p1[acceso], rumore_f1)
    if tono_f1 is None:
        raise TaraturaErrore(f"Nessun tono a {M.F1_HZ:.0f}Hz: la daisy chain non e' partita? "
                             "(Shelly, alimentazione, master non in modalita' produzione)")
    tono_f2 = livello_tono(p2[acceso], rumore_f2)

    on1 = FRAZIONE_SOGLIA_ON * tono_f1
    proposte = [on1, on1 * FRAZIONE_SOGLIA_OFF, M.SOGLIA_ON_F2, M.SOGLIA_OFF_F2]
    perdita = None
    if tono_f2 is not None:
        on2 = FRAZIONE_SOGLIA_ON * tono_f2
        proposte[2:] = [on2, on2 * FRAZIONE_SOGLIA_OFF]
        # f1 che suona senza f2: quanto si vede su f2 (darebbe chiusure false)
        solo_f1 = acceso & (p1 > on1) & (p2 < 0.05 * tono_f2)
        perdita = float(np.percentile(p2[solo_f1], 99)) if solo_f1.any() else 0.0

    attuali = (M.SOGLIA_ON_F1, M.SOGLIA_OFF_F1, M.SOGLIA_ON_F2, M.SOGLIA_OFF_F2)
    dec_attuali = decodifica(x, n_on, attuali, vib_intervalli)
    dec_proposte = decodifica(x, n_on, tuple(proposte), vib_intervalli)

    rapporti = [attuali[0] / proposte[0]] + ([attuali[2] / proposte[2]] if tono_f2 is not None else [])
    vicine = all(1 / FATTORE_TOLLERANZA <= r <= FATTORE_TOLLERANZA for r in rapporti)
    margine_f1 = proposte[0] / rumore_f1
    margine_f2 = proposte[2] / max(rumore_f2, perdita or 0.0) if tono_f2 is not None else None

    return {
        "durata_s": (len(x) - n_on) / FS, "tono_f1": tono_f1, "tono_f2": tono_f2,
        "rumore_f1": rumore_f1, "rumore_f2": rumore_f2, "perdita": perdita,
        "attuali": attuali, "proposte": tuple(proposte), "f2_tarata": tono_f2 is not None,
        "margine_f1": margine_f1, "margine_f2": margine_f2,
        "dec_attuali": dec_attuali, "dec_proposte": dec_proposte,
        "vib_intervalli": [v for v in vib_intervalli if v[1] > 0], "quota_vibrazione": quota_vibrazione,
        "da_rifare": quota_vibrazione > QUOTA_MAX_VIBRAZIONE,
        "cambiare": not (vicine and decodifica_ok(dec_attuali)),
        "rapporti": rapporti,
    }


def _righe_decodifica(nome: str, d: dict) -> list[str]:
    chiusure = ", ".join(f"tappeto {k}: {v}" for k, v in sorted(d["chiusure"].items())) or "nessuna"
    esito = "PERFETTA" if decodifica_ok(d) else "CON ERRORI"
    righe = [f"  {nome}: {esito} nei tratti tranquilli - marcatori {d['marcatori']}, slot {d['slot']}, "
             f"tappeti {d['n_tappeti']}, chiusure riconosciute: {chiusure}"]
    for titolo, errori in (("errori nei tratti TRANQUILLI (dipendono dalle soglie)", d["errori_tranquilli"]),
                           ("errori DURANTE le vibrazioni (limite di 3main.py, non delle soglie)", d["errori_vibrazione"])):
        if errori:
            dettaglio = ", ".join(f"{descr} a {t0:.1f}s" for t0, _, descr in errori[:6])
            altri = f" (+{len(errori) - 6})" if len(errori) > 6 else ""
            righe.append(f"      {len(errori)} {titolo}: {dettaglio}{altri}")
    return righe


def componi_report(r: dict) -> str:
    a, p = r["attuali"], r["proposte"]
    righe = ["=== 1. MISURA ==="]
    righe.append(f"Registrati {r['durata_s']:.0f}s di daisy chain accesa (f1={M.F1_HZ:.0f}Hz, f2={M.F2_HZ:.0f}Hz, lette da 3main.py).")
    primo = r["dec_proposte"]["primo_marcatore_s"]
    if primo is not None:
        righe.append(f"Avvio della daisy chain: primo marcatore {primo:.2f}s dopo l'accensione della Shelly.")
    if r["vib_intervalli"]:
        tratti = ", ".join(f"{max(v0, 0):.1f}-{v1:.1f}s" for v0, v1 in r["vib_intervalli"][:8])
        righe.append(f"Vibrazioni forti (passi, botte): {r['quota_vibrazione']*100:.0f}% del tempo, a {tratti} "
                     "- escluse dal calcolo delle soglie.")
    else:
        righe.append("Nessuna vibrazione forte durante la misura.")
    if r["da_rifare"]:
        righe.append(f"MISURA DA RIFARE: vibrazioni oltre il {QUOTA_MAX_VIBRAZIONE*100:.0f}% del tempo, "
                     "troppo pochi tratti tranquilli per tarare. Premi i tappeti senza colpire la fibra.")
        return "\n".join(righe)
    if not r["f2_tarata"]:
        righe.append("ATTENZIONE: nessuna pressione vista (f2 mai suonata) - soglie f2 NON tarate, "
                     "rifai la misura premendo i tappeti.")

    righe.append("\n=== 2. LIVELLI MISURATI nei tratti tranquilli (scala Goertzel di 3main.py) ===")
    righe.append(f"Tono f1: {r['tono_f1']:.6f}    rumore a f1 (picchi): {r['rumore_f1']:.1e}")
    if r["f2_tarata"]:
        righe.append(f"Tono f2: {r['tono_f2']:.6f}    rumore a f2 (picchi): {r['rumore_f2']:.1e}    "
                     f"f1 che sporca su f2: {r['perdita']:.1e}")

    righe.append("\n=== 3. SOGLIE GIUSTE (ON = 20% del tono, OFF = 65% dell'ON) ===")
    righe.append(f"f1: ON {p[0]:.6f}  OFF {p[1]:.6f}   - ON sta {r['margine_f1']:.0f}x sopra il rumore")
    if r["f2_tarata"]:
        righe.append(f"f2: ON {p[2]:.6f}  OFF {p[3]:.6f}   - ON sta {r['margine_f2']:.0f}x sopra rumore/sporco di f1")
    for nome, m in (("f1", r["margine_f1"]), ("f2", r["margine_f2"])):
        if m is not None and m < MARGINE_MINIMO:
            righe.append(f"ATTENZIONE: margine {nome} basso ({m:.0f}x, serve >= {MARGINE_MINIMO:.0f}x): "
                         "tono debole o rumore alto, valuta altre frequenze con 0trovafrequenze.py")

    righe.append("\n=== 4. CONFRONTO CON MAIN.PY (stessa registrazione, decodifica di 3main.py) ===")
    righe.append(f"Soglie attuali:  f1 ON {a[0]:.6f} OFF {a[1]:.6f}   f2 ON {a[2]:.6f} OFF {a[3]:.6f}")
    righe += _righe_decodifica("con soglie ATTUALI ", r["dec_attuali"])
    righe += _righe_decodifica("con soglie PROPOSTE", r["dec_proposte"])
    distanze = ", ".join(f"{nome} attuale = {v:.2f}x il giusto" for nome, v in zip(("ON f1", "ON f2"), r["rapporti"]))
    righe.append(f"Distanza dal giusto: {distanze} (tollerato fino a {FATTORE_TOLLERANZA:.0f}x in su o in giu').")
    if r["cambiare"]:
        motivo = ("decodifica con le soglie attuali CON ERRORI nei tratti tranquilli" if not decodifica_ok(r["dec_attuali"])
                  else f"soglie attuali oltre {FATTORE_TOLLERANZA:.0f}x dal giusto: oggi funzionano, ma con meno "
                       "margine del previsto")
        righe.append(f"ESITO: CAMBIARE le soglie in 3main.py - {motivo}.")
    else:
        righe.append("ESITO: le soglie attuali VANNO BENE (entro 2x dal giusto e decodifica perfetta nei tratti "
                     "tranquilli): non serve cambiarle.")
    if r["dec_attuali"]["errori_vibrazione"]:
        righe.append("Nota: gli errori durante le vibrazioni NON si correggono con le soglie - servono le difese "
                     "di 3main.py (controllo 'tono stretto', contatore limitato ai tappeti esistenti).")

    righe.append("\n=== 5. DA COPIARE in 3main.py (solo se l'esito dice CAMBIARE) ===")
    righe.append(f"SOGLIA_ON_F1 = {p[0]:.6f}\nSOGLIA_OFF_F1 = {p[1]:.6f}")
    if r["f2_tarata"]:
        righe.append(f"SOGLIA_ON_F2 = {p[2]:.6f}\nSOGLIA_OFF_F2 = {p[3]:.6f}")
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
        self.coda = np.zeros(0, dtype=np.int16)
        n_grafico = int(SECONDI_GRAFICO * FS / B)
        self.storico_f1 = np.full(n_grafico, 1e-9)
        self.storico_f2 = np.full(n_grafico, 1e-9)
        self._costruisci_ui()
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
        riga.addWidget(etichetta("IP Shelly"))
        self.campo_ip = QtWidgets.QLineEdit(M.Config().SMART_PLUG_HOST)
        riga.addWidget(self.campo_ip, 1)
        lay.addLayout(riga)
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

    def _shelly(self, host: str, accendi: bool) -> bool:
        for _ in range(5):
            try:
                (shelly_control.turn_on if accendi else shelly_control.turn_off)(host)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def _sequenza(self, host: str):
        time.sleep(PRIMA_ACCENSIONE_S)
        if not self._shelly(host, True):
            self.segnali.errore.emit(f"Shelly {host} non risponde: accensione non riuscita.")
            return
        self.segnali.fase.emit("misura")

    def _cambia_fase(self, fase: str):
        if self.fase == "inattivo":
            return
        self.fase = fase
        self.t_fase = time.monotonic()
        if fase == "misura":
            self.n_on = sum(len(b) for b in self.buf)

    def _tick(self):
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
        host = self.campo_ip.text().strip()
        threading.Thread(target=self._shelly, args=(host, False), daemon=True).start()
        self.sock.close()
        self.sock = None
        x = np.concatenate(self.buf)
        nota = self.campo_nota.text().strip()
        base = cartella_taratura() / time.strftime("soglie_%H%M%S")
        np.savez_compressed(base.with_suffix(".npz"), x=x, n_on=self.n_on, nota=nota)
        self._lancia_analisi(x, self.n_on, base, nota)

    def _apri(self):
        percorso, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Apri registrazione", str(CARTELLA_ARCHIVIO), "Registrazioni (*.npz)")
        if not percorso:
            return
        d = np.load(percorso)
        self.fase = "analisi"
        self.btn_avvia.setEnabled(False)
        self.btn_apri.setEnabled(False)
        nota = str(d["nota"]) if "nota" in d.files else ""
        self._lancia_analisi(d["x"], int(d["n_on"]), Path(percorso).with_suffix(""), nota)

    def _lancia_analisi(self, x, n_on, base: Path, nota: str = ""):
        self.label_fase.setText("Analisi in corso…")

        def lavoro():
            try:
                r = analizza(x, n_on)
                r["report"] = f"Nota: {nota or '(nessuna)'}\n\n" + componi_report(r)
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
        esito = "CAMBIARE le soglie" if r["cambiare"] else "soglie attuali OK"
        self._fine(f"Fatto: {esito}. Linee piene = soglie ON proposte. Report: {r['file']}")

    def _errore(self, messaggio: str):
        if self.fase in ("muta", "misura"):
            threading.Thread(target=self._shelly, args=(self.campo_ip.text().strip(), False), daemon=True).start()
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
        if self.fase in ("muta", "misura"):
            self._shelly(self.campo_ip.text().strip(), False)
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
