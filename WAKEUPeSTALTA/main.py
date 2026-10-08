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
import math
import socket
import time
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

import shelly_control
from pannello_timer import PannelloTimer
from tema import (COLORI, FONT_MONO, SPAZIO, STYLE_QSS, T_VALORE, Stepper, applica_palette,
                  crea_card, etichetta, legenda_linea, rgba, separatore, stile_chip, stile_plot)
from vista_impianto import VistaImpianto

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
ATTESA_STABILIZZAZIONE_STALTA_S = 1.0

# Se la smart plug non risponde (RPC falso/timeout, IP cambiato da DHCP...)
# non si deve procedere come se la daisy chain fosse alimentata: si resta in
# RISVEGLIO e si riprova a intervalli, invece di martellare l'host ogni 200ms
# (tick di controlla_transizioni_temporali).
RITARDO_RETRY_PLUG_S = 2.0


# ============================================================
# CONFIG — le soglie STA/LTA sopra sono di partenza (si tarano dal vivo con
# gli spinbox); i tempi sotto restano PLACEHOLDER, da tarare insieme.
# ============================================================
@dataclass
class Config:
    ATTESA_BOOT_DAISY_CHAIN_S: float = 2.0  # abbassato per ora, TODO misurare davvero (boot Arduino + primo ciclo)
    # Timeout inattività: si resetta ad ogni chiusura. Ottimizza il caso
    # normale (evento reale breve, poi silenzio) - si torna ad AMBIENTE
    # appena finisce l'attività, non dopo un tempo fisso.
    TIMEOUT_INATTIVITA_MIN: float = 2.0  # abbassato per le sessioni di test, TODO decidere il valore reale insieme
    # Timeout massimo: a differenza di quello sopra NON si resetta mai - è la
    # rete di sicurezza per il caso patologico (sensore bloccato chiuso,
    # rumore continuo) in cui il timeout di inattività non scatterebbe mai da
    # solo. float('inf') = "mai" (disattivato): nessun tetto, resta armato
    # finché non arriva un'inattività vera o un riarmo manuale - scelta
    # esplicita per un cliente che preferisce non avere un limite di tempo
    # prevedibile su quanto resta armato, vedi cambia_timeout_massimo.
    TIMEOUT_MASSIMO_MIN: float = 5.0  # TODO decidere il valore reale insieme
    SMART_PLUG_HOST: str = "192.168.1.2"
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

# Tetto a quanti "Sensore N" mostrare in tab Ascolto: il counter di
# RilevatoreProtocolloF1F2 non ha un limite proprio (sale finché non arriva
# un marcatore di zero) - se in ARMATO non c'è un vero segnale daisy chain
# (rumore/silenzio sulla fibra), il counter può salire a valori assurdi
# (es. 178) senza mai resettarsi. Mostrarne uno per ogni valore visto
# creerebbe widget senza fine; un pool fisso, ordinato per numero (i sensori
# veri 1/2 restano sempre in cima), evita sia l'esplosione di widget sia il
# pattern crea/cancella dinamico che causava il crash "already deleted".
MAX_TAPPETI_VISUALIZZATI = 8


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
# Design system: palette, QSS e componenti condivisi con pannello_timer.py
# e vista_impianto.py - tutto in tema.py, un posto solo.
# ============================================================


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

    def imposta_host(self, host: str) -> None:
        self.host = host.strip()

    def _host_configurato(self) -> bool:
        return not self.host.startswith("TODO")

    def accendi(self) -> bool:
        """Ritorna True solo se la chiamata RPC è andata a buon fine: il
        chiamante (_tenta_accensione_plug) deve saperlo, per non far partire
        il boot della daisy chain su una presa che non si è mai accesa."""
        if not self._host_configurato():
            log.info("SMART PLUG: host non ancora configurato (Config.SMART_PLUG_HOST), salto la chiamata")
            return False
        try:
            shelly_control.turn_on(self.host, self.channel)
            log.info("SMART PLUG: ON (host=%s)", self.host)
            return True
        except Exception as e:
            log.error("SMART PLUG: accensione fallita (host=%s): %s", self.host, e)
            return False

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
        self._inizio_armato_s = 0.0
        # Vedi _tenta_accensione_plug: finché la smart plug non confirma
        # l'accensione, il boot della daisy chain non parte - si resta in
        # RISVEGLIO e si riprova.
        self._plug_confermato = False
        self._prossimo_tentativo_plug_s = 0.0
        self.protocollo_tappeti: RilevatoreProtocolloF1F2 | None = None
        self._accumulo_tappeti = np.empty(0, dtype=np.int16)
        self.stato_tappeti: dict[int, bool] = {}  # counter -> chiuso?
        self._visti_questo_ciclo: set[int] = set()  # counter visti nel ciclo daisy chain in corso

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
        self.setWindowTitle("FIBRA&TAPPETI")
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
        centrale.setObjectName("Radice")
        self.setCentralWidget(centrale)
        layout_esterno = QtWidgets.QVBoxLayout(centrale)
        layout_esterno.setContentsMargins(14, 12, 14, 12)
        layout_esterno.setSpacing(10)

        layout_esterno.addWidget(self._crea_barra_stato())

        # GUI a fasi: fase 1 tara lo STA/LTA (onda + STA/LTA + soglie + log),
        # fase 2 è l'ascolto vero e proprio (onda + timer + tappeti), fase 3
        # è la vista impianto per il cliente (timer compatti + anello e
        # tappeti colorati) - 2 e 3 sbloccate solo premendo "OK STALTA
        # TARATO" nella fase 1: prima di quel momento il sistema è ancora in
        # dry-run, non ha senso mostrare al cliente uno stato che non
        # corrisponde a niente di reale.
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)  # niente riga sotto le tab
        self.tabs.addTab(self._crea_tab_taratura(), "1 · Taratura STA/LTA")
        self.tabs.addTab(self._crea_tab_ascolto(), "2 · Pannello di controllo")
        self.tabs.addTab(self._crea_tab_vista_impianto(), "3 · Pannello di visualizzazione")
        self.tabs.setTabEnabled(1, False)
        self.tabs.setTabEnabled(2, False)
        # RESET vive solo nell'header, uguale in tutte e tre le tab (la vista
        # impianto in tab 3 non ne ha più uno suo).
        layout_esterno.addWidget(self.tabs, stretch=1)

    def _crea_barra_stato(self) -> QtWidgets.QWidget:
        """Header fuori dalle tab, sempre visibile.

        Riga 1, a destra:  [RISVEGLIO]  ( ASCOLTO ) ──▶ ( ARMATO )  [RESET]
          - le due fasi del flusso sono SEMPRE visibili: quella attiva è
            piena e colorata, l'altra spenta (contorno grigio);
          - RISVEGLIO (transizione, 2 s di boot daisy chain) compare solo
            mentre è in corso, a sinistra di tutto, arancione e pulsante; in
            quel momento anche la freccia tra le due fasi diventa arancione.
        Riga 2 (solo se c'è qualcosa da dire): avvisi ed errori in rosso -
        rumore rilevato, smart plug che non risponde. Vedi _imposta_avviso.
        """
        barra = QtWidgets.QFrame()
        # objectName + selettore in tema.STYLE_QSS: uno stile senza
        # selettore finirebbe su TUTTI i figli.
        barra.setObjectName("Header")
        esterno = QtWidgets.QVBoxLayout(barra)
        esterno.setContentsMargins(18, 8, 12, 8)
        esterno.setSpacing(6)

        layout = QtWidgets.QHBoxLayout()
        layout.setSpacing(10)
        titolo = QtWidgets.QLabel("FIBRA&TAPPETI")
        titolo.setStyleSheet(f"font-weight: 800; font-size: 16px; color: {COLORI['accento']}; letter-spacing: 0.5px;")
        layout.addWidget(titolo)
        layout.addStretch(1)

        # RISVEGLIO: arancione, solo durante la transizione, con pulsazione
        # di opacità per non passare inosservato nei suoi pochi secondi.
        self.label_risveglio = QtWidgets.QLabel("◉  RISVEGLIO")
        colore_r = COLORE_STATO[Stato.RISVEGLIO]
        self.label_risveglio.setStyleSheet(
            f"background-color: {colore_r}; color: #1a1205; font-size: 13px; font-weight: 800;"
            f" letter-spacing: 1px; border: 1px solid {colore_r}; border-radius: 15px; padding: 6px 14px;"
        )
        effetto = QtWidgets.QGraphicsOpacityEffect(self.label_risveglio)
        self.label_risveglio.setGraphicsEffect(effetto)
        self._pulsazione_risveglio = QtCore.QPropertyAnimation(effetto, b"opacity", self)
        self._pulsazione_risveglio.setDuration(900)
        self._pulsazione_risveglio.setStartValue(1.0)
        self._pulsazione_risveglio.setKeyValueAt(0.5, 0.45)
        self._pulsazione_risveglio.setEndValue(1.0)
        self._pulsazione_risveglio.setLoopCount(-1)
        self.label_risveglio.hide()
        layout.addWidget(self.label_risveglio)
        layout.addSpacing(6)

        # Flusso ASCOLTO ──▶ ARMATO
        self.pillola_ascolto = QtWidgets.QLabel()
        self.freccia_flusso = QtWidgets.QLabel("──▶")
        self.pillola_armato = QtWidgets.QLabel()
        layout.addWidget(self.pillola_ascolto)
        layout.addWidget(self.freccia_flusso)
        layout.addWidget(self.pillola_armato)
        layout.addSpacing(6)

        self.btn_riarma = QtWidgets.QPushButton("⟲  RESET")
        self.btn_riarma.setObjectName("Accento")
        self.btn_riarma.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_riarma.setToolTip(
            "Spegne la smart plug e torna subito a fibra in ascolto ambiente,"
            " senza aspettare il timeout."
        )
        self.btn_riarma.clicked.connect(lambda: self._torna_ad_ambiente("RESET premuto"))
        layout.addWidget(self.btn_riarma)
        esterno.addLayout(layout)

        # Riga avvisi/errori in rosso, sempre presente ad altezza fissa (vuota = "nessun avviso").
        self._avvisi: dict[str, str] = {}
        self._riga_avvisi = QtWidgets.QWidget()
        layout_avvisi = QtWidgets.QVBoxLayout(self._riga_avvisi)
        layout_avvisi.setContentsMargins(0, 0, 0, 0)
        layout_avvisi.setSpacing(6)
        layout_avvisi.addWidget(separatore())
        self._layout_chip_avvisi = QtWidgets.QHBoxLayout()
        self._layout_chip_avvisi.setSpacing(8)
        self._layout_chip_avvisi.addStretch(1)  # stretch per primo: gli avvisi si allineano a destra
        # Segnaposto tenue quando non ci sono avvisi: la riga resta sempre lì
        # (header ad altezza fissa, con o senza errori) e non sembra un buco.
        self._nessun_avviso = QtWidgets.QLabel("✓  Nessun avviso")
        self._nessun_avviso.setStyleSheet(
            f"color: {COLORI['testo_spento']}; font-size: 12px; font-weight: 600; padding: 4px 2px;"
        )
        self._layout_chip_avvisi.addWidget(self._nessun_avviso)
        contenitore_chip = QtWidgets.QWidget()
        contenitore_chip.setLayout(self._layout_chip_avvisi)
        self._layout_chip_avvisi.setContentsMargins(0, 0, 0, 0)
        contenitore_chip.setFixedHeight(24)  # = altezza di un chip avviso
        layout_avvisi.addWidget(contenitore_chip)
        self._chip_avvisi: dict[str, QtWidgets.QLabel] = {}
        esterno.addWidget(self._riga_avvisi)  # sempre visibile: spazio riservato

        self._applica_stile_stato()
        return barra

    def _imposta_avviso(self, chiave: str, testo: str | None) -> None:
        """Mostra (testo) o toglie (None) un avviso rosso nella riga sotto
        l'header. Una chiave per tipo di problema ("rumore", "plug", ...):
        richiamarla col nuovo testo aggiorna l'avviso invece di duplicarlo."""
        if testo is None:
            chip = self._chip_avvisi.pop(chiave, None)
            if chip is not None:
                chip.deleteLater()
            self._avvisi.pop(chiave, None)
        else:
            self._avvisi[chiave] = testo
            chip = self._chip_avvisi.get(chiave)
            if chip is None:
                chip = QtWidgets.QLabel()
                chip.setStyleSheet(
                    f"background-color: {rgba(COLORI['pericolo'], 34)}; color: {COLORI['pericolo']}; "
                    "font-size: 13px; font-weight: 800; letter-spacing: 0.5px; "
                    f"border: 1px solid {rgba(COLORI['pericolo'], 120)}; border-radius: 11px; padding: 2px 12px;"
                )
                self._layout_chip_avvisi.addWidget(chip)
                self._chip_avvisi[chiave] = chip
            chip.setText(f"⚠  {testo}")
        self._nessun_avviso.setVisible(not self._avvisi)

    @staticmethod
    def _stile_fase(colore: str, attiva: bool) -> str:
        if attiva:
            r, g, b = QtGui.QColor(colore).getRgb()[:3]
            return (
                f"background-color: rgba({r}, {g}, {b}, 40); color: {colore}; font-size: 13px; "
                f"font-weight: 800; letter-spacing: 1px; border: 1px solid {colore}; "
                "border-radius: 15px; padding: 6px 16px;"
            )
        return (
            f"background-color: transparent; color: {COLORI['testo_spento']}; font-size: 13px; "
            f"font-weight: 700; letter-spacing: 1px; border: 1px solid {COLORI['bordo_forte']}; "
            "border-radius: 15px; padding: 6px 16px;"
        )

    def _crea_plot_onda(self) -> tuple[pg.PlotWidget, object]:
        plot = pg.PlotWidget()
        stile_plot(plot, "Ampiezza")
        curva = plot.plot(self.asse_tempo, self.buffer_audio, pen=pg.mkPen(color=COLORI["accento"], width=1.2))
        self._applica_ottimizzazioni_curva(curva)
        plot.setYRange(-20000, 20000, padding=0)
        plot.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        return plot, curva

    def _crea_tab_taratura(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout_principale = QtWidgets.QHBoxLayout(tab)
        layout_principale.setContentsMargins(0, 0, 0, 0)
        layout_principale.setSpacing(SPAZIO)

        # --- colonna sinistra: forma d'onda + storico STA/LTA ---
        colonna_sinistra = QtWidgets.QVBoxLayout()
        colonna_sinistra.setSpacing(SPAZIO)

        card_onda, layout_onda, testata_onda = crea_card("Segnale audio · tempo reale", spaziatura=8)
        self.label_rete = etichetta("Rete: in attesa di pacchetti…", "Mono")
        testata_onda.addWidget(self.label_rete)
        self.plot_widget_taratura, self.curva_onda_taratura = self._crea_plot_onda()
        layout_onda.addWidget(self.plot_widget_taratura)
        colonna_sinistra.addWidget(card_onda, stretch=3)

        card_storico, layout_storico, testata_storico = crea_card(
            "Storico STA/LTA", colore_titolo=COLORI["ambra"], spaziatura=8
        )
        testata_storico.addWidget(legenda_linea("soglia ON", COLORI["pericolo"]))
        testata_storico.addSpacing(8)
        testata_storico.addWidget(legenda_linea("soglia OFF", COLORI["blu"]))
        self.plot_stalta = pg.PlotWidget()
        stile_plot(self.plot_stalta, "STA/LTA")
        self.plot_stalta.setXRange(-SECONDI_VISIBILI, 0, padding=0)
        self.plot_stalta.setXLink(self.plot_widget_taratura)
        self.linea_stalta = self.plot_stalta.plot(
            self.asse_tempo_stalta, self.storico_stalta, pen=pg.mkPen(color=COLORI["ambra"], width=1.6)
        )
        self._applica_ottimizzazioni_curva(self.linea_stalta)
        self.linea_soglia_on = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(color=COLORI["pericolo"], width=1.5, style=QtCore.Qt.DashLine)
        )
        self.linea_soglia_off = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(color=COLORI["blu"], width=1.5, style=QtCore.Qt.DashLine)
        )
        self.linea_soglia_on.setValue(STA_LTA_SOGLIA_ON_DEFAULT)
        self.linea_soglia_off.setValue(STA_LTA_SOGLIA_OFF_DEFAULT)
        self.plot_stalta.addItem(self.linea_soglia_on)
        self.plot_stalta.addItem(self.linea_soglia_off)
        layout_storico.addWidget(self.plot_stalta)
        colonna_sinistra.addWidget(card_storico, stretch=2)

        layout_principale.addLayout(colonna_sinistra, stretch=3)

        # --- colonna destra: conferma + lettura live + soglie + finestre + log ---
        # Niente QScrollArea: la colonna si adatta all'altezza della finestra.
        # Tutte le card hanno altezza fissa (la loro minima, con margini
        # compatti) tranne il log, che è l'unica parte elastica: si allunga
        # o si accorcia (fino a ~3 righe) e scorre lui al suo interno.
        contenitore_destra = QtWidgets.QWidget()
        contenitore_destra.setMinimumWidth(380)
        contenitore_destra.setMaximumWidth(500)
        colonna_destra = QtWidgets.QVBoxLayout(contenitore_destra)
        colonna_destra.setContentsMargins(0, 0, 0, 0)
        colonna_destra.setSpacing(8)
        fissa = (QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)

        # Conferma in alto a destra, sopra i parametri: è la prima cosa che
        # si vede arrivando dall'alto.
        riga_conferma = QtWidgets.QHBoxLayout()
        self.btn_ok_tarato = QtWidgets.QPushButton("✓  OK STA/LTA TARATO — passa ad ASCOLTO")
        self.btn_ok_tarato.setObjectName("Primario")
        self.btn_ok_tarato.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_ok_tarato.clicked.connect(self._conferma_stalta_tarato)
        riga_conferma.addWidget(self.btn_ok_tarato)

        # Sostituisce il pulsante sopra dopo la conferma (vedi
        # _conferma_stalta_tarato): spiega perché i controlli sono bloccati.
        self.label_tarato_confermato = QtWidgets.QLabel("✓  STA/LTA tarato — parametri in sola lettura")
        self.label_tarato_confermato.setAlignment(QtCore.Qt.AlignCenter)
        self.label_tarato_confermato.setStyleSheet(stile_chip(COLORI["accento"]) + "padding: 8px 12px; border-radius: 8px;")
        self.label_tarato_confermato.hide()
        riga_conferma.addWidget(self.label_tarato_confermato)
        colonna_destra.addLayout(riga_conferma)

        card_stalta, layout_stalta, testata_stalta = crea_card("STA/LTA · soglie live", spaziatura=6, compatta=True)
        card_stalta.setSizePolicy(*fissa)
        btn_azzera_minmax = QtWidgets.QPushButton("Azzera min/max")
        btn_azzera_minmax.setObjectName("Piatto")
        btn_azzera_minmax.setCursor(QtCore.Qt.PointingHandCursor)
        btn_azzera_minmax.clicked.connect(self.azzera_minmax_stalta)
        testata_stalta.addWidget(btn_azzera_minmax)

        # Lettura grande del rapporto attuale + a destra soglia ON e min/max
        riga_lettura = QtWidgets.QHBoxLayout()
        riga_lettura.setSpacing(12)
        blocco_valore = QtWidgets.QVBoxLayout()
        blocco_valore.setSpacing(0)
        blocco_valore.addWidget(etichetta("RAPPORTO ATTUALE", "EyebrowTenue"))
        self.label_stalta = QtWidgets.QLabel("--")
        self.label_stalta.setStyleSheet(
            f"color: {COLORI['accento']}; font-family: {FONT_MONO}; font-size: 24px; font-weight: 700;"
        )
        blocco_valore.addWidget(self.label_stalta)
        riga_lettura.addLayout(blocco_valore)
        riga_lettura.addStretch(1)
        blocco_dettagli = QtWidgets.QVBoxLayout()
        blocco_dettagli.setSpacing(4)
        blocco_dettagli.addStretch(1)
        # Risposta esplicita a "quale soglia stiamo usando per detectare"
        self.label_soglia_attiva = QtWidgets.QLabel("rileva sopra --")
        self.label_soglia_attiva.setStyleSheet(stile_chip(COLORI["pericolo"]))
        blocco_dettagli.addWidget(self.label_soglia_attiva, 0, QtCore.Qt.AlignRight)
        self.label_stalta_minmax = etichetta("min -- · max --", "Mono")
        blocco_dettagli.addWidget(self.label_stalta_minmax, 0, QtCore.Qt.AlignRight)
        riga_lettura.addLayout(blocco_dettagli)
        layout_stalta.addLayout(riga_lettura)
        # Nota di stabilizzazione (compare solo nei primi secondi)
        self.label_stalta_nota = etichetta("", "Nota")
        self.label_stalta_nota.hide()
        layout_stalta.addWidget(self.label_stalta_nota)

        layout_stalta.addWidget(separatore())
        griglia_soglie = QtWidgets.QGridLayout()
        griglia_soglie.setHorizontalSpacing(12)
        griglia_soglie.setVerticalSpacing(6)
        stepper_on = self._crea_stepper(1.0, 500.0, STA_LTA_SOGLIA_ON_DEFAULT, 0.1)
        stepper_off = self._crea_stepper(0.5, 500.0, STA_LTA_SOGLIA_OFF_DEFAULT, 0.1)
        self.spin_soglia_on, self.spin_soglia_off = stepper_on.spin, stepper_off.spin
        self.spin_soglia_on.valueChanged.connect(self.cambia_soglie_stalta)
        self.spin_soglia_off.valueChanged.connect(self.cambia_soglie_stalta)
        for riga, (testo, colore, stepper) in enumerate((
            ("Soglia ON", COLORI["pericolo"], stepper_on),
            ("Soglia OFF", COLORI["blu"], stepper_off),
        )):
            griglia_soglie.addWidget(self._etichetta_con_pallino(testo, colore), riga, 0)
            griglia_soglie.addWidget(stepper, riga, 1, QtCore.Qt.AlignRight)
        layout_stalta.addLayout(griglia_soglie)
        colonna_destra.addWidget(card_stalta)

        # Finestre STA/LTA: a differenza di ON/OFF (soglie, si applicano a
        # caldo) cambiare la durata delle finestre richiede ricostruire il
        # rilevatore (gli alpha della EMA dipendono da sta_sec/lta_sec,
        # calcolati una volta sola al costruttore) - serve un "Applica"
        # esplicito. Per risparmiare righe: "Applica" sta nella testata e la
        # nota "ricostruisce da zero" è il suo tooltip.
        card_finestre, layout_finestre, testata_finestre = crea_card("Finestre STA/LTA", spaziatura=6, compatta=True)
        card_finestre.setSizePolicy(*fissa)
        self.btn_applica_finestre = QtWidgets.QPushButton("Applica")
        self.btn_applica_finestre.setObjectName("Accento")
        self.btn_applica_finestre.setStyleSheet("padding: 4px 14px;")
        self.btn_applica_finestre.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_applica_finestre.setToolTip(
            "Ricostruisce il rilevatore da zero con le nuove durate:"
            " rientra nella stabilizzazione come a un nuovo avvio"
        )
        self.btn_applica_finestre.clicked.connect(self.applica_finestre_stalta)
        testata_finestre.addWidget(self.btn_applica_finestre)
        griglia_finestre = QtWidgets.QGridLayout()
        griglia_finestre.setHorizontalSpacing(12)
        griglia_finestre.setVerticalSpacing(6)
        stepper_sta = self._crea_stepper(0.02, 5.0, STA_FINESTRA_SEC_DEFAULT, 0.02, " s")
        stepper_lta = self._crea_stepper(0.5, 60.0, LTA_FINESTRA_SEC_DEFAULT, 0.5, " s")
        self.spin_finestra_sta, self.spin_finestra_lta = stepper_sta.spin, stepper_lta.spin
        griglia_finestre.addWidget(etichetta("STA · breve"), 0, 0)
        griglia_finestre.addWidget(stepper_sta, 0, 1, QtCore.Qt.AlignRight)
        griglia_finestre.addWidget(etichetta("LTA · lunga"), 1, 0)
        griglia_finestre.addWidget(stepper_lta, 1, 1, QtCore.Qt.AlignRight)
        layout_finestre.addLayout(griglia_finestre)
        colonna_destra.addWidget(card_finestre)

        # Log eventi: qui si legge "Avrei attivato RISVEGLIO…" quando scatta
        # - la prova, fatto un rumore, che la soglia attuale lo riconosce.
        # Unica parte elastica della colonna (stretch=1).
        card_log, layout_log, _ = crea_card("Log eventi", spaziatura=6, compatta=True)
        self.lista_log = QtWidgets.QListWidget()
        # Le righe di log spiegano il motivo di ogni transizione: a capo
        # invece di tagliate, altrimenti la parte importante sparisce.
        self.lista_log.setWordWrap(True)
        self.lista_log.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.lista_log.setMinimumHeight(48)
        layout_log.addWidget(self.lista_log)
        colonna_destra.addWidget(card_log, stretch=1)

        layout_principale.addWidget(contenitore_destra, stretch=2)

        return tab

    def _crea_tab_ascolto(self) -> QtWidgets.QWidget:
        """Fase 2: onda + pannello timer + una riga impianto (tappeti e
        smart plug). Lo stato resta nella barra in alto, condivisa."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        card_onda, layout_onda, _ = crea_card("Segnale audio · tempo reale", spaziatura=6, compatta=True)
        self.plot_widget_ascolto, self.curva_onda_ascolto = self._crea_plot_onda()
        # Il grafico è la parte elastica della tab: con poca altezza (schermo
        # scalato, finestra bassa) cede lui, non i timer sotto.
        self.plot_widget_ascolto.setMinimumHeight(70)
        layout_onda.addWidget(self.plot_widget_ascolto)
        layout.addWidget(card_onda, stretch=1)

        # Pannello timer: la logica dei due timer resta tutta in
        # controlla_transizioni_temporali, qui si mostra/regola soltanto.
        self.pannello_timer = PannelloTimer(
            self.config.TIMEOUT_INATTIVITA_MIN,
            self.config.TIMEOUT_MASSIMO_MIN if not math.isinf(self.config.TIMEOUT_MASSIMO_MIN) else 5.0,
            mai=math.isinf(self.config.TIMEOUT_MASSIMO_MIN),
        )
        self.pannello_timer.timeout_inattivita_cambiato.connect(self._su_timeout_inattivita_cambiato)
        self.pannello_timer.timeout_massimo_cambiato.connect(self._su_timeout_massimo_cambiato)
        layout.addWidget(self.pannello_timer)

        # Riga impianto: counter tappeti + IP della smart plug, una sola
        # card bassa invece di un QGroupBox enorme per una riga di testo.
        card_impianto = QtWidgets.QFrame()
        card_impianto.setObjectName("Card")
        riga = QtWidgets.QHBoxLayout(card_impianto)
        riga.setContentsMargins(14, 8, 12, 8)
        riga.setSpacing(12)

        riga.addWidget(etichetta("TAPPETI", "Eyebrow"))
        # Counter del ciclo daisy chain - vive solo in ARMATO, vedi
        # _elabora_tappeti. (Il pool di etichette "Sensore N" resta
        # disattivato: la vista impianto in tab 3 fa già il lavoro visivo.)
        self.label_tappeti_counter = QtWidgets.QLabel("Counter: --")
        self.label_tappeti_counter.setStyleSheet(
            f"color: {COLORI['testo']}; font-family: {FONT_MONO}; font-size: {T_VALORE}px; font-weight: 700;"
        )
        riga.addWidget(self.label_tappeti_counter)
        riga.addSpacing(8)
        riga.addWidget(separatore(verticale=True))
        riga.addSpacing(8)

        # IP della smart plug, modificabile qui invece che nel codice: l'IP
        # della Shelly cambia da solo ogni tanto (riassegnazione DHCP).
        riga.addWidget(etichetta("SMART PLUG", "Eyebrow"))
        self.campo_smart_plug_ip = QtWidgets.QLineEdit(self.config.SMART_PLUG_HOST)
        self.campo_smart_plug_ip.setPlaceholderText("es. 192.168.1.2")
        self.campo_smart_plug_ip.setFixedWidth(170)
        self.campo_smart_plug_ip.returnPressed.connect(self.cambia_smart_plug_ip)
        riga.addWidget(self.campo_smart_plug_ip)
        btn_applica_ip = QtWidgets.QPushButton("Applica")
        btn_applica_ip.setCursor(QtCore.Qt.PointingHandCursor)
        btn_applica_ip.clicked.connect(self.cambia_smart_plug_ip)
        riga.addWidget(btn_applica_ip)
        riga.addStretch(1)
        layout.addWidget(card_impianto)
        return tab

    def _crea_tab_vista_impianto(self) -> QtWidgets.QWidget:
        """Fase 3, quella da mostrare al cliente: a sinistra lo stesso
        pannello timer di tab 2 in colonna (seconda istanza, tenuta
        sincronizzata), a destra l'anello/tappeti di VistaImpianto in una
        card - niente numeri grezzi, soglie o log tecnico."""
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPAZIO)

        self.pannello_timer_vista = PannelloTimer(
            self.config.TIMEOUT_INATTIVITA_MIN,
            self.config.TIMEOUT_MASSIMO_MIN if not math.isinf(self.config.TIMEOUT_MASSIMO_MIN) else 5.0,
            mai=math.isinf(self.config.TIMEOUT_MASSIMO_MIN),
            compatto=True,
        )
        # Larghezza elastica 320-400: con finestra stretta cede spazio
        # invece di spingere.
        self.pannello_timer_vista.setMinimumWidth(320)
        self.pannello_timer_vista.setMaximumWidth(400)
        self.pannello_timer_vista.timeout_inattivita_cambiato.connect(self._su_timeout_inattivita_cambiato)
        self.pannello_timer_vista.timeout_massimo_cambiato.connect(self._su_timeout_massimo_cambiato)
        layout.addWidget(self.pannello_timer_vista, stretch=0)

        card_vista, layout_vista, _ = crea_card("Impianto")
        # Senza RESET proprio: il RESET è quello dell'header, come nelle
        # altre tab.
        # Né RESET (sta nell'header) né didascalia di stato (lo stato è già
        # nel flusso ASCOLTO → ARMATO dell'header).
        self.vista_impianto = VistaImpianto(con_reset=False, con_didascalia=False)
        self.vista_impianto.imposta_stato(self.stato.name)
        layout_vista.addWidget(self.vista_impianto, stretch=1)
        layout.addWidget(card_vista, stretch=1)
        return tab

    @staticmethod
    def _etichetta_con_pallino(testo: str, colore: str) -> QtWidgets.QLabel:
        """Etichetta col pallino dello stesso colore della linea nel grafico:
        lega lo spinbox alla sua linea tratteggiata senza doverlo spiegare."""
        e = QtWidgets.QLabel(f"<span style='color:{colore}; font-size:16px;'>●</span>&nbsp;&nbsp;{testo}")
        e.setObjectName("Campo")
        e.setTextFormat(QtCore.Qt.RichText)
        return e

    def _su_timeout_inattivita_cambiato(self, valore: float) -> None:
        self.config.TIMEOUT_INATTIVITA_MIN = valore
        # Sincronizza l'altra istanza (tab 2 <-> tab 3): setValue() su Qt non
        # riemette il segnale se il valore è già quello, quindi qui non si
        # torna in loop infinito tra i due pannelli.
        for pannello in (self.pannello_timer, self.pannello_timer_vista):
            pannello.stepper_inattivita.spin.setValue(valore)

    def _su_timeout_massimo_cambiato(self, valore: float) -> None:
        self.config.TIMEOUT_MASSIMO_MIN = valore
        mai = math.isinf(valore)
        for pannello in (self.pannello_timer, self.pannello_timer_vista):
            pannello.switch_mai.setChecked(mai)
            if not mai:
                pannello.stepper_massimo.spin.setValue(valore)

    def cambia_smart_plug_ip(self) -> None:
        nuovo_host = self.campo_smart_plug_ip.text().strip()
        if not nuovo_host:
            return
        self.config.SMART_PLUG_HOST = nuovo_host
        self.plug.imposta_host(nuovo_host)
        self._aggiungi_log(f"Smart plug IP aggiornato: {nuovo_host}")

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
        self.tabs.setTabEnabled(2, True)
        self.tabs.setCurrentIndex(1)

    @staticmethod
    def _crea_stepper(minimo, massimo, valore, passo, suffisso: str = "") -> Stepper:
        """Stesso controllo −/+ dei timer (tema.Stepper): `.spin` è lo
        spinbox vero, disabilitarlo blocca anche i pulsanti."""
        return Stepper(minimo, massimo, valore, passo, decimali=2, suffisso=suffisso, larghezza=96)

    def _applica_stile_stato(self):
        in_ascolto = self.stato == Stato.AMBIENTE
        armato = self.stato == Stato.ARMATO
        risveglio = self.stato == Stato.RISVEGLIO
        self.pillola_ascolto.setText(("●" if in_ascolto else "○") + "  ASCOLTO")
        self.pillola_ascolto.setStyleSheet(self._stile_fase(COLORE_STATO[Stato.AMBIENTE], in_ascolto))
        self.pillola_armato.setText(("●" if armato else "○") + "  ARMATO")
        self.pillola_armato.setStyleSheet(self._stile_fase(COLORE_STATO[Stato.ARMATO], armato))
        colore_freccia = COLORE_STATO[Stato.RISVEGLIO] if risveglio else COLORI["bordo_forte"]
        self.freccia_flusso.setStyleSheet(f"color: {colore_freccia}; font-size: 14px; font-weight: 800;")

        self.label_risveglio.setVisible(risveglio)
        if risveglio:
            self._pulsazione_risveglio.start()
        else:
            self._pulsazione_risveglio.stop()
            self.label_risveglio.graphicsEffect().setOpacity(1.0)

        self._imposta_avviso("rumore", None if in_ascolto else "UNKNOWN NOISE DETECTED")
        # Guardia hasattr: la primissima chiamata arriva da _crea_barra_stato,
        # costruita prima della tab 3 (vedi _costruisci_ui) - vista_impianto
        # non esiste ancora in quel momento.
        if hasattr(self, "vista_impianto"):
            self.vista_impianto.imposta_stato(self.stato.name)

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
        self.label_soglia_attiva.setText(f"rileva sopra {self.spin_soglia_on.value():.2f}")

    def azzera_minmax_stalta(self):
        self.stalta_min_osservato = float("inf")
        self.stalta_max_osservato = float("-inf")
        self.label_stalta_minmax.setText("min -- · max --")

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
        self.label_rete.setText(f"● rete OK · RMS {int(rms)}")

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
        self.label_stalta.setText(f"{rapporto:.2f}")
        in_stab = secondi_rimanenti > 0
        if in_stab:
            self.label_stalta_nota.setText(f"Stabilizzazione in corso, ancora {secondi_rimanenti:.0f} s: i trigger vengono ignorati.")
        if self.label_stalta_nota.isVisible() != in_stab:
            self.label_stalta_nota.setVisible(in_stab)

        self.storico_stalta[self.write_pos_stalta] = rapporto
        self.write_pos_stalta = (self.write_pos_stalta + 1) % self.n_storico_stalta

        if rapporto < self.stalta_min_osservato:
            self.stalta_min_osservato = rapporto
        if rapporto > self.stalta_max_osservato:
            self.stalta_max_osservato = rapporto
        self.label_stalta_minmax.setText(
            f"min {self.stalta_min_osservato:.2f} · max {self.stalta_max_osservato:.2f}"
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
        self.pannello_timer.segna_allarme()
        self.pannello_timer_vista.segna_allarme()
        self._accumulo_tappeti = np.empty(0, dtype=np.int16)
        self._pulisci_indicatori_tappeti()
        self.protocollo_tappeti = RilevatoreProtocolloF1F2(
            SAMPLE_RATE, BLOCCO_GOERTZEL_CAMPIONI,
            F1_HZ, F2_HZ, T0_MS, T1_MS, TOLLERANZA_MS,
            SOGLIA_ON_F1, SOGLIA_OFF_F1, SOGLIA_ON_F2, SOGLIA_OFF_F2,
        )
        # Il boot della daisy chain (_inizio_risveglio_s) parte solo quando
        # _tenta_accensione_plug conferma l'accensione, non qui - vedi la
        # guardia su _plug_confermato in controlla_transizioni_temporali.
        self._plug_confermato = False
        self._inizio_risveglio_s = 0.0
        self.stato = Stato.RISVEGLIO
        self._applica_stile_stato()
        self._tenta_accensione_plug()

    def _tenta_accensione_plug(self) -> None:
        """Un tentativo di accensione. Se fallisce NON si avanza: si resta
        in RISVEGLIO (la UI mostra un avviso) e si riprova al prossimo tick
        utile - vedi RITARDO_RETRY_PLUG_S, per non martellare l'host ogni
        200ms mentre il problema persiste."""
        if self.plug.accendi():
            self._plug_confermato = True
            self._inizio_risveglio_s = time.monotonic()
            self._imposta_avviso("plug", None)
            self._aggiungi_log("Smart plug accesa -> attendo sincronizzazione daisy chain")
        else:
            self._prossimo_tentativo_plug_s = time.monotonic() + RITARDO_RETRY_PLUG_S
            self._imposta_avviso("plug", f"SMART PLUG NON RISPONDE ({self.plug.host}) — nuovo tentativo in corso…")
            self._aggiungi_log(
                f"Smart plug: accensione non riuscita, resto in RISVEGLIO e riprovo tra {RITARDO_RETRY_PLUG_S:.0f}s"
            )

    def _elabora_tappeti(self, campioni: np.ndarray) -> None:
        if self.protocollo_tappeti is None:
            return
        self._accumulo_tappeti = np.concatenate((self._accumulo_tappeti, campioni))
        eventi_slot = False
        while len(self._accumulo_tappeti) >= BLOCCO_GOERTZEL_CAMPIONI:
            blocco = self._accumulo_tappeti[:BLOCCO_GOERTZEL_CAMPIONI]
            self._accumulo_tappeti = self._accumulo_tappeti[BLOCCO_GOERTZEL_CAMPIONI:]
            for evento in self.protocollo_tappeti.elabora_blocco(blocco):
                if evento["tipo"] == "zero":
                    # Fine del ciclo appena concluso: un sensore che era
                    # visualizzato ma non si è ripresentato in QUESTO ciclo
                    # (comparso per rumore, mai esistito davvero) va tolto -
                    # ma solo lui, non tutto. Cancellare e ricreare l'intero
                    # pannello ad ogni marcatore di zero (una volta a ciclo,
                    # ~1s) faceva "flashare" anche i sensori veri e stabili,
                    # oltre a essere lavoro sprecato quando non cambia nulla.
                    mancanti = self.stato_tappeti.keys() - self._visti_questo_ciclo
                    for numero in mancanti:
                        del self.stato_tappeti[numero]
                    if mancanti:
                        eventi_slot = True
                    self._visti_questo_ciclo = set()
                elif evento["tipo"] == "slot":
                    eventi_slot = True
                    self.stato_tappeti[evento["counter"]] = evento["stato"] == "chiuso"
                    self._visti_questo_ciclo.add(evento["counter"])
                    if evento["stato"] == "chiuso":
                        self._ultima_chiusura_s = time.monotonic()
                        self._aggiungi_log(f"ALLARME: interruttore {evento['counter']} chiuso")
        if eventi_slot:
            self._aggiorna_ui_tappeti()

    def _aggiorna_ui_tappeti(self) -> None:
        counter = self.protocollo_tappeti.counter if self.protocollo_tappeti is not None else "--"
        self.label_tappeti_counter.setText(f"Counter: {counter}")

        numeri = sorted(self.stato_tappeti.items())  # i numeri bassi (sensori veri) vengono sempre per primi
        if len(numeri) > MAX_TAPPETI_VISUALIZZATI:
            log.warning(
                "Counter tappeti oltre %d valori distinti (%d) - probabile rumore, non un vero ciclo daisy chain",
                MAX_TAPPETI_VISUALIZZATI, len(numeri),
            )
        # Commentato per ora insieme alla creazione in _crea_tab_ascolto (vedi
        # lì il motivo): niente rettangoli "Sensore N" in tab 2 finché serve
        # solo gestire i 2 sensori fissi, già mostrati dalla vista impianto.
        # self.label_nessun_tappeto.setVisible(not numeri)
        #
        # for indice, etichetta in enumerate(self._indicatori_tappeti):
        #     if indice >= len(numeri):
        #         etichetta.hide()
        #         continue
        #     numero, chiuso = numeri[indice]
        #     colore = COLORI["pericolo"] if chiuso else "#1fbf75"
        #     etichetta.setText(f"Sensore {numero}")
        #     etichetta.setStyleSheet(
        #         f"background-color: {colore}; color: white; font-weight: 800; "
        #         "border-radius: 8px; font-size: 12px; padding: 8px 14px;"
        #     )
        #     etichetta.show()

        # stato_tappeti usa già True = chiuso, lo stesso significato atteso
        # da VistaImpianto.imposta_tappeti (vedi sua docstring) - nessuna
        # conversione necessaria.
        self.vista_impianto.imposta_tappeti(self.stato_tappeti)

    def _pulisci_indicatori_tappeti(self) -> None:
        self.stato_tappeti = {}
        self._visti_questo_ciclo = set()
        self.label_tappeti_counter.setText("Counter: --")
        # self.label_nessun_tappeto.show()
        # for etichetta in self._indicatori_tappeti:
        #     etichetta.hide()
        self.vista_impianto.imposta_tappeti({})

    # ------------------------------------------------------------------
    # Transizioni temporali (boot daisy chain, timeout spegnimento)
    # ------------------------------------------------------------------
    def controlla_transizioni_temporali(self) -> None:
        ora = time.monotonic()

        if self.stato == Stato.RISVEGLIO:
            if not self._plug_confermato:
                # Niente boot daisy chain finché la presa non è confermata
                # accesa - vedi _tenta_accensione_plug per il motivo.
                if ora >= self._prossimo_tentativo_plug_s:
                    self._tenta_accensione_plug()
            elif ora - self._inizio_risveglio_s >= self.config.ATTESA_BOOT_DAISY_CHAIN_S:
                self._ultima_chiusura_s = ora
                self._inizio_armato_s = ora
                self._aggiungi_log("Daisy chain considerata sincronizzata -> ARMATO")
                self.stato = Stato.ARMATO
                self._applica_stile_stato()

        elif self.stato == Stato.ARMATO:
            timeout_inattivita_s = self.config.TIMEOUT_INATTIVITA_MIN * 60
            timeout_massimo_s = self.config.TIMEOUT_MASSIMO_MIN * 60  # inf se "Mai" e' attivo, il confronto sotto resta sempre False
            if ora - self._ultima_chiusura_s >= timeout_inattivita_s:
                self._torna_ad_ambiente(f"nessuna chiusura da {self.config.TIMEOUT_INATTIVITA_MIN:.0f} min (timeout inattività)")
            elif ora - self._inizio_armato_s >= timeout_massimo_s:
                self._torna_ad_ambiente(
                    f"timeout massimo raggiunto ({self.config.TIMEOUT_MASSIMO_MIN:.0f} min, "
                    "nonostante chiusure continue)"
                )

        # PannelloTimer.aggiorna() fa già tutto il lavoro che prima era qui
        # dentro (quale dei due timer comanda, countdown, caso "Mai", caso
        # "chiusure in corso") - due istanze (tab 2 e tab 3), stesso stato.
        for pannello in (self.pannello_timer, self.pannello_timer_vista):
            pannello.aggiorna(
                self.stato.name, ora, self._ultima_chiusura_s, self._inizio_armato_s,
                # Finché la plug non è confermata passo "ora" invece del
                # vero inizio: il countdown resta fermo al valore pieno
                # invece di scorrere come se il boot fosse già in corso.
                self._inizio_risveglio_s if self._plug_confermato else ora,
                self.config.ATTESA_BOOT_DAISY_CHAIN_S,
            )

    def _torna_ad_ambiente(self, motivo: str) -> None:
        """Comune a timeout vero e al pulsante "Forza AMBIENTE": riparte da
        RilevatoreStaLta pulito invece di riusare sta/lta vecchi di minuti
        (stesso transitorio del cold-start, vedi ATTESA_STABILIZZAZIONE_STALTA_S)."""
        if self.stato == Stato.AMBIENTE:
            return
        self._aggiungi_log(f"{motivo} -> AMBIENTE")
        self.plug.spegni()
        self._plug_confermato = False
        self._prossimo_tentativo_plug_s = 0.0
        self._imposta_avviso("plug", None)
        self.protocollo_tappeti = None
        self._pulisci_indicatori_tappeti()
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
        # Ridisegna solo i grafici della tab attualmente visibile: prima
        # aggiornavamo sempre entrambe le curve onda (tab 1 e tab 2) ad ogni
        # frame (20fps) anche quando una delle due non si vedeva nemmeno -
        # lavoro sprecato, lo stesso genere di costo per cui avevamo valutato
        # un pulsante "nascondi grafico" manuale. La tab 3 (vista impianto)
        # non ha grafici pyqtgraph, nessun aggiornamento da fare per lei.
        indice_tab = self.tabs.currentIndex()
        if indice_tab not in (0, 1):
            return

        dati_ordinati = np.concatenate(
            (self.buffer_audio[self.write_pos:], self.buffer_audio[: self.write_pos])
        )
        if indice_tab == 0:
            self.curva_onda_taratura.setData(self.asse_tempo, dati_ordinati)
            storico_ordinato = np.concatenate(
                (self.storico_stalta[self.write_pos_stalta:], self.storico_stalta[: self.write_pos_stalta])
            )
            self.linea_stalta.setData(self.asse_tempo_stalta, storico_ordinato)
        else:
            self.curva_onda_ascolto.setData(self.asse_tempo, dati_ordinati)

    @staticmethod
    def _applica_ottimizzazioni_curva(curva) -> None:
        """Downsampling + clip alla vista: senza questo pyqtgraph ridisegna
        tutti i 360.000 campioni della finestra da 15s ad ogni frame (20fps),
        il costo vero dietro alla lentezza percepita - stesso accorgimento
        già usato in FREQUENCY DETECTION/main.py e 0registratoreStalta.py."""
        try:
            curva.setDownsampling(auto=True, method="peak")
        except TypeError:
            curva.setDownsampling(auto=True)
        curva.setClipToView(True)
        try:
            curva.setSkipFiniteCheck(True)
        except AttributeError:
            pass

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        # Senza questa wait, Qt può distruggere il QThread mentre il suo
        # run() è ancora nel loop (fino a 0.5s, il settimeout del socket) ->
        # "QThread: Destroyed while thread is still running" in chiusura.
        self.worker.running = False
        self.worker.wait(1000)
        self.plug.spegni()
        event.accept()


def main() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    applica_palette(app)
    finestra = FinestraWakeupStalta()
    finestra.show()
    app.exec()


if __name__ == "__main__":
    main()