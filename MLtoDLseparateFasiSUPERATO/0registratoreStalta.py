"""
registratore_stalta.py
-----------------------
Variante di registratore.py (script originale) con queste differenze:
- il trigger di cattura non usa più un valore RMS fisso, ma un rilevatore
  STA/LTA (Short-Term/Long-Term Average), più sensibile a eventi deboli e
  adattivo a un rumore di fondo che cambia nel tempo (con tempo di riarmo
  per non spezzare in due lo stesso evento fisico);
- la fibra in test è unica e fissa (ID_FIBRA_DEFAULT), non più selezionabile
  da interfaccia: non serve più, essendo già stata scelta la fibra da usare;
- rimossa la modalità di cattura "a durata fissa" (usata in origine per gli
  sweep di calibrazione): non più necessaria, la tassonomia attuale non
  prevede più classi di calibrazione tipo "FISCHIO";
- aggiunti controllo clipping e log strutturato degli eventi (JSONL).

La classificazione automatica in tempo reale è stata spostata in uno script
separato e più leggero, riconoscimento_realtime.py (nessuna interfaccia
grafica, processo indipendente da questo).

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

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("registratore_stalta")

# --- CONFIGURAZIONE FISSA (invariata rispetto all'originale) ---
UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000
SECONDI_VISIBILI = 15
STORICO_CAMPIONI = SAMPLE_RATE * SECONDI_VISIBILI
CARTELLA_DATASET = "dataset_sensori_24kHz"
FILE_CONFIG = "config_soglie_ia.json"
ID_FIBRA_DEFAULT = "fibra1"

INTERVALLO_ATTESO_PACCHETTO = CAMPIONI_PER_PACCHETTO / SAMPLE_RATE
# Il dispositivo NON invia un flusso uniforme pacchetto-per-pacchetto:
# bufferizza internamente e scarica raffiche di ~16 pacchetti quasi
# simultanei, con una pausa strutturale di ~169ms tra una raffica e la
# successiva (misurato con Wireshark il 2026: intervalli 0.1695/0.1692/
# 0.1693s, coerenti con 16 × 254/24000 = 0.1693s). Non è un problema di
# rete né una perdita di dati: è il comportamento normale del dispositivo,
# che non possiamo modificare. La diagnostica va calibrata su QUESTO
# ritmo, non su un intervallo pacchetto-per-pacchetto teorico che il
# dispositivo non rispetta mai.
PERIODO_RAFFICA_TIPICO_SEC = 0.169
# Oltre questa soglia sospettiamo un'ANOMALIA vera (una raffica intera
# saltata, non solo la normale pausa tra raffiche): margine ~1.8× il
# periodo tipico, abbastanza per non scattare sulla normale cadenza a
# raffiche ma abbastanza stretto da accorgersi se ne manca una.
SOGLIA_GAP_ANOMALO_SEC = PERIODO_RAFFICA_TIPICO_SEC * 1.8
# Percentuale minima di pacchetti/secondo rispetto al teorico, sotto la
# quale sospettiamo una perdita di dati reale (non solo consegna a scatti).
# Utile perché un singolo controllo sul gap tra due pacchetti non basta:
# se il dispositivo drifta leggermente nel proprio ritmo di raffica, la
# soglia sopra potrebbe non scattare pur perdendo qualche pacchetto per
# raffica. Il conteggio aggregato su 1 secondo intero è un controllo
# indipendente e complementare.
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

# --- PARAMETRI STA/LTA (nuovi) ---
# Finestra breve: deve essere abbastanza corta da reagire in fretta
# all'inizio di un evento, ma non così corta da inseguire il rumore
# campione-per-campione.
STA_FINESTRA_SEC = 0.1
# Finestra lunga: stima del "rumore di fondo attuale". Deve essere
# abbastanza lunga da non farsi influenzare da un singolo evento, ma
# abbastanza corta da adattarsi a condizioni che cambiano nell'arco di
# minuti (es. vento che aumenta).
LTA_FINESTRA_SEC = 3.0
# Isteresi: soglia di apertura più alta della soglia di chiusura, per
# evitare che il trigger si apra e chiuda a scatti sullo stesso evento
# quando il rapporto oscilla vicino alla soglia.
STA_LTA_SOGLIA_ON_DEFAULT = 1.55
STA_LTA_SOGLIA_OFF_DEFAULT = 1.2
# Contesto prima del trigger: lo STA/LTA scatta con un piccolo ritardo
# rispetto all'inizio fisico dell'evento (serve tempo perché la STA salga).
# Recuperiamo questo contesto dal buffer circolare già esistente, che
# contiene comunque gli ultimi SECONDI_VISIBILI secondi.
PRE_TRIGGER_SEC = 0.3
# Tempo minimo sotto la soglia OFF prima di considerare l'evento chiuso
# (altrimenti un singolo pacchetto sotto soglia in mezzo a un evento
# lungo lo spezzerebbe in due catture separate).
ISTERESI_CHIUSURA_SEC = 0.25
# Durata massima di un evento catturato (sicurezza: evita che un rumore
# prolungato saturi il buffer all'infinito).
DURATA_MASSIMA_EVENTO_SEC = 4.0
# Dimensione di ogni blocco salvato durante la registrazione continua di
# RUMORE_AMBIENTALE (l'unica classe catturata senza trigger, a blocchi
# regolari finché resti armato).
DURATA_BLOCCO_RUMORE_SEC = 1.5
# Tempo di riarmo: dopo la chiusura di un evento, per questo tempo si
# ignorano nuovi trigger. Senza questo, un evento che oscilla vicino alla
# soglia OFF (es. una corsa con passi ravvicinati) rischia di essere
# spezzato in più catture separate dello stesso evento fisico.
TEMPO_RIARMO_SEC = 1.0
# Soglia di clipping: quanto vicino al fondo scala di un int16 (±32768)
# consideriamo un campione "clippato". Un evento con clipping è un
# campione di training rovinato (la forma d'onda reale viene troncata):
# lo segnaliamo invece di salvarlo silenziosamente come se fosse pulito.
SOGLIA_CLIPPING = 32000
FILE_LOG_EVENTI = "eventi_log.jsonl"
# File tramite cui questo programma comunica le soglie STA/LTA correnti a
# processi esterni (es. riconoscimento_realtime.py) senza doverle ricopiare
# a mano ogni volta che vengono ricalibrate qui.
FILE_SOGLIE_STALTA = "soglie_stalta.json"


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
    Approssimazione corretta perché un pacchetto (~10.6ms a 24kHz/254
    campioni) è molto più corto sia della finestra STA (100ms) sia della
    finestra LTA (3s): trattarlo come un unico "super-campione" di potenza
    media introduce un errore trascurabile e costa O(1) per pacchetto
    invece di un loop su ogni campione.
    """

    def __init__(self, sample_rate, campioni_per_pacchetto,
                 sta_sec=STA_FINESTRA_SEC, lta_sec=LTA_FINESTRA_SEC,
                 soglia_on=STA_LTA_SOGLIA_ON_DEFAULT, soglia_off=STA_LTA_SOGLIA_OFF_DEFAULT):
        durata_pacchetto = campioni_per_pacchetto / sample_rate
        # alpha = quanto peso ha il nuovo pacchetto nell'aggiornare la media;
        # clip a 1.0 nel caso limite di finestre più corte di un pacchetto.
        self.alpha_sta = min(1.0, durata_pacchetto / sta_sec)
        self.alpha_lta = min(1.0, durata_pacchetto / lta_sec)

        self.soglia_on = soglia_on
        self.soglia_off = soglia_off

        # Floor piccolo ma non nullo: evita divisioni per zero quando il
        # segnale è a riposo assoluto (es. simulazioni/silenzio digitale).
        self._floor_potenza = 1e-6
        self.sta = 0.0
        self.lta = self._floor_potenza

        self.in_evento = False
        self.tempo_sotto_soglia_off = 0.0
        self.durata_pacchetto = durata_pacchetto

        self.ultimo_rapporto = 0.0

    def aggiorna(self, pacchetto_campioni):
        """
        Da chiamare una volta per ogni pacchetto ricevuto.
        Ritorna una tupla (evento_appena_iniziato, evento_appena_finito).
        Lo stato self.in_evento riflette lo stato corrente in ogni momento.
        """
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
    """Invariato rispetto all'originale: nessuna logica di rilevamento qui,
    solo ricezione e inoltro dei pacchetti grezzi."""

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
            # Sopra questa soglia non è la normale pausa tra raffiche (che è
            # attesa e innocua): è abbastanza lunga da far sospettare che
            # un'intera raffica sia mancata.
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

        self.buffer_audio = np.zeros(STORICO_CAMPIONI, dtype=np.int16)
        self.write_pos = 0
        self.asse_tempo = np.linspace(-SECONDI_VISIBILI, 0, STORICO_CAMPIONI, dtype=np.float32)

        # --- STORICO DEL RAPPORTO STA/LTA ---
        # Un pacchetto = un aggiornamento del rapporto (non un aggiornamento
        # per campione), quindi lo storico ha la sua granularità: un valore
        # ogni ~10.6ms, non uno ogni 1/24000s. Stesso principio del buffer
        # audio (buffer circolare + puntatore, srotolato solo al momento del
        # disegno), per non ricopiare l'array ad ogni pacchetto.
        self.n_storico_stalta = max(1, round(SECONDI_VISIBILI / INTERVALLO_ATTESO_PACCHETTO))
        self.storico_stalta = np.full(self.n_storico_stalta, np.nan, dtype=np.float32)
        self.write_pos_stalta = 0
        self.asse_tempo_stalta = np.linspace(-SECONDI_VISIBILI, 0, self.n_storico_stalta, dtype=np.float32)

        # Min/max osservati da quando è stato premuto "Azzera min/max" (o
        # dall'avvio): risolve il problema di dover leggere un numero che
        # cambia troppo in fretta per essere letto a occhio in tempo reale.
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

        # --- RILEVATORE STA/LTA (nuovo, al posto della soglia RMS fissa) ---
        self.rilevatore = RilevatoreStaLta(SAMPLE_RATE, CAMPIONI_PER_PACCHETTO)

        # Tempo di riarmo: timestamp (time.monotonic) dell'ultima chiusura
        # evento, per ignorare nuovi trigger troppo ravvicinati.
        self._tempo_ultima_chiusura_evento = -1e9

        self.path_log_eventi = os.path.join(CARTELLA_DATASET, FILE_LOG_EVENTI)

        self.armato = False
        self.catturando_evento = False

        # Buffer di cattura a dimensione massima prealloccata (invece che
        # a dimensione fissa nota in anticipo, perché con STA/LTA la durata
        # di un evento non è nota a priori: può chiudersi prima o dopo).
        self.campioni_pre_trigger = int(SAMPLE_RATE * PRE_TRIGGER_SEC)
        self.campioni_massimi_evento = int(SAMPLE_RATE * DURATA_MASSIMA_EVENTO_SEC) + self.campioni_pre_trigger
        self.buffer_impulso_array = np.zeros(self.campioni_massimi_evento, dtype=np.int16)
        self.pos_impulso = 0

        # Dimensione di ogni blocco salvato durante la registrazione
        # continua di RUMORE_AMBIENTALE (unico caso senza trigger STA/LTA:
        # qui si vuole proprio catturare rumore di fondo a prescindere).
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

        colonna_sinistra.addWidget(self.plot_spettro, stretch=3)

        # --- GRAFICO STORICO STA/LTA ---
        # Risponde direttamente al problema "il numero cambia troppo in
        # fretta per leggerlo a occhio": qui il rapporto resta visibile nel
        # tempo, con le soglie ON/OFF disegnate come riferimento, così si
        # vede a colpo d'occhio quanto un evento le supera e per quanto.
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

        colonna_sinistra.addWidget(self.plot_stalta, stretch=2)

        riga_minmax = QtWidgets.QHBoxLayout()
        self.label_stalta_minmax = QtWidgets.QLabel("Min/Max osservati: --")
        self.label_stalta_minmax.setStyleSheet("color: #aaaaaa; font-family: monospace; font-size: 11px;")
        riga_minmax.addWidget(self.label_stalta_minmax)
        btn_azzera_minmax = QtWidgets.QPushButton("Azzera min/max")
        btn_azzera_minmax.clicked.connect(self.azzera_minmax_stalta)
        riga_minmax.addWidget(btn_azzera_minmax)
        colonna_sinistra.addLayout(riga_minmax)

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

        # Diagnostica STA/LTA in tempo reale: fondamentale per calibrare le
        # soglie ON/OFF prima di iniziare la raccolta massiva (vedi rischio
        # "STA/LTA troppo sensibile" nel documento di planning).
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

        label_fibra = QtWidgets.QLabel(f"Fibra: {ID_FIBRA_DEFAULT}")
        label_fibra.setStyleSheet("color: #777777; font-family: monospace; font-size: 11px;")
        colonna_destra.addWidget(label_fibra)
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

        # --- Controlli STA/LTA (unica modalità di cattura) ---
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

        self.timer_spettro = QtCore.QTimer()
        self.timer_spettro.setInterval(INTERVALLO_TIMER_SPETTRO_MS)
        self.timer_spettro.timeout.connect(self.aggiorna_spettrogramma)
        self.timer_spettro.start()

        # Scrittura iniziale del file soglie, così esiste fin da subito
        # anche prima che l'utente tocchi gli spinbox.
        self._salva_soglie_su_file()

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
        self._salva_soglie_su_file()
        self._ferma_se_cattura_in_corso()

    def _salva_soglie_su_file(self):
        """Scrive le soglie ON/OFF correnti su un piccolo file JSON, letto
        all'avvio da riconoscimento_realtime.py (processo separato). Così
        una ricalibrazione fatta qui si riflette lì senza copia manuale —
        a costo di dover riavviare quel processo per ricaricarle (non
        rilette in tempo reale, per tenere quel processo il più semplice e
        leggero possibile)."""
        try:
            with open(FILE_SOGLIE_STALTA, "w", encoding="utf-8") as f:
                json.dump({
                    "soglia_on": self.spin_soglia_on.value(),
                    "soglia_off": self.spin_soglia_off.value(),
                }, f)
        except Exception as e:
            log.warning(f"Impossibile salvare {FILE_SOGLIE_STALTA}: {e}")

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
            self.btn_azione.setText("🛑 FERMA REGISTRAZIONE")
            self.btn_azione.setStyleSheet(
                "background-color: #ff4757; color: white; font-weight: bold; padding: 10px; border-radius: 5px;"
            )
            if self.classe_corrente == "RUMORE_AMBIENTALE":
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
        n = len(nuovi_campioni)
        fine = self.write_pos + n
        if fine <= STORICO_CAMPIONI:
            self.buffer_audio[self.write_pos:fine] = nuovi_campioni
        else:
            primo_pezzo = STORICO_CAMPIONI - self.write_pos
            self.buffer_audio[self.write_pos:] = nuovi_campioni[:primo_pezzo]
            self.buffer_audio[: n - primo_pezzo] = nuovi_campioni[primo_pezzo:]
        self.write_pos = fine % STORICO_CAMPIONI

        self.campioni_totali_ricevuti += n
        self.campioni_da_ultimo_hop += n
        if self.campioni_totali_ricevuti >= NPERSEG_SPETTROGRAMMA:
            while self.campioni_da_ultimo_hop >= HOP_SPETTROGRAMMA_CAMPIONI:
                self.campioni_da_ultimo_hop -= HOP_SPETTROGRAMMA_CAMPIONI
                self._calcola_nuova_colonna_spettro()

        rms = np.sqrt(np.mean(nuovi_campioni.astype(np.float64) ** 2))
        self.label_rms_live.setText(f"Livello Rumore di Fondo (RMS): {int(rms)}")

        # --- Aggiornamento STA/LTA: sempre attivo, non solo quando armato,
        # così il rapporto mostrato in UI è utile per calibrare le soglie
        # anche prima di iniziare a registrare. ---
        evento_iniziato, evento_finito = self.rilevatore.aggiorna(nuovi_campioni)
        rapporto = self.rilevatore.ultimo_rapporto
        self.label_stalta.setText(
            f"STA/LTA: {rapporto:.2f}  (ON={self.rilevatore.soglia_on:.1f} OFF={self.rilevatore.soglia_off:.1f})"
        )

        # Storico (buffer circolare, srotolato solo al momento del disegno
        # in aggiorna_grafico, non qui — stesso principio del buffer audio).
        self.storico_stalta[self.write_pos_stalta] = rapporto
        self.write_pos_stalta = (self.write_pos_stalta + 1) % self.n_storico_stalta

        # Min/max osservati dall'ultimo reset: qui il costo è trascurabile
        # (due confronti per pacchetto) e risolve esattamente il problema di
        # dover leggere a occhio un numero che cambia troppo in fretta.
        if rapporto < self.stalta_min_osservato:
            self.stalta_min_osservato = rapporto
        if rapporto > self.stalta_max_osservato:
            self.stalta_max_osservato = rapporto
        self.label_stalta_minmax.setText(
            f"Min/Max osservati: {self.stalta_min_osservato:.2f} / {self.stalta_max_osservato:.2f}"
        )

        if not self.armato:
            return

        if self.classe_corrente == "RUMORE_AMBIENTALE":
            # Registrazione continua a blocchi: qui non serve trigger, si
            # vuole proprio catturare rumore di fondo a prescindere.
            if self.pos_impulso == 0:
                self._avvia_nuova_cattura(con_pre_trigger=False)
            if self._accumula_in_buffer_cattura(nuovi_campioni, limite=self.campioni_blocco_rumore_continuo):
                self.salva_file(int(self.pos_impulso))
                self.pos_impulso = 0
            return

        # --- Cattura a evento, con trigger STA/LTA ---
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

    def _avvia_nuova_cattura(self, con_pre_trigger):
        """Prealloca/azzera il puntatore del buffer di cattura. Se
        con_pre_trigger è True, precarica i campioni immediatamente
        precedenti al trigger (recuperati dal buffer circolare, che li ha
        già in memoria) così l'evento salvato include il suo inizio reale
        e non solo la parte successiva al momento in cui STA/LTA ha
        scattato."""
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

    def salva_file(self, n_campioni_validi):
        cartella_dest = os.path.join(CARTELLA_DATASET, self.id_fibra, self.classe_corrente)
        os.makedirs(cartella_dest, exist_ok=True)

        timestamp_ms = int(time.time() * 1000)
        nome_file_base = f"campione_{timestamp_ms}.wav"
        nome_file = os.path.join(cartella_dest, nome_file_base)

        try:
            dati_validi = self.buffer_impulso_array[:n_campioni_validi]

            # Controllo clipping: NON alteriamo mai il WAV grezzo (deve
            # restare fedele al segnale originale), ci limitiamo a
            # segnalarlo nel log strutturato così un campione clippato può
            # essere escluso o rivisto in fase di training invece di essere
            # usato come se fosse pulito.
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
            self._scrivi_log_evento(record)
        except Exception as e:
            log.error(f"Errore salvataggio: {e}")

    def _scrivi_log_evento(self, record):
        """Aggiunge una riga JSON al log strutturato degli eventi. Formato
        JSONL (un oggetto JSON per riga) per poterlo leggere in streaming
        senza dover parsare l'intero file, e per poterci scrivere in append
        senza dover riscrivere tutto ad ogni evento."""
        try:
            with open(self.path_log_eventi, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"Errore scrittura log eventi: {e}")

    def aggiorna_grafico(self):
        dati_ordinati = np.concatenate(
            (self.buffer_audio[self.write_pos:], self.buffer_audio[: self.write_pos])
        )
        self.data_line.setData(self.asse_tempo, dati_ordinati)

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