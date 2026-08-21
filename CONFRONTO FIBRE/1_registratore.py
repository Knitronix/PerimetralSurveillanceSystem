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

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("registratore")

# --- CONFIGURAZIONE FISSA ---
UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000
SECONDI_VISIBILI = 15
STORICO_CAMPIONI = SAMPLE_RATE * SECONDI_VISIBILI
CARTELLA_DATASET = "dataset_sensori_24kHz"
FILE_CONFIG = "config_soglie.json"
ID_FIBRA_DEFAULT = "fibra1"

# Intervallo atteso tra due pacchetti IN UNA STESSA RAFFICA (in secondi).
# ATTENZIONE: il dispositivo interrogatore NON invia un flusso UDP uniforme.
# Verificato con Wireshark: bufferizza e invia a raffiche di ~16 pacchetti
# quasi simultanei, con una PAUSA STRUTTURALE di ~169ms tra una raffica e
# la successiva - comportamento normale del dispositivo, non modificabile,
# non un problema di rete. Un rilevamento di gap basato sull'intervallo
# medio teorico (come se il flusso fosse uniforme) segnalerebbe come
# "sospetta" ogni singola pausa tra raffiche, anche in condizioni perfette:
# per questo la soglia sotto è calibrata sulla pausa strutturale nota, non
# sull'intervallo teorico tra singoli pacchetti.
INTERVALLO_ATTESO_PACCHETTO = CAMPIONI_PER_PACCHETTO / SAMPLE_RATE
PAUSA_TRA_RAFFICHE_S = 0.169  # pausa strutturale nota tra raffiche (misurata con Wireshark)
FATTORE_GAP_SOSPETTO = 2.0  # margine di sicurezza sopra la pausa strutturale nota
SOGLIA_GAP_SOSPETTO_S = PAUSA_TRA_RAFFICHE_S * FATTORE_GAP_SOSPETTO

# Frequenza di refresh del grafico (Hz). 20-25 Hz è un buon compromesso fluidità/CPU
FPS_GRAFICO = 20
INTERVALLO_TIMER_MS = int(1000 / FPS_GRAFICO)

# --- CONFIGURAZIONE SPETTROGRAMMA ---
# Finestra FFT = SAMPLE_RATE campioni => risoluzione in frequenza esatta di 1 Hz
# (risoluzione = SAMPLE_RATE / NPERSEG). Costa una FFT su 24.000 campioni, ma la
# calcoliamo solo poche volte al secondo (vedi HOP), non ad ogni pacchetto.
NPERSEG_SPETTROGRAMMA = SAMPLE_RATE
MAX_FREQ_VISUALIZZATA = 6000  # Hz mostrati sull'asse Y (come da riferimento)

# Ogni quanti NUOVI campioni calcoliamo una nuova colonna dello spettrogramma.
# 2400 campioni = 100ms => una colonna nuova ogni 100ms, ~94% di overlap tra
# una finestra FFT e la successiva (spettrogramma "morbido" nel tempo).
HOP_SPETTROGRAMMA_CAMPIONI = 2400
N_COLONNE_SPETTROGRAMMA = STORICO_CAMPIONI // HOP_SPETTROGRAMMA_CAMPIONI

DINAMICA_DB_MIN = 10.0
DINAMICA_DB_MAX = 150.0
DINAMICA_DB_DEFAULT = 60.0
VALORE_INIZIALE_DB = -300.0  # riempimento iniziale, ben sotto qualunque segnale reale

# Frequenza di ridisegno dell'immagine dello spettrogramma: non ha senso
# ridisegnarla più spesso di quanto arrivino nuove colonne (== frequenza di HOP)
FPS_SPETTROGRAMMA = int(round(SAMPLE_RATE / HOP_SPETTROGRAMMA_CAMPIONI))
INTERVALLO_TIMER_SPETTRO_MS = int(1000 / FPS_SPETTROGRAMMA)

CONFIG_DEFAULT = {
    "RUMORE_AMBIENTALE": 99999.0,
    "BATTITO_MANI": 6000.0,
    "FISCHIO": 4000.0,
    "CORSA": 7500.0,
    "PASSI_LENTI": 3500.0,
}


@dataclass
class StatisticheRete:
    pacchetti_ricevuti: int = 0
    pacchetti_scartati: int = 0
    gap_sospetti: int = 0
    pacchetti_ultimo_secondo: int = 0


class NetworkWorker(QtCore.QThread):
    """
    Thread dedicato alla ricezione UDP. Non fa MAI lavoro pesante (niente roll,
    niente ricostruzioni di array grandi): si limita a leggere, scrivere il wav
    grezzo su disco e inoltrare il pacchetto grezzo alla GUI tramite segnale.
    """
    nuovi_dati_signal = QtCore.Signal(np.ndarray)

    def __init__(self, ip, port, packet_size, wav_file):
        super().__init__()
        self.ip = ip
        self.port = port
        self.packet_size = packet_size
        self.wav_file = wav_file
        self.running = True

        # Statistiche esposte in lettura alla GUI (interi semplici, lettura
        # atomica sotto il GIL di CPython: sufficiente per uno scopo di solo
        # monitoraggio/visualizzazione)
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
        bytes_attesi = self.packet_size * 2  # int16 = 2 byte

        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as e:
                # Errore di rete reale: qui ha senso interrompere il thread
                log.error(f"Errore socket: {e}")
                break

            if len(data) != bytes_attesi:
                # Pacchetto malformato/troncato: lo scartiamo ma il thread
                # continua a vivere, non deve morire per un pacchetto sporco
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
            if delta > SOGLIA_GAP_SOSPETTO_S:
                self.stats.gap_sospetti += 1
        self._ultimo_arrivo = ora

    def _aggiorna_rate(self, ora):
        if ora - self._ultimo_reset_stats >= 1.0:
            self.stats.pacchetti_ultimo_secondo = self._contatore_locale_secondo
            self._contatore_locale_secondo = 0
            self._ultimo_reset_stats = ora


class SagnacDatasetBuilder(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        pg.setConfigOptions(antialias=False)

        # --- BUFFER CIRCOLARE ---
        # A differenza della versione precedente NON usiamo np.roll ad ogni
        # pacchetto: manteniamo un array fisso e un puntatore di scrittura.
        # La "linearizzazione" per la visualizzazione avviene solo al momento
        # del refresh grafico (a FPS_GRAFICO Hz), non ad ogni pacchetto
        # (~94 volte al secondo). Questo è il cambiamento che elimina la
        # scattosità: prima si ricopiavano ~360.000 campioni decine di volte
        # al secondo, ora lo si fa solo 20 volte al secondo.
        self.buffer_audio = np.zeros(STORICO_CAMPIONI, dtype=np.int16)
        self.write_pos = 0
        # Asse temporale in secondi, condiviso dal grafico d'onda e dallo
        # spettrogramma (0 = "adesso", -SECONDI_VISIBILI = campione più vecchio)
        self.asse_tempo = np.linspace(-SECONDI_VISIBILI, 0, STORICO_CAMPIONI, dtype=np.float32)

        # --- STATO SPETTROGRAMMA ---
        # Stesso principio del buffer audio: NON ricalcoliamo/ricopiamo l'intera
        # immagine ad ogni pacchetto. Manteniamo un'immagine a larghezza fissa
        # (bin di frequenza x colonne temporali) con un puntatore di scrittura,
        # e calcoliamo UNA nuova colonna FFT solo ogni HOP_SPETTROGRAMMA_CAMPIONI
        # campioni nuovi (~10 volte al secondo), non una FFT per pacchetto.
        self.finestra_hann = np.hanning(NPERSEG_SPETTROGRAMMA).astype(np.float32)
        self.bin_freq_max = min(
            MAX_FREQ_VISUALIZZATA + 1, NPERSEG_SPETTROGRAMMA // 2 + 1
        )
        self.spettro_immagine = np.full(
            (self.bin_freq_max, N_COLONNE_SPETTROGRAMMA), VALORE_INIZIALE_DB, dtype=np.float32
        )
        self.col_scrittura_spettro = 0
        self.campioni_da_ultimo_hop = 0
        self.campioni_totali_ricevuti = 0  # serve a evitare artefatti da zero-padding iniziale
        self.dinamica_db = DINAMICA_DB_DEFAULT

        self.classi_dataset = self.carica_classi_da_json()

        # --- LOGICA RECORDER ---
        self.classe_corrente = self.classi_dataset[0] if self.classi_dataset else "RUMORE_AMBIENTALE"
        self.soglia_trigger = 2000.0
        self.usa_soglia = True  # False = cattura a durata fissa senza attendere soglia (es. sweep)
        # Stato unico di registrazione: quando True, il pulsante è "Ferma
        # Registrazione" ed è attiva la cattura (continua per RUMORE_AMBIENTALE,
        # a soglia per le altre classi). Quando False, non si cattura nulla.
        self.armato = False
        # Rilevante solo per le classi diverse da RUMORE_AMBIENTALE: True tra
        # il superamento della soglia e il completamento dei 1.5s dell'evento.
        self.catturando_evento = False
        # Buffer di cattura: array preallocato + puntatore di scrittura,
        # NON una lista Python con .extend(). Con catture fino a 30s (per lo
        # sweep) accumulare in una lista Python boxata sarebbe lo stesso
        # antipattern eliminato dal buffer del grafico (np.roll) - qui
        # sarebbe stato "list.extend()" ripetuto su ~700.000 elementi.
        self.buffer_impulso_array = np.zeros(0, dtype=np.int16)
        self.pos_impulso = 0
        self.campioni_da_raccogliere = int(SAMPLE_RATE * 1.5)

        # --- ID FIBRA IN TEST ---
        # Cambiando questo valore da interfaccia (quando si stacca una fibra
        # e se ne attacca un'altra) i campioni salvati finiscono in cartelle
        # separate, per non mescolare mai i dati di fibre diverse. Tocca solo
        # salva_file() più sotto: nessuna interazione col thread di rete.
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
        self.setWindowTitle("Project Q - DAS Super-Optimized Data Recorder")
        self.resize(1050, 470)

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
        # Downsampling automatico lato pyqtgraph: con 360.000 punti da
        # disegnare su ~1000 pixel di schermo non ha senso mandare tutti i
        # campioni alla GPU/CPU di rendering. mode='peak' preserva i picchi
        # (fondamentale per non "appiattire" gli impulsi dell'interferometro).
        try:
            # Nelle versioni recenti di pyqtgraph il parametro si chiama "method"
            self.data_line.setDownsampling(auto=True, method="peak")
        except TypeError:
            # Fallback per versioni più vecchie che usano "mode"
            try:
                self.data_line.setDownsampling(auto=True, mode="peak")
            except TypeError:
                self.data_line.setDownsampling(auto=True)
        self.data_line.setClipToView(True)
        try:
            self.data_line.setSkipFiniteCheck(True)  # pyqtgraph >= 0.12
        except AttributeError:
            pass

        self.linea_soglia = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen(color="#ff4757", width=1.5, style=QtCore.Qt.DashLine),
        )
        self.linea_soglia.setValue(self.soglia_trigger)
        self.plot_widget.addItem(self.linea_soglia)

        self.limite_y = 20000.0
        self.plot_widget.setYRange(-self.limite_y, self.limite_y, padding=0)
        self.plot_widget.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        colonna_sinistra.addWidget(self.plot_widget, stretch=3)

        # --- PANNELLO SPETTROGRAMMA ---
        self.plot_spettro = pg.PlotWidget()
        self.plot_spettro.setBackground("#111111")
        self.plot_spettro.setLabel("bottom", "Tempo", units="s")
        self.plot_spettro.setLabel("left", "Frequenza", units="Hz")
        self.plot_spettro.setTitle(f"Spettrogramma (Risoluzione {SAMPLE_RATE // NPERSEG_SPETTROGRAMMA or 1}Hz)")
        self.plot_spettro.setYRange(0, MAX_FREQ_VISUALIZZATA, padding=0)
        self.plot_spettro.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        # Zoom/pan sincronizzati tra i due grafici: scorrendo uno scorre anche l'altro
        self.plot_spettro.setXLink(self.plot_widget)

        # Rect usato come placeholder iniziale (nessuna immagine ancora
        # caricata); quello che conta davvero per l'allineamento è il rect
        # passato ad OGNI setImage(), vedi aggiorna_spettrogramma().
        self.rect_spettro = QtCore.QRectF(-SECONDI_VISIBILI, 0, SECONDI_VISIBILI, MAX_FREQ_VISUALIZZATA)
        self.img_spettro = pg.ImageItem()
        self.img_spettro.setRect(self.rect_spettro)
        self.plot_spettro.addItem(self.img_spettro)

        try:
            cmap = pg.colormap.get("viridis")
        except Exception:
            # Fallback manuale (stessi colori chiave della viridis) se la
            # versione di pyqtgraph/matplotlib installata non la fornisce
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
        # I livelli (min/max colore) NON sono fissi: vengono ricalcolati ad
        # ogni redraw in base al picco corrente, vedi aggiorna_spettrogramma().
        # Un valore assoluto in dB non funzionerebbe: dipende dalla scala
        # dell'ADC del sensore, che non conosciamo a priori.

        colonna_sinistra.addWidget(self.plot_spettro, stretch=3)

        # Slider "dinamica" dello spettrogramma: quanti dB sotto il picco
        # corrente restano visibili. Non è una soglia assoluta (che non
        # funzionerebbe senza conoscere la scala dell'ADC del sensore), ma un
        # range relativo che si adatta automaticamente all'ampiezza reale del segnale.
        riga_soglia = QtWidgets.QHBoxLayout()
        riga_soglia.addWidget(QtWidgets.QLabel("Dinamica Spettrogramma (dB sotto il picco):"))
        self.slider_soglia_spettro = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider_soglia_spettro.setRange(int(DINAMICA_DB_MIN * 10), int(DINAMICA_DB_MAX * 10))
        self.slider_soglia_spettro.setValue(int(DINAMICA_DB_DEFAULT * 10))
        self.slider_soglia_spettro.valueChanged.connect(self.cambia_dinamica_spettrogramma)
        riga_soglia.addWidget(self.slider_soglia_spettro)
        colonna_sinistra.addLayout(riga_soglia)

        # Etichetta di stato rete (pacchetti/s, scarti, gap sospetti)
        self.label_rete = QtWidgets.QLabel("Rete: in attesa di pacchetti...")
        self.label_rete.setStyleSheet("color: #777777; font-family: monospace; font-size: 11px;")
        colonna_sinistra.addWidget(self.label_rete)

        layout_principale.addLayout(colonna_sinistra, stretch=4)

        # Controlli Destra
        colonna_destra = QtWidgets.QVBoxLayout()
        colonna_destra.setContentsMargins(10, 10, 10, 10)

        titolo = QtWidgets.QLabel("DATASET RECORDER PRO")
        titolo.setStyleSheet("font-weight: bold; font-size: 14px; color: #00d2c4;")
        colonna_destra.addWidget(titolo)

        colonna_destra.addSpacing(10)

        # --- ID FIBRA IN TEST ---
        # Workflow: attacchi fibra1, registri tutto il protocollo, poi
        # scrivi "fibra2" qui e premi Invio (o "Applica") prima di
        # riattaccare l'altra fibra. Ogni fibra finisce in cartelle separate.
        colonna_destra.addWidget(QtWidgets.QLabel("ID Fibra in test:"))
        riga_fibra = QtWidgets.QHBoxLayout()
        self.campo_id_fibra = QtWidgets.QLineEdit(ID_FIBRA_DEFAULT)
        self.campo_id_fibra.editingFinished.connect(self.cambia_id_fibra)
        riga_fibra.addWidget(self.campo_id_fibra)
        btn_applica_fibra = QtWidgets.QPushButton("Applica")
        btn_applica_fibra.clicked.connect(self.cambia_id_fibra)
        riga_fibra.addWidget(btn_applica_fibra)
        colonna_destra.addLayout(riga_fibra)

        colonna_destra.addSpacing(10)

        self.label_rms_live = QtWidgets.QLabel("Livello Rumore di Fondo (RMS): 0")
        self.label_rms_live.setStyleSheet("color: #aaaaaa; font-family: monospace; font-size: 12px;")
        colonna_destra.addWidget(self.label_rms_live)

        colonna_destra.addSpacing(15)

        colonna_destra.addWidget(QtWidgets.QLabel("Cosa stai per registrare?"))
        self.combo_classi = QtWidgets.QComboBox()
        self.combo_classi.addItems(self.classi_dataset)
        self.combo_classi.currentTextChanged.connect(self.cambia_classe)
        colonna_destra.addWidget(self.combo_classi)

        colonna_destra.addSpacing(15)

        # --- MODALITÀ DI CATTURA ---
        # Soglia: adatta a eventi brevi e imprevedibili (battiti, passi) dove
        # non sai esattamente quando accadranno.
        # Durata fissa: adatta a segnali lunghi e controllati da te (es. uno
        # sweep in frequenza riprodotto da un altoparlante): premi Avvia
        # esattamente quando parte il segnale, cattura per N secondi senza
        # aspettare nessuna soglia (che rischierebbe di tagliare l'inizio,
        # specialmente se lo sweep parte a una frequenza poco sensibile per
        # quella fibra - cioè proprio dove servirebbe misurare di più).
        self.chk_usa_soglia = QtWidgets.QCheckBox("Attendi soglia prima di catturare")
        self.chk_usa_soglia.setChecked(True)
        self.chk_usa_soglia.toggled.connect(self.cambia_modalita_cattura)
        colonna_destra.addWidget(self.chk_usa_soglia)

        colonna_destra.addWidget(QtWidgets.QLabel("Soglia di Auto-Trigger (per impatti):"))
        self.spin_soglia = QtWidgets.QSpinBox()
        self.spin_soglia.setRange(100, 200000)
        self.spin_soglia.setValue(int(self.soglia_trigger))
        self.spin_soglia.setSingleStep(250)
        self.spin_soglia.valueChanged.connect(self.cambia_soglia)
        colonna_destra.addWidget(self.spin_soglia)

        colonna_destra.addSpacing(10)

        colonna_destra.addWidget(QtWidgets.QLabel("Durata cattura (s) — alza per sweep/segnali lunghi:"))
        self.spin_durata = QtWidgets.QDoubleSpinBox()
        self.spin_durata.setRange(0.5, 30.0)
        self.spin_durata.setSingleStep(0.5)
        self.spin_durata.setValue(1.5)
        self.spin_durata.valueChanged.connect(self.cambia_durata_cattura)
        colonna_destra.addWidget(self.spin_durata)

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

        # Timer grafico: unico punto in cui il buffer viene "linearizzato"
        self.timer = QtCore.QTimer()
        self.timer.setInterval(INTERVALLO_TIMER_MS)
        self.timer.timeout.connect(self.aggiorna_grafico)
        self.timer.start()

        # Timer separato e più lento per le statistiche di rete (non serve
        # aggiornarle a 20Hz, 2 volte al secondo bastano e costano meno)
        self.timer_stats = QtCore.QTimer()
        self.timer_stats.setInterval(500)
        self.timer_stats.timeout.connect(self.aggiorna_label_rete)
        self.timer_stats.start()

        # Timer spettrogramma: intervallo agganciato a HOP_SPETTROGRAMMA_CAMPIONI,
        # inutile ridisegnare più spesso di quanto arrivino nuove colonne
        self.timer_spettro = QtCore.QTimer()
        self.timer_spettro.setInterval(INTERVALLO_TIMER_SPETTRO_MS)
        self.timer_spettro.timeout.connect(self.aggiorna_spettrogramma)
        self.timer_spettro.start()

    def cambia_id_fibra(self):
        nuovo_id_grezzo = self.campo_id_fibra.text().strip()
        # Sanifica il nome per usarlo come cartella: solo alfanumerici,
        # underscore e trattino
        nuovo_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in nuovo_id_grezzo)
        if not nuovo_id.strip("_"):
            nuovo_id = ID_FIBRA_DEFAULT
        self.campo_id_fibra.setText(nuovo_id)

        if nuovo_id == self.id_fibra:
            return

        self.id_fibra = nuovo_id
        self.testo_log.append(f"➜ Fibra in test cambiata: {self.id_fibra}")
        log.info(f"Fibra in test cambiata: {self.id_fibra}")

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
        # Cambiare classe mentre si sta registrando è ambiguo: fermiamo
        # sempre e chiediamo all'utente di premere di nuovo "Avvia" per la
        # nuova classe, così non si mescolano mai eventi di classi diverse.
        self.armato = False
        self.catturando_evento = False
        self.pos_impulso = 0
        self.aggiorna_interfaccia_bottone()
        log.info(f"Classe cambiata in: {self.classe_corrente}")

    def cambia_soglia(self, valore):
        self.soglia_trigger = float(valore)
        self.linea_soglia.setValue(valore)
        self._ferma_se_cattura_in_corso()

    def cambia_durata_cattura(self, valore_secondi):
        self.campioni_da_raccogliere = int(SAMPLE_RATE * valore_secondi)
        self._ferma_se_cattura_in_corso()

    def _ferma_se_cattura_in_corso(self):
        """Cambiare soglia o durata mentre una cattura (o un blocco di
        RUMORE_AMBIENTALE) è a metà lascerebbe lo stato inconsistente (es.
        un evento che parte con una durata target e finisce con un'altra).
        Più semplice e prevedibile fermare e far ripartire l'utente, come
        già facciamo cambiando classe."""
        if self.catturando_evento or self.pos_impulso > 0:
            self.catturando_evento = False
            self.pos_impulso = 0
            self.aggiorna_interfaccia_bottone()

    def _avvia_nuova_cattura(self):
        """Prealloca il buffer per la prossima cattura, con la dimensione
        esatta e corrente di campioni_da_raccogliere (letta al momento in
        cui la cattura INIZIA, non cambia più a metà cattura)."""
        self.buffer_impulso_array = np.empty(self.campioni_da_raccogliere, dtype=np.int16)
        self.pos_impulso = 0

    def _accumula_in_buffer_cattura(self, nuovi_campioni):
        """Scrive nuovi_campioni nel buffer di cattura preallocato (O(n) sul
        pacchetto, come per buffer_audio - non più O(n) su una lista Python
        che si allunga). Tronca l'eccesso se il pacchetto sfora la
        dimensione target. Restituisce True quando il buffer è pieno."""
        n = len(nuovi_campioni)
        fine = min(self.pos_impulso + n, self.campioni_da_raccogliere)
        quanti = fine - self.pos_impulso
        if quanti > 0:
            self.buffer_impulso_array[self.pos_impulso:fine] = nuovi_campioni[:quanti]
            self.pos_impulso = fine
        return self.pos_impulso >= self.campioni_da_raccogliere

    def cambia_modalita_cattura(self, attiva_soglia):
        self.usa_soglia = attiva_soglia
        self.spin_soglia.setEnabled(attiva_soglia)
        self.linea_soglia.setVisible(attiva_soglia)
        # Cambiare modalità mentre si è armati è ambiguo: fermiamo, come
        # quando si cambia classe.
        if self.armato:
            self.armato = False
            self.catturando_evento = False
            self.pos_impulso = 0
        self.aggiorna_interfaccia_bottone()

    def aggiorna_interfaccia_bottone(self):
        """Un solo pulsante per tutte le classi: arma/disarma l'ascolto.
        - RUMORE_AMBIENTALE + armato: registra a blocchi continui finché non fermi.
        - Altre classi + armato: resta in ascolto e cattura automaticamente
          ogni evento che supera la soglia, senza bisogno di ripremere il
          pulsante per ogni singolo evento.
        """
        if self.armato:
            self.btn_azione.setText("🛑 FERMA REGISTRAZIONE")
            self.btn_azione.setStyleSheet(
                "background-color: #ff4757; color: white; font-weight: bold; padding: 10px; border-radius: 5px;"
            )
            if self.classe_corrente == "RUMORE_AMBIENTALE":
                self.label_stato.setText("🔴 REGISTRAZIONE CONTINUA RUMORE IN CORSO...")
            elif self.catturando_evento:
                durata_s = self.campioni_da_raccogliere / SAMPLE_RATE
                self.label_stato.setText(f"💥 CATTURA IN CORSO: {self.classe_corrente} ({durata_s:.1f}s)...")
            elif not self.usa_soglia:
                self.label_stato.setText("⏳ AVVIO CATTURA...")
            else:
                self.label_stato.setText(
                    f"🎯 IN ASCOLTO: {self.classe_corrente} (soglia {int(self.soglia_trigger)})"
                )
            self.label_stato.setStyleSheet("color: #ff4757; font-weight: bold;")
        else:
            self.btn_azione.setText("▶ AVVIA REGISTRAZIONE")
            self.btn_azione.setStyleSheet(
                "background-color: #2ed573; color: white; font-weight: bold; padding: 10px; border-radius: 5px;"
            )
            self.label_stato.setText("IN ATTESA (premi Avvia Registrazione)")
            self.label_stato.setStyleSheet("color: #aaaaaa; font-weight: bold;")

    def gestisci_pressione_bottone(self):
        self.armato = not self.armato
        self.catturando_evento = False
        self.pos_impulso = 0
        self.aggiorna_interfaccia_bottone()

    @QtCore.Slot(np.ndarray)
    def gestisci_dati(self, nuovi_campioni):
        # --- SCRITTURA NEL BUFFER CIRCOLARE (O(n) sul pacchetto, non sul totale) ---
        n = len(nuovi_campioni)
        fine = self.write_pos + n
        if fine <= STORICO_CAMPIONI:
            self.buffer_audio[self.write_pos:fine] = nuovi_campioni
        else:
            primo_pezzo = STORICO_CAMPIONI - self.write_pos
            self.buffer_audio[self.write_pos:] = nuovi_campioni[:primo_pezzo]
            self.buffer_audio[: n - primo_pezzo] = nuovi_campioni[primo_pezzo:]
        self.write_pos = fine % STORICO_CAMPIONI

        # --- AGGIORNAMENTO INCREMENTALE SPETTROGRAMMA ---
        # Calcoliamo una nuova colonna FFT solo quando si sono accumulati
        # abbastanza campioni nuovi (HOP), non ad ogni pacchetto: la FFT su
        # 24.000 campioni costa troppo per farla ~94 volte al secondo, ma è
        # banale farla ~10 volte al secondo.
        self.campioni_totali_ricevuti += n
        self.campioni_da_ultimo_hop += n
        # IMPORTANTE: aspettiamo di avere un secondo intero di dati REALI
        # prima di calcolare la prima colonna. Se calcolassimo la FFT su una
        # finestra che contiene ancora zeri di riempimento iniziale, il salto
        # brusco zero->segnale genererebbe energia artificiale su TUTTE le
        # frequenze, dominando il calcolo del picco e schiacciando il segnale
        # vero sotto soglia.
        if self.campioni_totali_ricevuti >= NPERSEG_SPETTROGRAMMA:
            while self.campioni_da_ultimo_hop >= HOP_SPETTROGRAMMA_CAMPIONI:
                self.campioni_da_ultimo_hop -= HOP_SPETTROGRAMMA_CAMPIONI
                self._calcola_nuova_colonna_spettro()

        rms = np.sqrt(np.mean(nuovi_campioni.astype(np.float64) ** 2))
        self.label_rms_live.setText(f"Livello Rumore di Fondo (RMS): {int(rms)}")

        if not self.armato:
            return  # non armato: nessuna cattura, il pulsante è "Avvia Registrazione"

        if self.classe_corrente == "RUMORE_AMBIENTALE":
            # Registrazione continua a blocchi finché resta armato
            if self.pos_impulso == 0:
                self._avvia_nuova_cattura()
            if self._accumula_in_buffer_cattura(nuovi_campioni):
                self.salva_file()
                self.pos_impulso = 0
        else:
            # Modalità "soglia": resta armato e cattura ogni evento che la
            # supera, senza bisogno di ripremere il pulsante ogni volta.
            # Modalità "durata fissa" (usa_soglia=False): parte a catturare
            # subito, appena armato, senza aspettare nessuna soglia - pensata
            # per segnali lunghi e controllati come uno sweep in frequenza,
            # dove aspettare una soglia rischierebbe di tagliare l'inizio.
            if not self.catturando_evento:
                if (not self.usa_soglia) or rms > self.soglia_trigger:
                    self.catturando_evento = True
                    self._avvia_nuova_cattura()
                    self._accumula_in_buffer_cattura(nuovi_campioni)
                    self.aggiorna_interfaccia_bottone()
            else:
                if self._accumula_in_buffer_cattura(nuovi_campioni):
                    self.salva_file()
                    self.catturando_evento = False
                    self.pos_impulso = 0
                    if not self.usa_soglia:
                        # Cattura a durata fissa: una presa per pressione del
                        # pulsante, poi si ridisarma da sola (come uno scatto
                        # fotografico, non un ascolto continuo)
                        self.armato = False
                    self.aggiorna_interfaccia_bottone()

    def salva_file(self):
        cartella_dest = os.path.join(CARTELLA_DATASET, self.id_fibra, self.classe_corrente)
        os.makedirs(cartella_dest, exist_ok=True)

        timestamp = int(time.time() * 1000)
        nome_file = os.path.join(cartella_dest, f"campione_{timestamp}.wav")

        try:
            # Il buffer è già esattamente della dimensione giusta (nessun
            # troncamento necessario: lo fa già _accumula_in_buffer_cattura)
            with wave.open(nome_file, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(self.buffer_impulso_array.tobytes())

            msg = f"✓ Salvato: {self.id_fibra}/{self.classe_corrente}/campione_{timestamp}.wav"
            self.testo_log.append(msg)
            log.info(msg)
        except Exception as e:
            log.error(f"Errore salvataggio: {e}")

    def aggiorna_grafico(self):
        # Unico punto in cui "srotoliamo" il buffer circolare per il disegno:
        # avviene a FPS_GRAFICO Hz (es. 20 volte/s), non ad ogni pacchetto.
        dati_ordinati = np.concatenate(
            (self.buffer_audio[self.write_pos:], self.buffer_audio[: self.write_pos])
        )
        self.data_line.setData(self.asse_tempo, dati_ordinati)

    def _estrai_ultimi_campioni(self, n):
        """Estrae gli ultimi n campioni dal buffer circolare, terminando alla
        posizione di scrittura corrente. Gestisce il wraparound."""
        inizio = (self.write_pos - n) % STORICO_CAMPIONI
        if inizio < self.write_pos:
            return self.buffer_audio[inizio:self.write_pos]
        return np.concatenate((self.buffer_audio[inizio:], self.buffer_audio[:self.write_pos]))

    def _calcola_nuova_colonna_spettro(self):
        """Calcola UNA colonna dello spettrogramma (una FFT su una finestra di
        1 secondo) e la scrive nella prossima posizione del buffer circolare
        dell'immagine. Costo: una FFT ogni ~100ms, trascurabile per la CPU."""
        finestra = self._estrai_ultimi_campioni(NPERSEG_SPETTROGRAMMA).astype(np.float32)
        finestra_pesata = finestra * self.finestra_hann
        spettro = np.fft.rfft(finestra_pesata)
        ampiezza = np.abs(spettro[: self.bin_freq_max]) / NPERSEG_SPETTROGRAMMA
        # dB relativi alla piena scala di un int16 (32768)
        db = 20.0 * np.log10(ampiezza / 32768.0 + 1e-12)

        self.spettro_immagine[:, self.col_scrittura_spettro] = db
        self.col_scrittura_spettro = (self.col_scrittura_spettro + 1) % N_COLONNE_SPETTROGRAMMA

    def aggiorna_spettrogramma(self):
        # Come per il buffer audio: "srotoliamo" l'immagine circolare solo qui,
        # al ritmo del timer dedicato, non ad ogni colonna calcolata.
        immagine_lineare = np.concatenate(
            (
                self.spettro_immagine[:, self.col_scrittura_spettro:],
                self.spettro_immagine[:, : self.col_scrittura_spettro],
            ),
            axis=1,
        )
        # Livelli auto-adattivi: il "top" è vicino al picco attuale nel
        # buffer visibile (99° percentile, non il massimo assoluto: un
        # singolo valore anomalo in una colonna non deve rovinare il
        # contrasto di tutta l'immagine), il "bottom" è il picco meno la
        # dinamica scelta.
        top_db = float(np.percentile(immagine_lineare, 99.0))
        bottom_db = top_db - self.dinamica_db
        self.img_spettro.setLevels([bottom_db, top_db])
        # IMPORTANTE: passiamo il rect QUI, ad ogni chiamata, invece di
        # affidarci al setRect() fatto una volta sola in __init__ prima che
        # esistesse un'immagine. Se il rect viene impostato prima del primo
        # setImage(), pyqtgraph può non calcolare correttamente la
        # trasformazione pixel->coordinate, e l'item finisce per riportare
        # un bounding box sbagliato quando si fa auto-range (era questa la
        # causa del salto a "2.2ks" sull'asse temporale). Passandolo sempre
        # insieme ai dati, la mappatura è garantita corretta ad ogni frame.
        self.img_spettro.setImage(immagine_lineare.T, autoLevels=False, rect=self.rect_spettro)

    def cambia_dinamica_spettrogramma(self, valore_slider):
        self.dinamica_db = valore_slider / 10.0

    def aggiorna_label_rete(self):
        s = self.worker.stats
        avviso = ""
        if s.gap_sospetti > 0:
            avviso = f" ⚠ gap sospetti: {s.gap_sospetti}"
        if s.pacchetti_scartati > 0:
            avviso += f" ⚠ scartati: {s.pacchetti_scartati}"
        self.label_rete.setText(
            f"Rete: {s.pacchetti_ultimo_secondo} pacchetti/s totale {s.pacchetti_ricevuti}{avviso}"
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
    viewer = SagnacDatasetBuilder()
    viewer.show()
    sys.exit(app.exec())