"""
main.py
-------
Interrogatore + visualizzatore + rilevatore protocollo f1/f2, in un unico
file/processo. Unico entry point del progetto (vedi SPECS.MD §1).

Riprende da MLtoDL/0registratoreStalta.py solo i pattern riusabili:
- NetworkWorker per la ricezione UDP grezza (identico, senza scrittura WAV:
  qui non c'è registrazione, vedi SPECS.MD §0).
- il pannello "pesante" attivabile/disattivabile a runtime (spettrogramma),
  dove il flag non nasconde solo il grafico ma ferma anche il timer/calcolo
  FFT reale (stesso principio di widget_analisi_pesante in 0registratoreStalta.py).
- lo schema a soglie con isteresi (ON/OFF) regolabili da spinbox a runtime,
  qui applicato alla potenza Goertzel invece che al rapporto STA/LTA.

Non riprende invece nulla della logica di dataset/etichettatura/training/
classificazione: quella pipeline è considerata chiusa (SPECS.MD §0).

Il metodo gestisci_dati() è il punto in cui è agganciato RilevatoreProtocolloF1F2,
che implementa il metodo di riconoscimento descritto in SPECS.MD §3 (doppio
filtro di Goertzel su f1/f2, isteresi, macchina a stati sulla durata,
contatore, sovrapposizione f1/f2 per lo stato dell'interruttore).

NOTA SU FREQUENZE E SOGLIE (aggiornato dopo taratura sul canale reale):
il canale reale (motorino/vibrazione -> terreno -> fibra -> interferometro)
consegna segnali molto più deboli di quanto ci si aspetterebbe da un tono a
piena scala: con l'hardware attuale, f1 (600Hz) arriva tipicamente intorno
a 0.0002-0.0003 di potenza normalizzata, f2 (1500Hz) intorno a 0.0014-0.0022,
e il rumore di fondo è sotto 0.00005 (arrotonda a 0 con 4 decimali). I
default sotto sono stati aggiornati di conseguenza; restano comunque valori
di partenza da rifinire sul campo, non costanti definitive.

NOTA DESIGN (v2): pannello destro riorganizzato in card scure coerenti con
i grafici (era pannello chiaro Fusion di default, a contrasto con i plot
neri). Il log "Interruttori chiusi" non è più un QTextEdit monospace ma una
QListWidget con una riga-card per evento (badge numero, testo, orario),
più recente in alto, con tetto MAX_RIGHE_LOG per non crescere all'infinito.
"""

import sys
import socket
import logging
import os
import time
import json
import numpy as np
from dataclasses import dataclass
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

import config_condivisa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rilevatore_f1_f2")

# --- CONFIGURAZIONE FISSA: interrogatore (invariata rispetto a main.py) ---
UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000
SECONDI_VISIBILI = 15
STORICO_CAMPIONI = SAMPLE_RATE * SECONDI_VISIBILI

INTERVALLO_ATTESO_PACCHETTO = CAMPIONI_PER_PACCHETTO / SAMPLE_RATE
# Il dispositivo bufferizza e invia a raffiche di ~16 pacchetti quasi
# simultanei, con pausa strutturale di ~169ms tra raffiche. Comportamento
# del dispositivo, non modificabile (vedi CLAUDE.md / main.py).
PERIODO_RAFFICA_TIPICO_SEC = 0.169
SOGLIA_GAP_ANOMALO_SEC = PERIODO_RAFFICA_TIPICO_SEC * 1.8
FRAZIONE_MINIMA_PACCHETTI_ATTESI = 0.90

FPS_GRAFICO = 20
INTERVALLO_TIMER_MS = int(1000 / FPS_GRAFICO)

# --- Spettrogramma: opzionale e disattivato di default (SPECS.MD §0/§1) ---
NPERSEG_SPETTROGRAMMA = SAMPLE_RATE
MAX_FREQ_VISUALIZZATA = 6000
HOP_SPETTROGRAMMA_CAMPIONI = 2400
N_COLONNE_SPETTROGRAMMA = STORICO_CAMPIONI // HOP_SPETTROGRAMMA_CAMPIONI
DINAMICA_DB_MIN = 10.0
DINAMICA_DB_MAX = 150.0
DINAMICA_DB_DEFAULT = 20.0
VALORE_INIZIALE_DB = -300.0
FPS_SPETTROGRAMMA = int(round(SAMPLE_RATE / HOP_SPETTROGRAMMA_CAMPIONI))
INTERVALLO_TIMER_SPETTRO_MS = int(1000 / FPS_SPETTROGRAMMA)

# --- Protocollo f1/f2 (SPECS.MD §2/§3): valori da tarare, aggiornati sul
# canale reale (vedi nota in cima al file) ---
# F1_HZ/F2_HZ vengono da config_condivisa.py, unica fonte di verità lato
# Python: cambiale lì, non qui. Lato Arduino non è un vero #include condi-
# viso (il toolchain Arduino non segue percorsi relativi fuori dalla
# cartella dello sketch): trasmettitore.ino ha le sue costanti FREQ1/FREQ2
# come valori letterali, tenute allineate da aggiorna_config_ino.py (lancialo
# dopo aver cambiato questo file, poi riflasha).
# Scelte con lo sweep a gradini di TEST ARMONICHE/ (vedi SPECS.MD §5.1/§6.2):
# tra le frequenze testate (200-6000Hz, oltre i 5400Hz il sistema smette di
# rispondere) sono le due con segnale più forte, meno armoniche proprie
# (rapporto spurie/fondamentale ~1.0-1.1) e zero energia spuria misurata
# dell'una vicino alla frequenza dell'altra, in nessuna direzione.
F1_HZ_DEFAULT = config_condivisa.F1_HZ
F2_HZ_DEFAULT = config_condivisa.F2_HZ
T0_MS_DEFAULT = config_condivisa.T0_MS      # marcatore di zero
T1_MS_DEFAULT = config_condivisa.T1_MS      # durata di uno slot
# Tolleranza stretta a 10ms (era 50ms quando t1 era 100/40ms): con t1=20ms
# uguale esattamente a un blocco Goertzel (BLOCCO_GOERTZEL_MS=20 sotto), una
# tolleranza di 50ms accetterebbe come "slot valido" anche una durata di 40
# o 60ms (2-3 blocchi, il doppio/triplo del previsto) - cioè non farebbe
# più da filtro anti-rumore, quasi ogni impulso più lungo del previsto
# passerebbe comunque come slot invece di essere scartato come rumore. Con
# tolleranza=10ms la fascia "slot" copre solo [10,30]ms: dato che le durate
# misurate sono multipli di 20ms, l'unico valore che ci cade dentro è
# esattamente 20ms - un vero controllo, non un pass-through.
TOLLERANZA_MS_DEFAULT = config_condivisa.TOLLERANZA_MS
# Blocco Goertzel: 20ms -> t1=20ms è ESATTAMENTE un blocco (il minimo
# teorico rappresentabile), t0=200ms sono 10 blocchi. Con tolleranza=10ms
# la fascia "slot" copre [10,30]ms (solo 20ms quantizzato ci cade dentro) e
# quella "marcatore zero" [190,210]ms (solo 200ms): separate, nessuna
# sovrapposizione. Girare a questa scala è il limite teorico del sistema:
# verificare con TEST TEMPI/ (stessa frequenza/soglie) che il canale regga
# davvero un ON così corto e un gap di 50ms prima di fidarsene sul campo -
# se in pratica capitano falsi "slot"/"zero" da rumore prolungato, la
# tolleranza è già al minimo sensato, il problema è a monte (soglie o
# durata stessa, non più questo parametro).
BLOCCO_GOERTZEL_MS = 20.0
BLOCCO_GOERTZEL_CAMPIONI = max(1, round(SAMPLE_RATE * BLOCCO_GOERTZEL_MS / 1000.0))
# Soglie sulla potenza Goertzel normalizzata (0..1 = piena scala int16).
# Frequenze cambiate a 4600/3200Hz (vedi F1_HZ_DEFAULT/F2_HZ_DEFAULT sopra),
# scelte proprio per non avere più il cross-talk pesante che c'era con la
# vecchia coppia 2500/1000Hz. Soglie derivate dai dati dello sweep a gradini
# di TEST ARMONICHE/ (non da un nuovo test a 3 punti dedicato - lo sweep dà
# già l'equivalente di "solo f1"/"solo f2" a queste frequenze esatte):
# forza segnale f1(4600Hz)=0.0365, f2(3200Hz)=0.0278, e nessuna spuria
# rilevata dell'una vicino all'altra (sotto l'1% della propria fondamentale,
# quindi pavimento cross-talk sotto ~0.0003-0.0004 per entrambe, contro un
# rumore puro di fondo storicamente ~0.000004 - margine ampio su entrambi i
# fronti). ON messa al 20% del segnale, OFF al 65% della ON, come da
# SPECS.MD §5.1. Da confermare con "Azzera picchi" al primo giro live prima
# di fidarsene sul campo - questi numeri vengono da un banco di sweep a tono
# singolo, non da una misura diretta col protocollo f1/f2 completo attivo.
SOGLIA_ON_F1_DEFAULT = 0.0070
SOGLIA_OFF_F1_DEFAULT = 0.0045
SOGLIA_ON_F2_DEFAULT = 0.0055
SOGLIA_OFF_F2_DEFAULT = 0.0036

FILE_LOG_SLOT = "eventi_f1_f2_log.jsonl"
MAX_RIGHE_LOG = 300

# --- Tema (v2): un solo stylesheet scuro per l'intera finestra, coerente
# con lo sfondo nero dei grafici pyqtgraph (prima il pannello destro era
# grigio chiaro di default, a contrasto). ---
STYLE_QSS = """
QMainWindow, QWidget { background-color: #1b1d1f; color: #d8dadd; font-family: 'Segoe UI', sans-serif; font-size: 12px; }
QLabel { color: #d8dadd; }
QPushButton { background-color: #2a2d31; border: 1px solid #3a3d42; border-radius: 6px; padding: 8px; color: #d8dadd; }
QPushButton:hover { background-color: #34383d; }
QPushButton:checked { background-color: #00d2c4; color: #10131a; font-weight: 600; }
QDoubleSpinBox, QSpinBox { background-color: #24262a; border: 1px solid #3a3d42; border-radius: 4px; padding: 4px; color: #d8dadd; }
QCheckBox { color: #d8dadd; }
QSlider::groove:horizontal { background: #3a3d42; height: 4px; border-radius: 2px; }
QSlider::handle:horizontal { background: #00d2c4; width: 12px; margin: -5px 0; border-radius: 6px; }
QListWidget { background-color: #16181a; border: 1px solid #2a2d31; border-radius: 8px; padding: 6px; }
QListWidget::item { border: none; }
QScrollBar:vertical { background: #1b1d1f; width: 10px; }
QScrollBar::handle:vertical { background: #3a3d42; border-radius: 5px; }
"""

# Stile card riusato per i blocchi di stato (potenza f1/f2, counter, verifica).
# Padding ridotto (era 12px): con log/parametri/counter tutti impilati nella
# stessa colonna, ogni px di padding tolto qui è un px in più per la lista
# log sotto, che è quella letta più spesso.
STILE_CARD = (
    "background-color: #212326; border: 1px solid #2e3134; border-radius: 8px; padding: 8px 10px;"
)


class SpinBoxSenzaRotella(QtWidgets.QDoubleSpinBox):
    """QDoubleSpinBox che ignora la rotella del mouse. Di default Qt cambia
    il valore anche solo passandoci sopra con la rotella mentre si scorre il
    pannello (causa reale di frequenze/soglie cambiate per sbaglio) — non
    serve nemmeno il focus/click. event.ignore() fa risalire lo scroll al
    genitore (la QScrollArea del pannello destro), che scorre la pagina
    invece di alterare il valore."""

    def wheelEvent(self, event):
        event.ignore()


class SpinBoxIntSenzaRotella(QtWidgets.QSpinBox):
    """Come SpinBoxSenzaRotella ma per QSpinBox (valori interi)."""

    def wheelEvent(self, event):
        event.ignore()


class SliderSenzaRotella(QtWidgets.QSlider):
    """Come SpinBoxSenzaRotella ma per QSlider (stesso problema di scroll
    accidentale)."""

    def wheelEvent(self, event):
        event.ignore()


@dataclass
class StatisticheRete:
    pacchetti_ricevuti: int = 0
    pacchetti_scartati: int = 0
    anomalie_raffica: int = 0
    pacchetti_ultimo_secondo: int = 0
    secondi_con_possibile_perdita: int = 0


class NetworkWorker(QtCore.QThread):
    """Interrogatore: riceve i pacchetti UDP grezzi e li inoltra via segnale
    Qt. Nessuna logica di rilevamento qui, nessuna scrittura su disco
    (SPECS.MD §0: nessuna registrazione in questa fase)."""

    nuovi_dati_signal = QtCore.Signal(np.ndarray)

    def __init__(self, ip, port, packet_size):
        super().__init__()
        self.ip = ip
        self.port = port
        self.packet_size = packet_size
        self.running = True
        self.stats = StatisticheRete()
        self._contatore_locale_secondo = 0
        self._ultimo_reset_stats = QtCore.QElapsedTimer()
        self._ultimo_reset_stats.start()
        self._ultimo_arrivo_ns = None

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        try:
            sock.bind((self.ip, self.port))
        except OSError as e:
            log.error(f"Impossibile aprire la porta UDP {self.port}: {e}")
            self.running = False
            return

        sock.settimeout(0.5)
        bytes_attesi = self.packet_size * 2
        timer = QtCore.QElapsedTimer()
        timer.start()

        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as e:
                log.error(f"Errore socket: {e}")
                break

            if len(data) != bytes_attesi:
                self.stats.pacchetti_scartati += 1
                continue

            self._aggiorna_gap(timer.nsecsElapsed())

            nuovi_campioni = np.frombuffer(data, dtype=np.int16)
            self.nuovi_dati_signal.emit(nuovi_campioni)

            self.stats.pacchetti_ricevuti += 1
            self._contatore_locale_secondo += 1
            self._aggiorna_rate()

        sock.close()

    def _aggiorna_gap(self, ora_ns):
        if self._ultimo_arrivo_ns is not None:
            delta = (ora_ns - self._ultimo_arrivo_ns) / 1e9
            if delta > SOGLIA_GAP_ANOMALO_SEC:
                self.stats.anomalie_raffica += 1
        self._ultimo_arrivo_ns = ora_ns

    def _aggiorna_rate(self):
        if self._ultimo_reset_stats.elapsed() >= 1000:
            self.stats.pacchetti_ultimo_secondo = self._contatore_locale_secondo
            attesi = SAMPLE_RATE / CAMPIONI_PER_PACCHETTO
            if self._contatore_locale_secondo < attesi * FRAZIONE_MINIMA_PACCHETTI_ATTESI:
                self.stats.secondi_con_possibile_perdita += 1
            self._contatore_locale_secondo = 0
            self._ultimo_reset_stats.restart()


class RilevatoreGoertzel:
    """Filtro di Goertzel su una singola frequenza nota: potenza normalizzata
    (0..1 = tono a piena scala int16), indipendente dalla dimensione del
    blocco. Più economico di una FFT completa quando serve solo decidere
    presenza/assenza di un tono noto (SPECS.MD §3)."""

    def __init__(self, frequenza_hz, sample_rate, dimensione_blocco):
        self.frequenza_hz = frequenza_hz
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


class MacchinaStatoFrequenza:
    """ON/OFF a doppia soglia (isteresi) sulla potenza Goertzel normalizzata,
    per evitare sfarfallio sul confine di soglia (SPECS.MD §3 punto 3).
    Misura anche la durata continuativa dello stato ON, restituita quando
    torna OFF perché il chiamante la classifichi (marcatore/slot/rumore)."""

    def __init__(self, soglia_on, soglia_off, durata_blocco_sec):
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off
        self.durata_blocco_sec = durata_blocco_sec
        self.attivo = False
        self.durata_on_sec = 0.0

    def imposta_soglie(self, soglia_on, soglia_off):
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off

    def aggiorna(self, potenza):
        """Ritorna (transizione_a_on, durata_completata_o_None)."""
        transizione_on = False
        durata_completata = None
        if not self.attivo:
            if potenza > self.soglia_on:
                self.attivo = True
                self.durata_on_sec = self.durata_blocco_sec
                transizione_on = True
        else:
            if potenza > self.soglia_off:
                self.durata_on_sec += self.durata_blocco_sec
            else:
                self.attivo = False
                durata_completata = self.durata_on_sec
                self.durata_on_sec = 0.0
        return transizione_on, durata_completata


class RilevatoreProtocolloF1F2:
    """Logica del protocollo f1 (clock/counter) / f2 (stato interruttore)
    descritta in SPECS.MD §2-3, indipendente dalla UI:

    - f1 misura la durata di ogni impulso e la classifica come marcatore di
      zero (~t0), slot valido (~t1, incrementa il contatore) o rumore/glitch
      (scartato).
    - f2 viene osservata in sovrapposizione: se è ON durante uno slot f1
      valido, quello slot è "chiuso", altrimenti "aperto".
    """

    def __init__(self, sample_rate, blocco_campioni,
                 f1_hz, f2_hz, t0_ms, t1_ms, tolleranza_ms,
                 soglia_on_f1, soglia_off_f1, soglia_on_f2, soglia_off_f2):
        durata_blocco_sec = blocco_campioni / sample_rate
        self.goertzel_f1 = RilevatoreGoertzel(f1_hz, sample_rate, blocco_campioni)
        self.goertzel_f2 = RilevatoreGoertzel(f2_hz, sample_rate, blocco_campioni)
        self.macchina_f1 = MacchinaStatoFrequenza(soglia_on_f1, soglia_off_f1, durata_blocco_sec)
        self.macchina_f2 = MacchinaStatoFrequenza(soglia_on_f2, soglia_off_f2, durata_blocco_sec)
        self.t0_sec = t0_ms / 1000.0
        self.t1_sec = t1_ms / 1000.0
        self.tolleranza_sec = tolleranza_ms / 1000.0
        self.counter = 0
        self.f2_attivo_durante_slot = False
        self.ultima_potenza_f1 = 0.0
        self.ultima_potenza_f2 = 0.0
        # False finché non è arrivato il primo marcatore di zero: serve a non
        # segnalare un falso "conteggio 0" alla partenza (vedi elabora_blocco).
        self._marcatore_zero_visto = False

    def imposta_soglie(self, soglia_on_f1, soglia_off_f1, soglia_on_f2, soglia_off_f2):
        self.macchina_f1.imposta_soglie(soglia_on_f1, soglia_off_f1)
        self.macchina_f2.imposta_soglie(soglia_on_f2, soglia_off_f2)

    def _classifica_durata_f1(self, durata_sec):
        if abs(durata_sec - self.t0_sec) <= self.tolleranza_sec:
            return "zero"
        if abs(durata_sec - self.t1_sec) <= self.tolleranza_sec:
            return "slot"
        return "rumore"

    def elabora_blocco(self, blocco):
        """Processa un blocco di BLOCCO_GOERTZEL_CAMPIONI campioni. Ritorna
        la lista di eventi generati (marcatori di zero e slot validi)."""
        self.ultima_potenza_f1 = self.goertzel_f1.potenza(blocco)
        self.ultima_potenza_f2 = self.goertzel_f2.potenza(blocco)

        eventi = []

        on_f1, durata_f1 = self.macchina_f1.aggiorna(self.ultima_potenza_f1)
        if on_f1:
            # Nuovo impulso f1: azzera l'overlap f2, verrà ricalcolato per
            # tutta la durata di questo impulso.
            self.f2_attivo_durante_slot = False

        self.macchina_f2.aggiorna(self.ultima_potenza_f2)
        if self.macchina_f1.attivo and self.macchina_f2.attivo:
            self.f2_attivo_durante_slot = True

        if durata_f1 is not None:
            tipo = self._classifica_durata_f1(durata_f1)
            if tipo == "zero":
                # Conteggio del ciclo appena concluso, catturato PRIMA di
                # azzerare il counter: None al primo marcatore dopo
                # l'avvio/riavvio (nessun ciclo completo ancora osservato).
                conteggio_ciclo_precedente = self.counter if self._marcatore_zero_visto else None
                self._marcatore_zero_visto = True
                self.counter = 0
                eventi.append({
                    "tipo": "zero",
                    "durata_s": durata_f1,
                    "conteggio_ciclo_precedente": conteggio_ciclo_precedente,
                })
            elif tipo == "slot":
                self.counter += 1
                stato = "chiuso" if self.f2_attivo_durante_slot else "aperto"
                eventi.append({
                    "tipo": "slot",
                    "counter": self.counter,
                    "stato": stato,
                    "durata_s": durata_f1,
                })
            # "rumore": scartato senza generare eventi (SPECS.MD §3 punto 4).

        return eventi


class RilevatoreF1F2(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        pg.setConfigOptions(antialias=False)

        self.buffer_audio = np.zeros(STORICO_CAMPIONI, dtype=np.int16)
        self.write_pos = 0
        self.asse_tempo = np.linspace(-SECONDI_VISIBILI, 0, STORICO_CAMPIONI, dtype=np.float32)

        self.finestra_hann = np.hanning(NPERSEG_SPETTROGRAMMA).astype(np.float32)
        self.bin_freq_max = min(MAX_FREQ_VISUALIZZATA + 1, NPERSEG_SPETTROGRAMMA // 2 + 1)
        self.spettro_immagine = np.full(
            (self.bin_freq_max, N_COLONNE_SPETTROGRAMMA), VALORE_INIZIALE_DB, dtype=np.float32
        )
        self.col_scrittura_spettro = 0
        self.campioni_da_ultimo_hop = 0
        self.campioni_totali_ricevuti = 0
        self.dinamica_db = DINAMICA_DB_DEFAULT
        self.spettrogramma_attivo = False
        self.totale_chiusure = 0
        self.picco_f1 = 0.0
        self.picco_f2 = 0.0
        # Verifica ripetibilità (SOLO per test col ciclo Arduino fisso, vedi
        # _verifica_ripetibilita_ciclo): stato_ciclo_corrente accumula gli
        # esiti slot->stato del ciclo in corso, ciclo_riferimento è il primo
        # ciclo completo osservato dopo l'avvio (o dopo un reset manuale).
        self.stato_ciclo_corrente = {}
        self.ciclo_riferimento = None

        self._accumulo_goertzel = np.empty(0, dtype=np.int16)
        self.protocollo = RilevatoreProtocolloF1F2(
            SAMPLE_RATE, BLOCCO_GOERTZEL_CAMPIONI,
            F1_HZ_DEFAULT, F2_HZ_DEFAULT, T0_MS_DEFAULT, T1_MS_DEFAULT, TOLLERANZA_MS_DEFAULT,
            SOGLIA_ON_F1_DEFAULT, SOGLIA_OFF_F1_DEFAULT, SOGLIA_ON_F2_DEFAULT, SOGLIA_OFF_F2_DEFAULT,
        )
        self.path_log_slot = os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_LOG_SLOT)

        self.setWindowTitle("Rilevatore f1/f2 — DAS (SPECS.MD)")
        self.resize(1300, 850)

        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        layout_principale = QtWidgets.QHBoxLayout(central_widget)
        layout_principale.setContentsMargins(12, 12, 12, 12)
        layout_principale.setSpacing(14)

        # ------------------------------------------------------------
        # Colonna sinistra: forma d'onda + spettrogramma opzionale
        # ------------------------------------------------------------
        colonna_sinistra = QtWidgets.QVBoxLayout()
        colonna_sinistra.setSpacing(10)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#111111")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.setLabel("bottom", "Tempo", units="s")
        self.plot_widget.setLabel("left", "Ampiezza")
        self.plot_widget.setTitle("Segnale Audio (Tempo reale)")

        self.data_line = self.plot_widget.plot(
            self.asse_tempo, self.buffer_audio, pen=pg.mkPen(color="#00d2c4", width=1.5)
        )
        try:
            self.data_line.setDownsampling(auto=True, method="peak")
        except TypeError:
            try:
                self.data_line.setDownsampling(auto=True, mode="peak")
            except TypeError:
                self.data_line.setDownsampling(auto=True)
        self.data_line.setClipToView(True)
        try:
            self.data_line.setSkipFiniteCheck(True)
        except AttributeError:
            pass

        self.limite_y = 20000.0
        self.plot_widget.setYRange(-self.limite_y, self.limite_y, padding=0)
        self.plot_widget.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        colonna_sinistra.addWidget(self.plot_widget, stretch=3)

        # Contenitore spettrogramma: nascosto E fermo di default. Il flag
        # spettrogramma_attivo decide se il calcolo FFT viene proprio fatto
        # in gestisci_dati, non solo se il grafico è visibile (stesso
        # principio di widget_analisi_pesante in 0registratoreStalta.py).
        self.widget_spettro = QtWidgets.QWidget()
        layout_spettro = QtWidgets.QVBoxLayout(self.widget_spettro)
        layout_spettro.setContentsMargins(0, 0, 0, 0)

        self.plot_spettro = pg.PlotWidget()
        self.plot_spettro.setBackground("#111111")
        self.plot_spettro.setLabel("bottom", "Tempo", units="s")
        self.plot_spettro.setLabel("left", "Frequenza", units="Hz")
        self.plot_spettro.setTitle(f"Spettrogramma (Risoluzione {SAMPLE_RATE // NPERSEG_SPETTROGRAMMA or 1}Hz)")
        self.plot_spettro.setYRange(0, MAX_FREQ_VISUALIZZATA, padding=0)
        self.plot_spettro.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        self.plot_spettro.setXLink(self.plot_widget)

        self.rect_spettro = QtCore.QRectF(-SECONDI_VISIBILI, 0, SECONDI_VISIBILI, MAX_FREQ_VISUALIZZATA)
        self.img_spettro = pg.ImageItem()
        self.img_spettro.setRect(self.rect_spettro)
        self.plot_spettro.addItem(self.img_spettro)

        try:
            cmap = pg.colormap.get("viridis")
        except Exception:
            cmap = pg.ColorMap(
                pos=[0.0, 0.25, 0.5, 0.75, 1.0],
                color=[
                    (68, 1, 84, 255),
                    (59, 82, 139, 255),
                    (33, 145, 140, 255),
                    (94, 201, 98, 255),
                    (253, 231, 37, 255),
                ],
            )
        self.img_spettro.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        layout_spettro.addWidget(self.plot_spettro)

        riga_soglia = QtWidgets.QHBoxLayout()
        riga_soglia.addWidget(QtWidgets.QLabel("Dinamica Spettrogramma (dB sotto il picco):"))
        self.slider_soglia_spettro = SliderSenzaRotella(QtCore.Qt.Horizontal)
        self.slider_soglia_spettro.setRange(int(DINAMICA_DB_MIN * 10), int(DINAMICA_DB_MAX * 10))
        self.slider_soglia_spettro.setValue(int(DINAMICA_DB_DEFAULT * 10))
        self.slider_soglia_spettro.valueChanged.connect(self.cambia_dinamica_spettrogramma)
        riga_soglia.addWidget(self.slider_soglia_spettro)
        layout_spettro.addLayout(riga_soglia)

        self.widget_spettro.setVisible(False)
        colonna_sinistra.addWidget(self.widget_spettro, stretch=2)

        # Bottone (non più checkbox) per renderlo più evidente: stessa
        # evidenza teal a piena larghezza usata per "Parametri avanzati".
        self.checkbox_spettro = QtWidgets.QPushButton(
            "📊  Spettrogramma (pesante — FFT 24000 punti, disattivato di default)"
        )
        self.checkbox_spettro.setCheckable(True)
        self.checkbox_spettro.setMinimumHeight(36)
        self.checkbox_spettro.toggled.connect(self.cambia_spettrogramma_attivo)
        colonna_sinistra.addWidget(self.checkbox_spettro)

        self.label_rete = QtWidgets.QLabel("Rete: in attesa di pacchetti...")
        self.label_rete.setStyleSheet("color: #7d8590; font-family: monospace; font-size: 11px;")
        colonna_sinistra.addWidget(self.label_rete)

        layout_principale.addLayout(colonna_sinistra, stretch=3)

        # ------------------------------------------------------------
        # Colonna destra: stato protocollo f1/f2 + parametri + log
        # Dentro una QScrollArea: quando si apre il pannello "Parametri
        # avanzati" il contenuto può superare l'altezza della finestra (la
        # finestra non si ridimensiona da sola) — senza scroll, il bottone
        # "Applica parametri protocollo" finiva fuori dall'area visibile e
        # irraggiungibile.
        # ------------------------------------------------------------
        contenitore_destra = QtWidgets.QWidget()
        colonna_destra = QtWidgets.QVBoxLayout(contenitore_destra)
        colonna_destra.setSpacing(8)

        titolo = QtWidgets.QLabel("RILEVATORE PROTOCOLLO f1 / f2")
        titolo.setStyleSheet("font-weight: 700; font-size: 14px; color: #00d2c4; letter-spacing: 0.5px;")
        colonna_destra.addWidget(titolo)

        # --- Card di stato: potenza f1/f2 + counter, raggruppate in un unico
        # riquadro scuro (prima erano tre label isolate senza contenitore).
        card_stato = QtWidgets.QWidget()
        card_stato.setStyleSheet(STILE_CARD)
        layout_card_stato = QtWidgets.QVBoxLayout(card_stato)
        layout_card_stato.setSpacing(4)

        # Potenza e picco (max-hold) sulla stessa riga, frequenza accanto
        # alla sua potenza: il picco serve a tarare le soglie a occhio (es.
        # lascia tutti gli interruttori aperti, azzera, aspetta qualche
        # secondo, leggi il massimo di rumore raggiunto su f2 e metti la
        # soglia ON un po' sopra). Azzerabile a mano (bottoncino ⟲ sulla riga
        # del counter, sotto) e in automatico quando cambiano i parametri del
        # protocollo (un picco preso alla frequenza vecchia non ha senso).
        riga_f1 = QtWidgets.QHBoxLayout()
        riga_f1.setSpacing(8)
        self.label_potenza_f1 = QtWidgets.QLabel("Potenza f1: -- (OFF)")
        self.label_potenza_f1.setStyleSheet("color: #ffb02e; font-family: monospace; font-size: 12px; background: none;")
        riga_f1.addWidget(self.label_potenza_f1, stretch=1)
        self.label_picco_f1 = QtWidgets.QLabel("picco --")
        self.label_picco_f1.setStyleSheet("color: #d9a04a; font-family: monospace; font-size: 12px; background: none;")
        riga_f1.addWidget(self.label_picco_f1)
        layout_card_stato.addLayout(riga_f1)

        riga_f2 = QtWidgets.QHBoxLayout()
        riga_f2.setSpacing(8)
        self.label_potenza_f2 = QtWidgets.QLabel("Potenza f2: -- (OFF)")
        self.label_potenza_f2.setStyleSheet("color: #5eb1ff; font-family: monospace; font-size: 12px; background: none;")
        riga_f2.addWidget(self.label_potenza_f2, stretch=1)
        self.label_picco_f2 = QtWidgets.QLabel("picco --")
        self.label_picco_f2.setStyleSheet("color: #5eb1ff; font-family: monospace; font-size: 12px; background: none;")
        riga_f2.addWidget(self.label_picco_f2)
        layout_card_stato.addLayout(riga_f2)

        linea_separatore = QtWidgets.QFrame()
        linea_separatore.setFrameShape(QtWidgets.QFrame.HLine)
        linea_separatore.setStyleSheet("background-color: #2e3134; max-height: 1px; border: none;")
        layout_card_stato.addWidget(linea_separatore)

        riga_counter = QtWidgets.QHBoxLayout()
        self.label_counter = QtWidgets.QLabel("Counter: --")
        self.label_counter.setStyleSheet(
            "color: #00d2c4; font-family: monospace; font-size: 20px; font-weight: 700; background: none;"
        )
        riga_counter.addWidget(self.label_counter, stretch=1)
        self.btn_azzera_picchi = QtWidgets.QPushButton("⟲")
        self.btn_azzera_picchi.setToolTip("Azzera i picchi f1/f2 (per tarare le soglie)")
        self.btn_azzera_picchi.setFixedSize(44, 44)
        self.btn_azzera_picchi.setStyleSheet("font-size: 20px;")
        self.btn_azzera_picchi.clicked.connect(self.azzera_picchi_potenza)
        riga_counter.addWidget(self.btn_azzera_picchi)
        layout_card_stato.addLayout(riga_counter)

        colonna_destra.addWidget(card_stato)

        # Verifica non bloccante: l'operatore inserisce quanti interruttori
        # si aspetta per ciclo (0 = verifica disattivata); al marcatore di
        # zero successivo confrontiamo il conteggio appena concluso con
        # questo valore e segnaliamo un'eventuale discrepanza (solo un
        # avviso a schermo + log, non blocca né interrompe il rilevamento).
        card_verifica = QtWidgets.QWidget()
        card_verifica.setStyleSheet(STILE_CARD)
        layout_card_verifica = QtWidgets.QVBoxLayout(card_verifica)
        layout_card_verifica.setSpacing(4)

        riga_numero_atteso = QtWidgets.QHBoxLayout()
        label_atteso = QtWidgets.QLabel("Interruttori attesi/ciclo (0=disattiva):")
        label_atteso.setStyleSheet("background: none;")
        riga_numero_atteso.addWidget(label_atteso)
        self.spin_numero_atteso = SpinBoxIntSenzaRotella()
        self.spin_numero_atteso.setRange(0, 1000)
        self.spin_numero_atteso.setValue(50)
        riga_numero_atteso.addWidget(self.spin_numero_atteso)
        layout_card_verifica.addLayout(riga_numero_atteso)

        self.label_verifica_ciclo = QtWidgets.QLabel("Verifica ciclo: --")
        self.label_verifica_ciclo.setAlignment(QtCore.Qt.AlignCenter)
        self.label_verifica_ciclo.setWordWrap(True)
        self.label_verifica_ciclo.setStyleSheet(
            "color: #9aa0a6; background-color: #2a2d31; font-size: 14px; "
            "font-weight: 600; padding: 5px; border-radius: 6px;"
        )
        layout_card_verifica.addWidget(self.label_verifica_ciclo)

        colonna_destra.addWidget(card_verifica)

        # --- Verifica ripetibilità (SOLO per test): nel banco di prova
        # attuale switchState su Arduino è randomizzato una sola volta al
        # boot e non cambia più (vedi trasmettitore.ino), quindi finché non
        # si tocca l'hardware ogni ciclo deve chiudere esattamente gli stessi
        # interruttori del precedente. Il primo ciclo completo osservato
        # diventa il riferimento; i successivi vengono confrontati slot per
        # slot. Da disattivare/ignorare quando si passa a interruttori reali
        # che possono cambiare stato nel tempo.
        card_ripetibilita = QtWidgets.QWidget()
        card_ripetibilita.setStyleSheet(STILE_CARD)
        layout_card_ripetibilita = QtWidgets.QVBoxLayout(card_ripetibilita)
        layout_card_ripetibilita.setSpacing(4)

        riga_titolo_ripetibilita = QtWidgets.QHBoxLayout()
        label_ripetibilita_titolo = QtWidgets.QLabel("Ripetibilità ciclo (solo test):")
        label_ripetibilita_titolo.setStyleSheet("background: none;")
        riga_titolo_ripetibilita.addWidget(label_ripetibilita_titolo, stretch=1)
        self.btn_ricattura_riferimento = QtWidgets.QPushButton("⟲")
        self.btn_ricattura_riferimento.setToolTip(
            "Scarta il riferimento: il prossimo ciclo completo ne diventa uno nuovo"
        )
        self.btn_ricattura_riferimento.setFixedSize(28, 28)
        self.btn_ricattura_riferimento.clicked.connect(self.azzera_riferimento_ripetibilita)
        riga_titolo_ripetibilita.addWidget(self.btn_ricattura_riferimento)
        layout_card_ripetibilita.addLayout(riga_titolo_ripetibilita)

        self.label_ripetibilita = QtWidgets.QLabel("In attesa del primo ciclo completo...")
        self.label_ripetibilita.setAlignment(QtCore.Qt.AlignCenter)
        self.label_ripetibilita.setWordWrap(True)
        self.label_ripetibilita.setStyleSheet(
            "color: #9aa0a6; background-color: #2a2d31; font-size: 13px; "
            "font-weight: 600; padding: 5px; border-radius: 6px;"
        )
        layout_card_ripetibilita.addWidget(self.label_ripetibilita)

        colonna_destra.addWidget(card_ripetibilita)

        # --- Parametri avanzati (frequenze/soglie): raccolti in un pannello
        # nascosto di default. Ora che il rilevamento è tarato e stabile
        # ("il main è perfetto così"), non serve vederli sempre — nasconderli
        # lascia molto più spazio al log delle chiusure, che è quello che va
        # letto più spesso. Riapribile al bisogno con il bottone sotto.
        self.btn_toggle_avanzate = QtWidgets.QPushButton("▸  Parametri avanzati (frequenze/soglie)")
        self.btn_toggle_avanzate.setCheckable(True)
        self.btn_toggle_avanzate.toggled.connect(self.cambia_visibilita_parametri_avanzati)
        colonna_destra.addWidget(self.btn_toggle_avanzate)

        self.widget_parametri_avanzati = QtWidgets.QWidget()
        self.widget_parametri_avanzati.setStyleSheet(STILE_CARD)
        layout_parametri_avanzati = QtWidgets.QVBoxLayout(self.widget_parametri_avanzati)
        layout_parametri_avanzati.setContentsMargins(12, 12, 12, 12)
        layout_parametri_avanzati.setSpacing(8)

        # SPECS.MD §5, "da tarare sperimentalmente, non assunti a priori".
        # Cambiare frequenze o durate ricostruisce i filtri di Goertzel
        # (serve "Applica"); le soglie invece si applicano subito, a runtime.
        label_param_titolo = QtWidgets.QLabel("Parametri protocollo (da tarare):")
        label_param_titolo.setStyleSheet("background: none; color: #9aa0a6; font-size: 11px; font-weight: 600;")
        layout_parametri_avanzati.addWidget(label_param_titolo)

        griglia_parametri = QtWidgets.QGridLayout()
        griglia_parametri.setVerticalSpacing(6)
        self.spin_f1_hz = self._crea_spin(100.0, 6000.0, F1_HZ_DEFAULT, 10.0)
        self.spin_f2_hz = self._crea_spin(100.0, 6000.0, F2_HZ_DEFAULT, 10.0)
        self.spin_t0_ms = self._crea_spin(20.0, 2000.0, T0_MS_DEFAULT, 10.0)
        self.spin_t1_ms = self._crea_spin(20.0, 2000.0, T1_MS_DEFAULT, 10.0)
        self.spin_tolleranza_ms = self._crea_spin(5.0, 500.0, TOLLERANZA_MS_DEFAULT, 5.0)
        griglia_parametri.addWidget(self._label_muta("f1 (Hz)"), 0, 0)
        griglia_parametri.addWidget(self.spin_f1_hz, 0, 1)
        griglia_parametri.addWidget(self._label_muta("f2 (Hz)"), 1, 0)
        griglia_parametri.addWidget(self.spin_f2_hz, 1, 1)
        griglia_parametri.addWidget(self._label_muta("t0 (ms, zero)"), 2, 0)
        griglia_parametri.addWidget(self.spin_t0_ms, 2, 1)
        griglia_parametri.addWidget(self._label_muta("t1 (ms, slot)"), 3, 0)
        griglia_parametri.addWidget(self.spin_t1_ms, 3, 1)
        griglia_parametri.addWidget(self._label_muta("tolleranza (ms)"), 4, 0)
        griglia_parametri.addWidget(self.spin_tolleranza_ms, 4, 1)
        layout_parametri_avanzati.addLayout(griglia_parametri)

        # Questi 5 campi (a differenza delle soglie sotto) non si applicano da
        # soli: serve ricostruire RilevatoreProtocolloF1F2, quindi finché non
        # si preme il bottone il rilevatore continua a girare con i valori
        # vecchi. Il bottone diventa arancione + il messaggio sotto avvisa
        # "modifiche in sospeso" appena si tocca un campo, e torna verde con
        # orario quando si preme "Applica" — prima non c'era nessun segnale
        # visivo di "hai cambiato un numero ma non è ancora attivo".
        for _spin in (self.spin_f1_hz, self.spin_f2_hz, self.spin_t0_ms,
                      self.spin_t1_ms, self.spin_tolleranza_ms):
            _spin.valueChanged.connect(self._segna_modifiche_protocollo_pendenti)

        self.btn_applica_parametri = QtWidgets.QPushButton("✓  Applica parametri protocollo")
        self.btn_applica_parametri.clicked.connect(self.applica_parametri_protocollo)
        layout_parametri_avanzati.addWidget(self.btn_applica_parametri)

        self.label_stato_parametri = QtWidgets.QLabel("")
        self.label_stato_parametri.setWordWrap(True)
        self.label_stato_parametri.setAlignment(QtCore.Qt.AlignCenter)
        layout_parametri_avanzati.addWidget(self.label_stato_parametri)
        self._conferma_parametri_applicati()
        layout_parametri_avanzati.addSpacing(6)

        # Soglie: 6 decimali e passo 0.00005, non più 3 decimali/passo 0.01.
        # Con segnali reali nell'ordine di 0.0001-0.002, la precisione
        # precedente non permetteva proprio di rappresentare le soglie utili.
        label_soglie_titolo = QtWidgets.QLabel("Soglie potenza Goertzel (isteresi, applicate live):")
        label_soglie_titolo.setStyleSheet("background: none; color: #9aa0a6; font-size: 11px; font-weight: 600;")
        layout_parametri_avanzati.addWidget(label_soglie_titolo)
        griglia_soglie = QtWidgets.QGridLayout()
        griglia_soglie.setVerticalSpacing(6)
        self.spin_soglia_on_f1 = self._crea_spin(0.0, 1.0, SOGLIA_ON_F1_DEFAULT, 0.00005, decimali=6)
        self.spin_soglia_off_f1 = self._crea_spin(0.0, 1.0, SOGLIA_OFF_F1_DEFAULT, 0.00005, decimali=6)
        self.spin_soglia_on_f2 = self._crea_spin(0.0, 1.0, SOGLIA_ON_F2_DEFAULT, 0.00005, decimali=6)
        self.spin_soglia_off_f2 = self._crea_spin(0.0, 1.0, SOGLIA_OFF_F2_DEFAULT, 0.00005, decimali=6)
        for spin in (self.spin_soglia_on_f1, self.spin_soglia_off_f1,
                     self.spin_soglia_on_f2, self.spin_soglia_off_f2):
            spin.valueChanged.connect(self.cambia_soglie)
        griglia_soglie.addWidget(self._label_muta("ON f1"), 0, 0)
        griglia_soglie.addWidget(self.spin_soglia_on_f1, 0, 1)
        griglia_soglie.addWidget(self._label_muta("OFF f1"), 1, 0)
        griglia_soglie.addWidget(self.spin_soglia_off_f1, 1, 1)
        griglia_soglie.addWidget(self._label_muta("ON f2"), 2, 0)
        griglia_soglie.addWidget(self.spin_soglia_on_f2, 2, 1)
        griglia_soglie.addWidget(self._label_muta("OFF f2"), 3, 0)
        griglia_soglie.addWidget(self.spin_soglia_off_f2, 3, 1)
        layout_parametri_avanzati.addLayout(griglia_soglie)

        self.widget_parametri_avanzati.setVisible(False)
        colonna_destra.addWidget(self.widget_parametri_avanzati)

        riga_titolo_log = QtWidgets.QHBoxLayout()
        label_log = QtWidgets.QLabel("INTERRUTTORI CHIUSI")
        label_log.setStyleSheet("font-size: 12px; font-weight: 700; color: #9aa0a6; letter-spacing: 1px;")
        riga_titolo_log.addWidget(label_log)
        riga_titolo_log.addStretch(1)
        self.label_totale_chiusure = QtWidgets.QLabel("0 totali")
        self.label_totale_chiusure.setStyleSheet("font-size: 11px; color: #6b7076; font-family: monospace;")
        riga_titolo_log.addWidget(self.label_totale_chiusure)
        self.btn_cancella_log = QtWidgets.QPushButton("🗑")
        self.btn_cancella_log.setToolTip("Svuota la lista qui sotto (non tocca il file eventi_f1_f2_log.jsonl)")
        self.btn_cancella_log.setFixedWidth(32)
        self.btn_cancella_log.clicked.connect(self.cancella_log_ui)
        riga_titolo_log.addWidget(self.btn_cancella_log)
        colonna_destra.addLayout(riga_titolo_log)

        # Log v2: lista di card (badge numero + testo + orario) invece di un
        # QTextEdit monospace — più leggibile a colpo d'occhio, con il più
        # recente sempre in cima e un tetto MAX_RIGHE_LOG.
        self.lista_log = QtWidgets.QListWidget()
        self.lista_log.setSpacing(4)
        self.lista_log.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        colonna_destra.addWidget(self.lista_log, stretch=1)

        scroll_destra = QtWidgets.QScrollArea()
        scroll_destra.setWidgetResizable(True)
        scroll_destra.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll_destra.setWidget(contenitore_destra)
        layout_principale.addWidget(scroll_destra, stretch=2)

        # ------------------------------------------------------------
        # Worker e timer
        # ------------------------------------------------------------
        self.worker = NetworkWorker(UDP_IP, UDP_PORT, CAMPIONI_PER_PACCHETTO)
        self.worker.nuovi_dati_signal.connect(self.gestisci_dati)
        self.worker.start()

        self.timer = QtCore.QTimer()
        self.timer.setInterval(INTERVALLO_TIMER_MS)
        self.timer.timeout.connect(self.aggiorna_grafico)
        self.timer.start()

        self.timer_stats = QtCore.QTimer()
        self.timer_stats.setInterval(500)
        self.timer_stats.timeout.connect(self.aggiorna_label_rete)
        self.timer_stats.start()

        # Fermo di default: parte solo quando si abilita il checkbox
        # spettrogramma (vedi cambia_spettrogramma_attivo).
        self.timer_spettro = QtCore.QTimer()
        self.timer_spettro.setInterval(INTERVALLO_TIMER_SPETTRO_MS)
        self.timer_spettro.timeout.connect(self.aggiorna_spettrogramma)

    @staticmethod
    def _label_muta(testo):
        """Label senza sfondo card (per non "sdoppiare" lo sfondo dentro le
        griglie di parametri, che vivono già dentro un widget con STILE_CARD)."""
        label = QtWidgets.QLabel(testo)
        label.setStyleSheet("background: none;")
        return label

    def cambia_visibilita_parametri_avanzati(self, visibile):
        self.widget_parametri_avanzati.setVisible(visibile)
        self.btn_toggle_avanzate.setText(
            "▾  Parametri avanzati (frequenze/soglie)" if visibile
            else "▸  Parametri avanzati (frequenze/soglie)"
        )

    @staticmethod
    def _crea_spin(minimo, massimo, valore, passo, decimali=1):
        spin = SpinBoxSenzaRotella()
        spin.setRange(minimo, massimo)
        spin.setSingleStep(passo)
        spin.setDecimals(decimali)
        spin.setValue(valore)
        return spin

    # ------------------------------------------------------------------
    # Parametri protocollo
    # ------------------------------------------------------------------
    def applica_parametri_protocollo(self):
        self.protocollo = RilevatoreProtocolloF1F2(
            SAMPLE_RATE, BLOCCO_GOERTZEL_CAMPIONI,
            self.spin_f1_hz.value(), self.spin_f2_hz.value(),
            self.spin_t0_ms.value(), self.spin_t1_ms.value(), self.spin_tolleranza_ms.value(),
            self.spin_soglia_on_f1.value(), self.spin_soglia_off_f1.value(),
            self.spin_soglia_on_f2.value(), self.spin_soglia_off_f2.value(),
        )
        self._accumulo_goertzel = np.empty(0, dtype=np.int16)
        self.azzera_picchi_potenza()
        self.azzera_riferimento_ripetibilita()
        self.label_counter.setText("Counter: --")
        self.label_verifica_ciclo.setText("Verifica ciclo: --")
        self.label_verifica_ciclo.setStyleSheet(
            "color: #9aa0a6; background-color: #2a2d31; font-size: 14px; "
            "font-weight: 600; padding: 8px; border-radius: 6px;"
        )
        msg = "➜ Parametri protocollo aggiornati, contatore azzerato"
        log.info(msg)
        self._conferma_parametri_applicati()

    def _segna_modifiche_protocollo_pendenti(self):
        """Chiamato a ogni tocco di f1/f2/t0/t1/tolleranza: il rilevatore
        gira ancora con i valori vecchi finché non si preme "Applica"."""
        self.btn_applica_parametri.setStyleSheet(
            "QPushButton { background-color: #e8a33d; color: #10131a; "
            "font-weight: 700; border: 1px solid #e8a33d; border-radius: 6px; padding: 8px; } "
            "QPushButton:hover { background-color: #f0b25a; }"
        )
        self.label_stato_parametri.setText(
            "● Modifiche non ancora attive — premi il bottone qui sopra per applicarle"
        )
        self.label_stato_parametri.setStyleSheet(
            "background: none; color: #e8a33d; font-size: 11px; font-weight: 600;"
        )

    def _conferma_parametri_applicati(self):
        """Stato "a riposo": nessuna modifica in sospeso, valori a schermo
        già attivi nel rilevatore (chiamato all'avvio e dopo ogni Applica)."""
        self.btn_applica_parametri.setStyleSheet("")
        self.label_stato_parametri.setText(
            f"✓ Parametri applicati alle {time.strftime('%H:%M:%S')}"
        )
        self.label_stato_parametri.setStyleSheet(
            "background: none; color: #6bcf8f; font-size: 11px; font-weight: 600;"
        )

    def azzera_picchi_potenza(self):
        """Azzera il picco (max-hold) di potenza f1/f2, usato per tarare le
        soglie: si azzera, si osserva il rumore per qualche secondo (o si fa
        un evento noto), e si legge il massimo raggiunto."""
        self.picco_f1 = 0.0
        self.picco_f2 = 0.0
        self.label_picco_f1.setText("picco --")
        self.label_picco_f2.setText("picco --")

    def cambia_soglie(self):
        self.protocollo.imposta_soglie(
            self.spin_soglia_on_f1.value(), self.spin_soglia_off_f1.value(),
            self.spin_soglia_on_f2.value(), self.spin_soglia_off_f2.value(),
        )

    # ------------------------------------------------------------------
    # Spettrogramma on/off
    # ------------------------------------------------------------------
    def cambia_spettrogramma_attivo(self, attivo):
        self.spettrogramma_attivo = attivo
        self.widget_spettro.setVisible(attivo)
        if attivo:
            self.campioni_totali_ricevuti = 0
            self.campioni_da_ultimo_hop = 0
            self.timer_spettro.start()
        else:
            self.timer_spettro.stop()

    # ------------------------------------------------------------------
    # Ricezione dati — punto di innesto del rilevatore f1/f2
    # ------------------------------------------------------------------
    @QtCore.Slot(np.ndarray)
    def gestisci_dati(self, nuovi_campioni):
        n = len(nuovi_campioni)
        fine = self.write_pos + n
        if fine <= STORICO_CAMPIONI:
            self.buffer_audio[self.write_pos:fine] = nuovi_campioni
        else:
            primo_pezzo = STORICO_CAMPIONI - self.write_pos
            self.buffer_audio[self.write_pos:] = nuovi_campioni[:primo_pezzo]
            self.buffer_audio[: n - primo_pezzo] = nuovi_campioni[primo_pezzo:]
        self.write_pos = fine % STORICO_CAMPIONI

        if self.spettrogramma_attivo:
            self.campioni_totali_ricevuti += n
            self.campioni_da_ultimo_hop += n
            if self.campioni_totali_ricevuti >= NPERSEG_SPETTROGRAMMA:
                while self.campioni_da_ultimo_hop >= HOP_SPETTROGRAMMA_CAMPIONI:
                    self.campioni_da_ultimo_hop -= HOP_SPETTROGRAMMA_CAMPIONI
                    self._calcola_nuova_colonna_spettro()

        # --- Rilevatore f1/f2 (Goertzel + isteresi + macchina a stati) ---
        self._accumulo_goertzel = np.concatenate((self._accumulo_goertzel, nuovi_campioni))
        while len(self._accumulo_goertzel) >= BLOCCO_GOERTZEL_CAMPIONI:
            blocco = self._accumulo_goertzel[:BLOCCO_GOERTZEL_CAMPIONI]
            self._accumulo_goertzel = self._accumulo_goertzel[BLOCCO_GOERTZEL_CAMPIONI:]
            for evento in self.protocollo.elabora_blocco(blocco):
                self._gestisci_evento_protocollo(evento)

        self.label_potenza_f1.setText(
            f"Potenza f1 (@{self.protocollo.goertzel_f1.frequenza_hz:.0f}Hz): "
            f"{self.protocollo.ultima_potenza_f1:.6f} "
            f"({'ON' if self.protocollo.macchina_f1.attivo else 'OFF'})"
        )
        self.label_potenza_f2.setText(
            f"Potenza f2 (@{self.protocollo.goertzel_f2.frequenza_hz:.0f}Hz): "
            f"{self.protocollo.ultima_potenza_f2:.6f} "
            f"({'ON' if self.protocollo.macchina_f2.attivo else 'OFF'})"
        )
        if self.protocollo.ultima_potenza_f1 > self.picco_f1:
            self.picco_f1 = self.protocollo.ultima_potenza_f1
            self.label_picco_f1.setText(f"picco {self.picco_f1:.6f}")
        if self.protocollo.ultima_potenza_f2 > self.picco_f2:
            self.picco_f2 = self.protocollo.ultima_potenza_f2
            self.label_picco_f2.setText(f"picco {self.picco_f2:.6f}")

    def _gestisci_evento_protocollo(self, evento):
        """Il log v2 (lista card) mostra SOLO le chiusure: è l'unica
        informazione che l'operatore deve leggere al volo. Marcatori di zero
        e slot "aperto" restano nel log tecnico (console + jsonl), utili per
        debug ma non per la lettura rapida."""
        ora_str = time.strftime("%H:%M:%S")

        if evento["tipo"] == "zero":
            log.info(f"⏱ {ora_str}  marcatore zero (durata {evento['durata_s']:.3f}s) → counter reset")
            self.label_counter.setText("Counter: 0")
            self._verifica_conteggio_ciclo(evento.get("conteggio_ciclo_precedente"))
            self._verifica_ripetibilita_ciclo()
            return

        # evento["tipo"] == "slot"
        self.label_counter.setText(f"Counter: {evento['counter']}")
        self.stato_ciclo_corrente[evento["counter"]] = evento["stato"]
        self._scrivi_log_slot({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "counter": evento["counter"],
            "stato": evento["stato"],
            "durata_s": round(evento["durata_s"], 3),
        })

        if evento["stato"] != "chiuso":
            log.info(f"🔓 {ora_str}  slot {evento['counter']:>3} → APERTO (durata {evento['durata_s']:.3f}s)")
            return

        self._aggiungi_riga_log(evento["counter"], ora_str)
        log.info(f"🔒 {ora_str}  Interruttore {evento['counter']} CHIUSO")

    def _crea_riga_log(self, numero, ora_str):
        riga = QtWidgets.QWidget()
        riga.setStyleSheet("background-color: #1f2124; border-radius: 6px;")
        layout = QtWidgets.QHBoxLayout(riga)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        badge = QtWidgets.QLabel(f"#{numero}")
        badge.setFixedWidth(46)
        badge.setAlignment(QtCore.Qt.AlignCenter)
        badge.setStyleSheet(
            "background-color: #143d33; color: #2ed573; font-family: monospace; "
            "font-size: 13px; font-weight: 700; border-radius: 5px; padding: 4px 0;"
        )
        layout.addWidget(badge)

        testo = QtWidgets.QLabel("Interruttore chiuso")
        testo.setStyleSheet("color: #e6e8eb; font-size: 14px; font-weight: 600; background: none;")
        layout.addWidget(testo, stretch=1)

        tempo = QtWidgets.QLabel(ora_str)
        tempo.setStyleSheet("color: #7d8590; font-family: monospace; font-size: 12px; background: none;")
        layout.addWidget(tempo)
        return riga

    def _aggiungi_riga_log(self, numero, ora_str):
        item = QtWidgets.QListWidgetItem()
        riga = self._crea_riga_log(numero, ora_str)
        item.setSizeHint(riga.sizeHint())
        self.lista_log.insertItem(0, item)
        self.lista_log.setItemWidget(item, riga)
        while self.lista_log.count() > MAX_RIGHE_LOG:
            self.lista_log.takeItem(self.lista_log.count() - 1)

        self.totale_chiusure += 1
        self.label_totale_chiusure.setText(f"{self.totale_chiusure} totali")

    def cancella_log_ui(self):
        """Svuota solo la QListWidget a schermo e il contatore totali:
        il file eventi_f1_f2_log.jsonl non viene toccato, resta lo storico
        completo su disco per analisi successive."""
        self.lista_log.clear()
        self.totale_chiusure = 0
        self.label_totale_chiusure.setText("0 totali")

    def _verifica_conteggio_ciclo(self, conteggio_rilevato):
        """Confronto non bloccante tra interruttori attesi (campo utente) e
        quelli effettivamente contati nel ciclo appena concluso. Solo un
        avviso (label + log.warning): non solleva eccezioni, non ferma il
        rilevamento."""
        atteso = self.spin_numero_atteso.value()
        if conteggio_rilevato is None or atteso <= 0:
            return

        if conteggio_rilevato == atteso:
            self.label_verifica_ciclo.setText(f"✓ Ultimo ciclo: {conteggio_rilevato}/{atteso} interruttori")
            self.label_verifica_ciclo.setStyleSheet(
                "color: #10131a; background-color: #2ed573; font-size: 14px; "
                "font-weight: 700; padding: 8px; border-radius: 6px;"
            )
        else:
            msg = f"⚠ Conteggio ciclo non corrisponde: rilevati {conteggio_rilevato}, attesi {atteso}"
            self.label_verifica_ciclo.setText(msg)
            self.label_verifica_ciclo.setStyleSheet(
                "color: #ffffff; background-color: #d9455f; font-size: 14px; "
                "font-weight: 700; padding: 8px; border-radius: 6px;"
            )
            log.warning(msg)

    def _verifica_ripetibilita_ciclo(self):
        """SOLO per test: nel banco di prova attuale switchState su Arduino
        è fisso per tutta la sessione (randomizzato una sola volta al boot,
        vedi trasmettitore.ino), quindi ogni ciclo dovrebbe chiudere
        esattamente gli stessi interruttori del precedente. Il primo ciclo
        completo osservato diventa il riferimento; i successivi vengono
        confrontati slot per slot e le differenze segnalate (solo un avviso,
        non blocca il rilevamento).

        Un ciclo con un numero di slot diverso da "Interruttori attesi/ciclo"
        è quasi certamente incompleto (pacchetti persi, marcatore di zero
        scambiato per rumore, ecc.): non viene mai preso come riferimento, né
        usato per il confronto, perché altrimenti il riferimento stesso (o il
        confronto) sarebbe inaffidabile fin dall'inizio."""
        ciclo = self.stato_ciclo_corrente
        self.stato_ciclo_corrente = {}
        if not ciclo:
            return

        atteso = self.spin_numero_atteso.value()
        if atteso <= 0:
            self.label_ripetibilita.setText("Imposta \"Interruttori attesi/ciclo\" per attivare la verifica")
            self.label_ripetibilita.setStyleSheet(
                "color: #9aa0a6; background-color: #2a2d31; font-size: 13px; "
                "font-weight: 600; padding: 5px; border-radius: 6px;"
            )
            return

        if len(ciclo) != atteso:
            msg = f"Ciclo scartato per la verifica: {len(ciclo)} slot contati, {atteso} attesi"
            log.warning(msg)
            self.label_ripetibilita.setText(f"⚠ {msg}")
            self.label_ripetibilita.setStyleSheet(
                "color: #ffffff; background-color: #8a6d1a; font-size: 13px; "
                "font-weight: 700; padding: 5px; border-radius: 6px;"
            )
            return

        if self.ciclo_riferimento is None:
            self.ciclo_riferimento = dict(ciclo)
            self.label_ripetibilita.setText(f"✓ Riferimento catturato ({len(ciclo)} slot)")
            self.label_ripetibilita.setStyleSheet(
                "color: #10131a; background-color: #5eb1ff; font-size: 13px; "
                "font-weight: 600; padding: 5px; border-radius: 6px;"
            )
            return

        slot_comuni = sorted(set(ciclo) & set(self.ciclo_riferimento))
        if not slot_comuni:
            return
        diversi = [k for k in slot_comuni if ciclo[k] != self.ciclo_riferimento[k]]

        if not diversi:
            msg = f"✓ Ripetibilità OK: {len(slot_comuni)}/{len(slot_comuni)} slot combaciano col riferimento"
            self.label_ripetibilita.setText(msg)
            self.label_ripetibilita.setStyleSheet(
                "color: #10131a; background-color: #2ed573; font-size: 13px; "
                "font-weight: 700; padding: 5px; border-radius: 6px;"
            )
        else:
            elenco = ", ".join(str(k) for k in diversi[:10])
            suffisso = "..." if len(diversi) > 10 else ""
            msg = f"⚠ {len(diversi)}/{len(slot_comuni)} slot diversi dal riferimento (es. {elenco}{suffisso})"
            self.label_ripetibilita.setText(msg)
            self.label_ripetibilita.setStyleSheet(
                "color: #ffffff; background-color: #d9455f; font-size: 13px; "
                "font-weight: 700; padding: 5px; border-radius: 6px;"
            )
            log.warning(msg)

    def azzera_riferimento_ripetibilita(self):
        """Scarta il riferimento di ripetibilità: il prossimo ciclo completo
        ne diventa uno nuovo (utile dopo un reset dell'Arduino, che
        rirandomizza switchState[])."""
        self.ciclo_riferimento = None
        self.stato_ciclo_corrente = {}
        self.label_ripetibilita.setText("In attesa del primo ciclo completo...")
        self.label_ripetibilita.setStyleSheet(
            "color: #9aa0a6; background-color: #2a2d31; font-size: 13px; "
            "font-weight: 600; padding: 5px; border-radius: 6px;"
        )

    def _scrivi_log_slot(self, record):
        try:
            with open(self.path_log_slot, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"Errore scrittura log slot: {e}")

    # ------------------------------------------------------------------
    # Grafica
    # ------------------------------------------------------------------
    def aggiorna_grafico(self):
        dati_ordinati = np.concatenate(
            (self.buffer_audio[self.write_pos:], self.buffer_audio[: self.write_pos])
        )
        self.data_line.setData(self.asse_tempo, dati_ordinati)

    def _estrai_ultimi_campioni(self, n):
        inizio = (self.write_pos - n) % STORICO_CAMPIONI
        if inizio < self.write_pos:
            return self.buffer_audio[inizio:self.write_pos]
        return np.concatenate((self.buffer_audio[inizio:], self.buffer_audio[:self.write_pos]))

    def _calcola_nuova_colonna_spettro(self):
        finestra = self._estrai_ultimi_campioni(NPERSEG_SPETTROGRAMMA).astype(np.float32)
        finestra_pesata = finestra * self.finestra_hann
        spettro = np.fft.rfft(finestra_pesata)
        ampiezza = np.abs(spettro[: self.bin_freq_max]) / NPERSEG_SPETTROGRAMMA
        db = 20.0 * np.log10(ampiezza / 32768.0 + 1e-12)
        self.spettro_immagine[:, self.col_scrittura_spettro] = db
        self.col_scrittura_spettro = (self.col_scrittura_spettro + 1) % N_COLONNE_SPETTROGRAMMA

    def aggiorna_spettrogramma(self):
        immagine_lineare = np.concatenate(
            (
                self.spettro_immagine[:, self.col_scrittura_spettro:],
                self.spettro_immagine[:, : self.col_scrittura_spettro],
            ),
            axis=1,
        )
        top_db = float(np.percentile(immagine_lineare, 99.0))
        bottom_db = top_db - self.dinamica_db
        self.img_spettro.setLevels([bottom_db, top_db])
        self.img_spettro.setImage(immagine_lineare.T, autoLevels=False, rect=self.rect_spettro)

    def cambia_dinamica_spettrogramma(self, valore_slider):
        self.dinamica_db = valore_slider / 10.0

    def aggiorna_label_rete(self):
        s = self.worker.stats
        avviso = ""
        if s.anomalie_raffica > 0:
            avviso = f" ⚠ anomalie raffica: {s.anomalie_raffica}"
        if s.secondi_con_possibile_perdita > 0:
            avviso += f" ⚠ secondi con possibile perdita dati: {s.secondi_con_possibile_perdita}"
        if s.pacchetti_scartati > 0:
            avviso += f" ⚠ scartati: {s.pacchetti_scartati}"
        self.label_rete.setText(
            f"Rete: {s.pacchetti_ultimo_secondo} pacchetti/s (raffiche, atteso in media "
            f"~{SAMPLE_RATE / CAMPIONI_PER_PACCHETTO:.1f}/s) totale {s.pacchetti_ricevuti}{avviso}"
        )

    def closeEvent(self, event):
        self.timer.stop()
        self.timer_stats.stop()
        self.timer_spettro.stop()
        self.worker.running = False
        if not self.worker.wait(2000):
            log.warning("Il thread di rete non si è chiuso entro il timeout, terminazione forzata.")
            self.worker.terminate()
        event.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE_QSS)
    viewer = RilevatoreF1F2()
    viewer.show()
    sys.exit(app.exec())
