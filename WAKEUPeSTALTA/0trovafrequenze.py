"""
0trovafrequenze.py — WAKEUPeSTALTA
-----------------------------------
Risponde a una sola domanda: "con QUESTA fibra, in QUESTO mezzo, quali
frequenze devo usare per f1/f2?". Sostituisce analizza_armoniche_sweep.py
per la scelta delle frequenze (quello resta per l'analisi delle spurie).

Cosa fa, da solo, premendo AVVIA:
  1. spegne la Shelly e registra SILENZIO_S secondi di fibra senza toni:
     e' il rumore ambientale vero, contro cui ogni tono deve spiccare
  2. porta il master in MODALITA' 1 (sweep 200->6000Hz, gradini da 4s)
     ciclando la Shelly, poi registra N cicli completi della rampa
  3. spegne la Shelly, analizza, salva registrazione grezza + report in
     misure_frequenze/ (la registrazione si puo' rianalizzare dopo con
     "Apri registrazione...")

Perche' e' affidabile dove il vecchio script non lo era: il vecchio
decideva QUALE gradino stava suonando contando il tempo da quando partiva
lui - ma il master parte a suonare al boot, ~3s prima, quindi ogni gradino
veniva etichettato con la frequenza sbagliata (+200Hz). Qui il tempo non
conta: l'allineamento si ricava DAI DATI, cercando lo sfasamento (e la
durata reale del gradino, per assorbire la deriva dell'orologio Arduino)
che fa combaciare l'intera rampa nota con l'energia misurata. Un solo
numero stimato su TUTTI i gradini insieme: un'armonica forte o una
frequenza morta non possono spostarlo. Il risultato e' poi verificato
(confidenza + controllo che nessun gradino sia "sovrastato" dal vicino a
+/-200Hz, il sintomo esatto del vecchio errore) e se non torna il report
lo dice esplicitamente invece di proporre frequenze.

Le potenze sono calcolate come le calcola 3main.py (blocchi da 20ms, stessa
normalizzazione del Goertzel) e riscalate a una STIMA della produzione
(FATTORE_PRODUZIONE): in modalita' sweep il master suona un tono a piena
ampiezza, in daisy chain a meta' ampiezza. La misura reale del 19/10/2026
ha mostrato che la stima e' ottimista di ~2x (~3dB): va bene per
CONFRONTARE le frequenze tra loro, non per le soglie. Questo script quindi
NON propone soglie: si misurano sulla daisy chain vera con 1tarasoglie.py.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from others.archivio import CARTELLA_ARCHIVIO, cartella_taratura
from others.shelly_mode_selector import seleziona_modalita, spegni_master
from others.tema import COLORI, FONT_MONO, STYLE_QSS, applica_palette, crea_card, etichetta, stile_plot

# --- Configurazione ---
SMART_PLUG_HOST_DEFAULT = "10.27.239.193"  # modificabile anche dalla GUI
MODALITA_SWEEP_FREQUENZE = 1  # vedi arduino/master/master.ino

UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000

# Rampa del master in modalita' 1 - DEVE restare uguale a SWEEPF_* in master.ino
SWEEP_FREQ_MIN = 200
SWEEP_FREQ_MAX = 6000
SWEEP_FREQ_STEP = 200
SWEEP_STEP_S = 4.0
FREQUENZE = np.arange(SWEEP_FREQ_MIN, SWEEP_FREQ_MAX + 1, SWEEP_FREQ_STEP)
N_GRADINI = len(FREQUENZE)
DURATA_CICLO_S = N_GRADINI * SWEEP_STEP_S

# Come 3main.py: blocco Goertzel da 20ms e regola delle soglie (ON = 20% del
# picco, OFF = 65% dell'ON).
BLOCCO = 480
FRAZIONE_SOGLIA_ON = 0.20
FRAZIONE_SOGLIA_OFF = 0.65
# Tono singolo a piena ampiezza nello sweep -> tono a meta' ampiezza in
# daisy chain: potenza x 0.25.
FATTORE_PRODUZIONE = 0.25

# Sotto questa frequenza c'e' l'energia di passi, motori e rumore
# ambientale (vedi CLAUDE.md): non si propone come f1/f2.
FREQ_MIN_UTILE = 1000

SILENZIO_S = 10.0
CICLI_DEFAULT = 3
GUARDIA_S = 0.5  # si scarta l'inizio e la fine di ogni gradino (transizioni)
FRAME_ALLINEAMENTO = 6000  # 0.25s, risoluzione 4Hz: le frequenze della rampa cadono su bin esatti
# Un "buco" nel flusso UDP piu' lungo di cosi' viene riempito di zeri (marcati
# non validi) per non far scivolare l'asse dei tempi: le raffiche normali
# dell'interrogatore (~169ms) restano ben sotto.
BUCO_MINIMO_S = 0.5

SOGLIE_VERDETTO_DB = ((20.0, "OTTIMA"), (12.0, "BUONA"), (6.0, "AL LIMITE"))

# Le misure si salvano nell'archivio della giornata: archivio/AAAA-MM-GG/taratura/
# (others/archivio.py). Le vecchie misure_frequenze/ restano dove sono.


# ======================================================================
# Analisi (pura, nessuna dipendenza dalla GUI)
# ======================================================================
class AnalisiErrore(Exception):
    pass


def potenze_blocchi(campioni: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """Potenza a ciascuna frequenza in blocchi da 20ms, stessa scala del
    Goertzel di 3main.py (1.0 = tono a piena scala int16). Righe = blocchi."""
    n = len(campioni) // BLOCCO
    if n == 0:
        return np.zeros((0, len(freqs)))
    blocchi = campioni[: n * BLOCCO].astype(np.float64).reshape(n, BLOCCO)
    spettro = np.fft.rfft(blocchi, axis=1)
    k = np.round(freqs * BLOCCO / SAMPLE_RATE).astype(int)
    return np.abs(spettro[:, k]) ** 2 / (BLOCCO / 2.0) ** 2 / 32768.0 ** 2


def _punteggi(z, t, durata, offsets):
    idx = np.floor((t[:, None] + offsets[None, :]) / durata).astype(np.int64) % N_GRADINI
    return z[np.arange(len(t))[:, None], idx].mean(axis=0)


def stima_allineamento(sweep: np.ndarray, valido: np.ndarray) -> dict:
    """Trova (durata gradino, sfasamento) che fanno combaciare la rampa nota
    con l'energia misurata. Convenzione: al tempo t (s dall'inizio della
    registrazione) suona il gradino floor((t + offset) / durata) % N."""
    f = FRAME_ALLINEAMENTO
    n = len(sweep) // f
    if n < 40:
        raise AnalisiErrore("Registrazione dello sweep troppo corta (servono almeno ~10s, meglio 3 cicli).")
    frame = sweep[: n * f].astype(np.float64).reshape(n, f)
    ok = valido[: n * f].reshape(n, f).all(axis=1)
    spettro = np.abs(np.fft.rfft(frame * np.hanning(f), axis=1))
    k = np.round(FREQUENZE * f / SAMPLE_RATE).astype(int)
    z = np.log10(spettro[:, k] ** 2 + 1e-30)[ok]
    t = ((np.arange(n) * f + f / 2) / SAMPLE_RATE)[ok]
    # Doppia centratura: una frequenza che la fibra sente sempre forte non
    # deve "attirare" l'allineamento, e nemmeno un frame piu' rumoroso.
    z = z - z.mean(axis=0, keepdims=True)
    z = z - z.mean(axis=1, keepdims=True)

    migliore = (-np.inf, SWEEP_STEP_S, 0.0)
    for durata in np.arange(SWEEP_STEP_S * 0.99, SWEEP_STEP_S * 1.01 + 1e-9, 0.002):
        offsets = np.arange(0.0, N_GRADINI * durata, 0.1)
        s = _punteggi(z, t, durata, offsets)
        i = int(np.argmax(s))
        if s[i] > migliore[0]:
            migliore = (float(s[i]), float(durata), float(offsets[i]))
    _, d0, o0 = migliore
    for durata in np.arange(d0 - 0.002, d0 + 0.002 + 1e-9, 0.0002):
        offsets = np.arange(o0 - 0.15, o0 + 0.15 + 1e-9, 0.01)
        s = _punteggi(z, t, durata, offsets)
        i = int(np.argmax(s))
        if s[i] > migliore[0]:
            migliore = (float(s[i]), float(durata), float(offsets[i]))
    punteggio, durata, offset = migliore

    # Confidenza: quanto il migliore stacca il miglior candidato sfasato di
    # almeno 3/4 di gradino (l'errore che conta e' "un gradino intero", come
    # nel vecchio script), rispetto al livello tipico. Un allineamento vero
    # da' ~3-4, il rumore puro ~1.
    periodo = N_GRADINI * durata
    offsets = np.arange(0.0, periodo, 0.05)
    s = _punteggi(z, t, durata, offsets)
    distanza = np.abs((offsets - offset + periodo / 2) % periodo - periodo / 2)
    tipico = float(np.median(s))
    secondo = float(s[distanza > 0.75 * durata].max())
    confidenza = (punteggio - tipico) / max(secondo - tipico, 1e-9)
    return {"durata": durata, "offset": offset % periodo, "confidenza": confidenza}


def estrai_gradini(sweep, valido, durata, offset):
    """Per ogni gradino interamente registrato: (indice frequenza, ciclo,
    potenze dei blocchi da 20ms nella parte centrale del gradino)."""
    durata_tot = len(sweep) / SAMPLE_RATE
    g_primo = int(np.ceil(offset / durata))
    g_ultimo = int(np.floor((durata_tot + offset) / durata)) - 1
    gradini = []
    for g in range(g_primo, g_ultimo + 1):
        inizio = g * durata - offset
        a = int((inizio + GUARDIA_S) * SAMPLE_RATE)
        b = int((inizio + durata - GUARDIA_S) * SAMPLE_RATE)
        if b - a < BLOCCO * 10 or not valido[a:b].all():
            continue
        gradini.append((g % N_GRADINI, g // N_GRADINI, potenze_blocchi(sweep[a:b], FREQUENZE)))
    return gradini


def relazione_armonica(fa: float, fb: float, tolleranza_hz: float = 40.0) -> int | None:
    basso, alto = min(fa, fb), max(fa, fb)
    for n in range(2, 7):
        if abs(alto - n * basso) <= tolleranza_hz:
            return n
    return None


def verdetto(margine_db: float) -> str:
    for soglia, nome in SOGLIE_VERDETTO_DB:
        if margine_db >= soglia:
            return nome
    return "SCONSIGLIATA"


def analizza(sweep: np.ndarray, valido: np.ndarray, silenzio: np.ndarray) -> dict:
    if len(silenzio) < 2 * SAMPLE_RATE:
        raise AnalisiErrore("Manca la registrazione del silenzio (servono almeno 2s): impossibile stimare il rumore ambientale.")

    allineamento = stima_allineamento(sweep, valido)
    gradini = estrai_gradini(sweep, valido, allineamento["durata"], allineamento["offset"])
    if not gradini:
        raise AnalisiErrore("Nessun gradino completo nella registrazione.")

    # Rumore ambientale: il 99esimo percentile dei blocchi a fibra muta.
    rumore = np.maximum(np.percentile(potenze_blocchi(silenzio, FREQUENZE), 99, axis=0), 1e-15)

    n = N_GRADINI
    segnale_med = np.full(n, np.nan)
    segnale_p5 = np.full(n, np.nan)
    perdita = np.full((n, n), np.nan)  # [suona i, misurato su j], 99esimo percentile
    instabilita = np.full(n, np.nan)  # max/min della mediana tra un ciclo e l'altro
    viste = np.zeros(n, dtype=int)
    for i in range(n):
        occorrenze = [b for k, _, b in gradini if k == i]
        viste[i] = len(occorrenze)
        if not occorrenze:
            continue
        blocchi = np.vstack(occorrenze)
        segnale_med[i] = np.median(blocchi[:, i])
        segnale_p5[i] = np.percentile(blocchi[:, i], 5)
        perdita[i] = np.percentile(blocchi, 99, axis=0)
        mediane = [np.median(b[:, i]) for b in occorrenze]
        instabilita[i] = max(mediane) / max(min(mediane), 1e-15)

    # Verifica dell'allineamento: con l'etichetta giusta, un tono chiaramente
    # presente non puo' essere sovrastato dal gradino vicino (+/-200Hz).
    controllati = superati = 0
    for k, _, b in gradini:
        if FREQUENZE[k] < 600:
            continue
        med = np.median(b, axis=0)
        if med[k] < 10 * rumore[k]:
            continue
        controllati += 1
        vicini = [med[j] for j in (k - 1, k + 1) if 0 <= j < n]
        superati += int(all(med[k] > v for v in vicini))
    verifica = superati / controllati if controllati else 0.0

    # Tutto riscalato a come lo vedra' 3main.py in produzione.
    segnale_prod = segnale_med * FATTORE_PRODUZIONE
    segnale_p5_prod = segnale_p5 * FATTORE_PRODUZIONE
    perdita_prod = perdita * FATTORE_PRODUZIONE
    snr_db = 10 * np.log10(segnale_prod / rumore)

    coppie = []
    candidati = [i for i in range(n) if FREQUENZE[i] >= FREQ_MIN_UTILE and viste[i] > 0]
    for a in candidati:
        for b in candidati:
            if a == b:
                continue
            # f1 suona anche da sola (marcatore, slot aperto): quello che
            # perde su f2 conta come rumore per f2 (falsa chiusura).
            # f2 invece non suona mai senza f1: niente da contare al contrario.
            pavimento_f1 = rumore[a]
            pavimento_f2 = max(rumore[b], perdita_prod[a, b])
            margine_f1 = 10 * np.log10(segnale_p5_prod[a] / pavimento_f1)
            margine_f2 = 10 * np.log10(segnale_p5_prod[b] / pavimento_f2)
            margine = min(margine_f1, margine_f2)
            armonica = relazione_armonica(FREQUENZE[a], FREQUENZE[b])
            if armonica:
                margine -= 6.0  # penalita': con i due toni insieme la distorsione puo' sommarsi
            coppie.append({
                "f1": int(FREQUENZE[a]), "f2": int(FREQUENZE[b]),
                "margine_db": float(margine), "margine_f1_db": float(margine_f1),
                "margine_f2_db": float(margine_f2), "armonica": armonica,
                "verdetto": verdetto(float(margine)),
            })
    coppie.sort(key=lambda c: -c["margine_db"])

    # Armoniche tipiche della fibra (quanto sotto la fondamentale).
    armoniche = {}
    for ordine in (2, 3):
        rapporti = []
        for i in range(n):
            if viste[i] == 0 or snr_db[i] < 20:
                continue
            j = np.where(FREQUENZE == FREQUENZE[i] * ordine)[0]
            if len(j):
                rapporti.append(perdita[i, j[0]] / segnale_med[i])
        if rapporti:
            armoniche[ordine] = float(10 * np.log10(np.median(rapporti)))

    cicli_completi = int(viste.min())
    mancante = float(1.0 - valido.mean())
    problemi = []
    if allineamento["confidenza"] < 2.0:
        problemi.append(f"allineamento incerto (confidenza {allineamento['confidenza']:.2f}, serve >= 2.0)")
    if verifica < 0.8:
        problemi.append(f"verifica allineamento fallita ({verifica*100:.0f}% gradini coerenti, serve >= 80%)")
    if mancante > 0.02:
        problemi.append(f"troppi dati persi dal flusso UDP ({mancante*100:.1f}%)")
    if cicli_completi < 1:
        problemi.append("nemmeno un ciclo completo della rampa registrato")

    return {
        "allineamento": allineamento, "verifica": verifica, "controllati": controllati,
        "cicli_completi": cicli_completi, "mancante": mancante,
        "durata_sweep_s": len(sweep) / SAMPLE_RATE, "durata_silenzio_s": len(silenzio) / SAMPLE_RATE,
        "affidabile": not problemi, "problemi": problemi,
        "rumore": rumore, "segnale_prod": segnale_prod, "snr_db": snr_db,
        "instabilita": instabilita, "viste": viste, "armoniche": armoniche, "coppie": coppie,
    }


def _intervalli(freqs: list[int]) -> str:
    if not freqs:
        return "nessuna"
    gruppi, inizio, prec = [], freqs[0], freqs[0]
    for f in freqs[1:]:
        if f - prec != SWEEP_FREQ_STEP:
            gruppi.append((inizio, prec))
            inizio = f
        prec = f
    gruppi.append((inizio, prec))
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in gruppi) + " Hz"


def componi_report(r: dict) -> str:
    al = r["allineamento"]
    righe = ["=== 1. QUALITA' DELLA MISURA ===",
             "(Leggere SEMPRE per prima: se l'esito e' NON AFFIDABILE il resto del report non va usato.",
             " Confidenza = quanto l'allineamento trovato batte quello sfasato di un gradino: >= 2 ok, ~1 = a caso.",
             " Verifica = % di gradini in cui il tono comandato e' piu' forte dei gradini vicini: >= 80% ok.)"]
    righe.append(f"Sweep registrato: {r['durata_sweep_s']:.0f}s ({r['cicli_completi']} cicli completi) - "
                 f"silenzio: {r['durata_silenzio_s']:.0f}s - dati UDP persi: {r['mancante']*100:.1f}%")
    righe.append(f"Allineamento: gradino reale {al['durata']:.4f}s, confidenza {al['confidenza']:.2f}, "
                 f"verifica {r['verifica']*100:.0f}% su {r['controllati']} gradini")
    if r["affidabile"]:
        righe.append("ESITO: MISURA AFFIDABILE" + (" (1 solo ciclo: indicativa, meglio 3)" if r["cicli_completi"] < 2 else ""))
    else:
        righe.append("ESITO: MISURA NON AFFIDABILE - NON usare le frequenze sotto. Motivi:")
        righe += [f"  - {p}" for p in r["problemi"]]

    snr = r["snr_db"]
    valide = [i for i in range(N_GRADINI) if r["viste"][i] > 0]
    righe.append("\n=== 2. COSA SUCCEDE IN QUESTA CONFIGURAZIONE ===")
    righe.append("(SNR = tono come lo vedra' 3main.py in produzione / picchi del rumore ambientale misurato")
    righe.append(" nei secondi di silenzio. 20dB = tono 100 volte sopra il rumore, 10dB = 10 volte.")
    righe.append(" Armoniche: una 3a armonica a -20dB vale 1/100 del tono; vicina a 0dB = forte quanto il tono.)")
    righe.append(f"Banda forte (SNR >= 20dB): {_intervalli([int(FREQUENZE[i]) for i in valide if snr[i] >= 20])}")
    righe.append(f"Banda debole (10-20dB):    {_intervalli([int(FREQUENZE[i]) for i in valide if 10 <= snr[i] < 20])}")
    righe.append(f"Inutilizzabile (< 10dB):   {_intervalli([int(FREQUENZE[i]) for i in valide if snr[i] < 10])}")
    rumorose = np.argsort(r["rumore"])[::-1][:3]
    righe.append("Rumore ambientale piu' alto a: " + ", ".join(
        f"{FREQUENZE[i]}Hz ({r['rumore'][i]:.1e})" for i in rumorose))
    if r["armoniche"]:
        righe.append("Distorsione della fibra: " + ", ".join(
            f"{o}a armonica tipicamente {v:+.0f}dB rispetto al tono" for o, v in r["armoniche"].items()))
    instabili = [int(FREQUENZE[i]) for i in valide if r["instabilita"][i] > 2.0]
    if instabili and r["cicli_completi"] >= 2:
        righe.append(f"Instabili tra un ciclo e l'altro (oltre 3dB): {', '.join(map(str, instabili))} Hz")

    righe.append("\n=== 3. FREQUENZE MIGLIORI ===")
    righe.append(f"Singole frequenze (>= {FREQ_MIN_UTILE}Hz), dalla piu' forte:")
    ordinate = sorted((i for i in valide if FREQUENZE[i] >= FREQ_MIN_UTILE), key=lambda i: -snr[i])
    for i in ordinate[:8]:
        righe.append(f"  {FREQUENZE[i]:5d} Hz   SNR {snr[i]:5.1f} dB")
    righe.append("\nCoppie, dalla migliore. Margine = di quanto anche i blocchi da 20ms piu' deboli del tono")
    righe.append("(il 5% peggiore) restano sopra il rumore; per f2 conta anche quanto f1 'sporca' su f2,")
    righe.append("che darebbe tappeti chiusi falsi. OTTIMA >= 20dB, BUONA >= 12, AL LIMITE >= 6, sotto: da non usare.")
    righe.append("L'ordine conta: la prima e' f1 (clock), la seconda f2 (tappeto chiuso).")
    for c in r["coppie"][:8]:
        nota = f"  [f2 = {c['armonica']} x f1, penalizzata]" if c["armonica"] else ""
        righe.append(f"  f1={c['f1']:5d}  f2={c['f2']:5d}   margine {c['margine_db']:5.1f} dB  {c['verdetto']}{nota}")

    if r["coppie"] and r["affidabile"]:
        c = r["coppie"][0]
        righe.append("\n=== 4. SE DECIDI DI CAMBIARE FREQUENZE ===")
        righe.append("(Cambiare conviene solo se la coppia migliore stacca di parecchi dB quella in uso:")
        righe.append(" pochi dB non valgono un riflash.)")
        righe.append("Frequenze che il master deve suonare (righe FREQ1/FREQ2 in testa allo sketch, poi riflash):")
        righe.append(f"  arduino/master/master.ino:  FREQ1 = {c['f1']};  FREQ2 = {c['f2']};")
        righe.append("Le stesse, lato PC - DEVONO coincidere con il master, altrimenti 3main.py non sente nulla:")
        righe.append(f"  3main.py:  F1_HZ = {c['f1']:.1f}   F2_HZ = {c['f2']:.1f}")
        righe.append("Se cambi f2, aggiorna anche FREQ_TEST_HZ in 2analizzatempiminimi.py.")
        righe.append("SOGLIE: questo script NON le propone (misura uno sweep, non la produzione).")
        righe.append("Dopo aver cambiato frequenze, misurale sulla daisy chain vera con 1tarasoglie.py.")
    return "\n".join(righe)


# ======================================================================
# GUI
# ======================================================================
class Segnali(QtCore.QObject):
    fase = QtCore.Signal(str)
    errore = QtCore.Signal(str)
    risultato = QtCore.Signal(object)


class FinestraTrovaFrequenze(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Trova frequenze f1/f2 — WAKEUPeSTALTA")
        self.resize(1400, 860)
        self.setStyleSheet(STYLE_QSS)

        self.segnali = Segnali()
        self.segnali.fase.connect(self._cambia_fase)
        self.segnali.errore.connect(self._errore)
        self.segnali.risultato.connect(self._mostra_risultato)

        self.fase = "inattivo"
        self.sock: socket.socket | None = None
        self._annulla = threading.Event()
        self._reset_buffer()
        self.ultimo_risultato: dict | None = None

        self._costruisci_ui()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)

    # --- UI -----------------------------------------------------------
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
        self.campo_ip = QtWidgets.QLineEdit(SMART_PLUG_HOST_DEFAULT)
        riga.addWidget(self.campo_ip, 1)
        riga.addWidget(etichetta("Cicli"))
        self.spin_cicli = QtWidgets.QSpinBox()
        self.spin_cicli.setRange(1, 10)
        self.spin_cicli.setValue(CICLI_DEFAULT)
        riga.addWidget(self.spin_cicli)
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
        self.btn_ferma = QtWidgets.QPushButton("Ferma e analizza")
        self.btn_ferma.clicked.connect(self._ferma)
        self.btn_ferma.setEnabled(False)
        self.btn_apri = QtWidgets.QPushButton("Apri registrazione…")
        self.btn_apri.setObjectName("Piatto")
        self.btn_apri.clicked.connect(self._apri_registrazione)
        for b in (self.btn_avvia, self.btn_ferma, self.btn_apri):
            bottoni.addWidget(b)
        lay.addLayout(bottoni)
        self.label_fase = QtWidgets.QLabel("Pronto. Durata: 10s di silenzio + ~15s di selezione modalità + i cicli scelti (2 min l'uno).")
        self.label_fase.setWordWrap(True)
        lay.addWidget(self.label_fase)
        self.barra = QtWidgets.QProgressBar()
        self.barra.setRange(0, 1000)
        self.barra.setTextVisible(False)
        lay.addWidget(self.barra)
        sinistra.addWidget(card)

        card, lay, testata = crea_card("Spettro dal vivo", colore_titolo=COLORI["blu"])
        self.label_picco = QtWidgets.QLabel("—")
        self.label_picco.setObjectName("Mono")
        testata.addWidget(self.label_picco)
        self.plot_live = pg.PlotWidget()
        stile_plot(self.plot_live, "Potenza", "Frequenza (Hz)")
        self.plot_live.setLogMode(y=True)
        self.plot_live.setXRange(0, 6500)
        self.curva_live = self.plot_live.plot(pen=pg.mkPen(COLORI["blu"], width=1.2))
        lay.addWidget(self.plot_live)
        sinistra.addWidget(card, 1)
        layout.addLayout(sinistra, 4)

        destra = QtWidgets.QVBoxLayout()
        card, lay, testata = crea_card("Risultato · come lo vedrà 3main.py", colore_titolo=COLORI["verde"])
        self.plot_ris = pg.PlotWidget()
        stile_plot(self.plot_ris, "Potenza (scala Goertzel)", "Frequenza (Hz)")
        self.plot_ris.setLogMode(y=True)
        self.plot_ris.addLegend(offset=(10, 10))
        self.curva_segnale = self.plot_ris.plot(pen=pg.mkPen(COLORI["accento"], width=2), symbol="o",
                                                symbolSize=6, symbolBrush=COLORI["accento"], name="tono (produzione)")
        self.curva_rumore = self.plot_ris.plot(pen=pg.mkPen(COLORI["pericolo"], width=1.5, style=QtCore.Qt.DashLine),
                                               name="rumore ambientale (picchi)")
        self.linea_f1 = pg.InfiniteLine(angle=90, pen=pg.mkPen(COLORI["verde"], width=2), label="f1",
                                        labelOpts={"color": COLORI["verde"], "position": 0.95})
        self.linea_f2 = pg.InfiniteLine(angle=90, pen=pg.mkPen(COLORI["ambra"], width=2), label="f2",
                                        labelOpts={"color": COLORI["ambra"], "position": 0.88})
        for linea in (self.linea_f1, self.linea_f2):
            linea.setVisible(False)
            self.plot_ris.addItem(linea)
        lay.addWidget(self.plot_ris, 3)

        self.tabella = QtWidgets.QTableWidget(0, 5)
        self.tabella.setHorizontalHeaderLabels(["f1 (Hz)", "f2 (Hz)", "margine (dB)", "verdetto", "note"])
        self.tabella.horizontalHeader().setStretchLastSection(True)
        self.tabella.verticalHeader().setVisible(False)
        self.tabella.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabella.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tabella.itemSelectionChanged.connect(self._evidenzia_coppia)
        lay.addWidget(self.tabella, 2)

        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setStyleSheet(f"font-family: {FONT_MONO}; font-size: 12px;")
        lay.addWidget(self.report, 3)
        destra.addWidget(card)
        layout.addLayout(destra, 6)

    # --- Acquisizione -------------------------------------------------
    def _reset_buffer(self):
        self.buf_silenzio: list[np.ndarray] = []
        self.buf_sweep: list[np.ndarray] = []
        self.buf_valido: list[np.ndarray] = []
        self.campioni_sweep = 0
        self.t_inizio_fase = 0.0
        self.coda_live = np.zeros(SAMPLE_RATE // 2, dtype=np.int16)

    def _apri_socket(self):
        if self.sock is not None:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        self.sock.bind((UDP_IP, UDP_PORT))
        self.sock.setblocking(False)

    def _chiudi_socket(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _avvia(self):
        try:
            self._apri_socket()
        except OSError as e:
            self._errore(f"Porta UDP {UDP_PORT} occupata (3main.py o un altro script è aperto?): {e}")
            return
        self._reset_buffer()
        self._annulla.clear()
        # Va impostata PRIMA di avviare la sequenza: _cambia_fase ignora i
        # cambi quando la fase e' "inattivo" (= misura fermata dall'utente).
        self.fase = "avvio"
        self.report.clear()
        self.tabella.setRowCount(0)
        self.btn_avvia.setEnabled(False)
        self.btn_apri.setEnabled(False)
        self.btn_ferma.setEnabled(True)
        host = self.campo_ip.text().strip()
        threading.Thread(target=self._sequenza_shelly, args=(host,), daemon=True).start()

    def _sequenza_shelly(self, host: str):
        try:
            self.segnali.fase.emit("spegnimento")
            spegni_master(host)
            if self._annulla.wait(2.0):
                return
            self.segnali.fase.emit("silenzio")
            if self._annulla.wait(SILENZIO_S):
                return
            self.segnali.fase.emit("selezione")
            seleziona_modalita(host, MODALITA_SWEEP_FREQUENZE)
            if not self._annulla.is_set():
                self.segnali.fase.emit("sweep")
        except Exception as e:  # rete, Shelly irraggiungibile...
            self.segnali.errore.emit(f"Shelly {host} non risponde: {e}")

    def _cambia_fase(self, fase: str):
        if self.fase in ("inattivo", "analisi"):
            return  # fermato nel frattempo
        self.fase = fase
        self.t_inizio_fase = time.monotonic()
        testi = {
            "spegnimento": "Spengo il master…",
            "silenzio": f"Registro {SILENZIO_S:.0f}s di SILENZIO (rumore ambientale) — non toccare la fibra",
            "selezione": "Porto il master in modalità sweep (cicli della Shelly)…",
            "sweep": "Registro lo sweep…",
        }
        self.label_fase.setText(testi.get(fase, fase))

    def _tick(self):
        if self.sock is None:
            return
        pacchetti = []
        while True:
            try:
                dati = self.sock.recv(4096)
            except (BlockingIOError, OSError):
                break
            if len(dati) == CAMPIONI_PER_PACCHETTO * 2:
                pacchetti.append(np.frombuffer(dati, dtype=np.int16))
        ora = time.monotonic()
        if pacchetti:
            nuovi = np.concatenate(pacchetti)
            self.coda_live = np.concatenate((self.coda_live, nuovi))[-(SAMPLE_RATE // 2):]
            if self.fase == "silenzio":
                self.buf_silenzio.append(nuovi)
            elif self.fase == "sweep":
                self._accoda_sweep(nuovi, ora)
            self._aggiorna_live()

        if self.fase == "silenzio":
            self.barra.setValue(int(1000 * min(1.0, (ora - self.t_inizio_fase) / SILENZIO_S)))
        elif self.fase == "sweep":
            durata = self.spin_cicli.value() * DURATA_CICLO_S + 2 * SWEEP_STEP_S
            trascorso = ora - self.t_inizio_fase
            self.barra.setValue(int(1000 * min(1.0, trascorso / durata)))
            self.label_fase.setText(f"Registro lo sweep… {trascorso:.0f}/{durata:.0f}s "
                                    f"(ciclo {min(int(trascorso // DURATA_CICLO_S) + 1, self.spin_cicli.value())}"
                                    f"/{self.spin_cicli.value()})")
            if trascorso >= durata:
                self._ferma()

    def _accoda_sweep(self, nuovi: np.ndarray, ora: float):
        attesi = (ora - self.t_inizio_fase) * SAMPLE_RATE
        buco = int(attesi - self.campioni_sweep - len(nuovi))
        if self.campioni_sweep > 0 and buco > BUCO_MINIMO_S * SAMPLE_RATE:
            self.buf_sweep.append(np.zeros(buco, dtype=np.int16))
            self.buf_valido.append(np.zeros(buco, dtype=bool))
            self.campioni_sweep += buco
        self.buf_sweep.append(nuovi)
        self.buf_valido.append(np.ones(len(nuovi), dtype=bool))
        self.campioni_sweep += len(nuovi)

    def _aggiorna_live(self):
        x = self.coda_live.astype(np.float64)
        spettro = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2 + 1e-12
        freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
        maschera = freqs <= 6500
        self.curva_live.setData(freqs[maschera], spettro[maschera])
        self.label_picco.setText(f"picco {freqs[maschera][np.argmax(spettro[maschera][5:]) + 5]:.0f} Hz")

    def _ferma(self):
        if self.fase in ("inattivo", "analisi"):
            return
        self._annulla.set()
        fase_prima = self.fase
        self.fase = "analisi"
        self.btn_ferma.setEnabled(False)
        host = self.campo_ip.text().strip()
        threading.Thread(target=self._spegni_silenzioso, args=(host,), daemon=True).start()
        if fase_prima != "sweep" or not self.buf_sweep:
            self._fine("Fermato prima dello sweep: niente da analizzare.")
            return
        sweep = np.concatenate(self.buf_sweep)
        valido = np.concatenate(self.buf_valido)
        silenzio = np.concatenate(self.buf_silenzio) if self.buf_silenzio else np.zeros(0, dtype=np.int16)
        nota = self.campo_nota.text().strip()
        base = cartella_taratura() / time.strftime("frequenze_%H%M%S")
        np.savez_compressed(base.with_suffix(".npz"), sweep=sweep, valido=valido, silenzio=silenzio,
                            sample_rate=SAMPLE_RATE, nota=nota)
        self._lancia_analisi(sweep, valido, silenzio, base, nota)

    @staticmethod
    def _spegni_silenzioso(host: str):
        try:
            spegni_master(host)
        except Exception:
            pass

    def _apri_registrazione(self):
        percorso, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Apri registrazione", str(CARTELLA_ARCHIVIO), "Registrazioni (*.npz)")
        if not percorso:
            return
        dati = np.load(percorso)
        self.fase = "analisi"
        self.btn_avvia.setEnabled(False)
        self.btn_apri.setEnabled(False)
        base = Path(percorso).with_suffix("")
        nota = str(dati["nota"]) if "nota" in dati.files else ""
        self._lancia_analisi(dati["sweep"], dati["valido"], dati["silenzio"], base, nota)

    def _lancia_analisi(self, sweep, valido, silenzio, base: Path, nota: str = ""):
        self.label_fase.setText("Analisi in corso…")
        self.barra.setValue(1000)

        def lavoro():
            try:
                risultato = analizza(sweep, valido, silenzio)
                testo = f"Nota: {nota or '(nessuna)'}\n\n" + componi_report(risultato)
                base.with_suffix(".txt").write_text(testo, encoding="utf-8")
                risultato["report"] = testo
                risultato["file"] = str(base.with_suffix(".txt"))
                self.segnali.risultato.emit(risultato)
            except AnalisiErrore as e:
                self.segnali.errore.emit(str(e))
            except Exception as e:
                self.segnali.errore.emit(f"Errore inatteso nell'analisi: {e!r}")

        threading.Thread(target=lavoro, daemon=True).start()

    # --- Risultati ----------------------------------------------------
    def _mostra_risultato(self, r: dict):
        self.ultimo_risultato = r
        self.report.setPlainText(r["report"])
        maschera = r["viste"] > 0
        self.curva_segnale.setData(FREQUENZE[maschera], np.maximum(r["segnale_prod"][maschera], 1e-12))
        self.curva_rumore.setData(FREQUENZE, r["rumore"])
        coppie = r["coppie"][:20] if r["affidabile"] else []
        self.tabella.setRowCount(len(coppie))
        for riga, c in enumerate(coppie):
            nota = f"f2 = {c['armonica']}×f1" if c["armonica"] else ""
            valori = (str(c["f1"]), str(c["f2"]), f"{c['margine_db']:.1f}", c["verdetto"], nota)
            for col, v in enumerate(valori):
                self.tabella.setItem(riga, col, QtWidgets.QTableWidgetItem(v))
        if coppie:
            self.tabella.selectRow(0)
        stato = "AFFIDABILE" if r["affidabile"] else "NON AFFIDABILE — vedi report"
        self._fine(f"Analisi completata: misura {stato}. Report salvato in {r['file']}")

    def _evidenzia_coppia(self):
        righe = self.tabella.selectionModel().selectedRows()
        if not righe or self.ultimo_risultato is None:
            return
        c = self.ultimo_risultato["coppie"][righe[0].row()]
        self.linea_f1.setValue(c["f1"])
        self.linea_f2.setValue(c["f2"])
        self.linea_f1.setVisible(True)
        self.linea_f2.setVisible(True)

    def _errore(self, messaggio: str):
        self._annulla.set()
        if self.fase not in ("inattivo", "analisi"):
            threading.Thread(target=self._spegni_silenzioso, args=(self.campo_ip.text().strip(),), daemon=True).start()
        self._fine("ERRORE: " + messaggio)

    def _fine(self, testo: str):
        self.fase = "inattivo"
        self.label_fase.setText(testo)
        self.btn_avvia.setEnabled(True)
        self.btn_apri.setEnabled(True)
        self.btn_ferma.setEnabled(False)

    def closeEvent(self, evento):
        if self.fase not in ("inattivo",):
            self._spegni_silenzioso(self.campo_ip.text().strip())
        self._annulla.set()
        self._chiudi_socket()
        super().closeEvent(evento)


def main() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    applica_palette(app)
    finestra = FinestraTrovaFrequenze()
    finestra.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
