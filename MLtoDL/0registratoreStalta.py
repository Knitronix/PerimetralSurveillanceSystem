"""
registratore_stalta.py
-----------------------
Script unico con DUE MODALITÀ selezionabili da interfaccia, non due processi
separati:

- Modalità Dataset: comportamento originale, etichettatura manuale,
  spettrogramma e grafico storico STA/LTA attivi.
- Modalità Riconoscimento: classificazione automatica di ogni evento
  catturato (nessuna etichetta manuale), con spettrogramma e grafico
  storico STA/LTA DISATTIVATI — non solo nascosti: il calcolo FFT dello
  spettrogramma e il timer che lo ridisegna vengono fermati, perché quello
  è il costo reale per la CPU, non il semplice disegno a schermo.

Motivazione completa nel documento di planning (planning_progetto_fibra.md).
"""

import sys
import socket
import logging
import numpy as np
import wave
import os
import time
import json
from dataclasses import dataclass
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

# Import opzionale: se joblib/scikit-learn o il modulo non sono ancora
# disponibili, l'app deve continuare a funzionare come semplice
# registratore — la modalità Riconoscimento sarà solo non selezionabile.
try:
    from realtime_inference import ClassificatoreEventi
    IMPORT_CLASSIFICATORE_OK = True
except Exception:
    IMPORT_CLASSIFICATORE_OK = False

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("registratore_stalta")

# --- CONFIGURAZIONE FISSA ---
UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000
SECONDI_VISIBILI = 15
STORICO_CAMPIONI = SAMPLE_RATE * SECONDI_VISIBILI
CARTELLA_DATASET = "dataset_sensori_24kHz"
CARTELLA_EVENTI_RICONOSCIUTI = "eventi_riconosciuti"
FILE_CONFIG = "config_soglie_ia.json"
ID_FIBRA_DEFAULT = "fibra1"
PATH_MODELLO_DEFAULT = "modello_classificatore.pkl"
# La logica di valutazione KPI vive in KPI/ (vedi KPI/calcola_kpi.py), non qui: questo
# path è calcolato relativo alla posizione dello script, non alla cwd, così il log
# finisce sempre nel posto giusto indipendentemente da dove viene lanciato l'app.
CARTELLA_KPI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "KPI")
PATH_LOG_CALIBRAZIONE = os.path.join(CARTELLA_KPI, "kpi_calibration_log.jsonl")

INTERVALLO_ATTESO_PACCHETTO = CAMPIONI_PER_PACCHETTO / SAMPLE_RATE
# Il dispositivo NON invia un flusso uniforme pacchetto-per-pacchetto:
# bufferizza internamente e scarica raffiche di ~16 pacchetti quasi
# simultanei, con una pausa strutturale di ~169ms tra una raffica e la
# successiva (misurato con Wireshark: intervalli ~0.169s, coerenti con
# 16 × 254/24000 = 0.1693s). Non è un problema di rete né una perdita di
# dati: è il comportamento normale del dispositivo, non modificabile. La
# diagnostica va calibrata su QUESTO ritmo, non su un intervallo
# pacchetto-per-pacchetto teorico che il dispositivo non rispetta mai.
PERIODO_RAFFICA_TIPICO_SEC = 0.169
SOGLIA_GAP_ANOMALO_SEC = PERIODO_RAFFICA_TIPICO_SEC * 1.8
FRAZIONE_MINIMA_PACCHETTI_ATTESI = 0.90

FPS_GRAFICO = 20
INTERVALLO_TIMER_MS = int(1000 / FPS_GRAFICO)

NPERSEG_SPETTROGRAMMA = SAMPLE_RATE
MAX_FREQ_VISUALIZZATA = 6000
HOP_SPETTROGRAMMA_CAMPIONI = 2400
N_COLONNE_SPETTROGRAMMA = STORICO_CAMPIONI // HOP_SPETTROGRAMMA_CAMPIONI
DINAMICA_DB_MIN = 10.0
DINAMICA_DB_MAX = 150.0
DINAMICA_DB_DEFAULT = 60.0
VALORE_INIZIALE_DB = -300.0
FPS_SPETTROGRAMMA = int(round(SAMPLE_RATE / HOP_SPETTROGRAMMA_CAMPIONI))
INTERVALLO_TIMER_SPETTRO_MS = int(1000 / FPS_SPETTROGRAMMA)

CONFIG_DEFAULT = {
    "RUMORE_AMBIENTALE": 0.0,
    "PASSI_LENTI": 0.0,
    "PASSI_CORSA": 0.0,
    "SCAVO": 0.0,
    "VEICOLO_TRANSITO": 0.0,
    "COLPO_IMPATTO": 0.0,
    "MANIPOLAZIONE_FIBRA": 0.0,
    "VOCE": 0.0,
    "VENTO_PIOGGIA": 0.0,
    "ANIMALE": 0.0,
    "SCONOSCIUTO_DA_VERIFICARE": 0.0,
}

# --- PARAMETRI STA/LTA ---
STA_FINESTRA_SEC = 0.1
LTA_FINESTRA_SEC = 3.0
STA_LTA_SOGLIA_ON_DEFAULT = 1.55
STA_LTA_SOGLIA_OFF_DEFAULT = 1.2
PRE_TRIGGER_SEC = 0.3
ISTERESI_CHIUSURA_SEC = 0.25
DURATA_MASSIMA_EVENTO_SEC = 4.0
DURATA_BLOCCO_RUMORE_SEC = 1.5
TEMPO_RIARMO_SEC = 1.0
SOGLIA_CLIPPING = 32000
FILE_LOG_EVENTI = "eventi_log.jsonl"
FILE_LOG_RICONOSCIMENTI = "riconoscimenti_log.jsonl"
SOGLIA_CONFIDENZA_RICONOSCIMENTO = 0.75


@dataclass
class StatisticheRete:
    pacchetti_ricevuti: int = 0
    pacchetti_scartati: int = 0
    anomalie_raffica: int = 0
    pacchetti_ultimo_secondo: int = 0
    secondi_con_possibile_perdita: int = 0


class RilevatoreStaLta:
    """
    Implementazione streaming di STA/LTA basata su medie mobili esponenziali
    (EMA) aggiornate una volta per pacchetto, non campione per campione.
    """

    def __init__(self, sample_rate, campioni_per_pacchetto,
                 sta_sec=STA_FINESTRA_SEC, lta_sec=LTA_FINESTRA_SEC,
                 soglia_on=STA_LTA_SOGLIA_ON_DEFAULT, soglia_off=STA_LTA_SOGLIA_OFF_DEFAULT):
        durata_pacchetto = campioni_per_pacchetto / sample_rate
        self.alpha_sta = min(1.0, durata_pacchetto / sta_sec)
        self.alpha_lta = min(1.0, durata_pacchetto / lta_sec)
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off
        self._floor_potenza = 1e-6
        self.sta = 0.0
        self.lta = self._floor_potenza
        self.in_evento = False
        self.tempo_sotto_soglia_off = 0.0
        self.durata_pacchetto = durata_pacchetto
        self.ultimo_rapporto = 0.0

    def aggiorna(self, pacchetto_campioni):
        potenza = float(np.mean(pacchetto_campioni.astype(np.float64) ** 2))
        self.sta += self.alpha_sta * (potenza - self.sta)
        self.lta += self.alpha_lta * (potenza - self.lta)
        self.lta = max(self.lta, self._floor_potenza)
        rapporto = self.sta / self.lta
        self.ultimo_rapporto = rapporto

        evento_iniziato = False
        evento_finito = False
        if not self.in_evento:
            if rapporto > self.soglia_on:
                self.in_evento = True
                self.tempo_sotto_soglia_off = 0.0
                evento_iniziato = True
        else:
            if rapporto < self.soglia_off:
                self.tempo_sotto_soglia_off += self.durata_pacchetto
                if self.tempo_sotto_soglia_off >= ISTERESI_CHIUSURA_SEC:
                    self.in_evento = False
                    evento_finito = True
            else:
                self.tempo_sotto_soglia_off = 0.0
        return evento_iniziato, evento_finito

    def imposta_soglie(self, soglia_on, soglia_off):
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off


class NetworkWorker(QtCore.QThread):
    """Nessuna logica di rilevamento qui, solo ricezione e inoltro dei
    pacchetti grezzi — invariato rispetto a prima."""

    nuovi_dati_signal = QtCore.Signal(np.ndarray)

    def __init__(self, ip, port, packet_size, wav_file):
        super().__init__()
        self.ip = ip
        self.port = port
        self.packet_size = packet_size
        self.wav_file = wav_file
        self.running = True
        self.stats = StatisticheRete()
        self._contatore_locale_secondo = 0
        self._ultimo_reset_stats = time.monotonic()
        self._ultimo_arrivo = None

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

            self._aggiorna_gap(time.monotonic())

            if self.wav_file is not None:
                try:
                    self.wav_file.writeframes(data)
                except Exception as e:
                    log.error(f"Errore scrittura wav continuo: {e}")

            nuovi_campioni = np.frombuffer(data, dtype=np.int16)
            self.nuovi_dati_signal.emit(nuovi_campioni)

            self.stats.pacchetti_ricevuti += 1
            self._contatore_locale_secondo += 1
            self._aggiorna_rate(time.monotonic())

        sock.close()

    def _aggiorna_gap(self, ora):
        if self._ultimo_arrivo is not None:
            delta = ora - self._ultimo_arrivo
            if delta > SOGLIA_GAP_ANOMALO_SEC:
                self.stats.anomalie_raffica += 1
        self._ultimo_arrivo = ora

    def _aggiorna_rate(self, ora):
        if ora - self._ultimo_reset_stats >= 1.0:
            self.stats.pacchetti_ultimo_secondo = self._contatore_locale_secondo
            attesi = SAMPLE_RATE / CAMPIONI_PER_PACCHETTO
            if self._contatore_locale_secondo < attesi * FRAZIONE_MINIMA_PACCHETTI_ATTESI:
                self.stats.secondi_con_possibile_perdita += 1
            self._contatore_locale_secondo = 0
            self._ultimo_reset_stats = ora


class SagnacDatasetBuilderStaLta(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        pg.setConfigOptions(antialias=False)

        # KPI 4.1 (Calibration time): timestamp di avvio dell'app.
        self._tempo_avvio_app = time.monotonic()

        self.buffer_audio = np.zeros(STORICO_CAMPIONI, dtype=np.int16)
        self.write_pos = 0
        self.asse_tempo = np.linspace(-SECONDI_VISIBILI, 0, STORICO_CAMPIONI, dtype=np.float32)

        self.n_storico_stalta = max(1, round(SECONDI_VISIBILI / INTERVALLO_ATTESO_PACCHETTO))
        self.storico_stalta = np.full(self.n_storico_stalta, np.nan, dtype=np.float32)
        self.write_pos_stalta = 0
        self.asse_tempo_stalta = np.linspace(-SECONDI_VISIBILI, 0, self.n_storico_stalta, dtype=np.float32)

        self.stalta_min_osservato = float("inf")
        self.stalta_max_osservato = float("-inf")

        self.finestra_hann = np.hanning(NPERSEG_SPETTROGRAMMA).astype(np.float32)
        self.bin_freq_max = min(MAX_FREQ_VISUALIZZATA + 1, NPERSEG_SPETTROGRAMMA // 2 + 1)
        self.spettro_immagine = np.full(
            (self.bin_freq_max, N_COLONNE_SPETTROGRAMMA), VALORE_INIZIALE_DB, dtype=np.float32
        )
        self.col_scrittura_spettro = 0
        self.campioni_da_ultimo_hop = 0
        self.campioni_totali_ricevuti = 0
        self.dinamica_db = DINAMICA_DB_DEFAULT

        self.classi_dataset = self.carica_classi_da_json()
        self.classe_corrente = self.classi_dataset[0] if self.classi_dataset else "RUMORE_AMBIENTALE"

        self.rilevatore = RilevatoreStaLta(SAMPLE_RATE, CAMPIONI_PER_PACCHETTO)
        self._tempo_ultima_chiusura_evento = -1e9

        self.path_log_eventi = os.path.join(CARTELLA_DATASET, FILE_LOG_EVENTI)

        # --- Modalità: "dataset" (default) o "riconoscimento" ---
        self.modalita = "dataset"
        self.classificatore = None

        self.armato = False
        self.catturando_evento = False

        self.campioni_pre_trigger = int(SAMPLE_RATE * PRE_TRIGGER_SEC)
        self.campioni_massimi_evento = int(SAMPLE_RATE * DURATA_MASSIMA_EVENTO_SEC) + self.campioni_pre_trigger
        self.buffer_impulso_array = np.zeros(self.campioni_massimi_evento, dtype=np.int16)
        self.pos_impulso = 0

        self.campioni_blocco_rumore_continuo = int(SAMPLE_RATE * DURATA_BLOCCO_RUMORE_SEC)

        self.id_fibra = ID_FIBRA_DEFAULT

        os.makedirs(CARTELLA_DATASET, exist_ok=True)
        self.cartella_registrazioni = "registrazioni_audio"
        os.makedirs(self.cartella_registrazioni, exist_ok=True)

        self.nome_file_wav = os.path.join(
            self.cartella_registrazioni,
            f"full_streaming_{time.strftime('%Y%m%d_%H%M%S')}.wav",
        )
        try:
            self.wav_file = wave.open(self.nome_file_wav, "wb")
            self.wav_file.setnchannels(1)
            self.wav_file.setsampwidth(2)
            self.wav_file.setframerate(SAMPLE_RATE)
        except Exception as e:
            log.error(f"Impossibile aprire il file wav continuo: {e}")
            self.wav_file = None

        # --- INTERFACCIA GRAFICA ---
        self.setWindowTitle("Project Q - DAS Recorder (trigger STA/LTA)")
        self.resize(1080, 500)

        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        layout_principale = QtWidgets.QHBoxLayout(central_widget)

        colonna_sinistra = QtWidgets.QVBoxLayout()
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

        # --- Contenitore per tutto ciò che è pesante e va disattivato in
        # modalità Riconoscimento (spettrogramma + storico STA/LTA): un
        # unico widget, così nascondere/rimostrare è una riga sola, e lo
        # stesso flag "modalita" decide anche se il lavoro va CALCOLATO,
        # non solo se va disegnato (vedi gestisci_dati). ---
        self.widget_analisi_pesante = QtWidgets.QWidget()
        layout_analisi_pesante = QtWidgets.QVBoxLayout(self.widget_analisi_pesante)
        layout_analisi_pesante.setContentsMargins(0, 0, 0, 0)

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
        layout_analisi_pesante.addWidget(self.plot_spettro, stretch=3)

        self.plot_stalta = pg.PlotWidget()
        self.plot_stalta.setBackground("#111111")
        self.plot_stalta.showGrid(x=True, y=True, alpha=0.2)
        self.plot_stalta.setLabel("bottom", "Tempo", units="s")
        self.plot_stalta.setLabel("left", "Rapporto STA/LTA")
        self.plot_stalta.setTitle("Storico STA/LTA (linee tratteggiate = soglie ON/OFF)")
        self.plot_stalta.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        self.plot_stalta.setXLink(self.plot_widget)

        self.linea_stalta = self.plot_stalta.plot(
            self.asse_tempo_stalta, self.storico_stalta, pen=pg.mkPen(color="#ffb02e", width=1.5)
        )
        self.linea_soglia_on = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(color="#ff4757", width=1.5, style=QtCore.Qt.DashLine)
        )
        self.linea_soglia_off = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(color="#5eb1ff", width=1.5, style=QtCore.Qt.DashLine)
        )
        self.linea_soglia_on.setValue(STA_LTA_SOGLIA_ON_DEFAULT)
        self.linea_soglia_off.setValue(STA_LTA_SOGLIA_OFF_DEFAULT)
        self.plot_stalta.addItem(self.linea_soglia_on)
        self.plot_stalta.addItem(self.linea_soglia_off)
        layout_analisi_pesante.addWidget(self.plot_stalta, stretch=2)

        riga_minmax = QtWidgets.QHBoxLayout()
        self.label_stalta_minmax = QtWidgets.QLabel("Min/Max osservati: --")
        self.label_stalta_minmax.setStyleSheet("color: #aaaaaa; font-family: monospace; font-size: 11px;")
        riga_minmax.addWidget(self.label_stalta_minmax)
        btn_azzera_minmax = QtWidgets.QPushButton("Azzera min/max")
        btn_azzera_minmax.clicked.connect(self.azzera_minmax_stalta)
        riga_minmax.addWidget(btn_azzera_minmax)
        layout_analisi_pesante.addLayout(riga_minmax)

        colonna_sinistra.addWidget(self.widget_analisi_pesante, stretch=5)

        riga_soglia = QtWidgets.QHBoxLayout()
        riga_soglia.addWidget(QtWidgets.QLabel("Dinamica Spettrogramma (dB sotto il picco):"))
        self.slider_soglia_spettro = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider_soglia_spettro.setRange(int(DINAMICA_DB_MIN * 10), int(DINAMICA_DB_MAX * 10))
        self.slider_soglia_spettro.setValue(int(DINAMICA_DB_DEFAULT * 10))
        self.slider_soglia_spettro.valueChanged.connect(self.cambia_dinamica_spettrogramma)
        riga_soglia.addWidget(self.slider_soglia_spettro)
        colonna_sinistra.addLayout(riga_soglia)

        self.label_rete = QtWidgets.QLabel("Rete: in attesa di pacchetti...")
        self.label_rete.setStyleSheet("color: #777777; font-family: monospace; font-size: 11px;")
        colonna_sinistra.addWidget(self.label_rete)

        self.label_stalta = QtWidgets.QLabel("STA/LTA: --")
        self.label_stalta.setStyleSheet("color: #00d2c4; font-family: monospace; font-size: 12px; font-weight: bold;")
        colonna_sinistra.addWidget(self.label_stalta)

        layout_principale.addLayout(colonna_sinistra, stretch=4)

        colonna_destra = QtWidgets.QVBoxLayout()
        colonna_destra.setContentsMargins(10, 10, 10, 10)

        titolo = QtWidgets.QLabel("DATASET RECORDER PRO — STA/LTA")
        titolo.setStyleSheet("font-weight: bold; font-size: 14px; color: #00d2c4;")
        colonna_destra.addWidget(titolo)
        colonna_destra.addSpacing(10)

        # --- Selettore di modalità ---
        colonna_destra.addWidget(QtWidgets.QLabel("Modalità:"))
        self.combo_modalita = QtWidgets.QComboBox()
        self.combo_modalita.addItems([
            "Dataset (etichettatura manuale)",
            "Riconoscimento (automatico, leggero)",
        ])
        self.combo_modalita.currentIndexChanged.connect(self.cambia_modalita_principale)
        colonna_destra.addWidget(self.combo_modalita)
        colonna_destra.addSpacing(10)

        label_fibra = QtWidgets.QLabel(f"Fibra: {ID_FIBRA_DEFAULT}")
        label_fibra.setStyleSheet("color: #777777; font-family: monospace; font-size: 11px;")
        colonna_destra.addWidget(label_fibra)
        colonna_destra.addSpacing(10)

        self.label_rms_live = QtWidgets.QLabel("Livello Rumore di Fondo (RMS): 0")
        self.label_rms_live.setStyleSheet("color: #aaaaaa; font-family: monospace; font-size: 12px;")
        colonna_destra.addWidget(self.label_rms_live)
        colonna_destra.addSpacing(15)

        # --- Contenitore per la selezione classe: nascosto in modalità
        # Riconoscimento, dove non serve. ---
        self.widget_selezione_classe = QtWidgets.QWidget()
        layout_selezione_classe = QtWidgets.QVBoxLayout(self.widget_selezione_classe)
        layout_selezione_classe.setContentsMargins(0, 0, 0, 0)
        layout_selezione_classe.addWidget(QtWidgets.QLabel("Cosa stai per registrare?"))
        self.combo_classi = QtWidgets.QComboBox()
        self.combo_classi.addItems(self.classi_dataset)
        self.combo_classi.currentTextChanged.connect(self.cambia_classe)
        layout_selezione_classe.addWidget(self.combo_classi)
        colonna_destra.addWidget(self.widget_selezione_classe)
        colonna_destra.addSpacing(15)

        self.label_stato_modello = QtWidgets.QLabel("")
        self.label_stato_modello.setStyleSheet("color: #aaaaaa; font-family: monospace; font-size: 11px;")
        self.label_stato_modello.setWordWrap(True)
        colonna_destra.addWidget(self.label_stato_modello)

        colonna_destra.addWidget(QtWidgets.QLabel("Soglia ON (rapporto STA/LTA — apre il trigger):"))
        self.spin_soglia_on = QtWidgets.QDoubleSpinBox()
        self.spin_soglia_on.setRange(1.1, 50.0)
        self.spin_soglia_on.setSingleStep(0.5)
        self.spin_soglia_on.setValue(STA_LTA_SOGLIA_ON_DEFAULT)
        self.spin_soglia_on.valueChanged.connect(self.cambia_soglie_stalta)
        colonna_destra.addWidget(self.spin_soglia_on)

        colonna_destra.addWidget(QtWidgets.QLabel("Soglia OFF (rapporto STA/LTA — chiude il trigger):"))
        self.spin_soglia_off = QtWidgets.QDoubleSpinBox()
        self.spin_soglia_off.setRange(1.0, 49.0)
        self.spin_soglia_off.setSingleStep(0.25)
        self.spin_soglia_off.setValue(STA_LTA_SOGLIA_OFF_DEFAULT)
        self.spin_soglia_off.valueChanged.connect(self.cambia_soglie_stalta)
        colonna_destra.addWidget(self.spin_soglia_off)

        colonna_destra.addSpacing(15)

        # --- KPI 4.1 (Calibration time): pulsante che logga il tempo
        # trascorso dall'avvio dell'app al momento in cui l'operatore
        # dichiara la calibrazione completata. Vedi protocollo_kpi.md. ---
        self.btn_sistema_pronto = QtWidgets.QPushButton("✅ System Ready (Calibration Time 4.1)")
        self.btn_sistema_pronto.setStyleSheet(
            "background-color: #3a3a3a; color: white; padding: 6px; border-radius: 4px;"
        )
        self.btn_sistema_pronto.clicked.connect(self.segna_sistema_pronto)
        colonna_destra.addWidget(self.btn_sistema_pronto)
        colonna_destra.addSpacing(15)

        self.btn_azione = QtWidgets.QPushButton()
        self.btn_azione.clicked.connect(self.gestisci_pressione_bottone)
        colonna_destra.addWidget(self.btn_azione)
        colonna_destra.addSpacing(20)

        self.label_stato = QtWidgets.QLabel("IN ATTESA DI EVENTI...")
        self.label_stato.setStyleSheet("color: #aaaaaa; font-weight: bold;")
        colonna_destra.addWidget(self.label_stato)

        self.testo_log = QtWidgets.QTextEdit()
        self.testo_log.setReadOnly(True)
        self.testo_log.setStyleSheet("background-color: #222222; color: #ffffff; font-family: monospace;")
        colonna_destra.addWidget(self.testo_log)

        layout_principale.addLayout(colonna_destra, stretch=1)

        self.aggiorna_interfaccia_bottone()

        self.worker = NetworkWorker(UDP_IP, UDP_PORT, CAMPIONI_PER_PACCHETTO, self.wav_file)
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

        # Timer spettrogramma: parte SOLO in modalità dataset. È il timer
        # che guida anche il ridisegno del grafico storico STA/LTA in
        # questa implementazione (li teniamo agganciati insieme, dato che
        # devono attivarsi/disattivarsi sempre in coppia).
        self.timer_spettro = QtCore.QTimer()
        self.timer_spettro.setInterval(INTERVALLO_TIMER_SPETTRO_MS)
        self.timer_spettro.timeout.connect(self.aggiorna_spettrogramma)
        self.timer_spettro.start()

    # ------------------------------------------------------------------
    # Gestione modalità
    # ------------------------------------------------------------------
    def cambia_modalita_principale(self, indice):
        nuova_modalita = "dataset" if indice == 0 else "riconoscimento"

        if nuova_modalita == "riconoscimento":
            if not IMPORT_CLASSIFICATORE_OK:
                self.testo_log.append("⚠ Modulo realtime_inference.py non trovato o dipendenze mancanti.")
                self.combo_modalita.blockSignals(True)
                self.combo_modalita.setCurrentIndex(0)
                self.combo_modalita.blockSignals(False)
                return
            if not os.path.exists(PATH_MODELLO_DEFAULT):
                self.testo_log.append(f"⚠ Modello non trovato: {PATH_MODELLO_DEFAULT} (allena prima con train_classifier.py)")
                self.combo_modalita.blockSignals(True)
                self.combo_modalita.setCurrentIndex(0)
                self.combo_modalita.blockSignals(False)
                return
            try:
                self.classificatore = ClassificatoreEventi(PATH_MODELLO_DEFAULT, soglia_confidenza=SOGLIA_CONFIDENZA_RICONOSCIMENTO)
                self.label_stato_modello.setText(f"Modello: {PATH_MODELLO_DEFAULT} ({self.classificatore.tipo_modello})")
            except Exception as e:
                self.testo_log.append(f"⚠ Errore caricamento modello: {e}")
                self.combo_modalita.blockSignals(True)
                self.combo_modalita.setCurrentIndex(0)
                self.combo_modalita.blockSignals(False)
                return
            os.makedirs(CARTELLA_EVENTI_RICONOSCIUTI, exist_ok=True)
        else:
            self.classificatore = None
            self.label_stato_modello.setText("")

        # Ferma qualunque cattura/armamento in corso: cambiare modalità a
        # metà evento è ambiguo, stesso principio già usato per il cambio
        # classe.
        self.armato = False
        self.catturando_evento = False
        self.pos_impulso = 0
        self.modalita = nuova_modalita

        # Mostra/nasconde i pannelli, e soprattutto ferma/riavvia il timer
        # che guida il calcolo FFT dello spettrogramma: è questo il vero
        # risparmio di CPU, non solo il fatto che il pannello sia nascosto.
        modalita_dataset = self.modalita == "dataset"
        self.widget_analisi_pesante.setVisible(modalita_dataset)
        self.widget_selezione_classe.setVisible(modalita_dataset)
        if modalita_dataset:
            self.timer_spettro.start()
        else:
            self.timer_spettro.stop()

        self.testo_log.append(f"➜ Modalità: {'Dataset' if modalita_dataset else 'Riconoscimento'}")
        self.aggiorna_interfaccia_bottone()

    def carica_classi_da_json(self):
        if os.path.exists(FILE_CONFIG):
            try:
                with open(FILE_CONFIG, "r") as f:
                    return list(json.load(f).keys())
            except Exception as e:
                log.warning(f"Impossibile leggere {FILE_CONFIG}: {e}, uso default")
        return list(CONFIG_DEFAULT.keys())

    def cambia_classe(self, nuova_classe):
        self.classe_corrente = nuova_classe
        self.armato = False
        self.catturando_evento = False
        self.pos_impulso = 0
        self.aggiorna_interfaccia_bottone()
        log.info(f"Classe cambiata in: {self.classe_corrente}")

    def cambia_soglie_stalta(self):
        self.rilevatore.imposta_soglie(self.spin_soglia_on.value(), self.spin_soglia_off.value())
        self.linea_soglia_on.setValue(self.spin_soglia_on.value())
        self.linea_soglia_off.setValue(self.spin_soglia_off.value())
        self._ferma_se_cattura_in_corso()

    def azzera_minmax_stalta(self):
        self.stalta_min_osservato = float("inf")
        self.stalta_max_osservato = float("-inf")
        self.label_stalta_minmax.setText("Min/Max osservati: -- (azzerato)")

    def _ferma_se_cattura_in_corso(self):
        if self.catturando_evento or self.pos_impulso > 0:
            self.catturando_evento = False
            self.pos_impulso = 0
            self.aggiorna_interfaccia_bottone()

    def aggiorna_interfaccia_bottone(self):
        if self.armato:
            self.btn_azione.setText("🛑 FERMA")
            self.btn_azione.setStyleSheet(
                "background-color: #ff4757; color: white; font-weight: bold; padding: 10px; border-radius: 5px;"
            )
            if self.modalita == "riconoscimento":
                if self.catturando_evento:
                    self.label_stato.setText("💥 CATTURA IN CORSO (classificazione automatica)...")
                else:
                    self.label_stato.setText(
                        f"🎯 IN ASCOLTO (riconoscimento) "
                        f"[ON {self.spin_soglia_on.value():.1f} / OFF {self.spin_soglia_off.value():.1f}]"
                    )
            elif self.classe_corrente == "RUMORE_AMBIENTALE":
                self.label_stato.setText("🔴 REGISTRAZIONE CONTINUA RUMORE IN CORSO...")
            elif self.catturando_evento:
                self.label_stato.setText(f"💥 CATTURA IN CORSO: {self.classe_corrente}...")
            else:
                self.label_stato.setText(
                    f"🎯 IN ASCOLTO (STA/LTA): {self.classe_corrente} "
                    f"[ON {self.spin_soglia_on.value():.1f} / OFF {self.spin_soglia_off.value():.1f}]"
                )
            self.label_stato.setStyleSheet("color: #ff4757; font-weight: bold;")
        else:
            self.btn_azione.setText("▶ AVVIA")
            self.btn_azione.setStyleSheet(
                "background-color: #2ed573; color: white; font-weight: bold; padding: 10px; border-radius: 5px;"
            )
            self.label_stato.setText("IN ATTESA (premi Avvia)")
            self.label_stato.setStyleSheet("color: #aaaaaa; font-weight: bold;")

    def segna_sistema_pronto(self):
        """KPI 4.1: registra in kpi_calibration_log.jsonl il tempo trascorso
        dall'avvio dell'app a questo momento (calibrazione dichiarata
        completata dall'operatore). Ripetere N=10 volte per il KPI
        consolidato — vedi protocollo_kpi.md."""
        secondi = time.monotonic() - self._tempo_avvio_app
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "secondi_da_avvio": round(secondi, 2),
        }
        try:
            with open(PATH_LOG_CALIBRAZIONE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            msg = f"✅ Calibration Time registrato: {secondi:.1f}s dall'avvio"
            self.testo_log.append(msg)
            log.info(msg)
        except Exception as e:
            log.error(f"Errore scrittura log calibrazione: {e}")

    def gestisci_pressione_bottone(self):
        self.armato = not self.armato
        self.catturando_evento = False
        self.pos_impulso = 0
        self.aggiorna_interfaccia_bottone()

    # ------------------------------------------------------------------
    # Ricezione dati
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

        # --- Spettrogramma: calcolato SOLO in modalità dataset. Questo è
        # il risparmio di CPU vero e proprio (FFT su 24.000 campioni),
        # non solo il pannello nascosto. ---
        if self.modalita == "dataset":
            self.campioni_totali_ricevuti += n
            self.campioni_da_ultimo_hop += n
            if self.campioni_totali_ricevuti >= NPERSEG_SPETTROGRAMMA:
                while self.campioni_da_ultimo_hop >= HOP_SPETTROGRAMMA_CAMPIONI:
                    self.campioni_da_ultimo_hop -= HOP_SPETTROGRAMMA_CAMPIONI
                    self._calcola_nuova_colonna_spettro()

        rms = np.sqrt(np.mean(nuovi_campioni.astype(np.float64) ** 2))
        self.label_rms_live.setText(f"Livello Rumore di Fondo (RMS): {int(rms)}")

        # --- STA/LTA: SEMPRE calcolato (serve al rilevamento eventi in
        # entrambe le modalità), ma lo storico/plot/min-max vengono
        # aggiornati solo in modalità dataset, dove sono visibili. ---
        evento_iniziato, evento_finito = self.rilevatore.aggiorna(nuovi_campioni)
        rapporto = self.rilevatore.ultimo_rapporto
        self.label_stalta.setText(
            f"STA/LTA: {rapporto:.2f}  (ON={self.rilevatore.soglia_on:.1f} OFF={self.rilevatore.soglia_off:.1f})"
        )

        if self.modalita == "dataset":
            self.storico_stalta[self.write_pos_stalta] = rapporto
            self.write_pos_stalta = (self.write_pos_stalta + 1) % self.n_storico_stalta

            if rapporto < self.stalta_min_osservato:
                self.stalta_min_osservato = rapporto
            if rapporto > self.stalta_max_osservato:
                self.stalta_max_osservato = rapporto
            self.label_stalta_minmax.setText(
                f"Min/Max osservati: {self.stalta_min_osservato:.2f} / {self.stalta_max_osservato:.2f}"
            )

        if not self.armato:
            return

        if self.modalita == "riconoscimento":
            self._gestisci_dati_riconoscimento(evento_iniziato, evento_finito, nuovi_campioni)
            return

        # --- Modalità dataset: comportamento originale ---
        if self.classe_corrente == "RUMORE_AMBIENTALE":
            if self.pos_impulso == 0:
                self._avvia_nuova_cattura(con_pre_trigger=False)
            if self._accumula_in_buffer_cattura(nuovi_campioni, limite=self.campioni_blocco_rumore_continuo):
                self.salva_file(int(self.pos_impulso))
                self.pos_impulso = 0
            return

        if not self.catturando_evento:
            tempo_dal_ultimo_evento = time.monotonic() - self._tempo_ultima_chiusura_evento
            if evento_iniziato and tempo_dal_ultimo_evento >= TEMPO_RIARMO_SEC:
                self.catturando_evento = True
                self._avvia_nuova_cattura(con_pre_trigger=True)
                self._accumula_in_buffer_cattura(nuovi_campioni, limite=self.campioni_massimi_evento)
                self.aggiorna_interfaccia_bottone()
        else:
            pieno = self._accumula_in_buffer_cattura(nuovi_campioni, limite=self.campioni_massimi_evento)
            if evento_finito or pieno:
                self.salva_file(int(self.pos_impulso))
                self.catturando_evento = False
                self.pos_impulso = 0
                self._tempo_ultima_chiusura_evento = time.monotonic()
                self.aggiorna_interfaccia_bottone()

    def _gestisci_dati_riconoscimento(self, evento_iniziato, evento_finito, nuovi_campioni):
        """Cattura a evento identica a quella del dataset (stesso trigger,
        pre-roll, riarmo), ma classifica invece di chiedere un'etichetta
        manuale, e salva in una cartella separata dal dataset di training."""
        if not self.catturando_evento:
            tempo_dal_ultimo_evento = time.monotonic() - self._tempo_ultima_chiusura_evento
            if evento_iniziato and tempo_dal_ultimo_evento >= TEMPO_RIARMO_SEC:
                self.catturando_evento = True
                self._avvia_nuova_cattura(con_pre_trigger=True)
                self._accumula_in_buffer_cattura(nuovi_campioni, limite=self.campioni_massimi_evento)
                self.aggiorna_interfaccia_bottone()
        else:
            pieno = self._accumula_in_buffer_cattura(nuovi_campioni, limite=self.campioni_massimi_evento)
            if evento_finito or pieno:
                self.salva_evento_riconosciuto(int(self.pos_impulso))
                self.catturando_evento = False
                self.pos_impulso = 0
                self._tempo_ultima_chiusura_evento = time.monotonic()
                self.aggiorna_interfaccia_bottone()

    def _avvia_nuova_cattura(self, con_pre_trigger):
        self.pos_impulso = 0
        if con_pre_trigger and self.campioni_pre_trigger > 0:
            pre_roll = self._estrai_ultimi_campioni(self.campioni_pre_trigger)
            self.buffer_impulso_array[: len(pre_roll)] = pre_roll
            self.pos_impulso = len(pre_roll)

    def _accumula_in_buffer_cattura(self, nuovi_campioni, limite):
        n = len(nuovi_campioni)
        fine = min(self.pos_impulso + n, limite)
        quanti = fine - self.pos_impulso
        if quanti > 0:
            self.buffer_impulso_array[self.pos_impulso:fine] = nuovi_campioni[:quanti]
            self.pos_impulso = fine
        return self.pos_impulso >= limite

    # ------------------------------------------------------------------
    # Salvataggio — modalità Dataset
    # ------------------------------------------------------------------
    def salva_file(self, n_campioni_validi):
        cartella_dest = os.path.join(CARTELLA_DATASET, self.id_fibra, self.classe_corrente)
        os.makedirs(cartella_dest, exist_ok=True)

        timestamp_ms = int(time.time() * 1000)
        nome_file_base = f"campione_{timestamp_ms}.wav"
        nome_file = os.path.join(cartella_dest, nome_file_base)

        try:
            dati_validi = self.buffer_impulso_array[:n_campioni_validi]

            picco_assoluto = int(np.max(np.abs(dati_validi))) if len(dati_validi) else 0
            clipping = picco_assoluto >= SOGLIA_CLIPPING

            with wave.open(nome_file, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(dati_validi.tobytes())

            durata_s = n_campioni_validi / SAMPLE_RATE
            rms = float(np.sqrt(np.mean(dati_validi.astype(np.float64) ** 2))) if len(dati_validi) else 0.0

            avviso_clip = "  ⚠ CLIPPING" if clipping else ""
            msg = (f"✓ Salvato: {self.id_fibra}/{self.classe_corrente}/{nome_file_base} "
                   f"({durata_s:.2f}s){avviso_clip}")
            self.testo_log.append(msg)
            log.info(msg)

            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "fibra_id": self.id_fibra,
                "classe": self.classe_corrente,
                "durata_s": round(durata_s, 3),
                "rms": round(rms, 1),
                "picco": picco_assoluto,
                "clipping": clipping,
                "file_audio": os.path.join(self.id_fibra, self.classe_corrente, nome_file_base),
            }
            self._scrivi_log(self.path_log_eventi, record)
        except Exception as e:
            log.error(f"Errore salvataggio: {e}")

    # ------------------------------------------------------------------
    # Salvataggio — modalità Riconoscimento
    # ------------------------------------------------------------------
    def salva_evento_riconosciuto(self, n_campioni_validi):
        try:
            dati_validi = self.buffer_impulso_array[:n_campioni_validi]
            etichetta, confidenza, _ = self.classificatore.classifica(dati_validi, SAMPLE_RATE)

            cartella_dest = os.path.join(CARTELLA_EVENTI_RICONOSCIUTI, etichetta)
            os.makedirs(cartella_dest, exist_ok=True)

            picco_assoluto = int(np.max(np.abs(dati_validi))) if len(dati_validi) else 0
            clipping = picco_assoluto >= SOGLIA_CLIPPING
            durata_s = n_campioni_validi / SAMPLE_RATE

            timestamp_ms = int(time.time() * 1000)
            nome_file_base = f"evento_{timestamp_ms}.wav"
            with wave.open(os.path.join(cartella_dest, nome_file_base), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(dati_validi.tobytes())

            ora_str = time.strftime("%H:%M:%S")
            msg = f"🔎 {ora_str}  {etichetta:<26} {confidenza:.0%}"
            self.testo_log.append(msg)
            log.info(msg)

            self._scrivi_log(FILE_LOG_RICONOSCIMENTI, {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "fibra_id": self.id_fibra,
                "classe_predetta": etichetta,
                "confidenza": round(confidenza, 3),
                "durata_s": round(durata_s, 3),
                "picco": picco_assoluto,
                "clipping": clipping,
                "file_audio": os.path.join(etichetta, nome_file_base),
                "modello": os.path.basename(PATH_MODELLO_DEFAULT),
            })
        except Exception as e:
            log.error(f"Errore salvataggio evento riconosciuto: {e}")

    def _scrivi_log(self, path, record):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"Errore scrittura log ({path}): {e}")

    # ------------------------------------------------------------------
    # Grafica
    # ------------------------------------------------------------------
    def aggiorna_grafico(self):
        dati_ordinati = np.concatenate(
            (self.buffer_audio[self.write_pos:], self.buffer_audio[: self.write_pos])
        )
        self.data_line.setData(self.asse_tempo, dati_ordinati)

        if self.modalita == "dataset":
            storico_ordinato = np.concatenate(
                (self.storico_stalta[self.write_pos_stalta:], self.storico_stalta[: self.write_pos_stalta])
            )
            self.linea_stalta.setData(self.asse_tempo_stalta, storico_ordinato)

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
        # Il timer che chiama questo metodo è fermo in modalità
        # riconoscimento (vedi cambia_modalita_principale), quindi qui non
        # serve un controllo aggiuntivo.
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
        if self.wav_file:
            try:
                self.wav_file.close()
            except Exception as e:
                log.error(f"Errore chiusura wav: {e}")
        event.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    viewer = SagnacDatasetBuilderStaLta()
    viewer.show()
    sys.exit(app.exec())