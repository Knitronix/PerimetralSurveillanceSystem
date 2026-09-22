"""
main.py — WAKEUPeSTALTA
------------------------
Interrogatore + visualizzatore + orchestratore, in un unico file/processo
(stesso pattern di MLtoDL/0registratoreStalta.py e FREQUENCY DETECTION/main.py):
decide quando la fibra ascolta l'ambiente (STA/LTA, alla ricerca di un evento
vibrazionale forte) e quando svegliare la daisy chain per identificare quale
tappeto ha generato l'evento — e mostra dal vivo la forma d'onda e il
rapporto STA/LTA, con soglie ON/OFF regolabili a schermo, perché tararle
guardando solo numeri in console non basta a capire su che tipo di evento si
sta tarando.

Progetto separato da MLtoDL/ e FREQUENCY DETECTION/: non modifica quei
flussi. La logica di rilevamento (STA/LTA e protocollo f1/f2, Goertzel +
isteresi + macchina a stati, SPECS.MD §3) è DUPLICATA qui sotto invece di
essere richiamata come processo esterno — scelta esplicita per restare un
progetto autonomo. Occhio al rovescio della medaglia: se frequenze/soglie/
timing cambiano di là (config_condivisa.py, MLtoDL/0registratoreStalta.py),
vanno aggiornate a mano anche qui, non si aggiornano da sole.

Macchina a stati:
  AMBIENTE  --evento forte rilevato-->        RISVEGLIO
  RISVEGLIO --daisy chain sincronizzata-->    ARMATO
  ARMATO    --interruttore chiuso-->          ALLARME (resta ARMATO, resetta il timeout)
  ARMATO    --timeout senza chiusure-->       AMBIENTE

I tempi in Config sotto (attesa di boot, timeout di spegnimento) restano
PLACEHOLDER: le soglie STA/LTA invece si tarano ora dal vivo, dagli spinbox
nel pannello a destra.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

import shelly_control

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wakeup_stalta")


# ============================================================
# Rete (duplicato da FREQUENCY DETECTION/main.py)
# ============================================================
UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000
SECONDI_VISIBILI = 15
STORICO_CAMPIONI = SAMPLE_RATE * SECONDI_VISIBILI
INTERVALLO_ATTESO_PACCHETTO = CAMPIONI_PER_PACCHETTO / SAMPLE_RATE

FPS_GRAFICO = 20
INTERVALLO_TIMER_MS = int(1000 / FPS_GRAFICO)
INTERVALLO_TIMER_STATO_MS = 200

# ============================================================
# Protocollo f1/f2 (duplicato da config_condivisa.py e
# FREQUENCY DETECTION/main.py — NON importato apposta, vedi docstring sopra)
# ============================================================
F1_HZ = 4600.0
F2_HZ = 3200.0
T0_MS = 200.0
T1_MS = 50.0
TOLLERANZA_MS = 30.0

BLOCCO_GOERTZEL_MS = 20.0
BLOCCO_GOERTZEL_CAMPIONI = max(1, round(SAMPLE_RATE * BLOCCO_GOERTZEL_MS / 1000.0))

SOGLIA_ON_F1 = 0.000082
SOGLIA_OFF_F1 = 0.000053
SOGLIA_ON_F2 = 0.00056
SOGLIA_OFF_F2 = 0.000364

# --- STA/LTA (duplicato da MLtoDL/0registratoreStalta.py) ---
STA_FINESTRA_SEC_DEFAULT = 0.5
LTA_FINESTRA_SEC_DEFAULT = 3.0
STA_LTA_SOGLIA_ON_DEFAULT = 1.5
STA_LTA_SOGLIA_OFF_DEFAULT = 1.2

# L'LTA parte da un pavimento quasi zero: finché la EMA non ha "visto"
# abbastanza campioni reali il rapporto STA/LTA è artificialmente gonfiato
# (misurato dal vivo: valori 8-30 nei primi 1-2s, poi assestamento vero non
# prima di ~10-15s con lta_sec=3.0). Senza questa attesa, ogni avvio/rientro
# in AMBIENTE scatterebbe un risveglio falso sul solo transitorio - inutile
# per tarare (il pannello smetterebbe di aggiornarsi subito) e sbagliato in
# produzione (sveglierebbe i tappeti senza nessun evento vero).
ATTESA_STABILIZZAZIONE_STALTA_S = 10.0


# ============================================================
# CONFIG — le soglie STA/LTA sopra sono di partenza (si tarano dal vivo con
# gli spinbox); i tempi sotto restano PLACEHOLDER, da tarare insieme.
# ============================================================
@dataclass
class Config:
    ATTESA_BOOT_DAISY_CHAIN_S: float = 5.0  # TODO misurare (boot Arduino + primo ciclo)
    TIMEOUT_NESSUNA_CHIUSURA_MIN: float = 10.0  # TODO decidere insieme
    SMART_PLUG_HOST: str = "TODO-inserire-ip-plug"
    SMART_PLUG_CHANNEL: int = 0


class Stato(Enum):
    AMBIENTE = auto()
    RISVEGLIO = auto()
    ARMATO = auto()


COLORE_STATO = {
    Stato.AMBIENTE: "#5eb1ff",
    Stato.RISVEGLIO: "#e8a33d",
    Stato.ARMATO: "#00d2c4",
}
# Stessi colori in RGB, per costruire a runtime il badge di stato "a
# pillola" (sfondo tenue + testo/bordo pieno) senza doverne tenere una
# seconda tabella hardcoded in giro.
COLORE_STATO_RGB = {
    Stato.AMBIENTE: (94, 177, 255),
    Stato.RISVEGLIO: (232, 163, 61),
    Stato.ARMATO: (0, 210, 196),
}


# ============================================================
# Rilevamento f1/f2 — duplicato da FREQUENCY DETECTION/main.py
# (RilevatoreGoertzel + MacchinaStatoFrequenza + RilevatoreProtocolloF1F2),
# pure logica, indipendente dalla UI.
# ============================================================
class RilevatoreGoertzel:
    """Filtro di Goertzel su una singola frequenza nota: potenza normalizzata
    (0..1 = tono a piena scala int16), indipendente dalla dimensione del
    blocco."""

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
    per evitare sfarfallio sul confine di soglia. Misura anche la durata
    continuativa dello stato ON, restituita quando torna OFF perché il
    chiamante la classifichi (marcatore/slot/rumore)."""

    def __init__(self, soglia_on, soglia_off, durata_blocco_sec):
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off
        self.durata_blocco_sec = durata_blocco_sec
        self.attivo = False
        self.durata_on_sec = 0.0

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
    """Logica del protocollo f1 (clock/counter) / f2 (stato interruttore):

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
        self._marcatore_zero_visto = False
        self.ultima_potenza_f1 = 0.0
        self.ultima_potenza_f2 = 0.0

    def _classifica_durata_f1(self, durata_sec):
        if abs(durata_sec - self.t0_sec) <= self.tolleranza_sec:
            return "zero"
        if abs(durata_sec - self.t1_sec) <= self.tolleranza_sec:
            return "slot"
        return "rumore"

    def elabora_blocco(self, blocco):
        """Processa un blocco di BLOCCO_GOERTZEL_CAMPIONI campioni. Ritorna
        la lista di eventi generati (marcatori di zero, slot validi e durate
        scartate come rumore)."""
        potenza_f1 = self.goertzel_f1.potenza(blocco)
        potenza_f2 = self.goertzel_f2.potenza(blocco)
        self.ultima_potenza_f1 = potenza_f1
        self.ultima_potenza_f2 = potenza_f2

        eventi = []

        on_f1, durata_f1 = self.macchina_f1.aggiorna(potenza_f1)
        if on_f1:
            self.f2_attivo_durante_slot = False

        self.macchina_f2.aggiorna(potenza_f2)
        if self.macchina_f1.attivo and self.macchina_f2.attivo:
            self.f2_attivo_durante_slot = True

        if durata_f1 is not None:
            tipo = self._classifica_durata_f1(durata_f1)
            if tipo == "zero":
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
            else:
                eventi.append({"tipo": "rumore", "durata_s": durata_f1})

        return eventi


class RilevatoreStaLta:
    """Duplicato da MLtoDL/0registratoreStalta.py: STA/LTA in streaming via
    medie mobili esponenziali (EMA), aggiornate una volta per pacchetto
    invece che campione per campione."""

    def __init__(self, sample_rate, campioni_per_pacchetto, sta_sec, lta_sec, soglia_on, soglia_off):
        durata_pacchetto = campioni_per_pacchetto / sample_rate
        self.alpha_sta = min(1.0, durata_pacchetto / sta_sec)
        self.alpha_lta = min(1.0, durata_pacchetto / lta_sec)
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off
        self._floor_potenza = 1e-6
        self.sta = 0.0
        self.lta = self._floor_potenza
        self.in_evento = False
        self.ultimo_rapporto = 0.0

    def imposta_soglie(self, soglia_on, soglia_off):
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off

    def aggiorna(self, pacchetto_campioni: np.ndarray) -> bool:
        """Ritorna True nell'istante in cui il rapporto supera soglia_on
        (fronte di salita, non lo stato continuativo)."""
        potenza = float(np.mean(pacchetto_campioni.astype(np.float64) ** 2))
        self.sta += self.alpha_sta * (potenza - self.sta)
        self.lta += self.alpha_lta * (potenza - self.lta)
        self.lta = max(self.lta, self._floor_potenza)
        rapporto = self.sta / self.lta
        self.ultimo_rapporto = rapporto

        evento_iniziato = False
        if not self.in_evento:
            if rapporto > self.soglia_on:
                self.in_evento = True
                evento_iniziato = True
        elif rapporto < self.soglia_off:
            self.in_evento = False
        return evento_iniziato


# ============================================================
# UI: pattern riusati dal resto del progetto
# ============================================================
class SpinBoxSenzaRotella(QtWidgets.QDoubleSpinBox):
    """QDoubleSpinBox che ignora la rotella del mouse (vedi
    FREQUENCY DETECTION/main.py per il motivo: cambia soglie per sbaglio
    scorrendo il pannello, senza nemmeno il focus/click)."""

    def wheelEvent(self, event):
        event.ignore()


# ============================================================
# Design system minimo: una palette sola, riusata da QSS e dai punti che
# costruiscono stringhe di stile a runtime (badge di stato, pillola
# allarme) - per non finire con due tavolozze di colori scoordinate.
# ============================================================
COLORI = {
    "bg": "#14161a",
    "bg_elevato": "#1c1f24",
    "bg_input": "#232730",
    "bg_lista": "#101216",
    "bordo": "#2b2f37",
    "bordo_forte": "#3a3f48",
    "testo": "#e4e6eb",
    "testo_tenue": "#8b92a0",
    "accento": "#00d2c4",
    "accento_hover": "#33ddd1",
    "accento_testo_su": "#0a1210",
    "pericolo": "#ff4757",
}

STYLE_QSS = f"""
QMainWindow, QWidget {{
    background-color: {COLORI['bg']};
    color: {COLORI['testo']};
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
}}
QLabel {{ color: {COLORI['testo']}; background: transparent; }}

QGroupBox {{
    background-color: {COLORI['bg_elevato']};
    border: 1px solid {COLORI['bordo']};
    border-radius: 10px;
    margin-top: 16px;
    padding: 16px 12px 12px 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    color: {COLORI['accento']};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
}}

QPushButton {{
    background-color: {COLORI['bg_input']};
    border: 1px solid {COLORI['bordo_forte']};
    border-radius: 6px;
    padding: 8px 14px;
    color: {COLORI['testo']};
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: #33383f;
    border-color: {COLORI['accento']};
    color: {COLORI['accento']};
}}
QPushButton:pressed {{ background-color: #1c1f24; }}
QPushButton:disabled {{ color: {COLORI['testo_tenue']}; border-color: {COLORI['bordo']}; }}

QPushButton#Primario {{
    background-color: {COLORI['accento']};
    color: {COLORI['accento_testo_su']};
    border: none;
    font-weight: 800;
    font-size: 13px;
}}
QPushButton#Primario:hover {{
    background-color: {COLORI['accento_hover']};
    color: {COLORI['accento_testo_su']};
    border: none;
}}

QPushButton#Accento {{
    background-color: {COLORI['bg_input']};
    border: 1px solid {COLORI['accento']};
    color: {COLORI['accento']};
    font-weight: 700;
}}
QPushButton#Accento:hover {{
    background-color: #1f3a37;
    border-color: {COLORI['accento_hover']};
    color: {COLORI['accento_hover']};
}}

QDoubleSpinBox {{
    background-color: {COLORI['bg_input']};
    border: 1px solid {COLORI['bordo']};
    border-radius: 5px;
    padding: 4px 6px;
    color: {COLORI['testo']};
    selection-background-color: {COLORI['accento']};
}}
QDoubleSpinBox:focus {{ border: 1px solid {COLORI['accento']}; }}

QListWidget {{
    background-color: {COLORI['bg_lista']};
    border: 1px solid {COLORI['bordo']};
    border-radius: 8px;
    padding: 4px;
    font-family: 'Consolas', monospace;
    font-size: 11px;
}}
QListWidget::item {{ border: none; padding: 4px 6px; border-radius: 4px; color: {COLORI['testo_tenue']}; }}
QListWidget::item:hover {{ background-color: {COLORI['bg_elevato']}; }}

QTabWidget::pane {{
    border: 1px solid {COLORI['bordo']};
    border-radius: 10px;
    background-color: {COLORI['bg_elevato']};
    top: -1px;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {COLORI['testo_tenue']};
    padding: 10px 20px;
    margin-right: 4px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    background-color: {COLORI['bg_elevato']};
    color: {COLORI['accento']};
    border: 1px solid {COLORI['bordo']};
    border-bottom: none;
}}
QTabBar::tab:hover:!selected {{ color: {COLORI['testo']}; }}
QTabBar::tab:disabled {{ color: #454a52; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: {COLORI['bg']}; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {COLORI['bordo_forte']}; border-radius: 5px; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QFrame#Separatore {{ background-color: {COLORI['bordo']}; max-height: 1px; border: none; }}
"""

STILE_HEADER = f"background-color: {COLORI['bg_elevato']}; border: none; border-radius: 10px; padding: 10px 14px;"

STILE_NOTA_TENUE = f"color: {COLORI['testo_tenue']}; font-size: 11px; background: none;"


class NetworkWorker(QtCore.QThread):
    """Riceve i pacchetti UDP grezzi della fibra e li inoltra via segnale Qt.
    Stesso pattern di NetworkWorker in FREQUENCY DETECTION/main.py — un solo
    socket per tutto il processo: AMBIENTE e ARMATO non ascoltano mai
    insieme, quindi condividono la stessa porta senza doverla aprire/chiudere
    ad ogni cambio di stato."""

    nuovi_dati_signal = QtCore.Signal(np.ndarray)

    def __init__(self, ip, port, packet_size):
        super().__init__()
        self.ip = ip
        self.port = port
        self.packet_size = packet_size
        self.running = True

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
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as e:
                log.error(f"Errore socket: {e}")
                break
            if len(data) != bytes_attesi:
                continue
            self.nuovi_dati_signal.emit(np.frombuffer(data, dtype=np.int16))
        sock.close()


class SmartPlug:
    """Presa Shelly che alimenta la daisy chain, pilotata via API RPC HTTP
    locale (shelly_control.py — nessun cloud, nessun account)."""

    def __init__(self, host: str, channel: int = 0):
        self.host = host
        self.channel = channel

    def _host_configurato(self) -> bool:
        return not self.host.startswith("TODO")

    def accendi(self) -> None:
        if not self._host_configurato():
            log.info("SMART PLUG: host non ancora configurato (Config.SMART_PLUG_HOST), salto la chiamata")
            return
        try:
            shelly_control.turn_on(self.host, self.channel)
            log.info("SMART PLUG: ON (host=%s)", self.host)
        except Exception as e:
            log.error("SMART PLUG: accensione fallita (host=%s): %s", self.host, e)

    def spegni(self) -> None:
        if not self._host_configurato():
            return
        try:
            shelly_control.turn_off(self.host, self.channel)
            log.info("SMART PLUG: OFF (host=%s)", self.host)
        except Exception as e:
            log.error("SMART PLUG: spegnimento fallito (host=%s): %s", self.host, e)


class FinestraWakeupStalta(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        pg.setConfigOptions(antialias=False)

        self.config = Config()
        self.plug = SmartPlug(self.config.SMART_PLUG_HOST, self.config.SMART_PLUG_CHANNEL)
        self.stato = Stato.AMBIENTE
        # Finché non si preme "OK STALTA TARATO": un evento rilevato viene
        # solo LOGGATO ("avrei attivato RISVEGLIO"), non cambia mai stato né
        # tocca la smart plug - la tab 1 serve a guardare la soglia scattare
        # sui suoni di prova, non a pilotare davvero l'impianto.
        self._modalita_dry_run = True
        self._inizio_risveglio_s = 0.0
        self._ultima_chiusura_s = 0.0
        self.protocollo_tappeti: RilevatoreProtocolloF1F2 | None = None
        self._accumulo_tappeti = np.empty(0, dtype=np.int16)

        # --- buffer forma d'onda (sempre aggiornato, in qualunque stato) ---
        self.buffer_audio = np.zeros(STORICO_CAMPIONI, dtype=np.int16)
        self.write_pos = 0
        self.asse_tempo = np.linspace(-SECONDI_VISIBILI, 0, STORICO_CAMPIONI, dtype=np.float32)

        # --- buffer STA/LTA (aggiornato solo in AMBIENTE) ---
        self.n_storico_stalta = max(1, round(SECONDI_VISIBILI / INTERVALLO_ATTESO_PACCHETTO))
        self.storico_stalta = np.full(self.n_storico_stalta, np.nan, dtype=np.float32)
        self.write_pos_stalta = 0
        self.asse_tempo_stalta = np.linspace(-SECONDI_VISIBILI, 0, self.n_storico_stalta, dtype=np.float32)
        self.stalta_min_osservato = float("inf")
        self.stalta_max_osservato = float("-inf")

        self.rilevatore_stalta = RilevatoreStaLta(
            SAMPLE_RATE, CAMPIONI_PER_PACCHETTO,
            STA_FINESTRA_SEC_DEFAULT, LTA_FINESTRA_SEC_DEFAULT,
            STA_LTA_SOGLIA_ON_DEFAULT, STA_LTA_SOGLIA_OFF_DEFAULT,
        )
        self._tempo_ultimo_ingresso_ambiente = time.monotonic()

        self.setStyleSheet(STYLE_QSS)
        self.setWindowTitle("WAKEUPeSTALTA — orchestratore ambiente/tappeti")
        self.resize(1300, 850)

        self._costruisci_ui()
        self.cambia_soglie_stalta()  # inizializza label_soglia_attiva col valore di partenza

        self.worker = NetworkWorker(UDP_IP, UDP_PORT, CAMPIONI_PER_PACCHETTO)
        self.worker.nuovi_dati_signal.connect(self.gestisci_dati)
        self.worker.start()

        self.timer_grafico = QtCore.QTimer()
        self.timer_grafico.setInterval(INTERVALLO_TIMER_MS)
        self.timer_grafico.timeout.connect(self.aggiorna_grafico)
        self.timer_grafico.start()

        self.timer_stato = QtCore.QTimer()
        self.timer_stato.setInterval(INTERVALLO_TIMER_STATO_MS)
        self.timer_stato.timeout.connect(self.controlla_transizioni_temporali)
        self.timer_stato.start()

        self._aggiungi_log("Avviato in AMBIENTE, in ascolto su UDP %s:%s" % (UDP_IP, UDP_PORT))

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _costruisci_ui(self):
        centrale = QtWidgets.QWidget()
        self.setCentralWidget(centrale)
        layout_esterno = QtWidgets.QVBoxLayout(centrale)
        layout_esterno.setContentsMargins(12, 12, 12, 12)
        layout_esterno.setSpacing(10)

        layout_esterno.addWidget(self._crea_barra_stato())

        # GUI a fasi: fase 1 tara lo STA/LTA (onda + STA/LTA + soglie + log),
        # fase 2 è l'ascolto vero e proprio (solo onda + stato nella barra
        # sopra) - sbloccata solo premendo "OK STALTA TARATO" nella fase 1.
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._crea_tab_taratura(), "1 · Taratura STA/LTA")
        self.tabs.addTab(self._crea_tab_ascolto(), "2 · Ascolto")
        self.tabs.setTabEnabled(1, False)
        layout_esterno.addWidget(self.tabs, stretch=1)

    def _crea_barra_stato(self) -> QtWidgets.QWidget:
        """Stato e RIARMA FIBRA: fuori dalle tab, sempre visibili in entrambe
        le fasi (in fase 1 per leggere se scatta risveglio, in fase 2 perché
        è l'unica cosa che si vede oltre all'onda, per ora)."""
        barra = QtWidgets.QWidget()
        barra.setStyleSheet(STILE_HEADER)
        layout = QtWidgets.QHBoxLayout(barra)
        layout.setSpacing(14)

        titolo = QtWidgets.QLabel("WAKEUPeSTALTA")
        titolo.setStyleSheet(
            f"font-weight: 800; font-size: 15px; color: {COLORI['accento']}; letter-spacing: 0.5px;"
        )
        layout.addWidget(titolo)

        layout.addStretch(1)

        # Allarme testuale, tra il titolo e lo stato: visibile solo quando si
        # esce da AMBIENTE - "unknown" perché a questo punto la STA/LTA sa
        # solo che è successo qualcosa di forte, non ancora cosa (quello lo
        # scoprirebbe l'identificazione tappeti, quando ci arriva).
        self.label_allarme = QtWidgets.QLabel("⚠  UNKNOWN NOISE DETECTED")
        self.label_allarme.setStyleSheet(
            f"background-color: rgba(255, 71, 87, 40); color: {COLORI['pericolo']}; "
            "font-size: 13px; font-weight: 800; letter-spacing: 0.5px; "
            "border: 1px solid rgba(255, 71, 87, 120); border-radius: 14px; padding: 6px 14px;"
        )
        self.label_allarme.hide()
        layout.addWidget(self.label_allarme)

        self.label_stato = QtWidgets.QLabel(self.stato.name)
        self._applica_stile_stato()
        layout.addWidget(self.label_stato)

        btn_riarma = QtWidgets.QPushButton("⟲  RIARMA FIBRA")
        btn_riarma.setObjectName("Accento")
        btn_riarma.setToolTip(
            "Spegne la smart plug e torna subito a fibra in ascolto ambiente,"
            " senza aspettare il timeout - comodo mentre si tara facendo più prove"
        )
        btn_riarma.clicked.connect(lambda: self._torna_ad_ambiente("RIARMA FIBRA premuto"))
        layout.addWidget(btn_riarma)
        return barra

    def _crea_tab_taratura(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout_principale = QtWidgets.QHBoxLayout(tab)
        layout_principale.setSpacing(14)

        # --- colonna sinistra: forma d'onda + STA/LTA ---
        colonna_sinistra = QtWidgets.QVBoxLayout()
        colonna_sinistra.setSpacing(10)

        self.plot_widget_taratura = pg.PlotWidget()
        self.plot_widget_taratura.setBackground("#111111")
        self.plot_widget_taratura.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget_taratura.setLabel("bottom", "Tempo", units="s")
        self.plot_widget_taratura.setLabel("left", "Ampiezza")
        self.plot_widget_taratura.setTitle("Segnale audio (tempo reale)")
        self.curva_onda_taratura = self.plot_widget_taratura.plot(
            self.asse_tempo, self.buffer_audio, pen=pg.mkPen(color="#00d2c4", width=1.2)
        )
        self.plot_widget_taratura.setYRange(-20000, 20000, padding=0)
        self.plot_widget_taratura.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        colonna_sinistra.addWidget(self.plot_widget_taratura, stretch=3)

        self.plot_stalta = pg.PlotWidget()
        self.plot_stalta.setBackground("#111111")
        self.plot_stalta.showGrid(x=True, y=True, alpha=0.2)
        self.plot_stalta.setLabel("bottom", "Tempo", units="s")
        self.plot_stalta.setLabel("left", "Rapporto STA/LTA")
        self.plot_stalta.setTitle("Storico STA/LTA (linee tratteggiate = soglie ON/OFF)")
        self.plot_stalta.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        self.plot_stalta.setXLink(self.plot_widget_taratura)
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

        self.label_rete = QtWidgets.QLabel("Rete: in attesa di pacchetti...")
        self.label_rete.setStyleSheet(f"color: {COLORI['testo_tenue']}; font-family: 'Consolas', monospace; font-size: 11px;")
        colonna_sinistra.addWidget(self.label_rete)

        layout_principale.addLayout(colonna_sinistra, stretch=3)

        # --- colonna destra: STA/LTA + soglie + log + conferma ---
        contenitore_destra = QtWidgets.QWidget()
        colonna_destra = QtWidgets.QVBoxLayout(contenitore_destra)
        colonna_destra.setSpacing(8)

        card_stalta = QtWidgets.QGroupBox("STA/LTA — SOGLIE LIVE")
        layout_stalta = QtWidgets.QVBoxLayout(card_stalta)
        layout_stalta.setSpacing(6)
        self.label_stalta = QtWidgets.QLabel("STA/LTA: --")
        self.label_stalta.setStyleSheet(
            f"color: {COLORI['accento']}; font-family: 'Consolas', monospace; font-size: 16px; font-weight: 700;"
        )
        layout_stalta.addWidget(self.label_stalta)
        # Risposta esplicita a "quale soglia stiamo usando per detectare":
        # non solo dentro la stringa STA/LTA sopra, una riga dedicata.
        self.label_soglia_attiva = QtWidgets.QLabel("Soglia di rilevamento (ON): --")
        self.label_soglia_attiva.setStyleSheet(
            f"color: {COLORI['pericolo']}; font-family: 'Consolas', monospace; font-size: 12px; font-weight: 700;"
        )
        layout_stalta.addWidget(self.label_soglia_attiva)
        self.label_stalta_minmax = QtWidgets.QLabel("Min/Max osservati: --")
        self.label_stalta_minmax.setStyleSheet(
            f"color: {COLORI['testo_tenue']}; font-family: 'Consolas', monospace; font-size: 11px;"
        )
        layout_stalta.addWidget(self.label_stalta_minmax)
        btn_azzera_minmax = QtWidgets.QPushButton("Azzera min/max")
        btn_azzera_minmax.clicked.connect(self.azzera_minmax_stalta)
        layout_stalta.addWidget(btn_azzera_minmax)

        griglia_soglie = QtWidgets.QGridLayout()
        griglia_soglie.setVerticalSpacing(6)
        self.spin_soglia_on = self._crea_spin(1.0, 500.0, STA_LTA_SOGLIA_ON_DEFAULT, 0.1)
        self.spin_soglia_off = self._crea_spin(0.5, 500.0, STA_LTA_SOGLIA_OFF_DEFAULT, 0.1)
        self.spin_soglia_on.valueChanged.connect(self.cambia_soglie_stalta)
        self.spin_soglia_off.valueChanged.connect(self.cambia_soglie_stalta)
        griglia_soglie.addWidget(QtWidgets.QLabel("ON"), 0, 0)
        griglia_soglie.addWidget(self.spin_soglia_on, 0, 1)
        griglia_soglie.addWidget(QtWidgets.QLabel("OFF"), 1, 0)
        griglia_soglie.addWidget(self.spin_soglia_off, 1, 1)
        layout_stalta.addLayout(griglia_soglie)
        colonna_destra.addWidget(card_stalta)

        # Finestre STA/LTA: a differenza di ON/OFF sopra (soglie, si
        # applicano a caldo) cambiare la durata delle finestre richiede
        # ricostruire il rilevatore (gli alpha della EMA dipendono da
        # sta_sec/lta_sec calcolati una volta sola al costruttore) - serve
        # un "Applica" esplicito, non un aggiornamento automatico ad ogni
        # tocco dello spinbox. Card separata dalle soglie sopra: azione
        # diversa (ricostruisce il rilevatore invece di applicare a caldo),
        # merita un contenitore proprio invece di stare appesa in fondo alla
        # card ON/OFF con solo una riga a separarle.
        card_finestre = QtWidgets.QGroupBox("FINESTRE STA/LTA")
        layout_finestre = QtWidgets.QVBoxLayout(card_finestre)
        layout_finestre.setSpacing(6)
        label_finestre_nota = QtWidgets.QLabel("Richiede Applica: ricostruisce il rilevatore da zero")
        label_finestre_nota.setStyleSheet(STILE_NOTA_TENUE)
        layout_finestre.addWidget(label_finestre_nota)
        griglia_finestre = QtWidgets.QGridLayout()
        griglia_finestre.setVerticalSpacing(6)
        self.spin_finestra_sta = self._crea_spin(0.02, 5.0, STA_FINESTRA_SEC_DEFAULT, 0.02)
        self.spin_finestra_lta = self._crea_spin(0.5, 60.0, LTA_FINESTRA_SEC_DEFAULT, 0.5)
        griglia_finestre.addWidget(QtWidgets.QLabel("STA (s)"), 0, 0)
        griglia_finestre.addWidget(self.spin_finestra_sta, 0, 1)
        griglia_finestre.addWidget(QtWidgets.QLabel("LTA (s)"), 1, 0)
        griglia_finestre.addWidget(self.spin_finestra_lta, 1, 1)
        layout_finestre.addLayout(griglia_finestre)
        self.btn_applica_finestre = QtWidgets.QPushButton("Applica finestre STA/LTA")
        self.btn_applica_finestre.setToolTip(
            "Ricostruisce il rilevatore con le nuove durate - riparte da zero,"
            " quindi rientra nella stabilizzazione come a un nuovo avvio"
        )
        self.btn_applica_finestre.clicked.connect(self.applica_finestre_stalta)
        layout_finestre.addWidget(self.btn_applica_finestre)
        colonna_destra.addWidget(card_finestre)

        # Log eventi: qui si legge "Evento rilevato... => accensione smart
        # plug" quando scatta il risveglio - la prova, fatto un rumore, che
        # la soglia attuale lo riconosce (o silenzio, che NON deve comparire).
        card_log = QtWidgets.QGroupBox("LOG EVENTI")
        layout_log = QtWidgets.QVBoxLayout(card_log)
        self.lista_log = QtWidgets.QListWidget()
        # Le righe di log sono spesso più lunghe della card (spiegano il
        # motivo di ogni transizione): a capo invece di tagliate a bordo,
        # altrimenti la parte più importante del messaggio sparisce.
        self.lista_log.setWordWrap(True)
        self.lista_log.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        layout_log.addWidget(self.lista_log)
        colonna_destra.addWidget(card_log, stretch=1)

        self.btn_ok_tarato = QtWidgets.QPushButton("✓  OK STALTA TARATO — passa ad ASCOLTO")
        self.btn_ok_tarato.setObjectName("Primario")
        self.btn_ok_tarato.setMinimumHeight(44)
        self.btn_ok_tarato.clicked.connect(self._conferma_stalta_tarato)
        colonna_destra.addWidget(self.btn_ok_tarato)

        # Sostituisce il pulsante sopra dopo la conferma (vedi
        # _conferma_stalta_tarato): nascosta finché non si preme OK, poi resta
        # a spiegare perché gli spinbox sopra sono bloccati in sola lettura.
        self.label_tarato_confermato = QtWidgets.QLabel("✓  STA/LTA tarato — parametri in sola lettura")
        self.label_tarato_confermato.setAlignment(QtCore.Qt.AlignCenter)
        self.label_tarato_confermato.setStyleSheet(
            f"color: {COLORI['accento']}; font-weight: 700; font-size: 12px; padding: 8px;"
        )
        self.label_tarato_confermato.hide()
        colonna_destra.addWidget(self.label_tarato_confermato)

        scroll_destra = QtWidgets.QScrollArea()
        scroll_destra.setWidgetResizable(True)
        scroll_destra.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll_destra.setWidget(contenitore_destra)
        layout_principale.addWidget(scroll_destra, stretch=2)

        return tab

    def _crea_tab_ascolto(self) -> QtWidgets.QWidget:
        """Fase 2: onda + un pannello minimo con l'ultimo allarme, il
        conto alla rovescia del riarmo e il timeout stesso regolabile.
        Lo stato (AMBIENTE/RISVEGLIO/ARMATO) resta nella barra in alto,
        condivisa con la fase 1."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        self.plot_widget_ascolto = pg.PlotWidget()
        self.plot_widget_ascolto.setBackground("#111111")
        self.plot_widget_ascolto.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget_ascolto.setLabel("bottom", "Tempo", units="s")
        self.plot_widget_ascolto.setLabel("left", "Ampiezza")
        self.plot_widget_ascolto.setTitle("Segnale audio (tempo reale)")
        self.curva_onda_ascolto = self.plot_widget_ascolto.plot(
            self.asse_tempo, self.buffer_audio, pen=pg.mkPen(color="#00d2c4", width=1.2)
        )
        self.plot_widget_ascolto.setYRange(-20000, 20000, padding=0)
        self.plot_widget_ascolto.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        layout.addWidget(self.plot_widget_ascolto, stretch=1)

        pannello = QtWidgets.QGroupBox("STATO ASCOLTO")
        layout_pannello = QtWidgets.QHBoxLayout(pannello)

        colonna_allarme = QtWidgets.QVBoxLayout()
        colonna_allarme.setSpacing(4)
        self.label_ultimo_allarme = QtWidgets.QLabel("Ultimo allarme: --")
        self.label_ultimo_allarme.setStyleSheet(
            f"color: {COLORI['pericolo']}; font-family: 'Consolas', monospace; font-size: 12px; font-weight: 700;"
        )
        colonna_allarme.addWidget(self.label_ultimo_allarme)
        self.label_countdown_riarmo = QtWidgets.QLabel("Riarmo tra: --")
        self.label_countdown_riarmo.setStyleSheet(
            f"color: {COLORI['testo']}; font-family: 'Consolas', monospace; font-size: 12px;"
        )
        colonna_allarme.addWidget(self.label_countdown_riarmo)
        layout_pannello.addLayout(colonna_allarme, stretch=1)

        layout_pannello.addWidget(QtWidgets.QLabel("Timeout riarmo (min):"))
        self.spin_timeout_riarmo = self._crea_spin(0.5, 120.0, self.config.TIMEOUT_NESSUNA_CHIUSURA_MIN, 0.5)
        self.spin_timeout_riarmo.valueChanged.connect(self.cambia_timeout_riarmo)
        layout_pannello.addWidget(self.spin_timeout_riarmo)

        pannello.setMaximumWidth(560)
        layout_centrato = QtWidgets.QHBoxLayout()
        layout_centrato.addStretch(1)
        layout_centrato.addWidget(pannello)
        layout_centrato.addStretch(1)
        layout.addLayout(layout_centrato)
        return tab

    def cambia_timeout_riarmo(self) -> None:
        self.config.TIMEOUT_NESSUNA_CHIUSURA_MIN = self.spin_timeout_riarmo.value()

    def _conferma_stalta_tarato(self) -> None:
        self._modalita_dry_run = False
        self._aggiungi_log(
            f"STA/LTA tarato (ON={self.spin_soglia_on.value():.2f} OFF={self.spin_soglia_off.value():.2f}) "
            "-> passo alla modalità Ascolto, RISVEGLIO ora è reale"
        )
        # Una volta confermato non si torna indietro dalla GUI: bottone
        # sparito, spinbox/pulsante finestre bloccati in sola lettura -
        # altrimenti tornare su questa tab e toccare uno spinbox per sbaglio
        # farebbe scattare un risveglio vero (il dry-run non si riattiva
        # più, vedi _elabora_ambiente).
        self.btn_ok_tarato.hide()
        self.label_tarato_confermato.show()
        for controllo in (
            self.spin_soglia_on, self.spin_soglia_off,
            self.spin_finestra_sta, self.spin_finestra_lta,
            self.btn_applica_finestre,
        ):
            controllo.setEnabled(False)
        self.tabs.setTabEnabled(1, True)
        self.tabs.setCurrentIndex(1)

    @staticmethod
    def _crea_spin(minimo, massimo, valore, passo):
        spin = SpinBoxSenzaRotella()
        spin.setRange(minimo, massimo)
        spin.setSingleStep(passo)
        spin.setDecimals(2)
        spin.setValue(valore)
        return spin

    def _applica_stile_stato(self):
        colore = COLORE_STATO[self.stato]
        r, g, b = COLORE_STATO_RGB[self.stato]
        self.label_stato.setText(f"●  {self.stato.name}")
        self.label_stato.setStyleSheet(
            f"background-color: rgba({r}, {g}, {b}, 35); color: {colore}; font-size: 14px; "
            f"font-weight: 800; letter-spacing: 1px; border: 1px solid rgba({r}, {g}, {b}, 130); "
            "border-radius: 14px; padding: 6px 16px;"
        )
        self.label_allarme.setVisible(self.stato != Stato.AMBIENTE)

    def _aggiungi_log(self, testo: str):
        log.info(testo)
        self.lista_log.insertItem(0, f"[{time.strftime('%H:%M:%S')}] {testo}")
        while self.lista_log.count() > 200:
            self.lista_log.takeItem(self.lista_log.count() - 1)

    # ------------------------------------------------------------------
    # Soglie STA/LTA (regolabili a schermo)
    # ------------------------------------------------------------------
    def cambia_soglie_stalta(self):
        self.rilevatore_stalta.imposta_soglie(self.spin_soglia_on.value(), self.spin_soglia_off.value())
        self.linea_soglia_on.setValue(self.spin_soglia_on.value())
        self.linea_soglia_off.setValue(self.spin_soglia_off.value())
        self.label_soglia_attiva.setText(f"Soglia di rilevamento (ON): {self.spin_soglia_on.value():.2f}")

    def azzera_minmax_stalta(self):
        self.stalta_min_osservato = float("inf")
        self.stalta_max_osservato = float("-inf")
        self.label_stalta_minmax.setText("Min/Max osservati: -- (azzerato)")

    def applica_finestre_stalta(self) -> None:
        """Ricostruisce il rilevatore con le nuove durate STA/LTA (a
        differenza delle soglie ON/OFF, non si possono cambiare a caldo:
        gli alpha della EMA sono calcolati una volta al costruttore).
        Riparte da zero, quindi passa di nuovo dalla stabilizzazione -
        stesso motivo del cold-start, i valori vecchi non hanno senso con
        finestre diverse."""
        sta_sec = self.spin_finestra_sta.value()
        lta_sec = self.spin_finestra_lta.value()
        self.rilevatore_stalta = RilevatoreStaLta(
            SAMPLE_RATE, CAMPIONI_PER_PACCHETTO,
            sta_sec, lta_sec,
            self.spin_soglia_on.value(), self.spin_soglia_off.value(),
        )
        self._tempo_ultimo_ingresso_ambiente = time.monotonic()
        self.azzera_minmax_stalta()
        self._aggiungi_log(f"Finestre STA/LTA applicate: STA={sta_sec:.2f}s LTA={lta_sec:.2f}s (rilevatore riavviato)")

    # ------------------------------------------------------------------
    # Ricezione dati
    # ------------------------------------------------------------------
    @QtCore.Slot(np.ndarray)
    def gestisci_dati(self, nuovi_campioni: np.ndarray) -> None:
        n = len(nuovi_campioni)
        fine = self.write_pos + n
        if fine <= STORICO_CAMPIONI:
            self.buffer_audio[self.write_pos:fine] = nuovi_campioni
        else:
            primo_pezzo = STORICO_CAMPIONI - self.write_pos
            self.buffer_audio[self.write_pos:] = nuovi_campioni[:primo_pezzo]
            self.buffer_audio[: n - primo_pezzo] = nuovi_campioni[primo_pezzo:]
        self.write_pos = fine % STORICO_CAMPIONI

        rms = np.sqrt(np.mean(nuovi_campioni.astype(np.float64) ** 2))
        self.label_rete.setText(f"Rete: OK  RMS={int(rms)}")

        if self.stato == Stato.AMBIENTE:
            self._elabora_ambiente(nuovi_campioni)
        elif self.stato == Stato.ARMATO:
            self._elabora_tappeti(nuovi_campioni)
        # in RISVEGLIO: solo la forma d'onda sopra viene aggiornata, si
        # aspetta il boot della daisy chain prima di fidarsi del segnale

    def _elabora_ambiente(self, campioni: np.ndarray) -> None:
        evento_iniziato = self.rilevatore_stalta.aggiorna(campioni)
        rapporto = self.rilevatore_stalta.ultimo_rapporto
        secondi_rimanenti = ATTESA_STABILIZZAZIONE_STALTA_S - (time.monotonic() - self._tempo_ultimo_ingresso_ambiente)
        suffisso = f"  [stabilizzazione, ancora {secondi_rimanenti:.0f}s]" if secondi_rimanenti > 0 else ""
        self.label_stalta.setText(
            f"STA/LTA: {rapporto:.2f}  (ON={self.rilevatore_stalta.soglia_on:.2f} "
            f"OFF={self.rilevatore_stalta.soglia_off:.2f}){suffisso}"
        )

        self.storico_stalta[self.write_pos_stalta] = rapporto
        self.write_pos_stalta = (self.write_pos_stalta + 1) % self.n_storico_stalta

        if rapporto < self.stalta_min_osservato:
            self.stalta_min_osservato = rapporto
        if rapporto > self.stalta_max_osservato:
            self.stalta_max_osservato = rapporto
        self.label_stalta_minmax.setText(
            f"Min/Max osservati: {self.stalta_min_osservato:.2f} / {self.stalta_max_osservato:.2f}"
        )

        in_stabilizzazione = (time.monotonic() - self._tempo_ultimo_ingresso_ambiente) < ATTESA_STABILIZZAZIONE_STALTA_S
        if evento_iniziato and in_stabilizzazione:
            log.info("STA/LTA: trigger ignorato, ancora in stabilizzazione (transitorio EMA)")
        elif evento_iniziato and self._modalita_dry_run:
            self._aggiungi_log(
                f"Avrei attivato RISVEGLIO (STA/LTA={rapporto:.2f} > ON={self.rilevatore_stalta.soglia_on:.2f}) "
                "- solo lettura, in Taratura non cambio stato ne' tocco la smart plug"
            )
        elif evento_iniziato:
            self._vai_a_risveglio()

    def _vai_a_risveglio(self) -> None:
        rapporto = self.rilevatore_stalta.ultimo_rapporto
        soglia_on = self.rilevatore_stalta.soglia_on
        self._aggiungi_log(
            f"Evento rilevato (STA/LTA={rapporto:.2f} > ON={soglia_on:.2f}) => accensione smart plug"
        )
        self.label_ultimo_allarme.setText(f"Ultimo allarme: {time.strftime('%H:%M:%S')}")
        self.plug.accendi()
        self._accumulo_tappeti = np.empty(0, dtype=np.int16)
        self.protocollo_tappeti = RilevatoreProtocolloF1F2(
            SAMPLE_RATE, BLOCCO_GOERTZEL_CAMPIONI,
            F1_HZ, F2_HZ, T0_MS, T1_MS, TOLLERANZA_MS,
            SOGLIA_ON_F1, SOGLIA_OFF_F1, SOGLIA_ON_F2, SOGLIA_OFF_F2,
        )
        self._inizio_risveglio_s = time.monotonic()
        self.stato = Stato.RISVEGLIO
        self._applica_stile_stato()

    def _elabora_tappeti(self, campioni: np.ndarray) -> None:
        if self.protocollo_tappeti is None:
            return
        self._accumulo_tappeti = np.concatenate((self._accumulo_tappeti, campioni))
        while len(self._accumulo_tappeti) >= BLOCCO_GOERTZEL_CAMPIONI:
            blocco = self._accumulo_tappeti[:BLOCCO_GOERTZEL_CAMPIONI]
            self._accumulo_tappeti = self._accumulo_tappeti[BLOCCO_GOERTZEL_CAMPIONI:]
            for evento in self.protocollo_tappeti.elabora_blocco(blocco):
                if evento["tipo"] == "slot" and evento["stato"] == "chiuso":
                    self._ultima_chiusura_s = time.monotonic()
                    self._aggiungi_log(f"ALLARME: interruttore {evento['counter']} chiuso")

    # ------------------------------------------------------------------
    # Transizioni temporali (boot daisy chain, timeout spegnimento)
    # ------------------------------------------------------------------
    def controlla_transizioni_temporali(self) -> None:
        ora = time.monotonic()

        if self.stato == Stato.RISVEGLIO:
            if ora - self._inizio_risveglio_s >= self.config.ATTESA_BOOT_DAISY_CHAIN_S:
                self._ultima_chiusura_s = ora
                self._aggiungi_log("Daisy chain considerata sincronizzata -> ARMATO")
                self.stato = Stato.ARMATO
                self._applica_stile_stato()

        elif self.stato == Stato.ARMATO:
            timeout_s = self.config.TIMEOUT_NESSUNA_CHIUSURA_MIN * 60
            if ora - self._ultima_chiusura_s >= timeout_s:
                self._torna_ad_ambiente(f"nessuna chiusura da {self.config.TIMEOUT_NESSUNA_CHIUSURA_MIN:.0f} min")

        self._aggiorna_countdown_riarmo(ora)

    def _aggiorna_countdown_riarmo(self, ora: float) -> None:
        if self.stato == Stato.AMBIENTE:
            self.label_countdown_riarmo.setText("Riarmo tra: -- (già in ascolto ambiente)")
        elif self.stato == Stato.RISVEGLIO:
            self.label_countdown_riarmo.setText("Riarmo tra: -- (in attesa sync daisy chain)")
        else:  # ARMATO
            rimanenti_s = max(0.0, self.config.TIMEOUT_NESSUNA_CHIUSURA_MIN * 60 - (ora - self._ultima_chiusura_s))
            minuti, secondi = divmod(int(rimanenti_s), 60)
            self.label_countdown_riarmo.setText(f"Riarmo tra: {minuti:02d}:{secondi:02d}")

    def _torna_ad_ambiente(self, motivo: str) -> None:
        """Comune a timeout vero e al pulsante "Forza AMBIENTE": riparte da
        RilevatoreStaLta pulito invece di riusare sta/lta vecchi di minuti
        (stesso transitorio del cold-start, vedi ATTESA_STABILIZZAZIONE_STALTA_S)."""
        if self.stato == Stato.AMBIENTE:
            return
        self._aggiungi_log(f"{motivo} -> AMBIENTE")
        self.plug.spegni()
        self.protocollo_tappeti = None
        self.rilevatore_stalta = RilevatoreStaLta(
            SAMPLE_RATE, CAMPIONI_PER_PACCHETTO,
            STA_FINESTRA_SEC_DEFAULT, LTA_FINESTRA_SEC_DEFAULT,
            self.spin_soglia_on.value(), self.spin_soglia_off.value(),
        )
        self._tempo_ultimo_ingresso_ambiente = time.monotonic()
        self.stato = Stato.AMBIENTE
        self._applica_stile_stato()

    # ------------------------------------------------------------------
    # Grafica
    # ------------------------------------------------------------------
    def aggiorna_grafico(self) -> None:
        dati_ordinati = np.concatenate(
            (self.buffer_audio[self.write_pos:], self.buffer_audio[: self.write_pos])
        )
        self.curva_onda_taratura.setData(self.asse_tempo, dati_ordinati)
        self.curva_onda_ascolto.setData(self.asse_tempo, dati_ordinati)

        storico_ordinato = np.concatenate(
            (self.storico_stalta[self.write_pos_stalta:], self.storico_stalta[: self.write_pos_stalta])
        )
        self.linea_stalta.setData(self.asse_tempo_stalta, storico_ordinato)

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        self.worker.running = False
        self.plug.spegni()
        event.accept()


def main() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    finestra = FinestraWakeupStalta()
    finestra.show()
    app.exec()


if __name__ == "__main__":
    main()
