#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor Allarme - GUI cliente (dati REALI)
============================================
Stessa interfaccia del design di riferimento
(DESIGN/design_handoff_gui_monitoraggio_sensori/alarm_monitor_gui.py, non
toccato se non per aggiungere lo stato "segnale assente" - resta il mockup
visivo, con dati simulati e un checkbox per provare quello stato a mano),
ma collegata ai dati reali del sistema invece che a una sorgente simulata.

Cosa cambia rispetto al design:
- `RilevatoreAlarmSource` sostituisce `SimulatedAlarmSource`, con lo STESSO
  "contratto" (segnali `stato_cambiato`/`connessione_globale_cambiata`,
  metodo `imposta_stato`/`stato_corrente`) - per questo `SensoreWidget` e il
  resto della UI restano identici al design, cambia solo da dove arrivano i
  dati.
- Nessun pannello di test/simulazione (era lì solo per provare la UI senza
  hardware).
- `N_SENSORI_ATTIVI` non è più fisso a 2: viene da `config_condivisa.NUMERO_NODI`
  (cambia in automatico se in futuro si aggiungono nodi alla daisy chain -
  vedi ../DAISY CHAIN/restart.md). `N_SENSORI` (capacità visualizzata,
  compresi gli slot futuri non ancora cablati) resta una scelta di
  presentazione, non legata al protocollo.
- **Watchdog "nessun conteggio"**: se il sistema non riconosce nessun evento
  di protocollo valido (marcatore o slot) per più di
  `SOGLIA_TIMEOUT_CONNESSIONE_MS`, tutti i sensori attivi passano allo stato
  visivo "⚠ Segnale assente" (stesso trattamento introdotto nel design, ma
  qui guidato da un vero timeout sul traffico reale, non da un checkbox
  manuale) - tipicamente significa che gli Arduino della catena di
  sincronizzazione non sono collegati/alimentati.
- **Cavo e "spinotti" uniti in un'unica struttura** (fix rispetto alla prima
  versione): prima il cavo orizzontale era disegnato con un paintEvent a
  un'altezza fissa (y=88) indovinata sulle dimensioni vecchie delle card,
  che con le card più piccole/di altezza variabile di questa versione non
  coincideva più con gli spinotti verticali sotto ogni sensore (il cavo
  finiva a metà card, scollegato dagli spinotti). Ora `CatenaSensoriWidget`
  usa un QGridLayout a 3 righe (card [bottom-aligned] / spinotto / cavo
  unico a tutta larghezza) così lo spinotto di ogni card TOCCA sempre
  esattamente il cavo, qualunque sia l'altezza della card - nessuna
  coordinata indovinata a mano.

Riuso diretto da main.py (stessa cartella FREQUENCY DETECTION/, importato
come modulo - non duplicato): `NetworkWorker` (ricezione UDP) e
`RilevatoreProtocolloF1F2` (Goertzel + isteresi + macchina a stati, SPECS.MD
§3), con esattamente gli stessi parametri di default che main.py userebbe.
main.py è importabile in sicurezza: crea la QApplication e apre la finestra
SOLO dentro `if __name__ == "__main__":`, quindi importarlo qui come modulo
non apre nessuna finestra né socket in più - solo le classi/costanti servono.

Esegui con: python gui.py (da questa cartella, FREQUENCY DETECTION/GUI CLIENT/)
Richiede: PySide6, numpy (già usati da main.py)

ATTENZIONE: non tenere aperta insieme a main.py - entrambi aprono un socket
UDP sulla stessa porta (config_condivisa non definisce la porta, vedi
UDP_PORT in main.py), il secondo che parte fallisce il bind.
"""

import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal, Slot, QObject
from PySide6.QtGui import QFont, QColor, QPixmap, QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QGridLayout, QFrame, QScrollArea, QSizePolicy, QGraphicsDropShadowEffect,
)

# --- Riuso diretto della logica di rilevamento e dei parametri da main.py e
# config_condivisa.py (una cartella sopra, FREQUENCY DETECTION/) - stesso
# pattern Path(__file__).resolve().parent già usato altrove nel progetto
# (es. DAISY CHAIN/aggiorna_config_daisy.py), mai percorsi relativi alla cwd.
CARTELLA = Path(__file__).resolve().parent
CARTELLA_FREQUENCY_DETECTION = CARTELLA.parent
sys.path.insert(0, str(CARTELLA_FREQUENCY_DETECTION))

import config_condivisa
from main import (
    NetworkWorker, RilevatoreProtocolloF1F2,
    UDP_IP, UDP_PORT, CAMPIONI_PER_PACCHETTO,
    SAMPLE_RATE, BLOCCO_GOERTZEL_CAMPIONI,
    F1_HZ_DEFAULT, F2_HZ_DEFAULT, T0_MS_DEFAULT, T1_MS_DEFAULT, TOLLERANZA_MS_DEFAULT,
    SOGLIA_ON_F1_DEFAULT, SOGLIA_OFF_F1_DEFAULT, SOGLIA_ON_F2_DEFAULT, SOGLIA_OFF_F2_DEFAULT,
)

N_SENSORI = 10                                  # capacità visualizzata (presentazione)
N_SENSORI_ATTIVI = config_condivisa.NUMERO_NODI  # nodi daisy chain realmente presenti


# =============================================================================
# MODELLO DATI (identico al design di riferimento)
# =============================================================================

class StatoSensore(Enum):
    LIBERO = "libero"
    ALLARME = "allarme"


# =============================================================================
# SORGENTE DATI REALE
# =============================================================================

class RilevatoreAlarmSource(QObject):
    """
    Sorgente dati REALE per lo stato dei sensori: riceve lo stream UDP vero
    (NetworkWorker) e lo passa al rilevatore f1/f2 (RilevatoreProtocolloF1F2)
    - entrambi importati da main.py, stessa identica logica di
    riconoscimento usata dall'interrogatore tecnico, non reimplementata qui.

    Ogni slot valido riconosciuto (counter N, stato aperto/chiuso) diventa lo
    stato del sensore N (il protocollo numera i counter da 1, come i sensori
    del design - nessun offset da applicare). Il marcatore di zero non
    aggiorna nessuno stato sensore, ma conta comunque come "il sistema sta
    contando" per il watchdog sotto.

    Stesso "contratto" della SimulatedAlarmSource del design (segnali
    `stato_cambiato`/`connessione_globale_cambiata`, metodo
    `imposta_stato`/`stato_corrente`): il resto della UI non sa (e non deve
    sapere) se i dati sono reali o simulati.
    """

    stato_cambiato = Signal(int, object)          # (sensor_id, StatoSensore)
    connessione_globale_cambiata = Signal(bool)    # True = riceve dati validi

    # Quanto aspettare senza nessun marcatore/slot valido prima di dichiarare
    # "nessun conteggio". Il ciclo completo attuale dura ~1s con 2 nodi (vedi
    # SPECS.MD §5, DAISY CHAIN/restart.md) - margine di ~5 cicli, abbastanza
    # per non scattare su un singolo ciclo rumoroso ma abbastanza reattivo da
    # accorgersi in fretta di una catena spenta/scollegata.
    SOGLIA_TIMEOUT_CONNESSIONE_MS = 5000

    def __init__(self, id_sensori_attivi, parent=None):
        super().__init__(parent)
        self._stati = {i: StatoSensore.LIBERO for i in id_sensori_attivi}
        self._accumulo_goertzel = np.zeros(0, dtype=np.int16)

        self._connesso = False           # nessun evento ancora visto dal boot
        self._ultimo_evento_valido = None  # time.monotonic() dell'ultimo marcatore/slot

        self.protocollo = RilevatoreProtocolloF1F2(
            SAMPLE_RATE, BLOCCO_GOERTZEL_CAMPIONI,
            F1_HZ_DEFAULT, F2_HZ_DEFAULT, T0_MS_DEFAULT, T1_MS_DEFAULT, TOLLERANZA_MS_DEFAULT,
            SOGLIA_ON_F1_DEFAULT, SOGLIA_OFF_F1_DEFAULT, SOGLIA_ON_F2_DEFAULT, SOGLIA_OFF_F2_DEFAULT,
        )

        self.worker = NetworkWorker(UDP_IP, UDP_PORT, CAMPIONI_PER_PACCHETTO)
        self.worker.nuovi_dati_signal.connect(self._gestisci_dati)

        self._timer_watchdog = QTimer(self)
        self._timer_watchdog.setInterval(1000)
        self._timer_watchdog.timeout.connect(self._controlla_connessione)

    def avvia(self):
        self.worker.start()
        self._timer_watchdog.start()

    def ferma(self):
        self._timer_watchdog.stop()
        self.worker.running = False
        if not self.worker.wait(2000):
            self.worker.terminate()

    def stato_corrente(self, sensor_id: int) -> StatoSensore:
        return self._stati[sensor_id]

    def imposta_stato(self, sensor_id: int, stato: StatoSensore):
        """Imposta lo stato di un sensore e notifica chi è in ascolto (se cambia)."""
        if self._stati.get(sensor_id) != stato:
            self._stati[sensor_id] = stato
            self.stato_cambiato.emit(sensor_id, stato)

    @Slot(np.ndarray)
    def _gestisci_dati(self, nuovi_campioni):
        # Stesso accumulo a blocchi da 20ms di RilevatoreF1F2.gestisci_dati()
        # in main.py: non è logica di rilevamento (quella vive tutta dentro
        # RilevatoreProtocolloF1F2, importata sopra), solo il buffer che la
        # alimenta.
        self._accumulo_goertzel = np.concatenate((self._accumulo_goertzel, nuovi_campioni))
        while len(self._accumulo_goertzel) >= BLOCCO_GOERTZEL_CAMPIONI:
            blocco = self._accumulo_goertzel[:BLOCCO_GOERTZEL_CAMPIONI]
            self._accumulo_goertzel = self._accumulo_goertzel[BLOCCO_GOERTZEL_CAMPIONI:]
            for evento in self.protocollo.elabora_blocco(blocco):
                self._gestisci_evento(evento)

    def _gestisci_evento(self, evento):
        if evento["tipo"] in ("slot", "zero"):
            self._segnala_evento_valido()
        if evento["tipo"] != "slot":
            return  # marcatore zero e rumore non aggiornano nessun sensore
        sensor_id = evento["counter"]
        if sensor_id not in self._stati:
            return  # counter oltre N_SENSORI_ATTIVI: ciclo incompleto/spurio, ignora
        nuovo_stato = StatoSensore.ALLARME if evento["stato"] == "chiuso" else StatoSensore.LIBERO
        self.imposta_stato(sensor_id, nuovo_stato)

    def _segnala_evento_valido(self):
        self._ultimo_evento_valido = time.monotonic()
        if not self._connesso:
            self._connesso = True
            self.connessione_globale_cambiata.emit(True)

    def _controlla_connessione(self):
        if self._ultimo_evento_valido is None:
            return  # mai visto nulla dal boot: resta "non connesso", già comunicato all'avvio
        trascorso_ms = (time.monotonic() - self._ultimo_evento_valido) * 1000
        if self._connesso and trascorso_ms > self.SOGLIA_TIMEOUT_CONNESSIONE_MS:
            self._connesso = False
            self.connessione_globale_cambiata.emit(False)


# =============================================================================
# PALETTE COLORI - coerente col logo Knitronix (rosso/nero/bianco), identica
# al design di riferimento (non duplicare/discostare senza aggiornare anche lì)
# =============================================================================

class Palette:
    SFONDO = "#FFFFFF"
    TESTO_PRINCIPALE = "#1A1A1A"
    TESTO_SECONDARIO = "#6B6560"
    BORDO = "#DCD8D4"

    LIBERO_BG = "#E9E7E5"
    LIBERO_ACCENTO = "#1A1A1A"
    ALLARME_BG = "#FBE2DF"
    ALLARME_ACCENTO = "#E13A2E"
    NON_COLLEGATO_BG = "#EDEBE9"
    NON_COLLEGATO_ACCENTO = "#BFBAB6"
    DISCONNESSO_BG = "#FDF3DC"
    DISCONNESSO_ACCENTO = "#C98A1D"


# =============================================================================
# WIDGET: singolo sensore (identico al design di riferimento)
# =============================================================================

class SensoreWidget(QFrame):
    """
    Rappresenta visivamente un singolo sensore della catena:
    - nome ("Sensore N")
    - blocco colorato che riflette lo stato
    - etichetta testuale di stato
    - timestamp dell'ultimo cambio (solo se attivo)

    Nota: la connessione visiva della card al cavo comune (lo "spinotto")
    NON vive più qui - è gestita da CatenaSensoriWidget insieme al cavo
    stesso, così i due pezzi sono garantiti allineati (vedi lì).
    """

    def __init__(self, numero: int, attivo: bool, parent=None):
        super().__init__(parent)
        self.numero = numero
        self.attivo = attivo
        self.stato = StatoSensore.LIBERO
        self.ultimo_cambio = None
        # Rilevante SOLO se attivo=True: il sistema sta ricevendo dati validi
        # per questo sensore adesso? Indipendente da `stato` (che resta
        # l'ultimo valore libero/allarme noto, non viene toccato da un calo
        # di connessione - vedi imposta_connesso()).
        self.connesso = True

        # Card piccole apposta: l'obiettivo e' vederne almeno 7 insieme nella
        # finestra senza scorrere, non massimizzare la dimensione della
        # singola card. Il testo va comunque a 2 righe su "Rilevata
        # pressione"/"⚠ Segnale assente"/"Non collegato" (vedi
        # setWordWrap(True) sotto) - piu' piccolo aiuta anche quello, meno
        # margine serve per il testo a capo.
        larghezza = 110 if attivo else 82
        padding = 10 if attivo else 8
        self._dim_blocco = (66, 90) if attivo else (44, 62)

        self.setObjectName("card")
        self.setFixedWidth(larghezza)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)

        if attivo:
            ombra = QGraphicsDropShadowEffect(self)
            ombra.setBlurRadius(16)
            ombra.setOffset(0, 3)
            ombra.setColor(QColor(0, 0, 0, 20))
            self.setGraphicsEffect(ombra)
        else:
            self.setWindowOpacity(0.55)  # nota: non ha effetto su QFrame figlio, vedi stylesheet

        layout = QVBoxLayout(self)
        layout.setContentsMargins(padding, padding, padding, padding)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignHCenter)

        # setWordWrap(True) su tutte e tre: senza, Qt non taglia a capo, taglia
        # (centrato, quindi simmetricamente da entrambi i lati) qualunque
        # testo piu' largo della card - "SENSORE 1" diventava "ENSORE",
        # "Non collegato" diventava "n collega". Il layout gestisce
        # l'altezza da solo (nessun setFixedHeight sulla card), quindi 2
        # righe invece di 1 non rompe nulla.
        self.lbl_nome = QLabel(f"SENSORE {numero}")
        self.lbl_nome.setAlignment(Qt.AlignCenter)
        self.lbl_nome.setWordWrap(True)
        self.lbl_nome.setFont(QFont("Helvetica", 11 if attivo else 9, QFont.Bold))
        layout.addWidget(self.lbl_nome)

        self.blocco_stato = QFrame()
        self.blocco_stato.setFixedSize(*self._dim_blocco)
        layout.addWidget(self.blocco_stato, alignment=Qt.AlignHCenter)

        self.lbl_stato = QLabel()
        self.lbl_stato.setAlignment(Qt.AlignCenter)
        self.lbl_stato.setWordWrap(True)
        self.lbl_stato.setFont(QFont("Helvetica", 10 if attivo else 9, QFont.Bold))
        # Altezza minima riservata per 3 righe (non 2): "⚠ Segnale assente"
        # su una card larga ~90px utili può andare a 3 righe a seconda del
        # rendering del font sul sistema del cliente - riservare solo 2
        # righe (con margine di sicurezza minimo) è quello che causava il
        # testo compresso/sovrapposto visto nello screenshot. lineSpacing()
        # e' la vera altezza riga a riga di QUESTO font su QUESTO sistema,
        # non una stima; +8px di margine oltre le 3 righe invece di +4.
        metriche_stato = QFontMetrics(self.lbl_stato.font())
        self.lbl_stato.setMinimumHeight(metriche_stato.lineSpacing() * 3 + 8)
        layout.addWidget(self.lbl_stato)

        self.lbl_timestamp = QLabel()
        self.lbl_timestamp.setAlignment(Qt.AlignCenter)
        self.lbl_timestamp.setWordWrap(True)
        self.lbl_timestamp.setFont(QFont("Helvetica", 8))
        self.lbl_timestamp.setStyleSheet(f"color: {Palette.TESTO_SECONDARIO};")
        metriche_ts = QFontMetrics(self.lbl_timestamp.font())
        self.lbl_timestamp.setMinimumHeight(metriche_ts.lineSpacing() * 2 + 6)
        layout.addWidget(self.lbl_timestamp)

        self._applica_stato_visuale()

    def imposta_stato(self, stato: StatoSensore):
        if not self.attivo:
            return
        self.stato = stato
        self.ultimo_cambio = datetime.now()
        self._applica_stato_visuale()

    def imposta_connesso(self, connesso: bool):
        """Sensore attivo ma il sistema (globale) sta/non sta ricevendo dati
        validi adesso. Non tocca `stato` - al ritorno della connessione si
        rivede l'ultimo stato libero/allarme noto, non un default ottimistico
        (in un sistema di allarme è più sicuro mostrare un valore stantio
        dichiarato tale, con l'avviso, che nasconderlo dietro un "Libero")."""
        if not self.attivo:
            return
        if self.connesso != connesso:
            self.connesso = connesso
            self._applica_stato_visuale()

    def _applica_stato_visuale(self):
        if not self.attivo:
            bg_card, colore_blocco = Palette.NON_COLLEGATO_BG, Palette.NON_COLLEGATO_ACCENTO
            testo_stato, opacita = "Non collegato", "0.55"
            timestamp = "—"
        elif not self.connesso:
            bg_card, colore_blocco = Palette.DISCONNESSO_BG, Palette.DISCONNESSO_ACCENTO
            testo_stato, opacita = "⚠ Segnale assente", "1"
            timestamp = "Verificare il collegamento"
        elif self.stato == StatoSensore.ALLARME:
            bg_card, colore_blocco = Palette.ALLARME_BG, Palette.ALLARME_ACCENTO
            testo_stato, opacita = "Rilevata pressione", "1"
            timestamp = f"Ultimo cambio: {self.ultimo_cambio.strftime('%H:%M:%S')}" if self.ultimo_cambio else "—"
        else:
            bg_card, colore_blocco = Palette.LIBERO_BG, Palette.LIBERO_ACCENTO
            testo_stato, opacita = "Libero", "1"
            timestamp = f"Ultimo cambio: {self.ultimo_cambio.strftime('%H:%M:%S')}" if self.ultimo_cambio else "—"

        self.setStyleSheet(f"""
            QFrame#card {{
                background-color: {bg_card};
                border: 1px solid {Palette.BORDO};
                border-radius: 16px;
            }}
        """)
        self.blocco_stato.setStyleSheet(f"background-color: {colore_blocco}; border-radius: 18px;")
        self.lbl_nome.setStyleSheet(f"color: {Palette.TESTO_PRINCIPALE};")
        self.lbl_stato.setText(testo_stato)
        self.lbl_stato.setStyleSheet(f"color: {colore_blocco};")
        self.lbl_timestamp.setText(timestamp)
        self.setProperty("opacita_richiesta", opacita)


# =============================================================================
# WIDGET: fila della catena con cavo comune
# =============================================================================

class CatenaSensoriWidget(QWidget):
    """
    Mostra la fila di sensori con un cavo comune che li collega davvero
    visivamente: card (riga 0, allineate in basso), un piccolo spinotto per
    card (riga 1, altezza fissa uguale per tutte le card) e il cavo unico a
    tutta larghezza (riga 2). Le tre righe sono un unico QGridLayout, quindi
    lo spinotto di OGNI card - grande o piccola che sia - tocca sempre
    esattamente il cavo: non serve indovinare nessuna coordinata (era il bug
    della versione precedente, che disegnava il cavo con un paintEvent a
    un'altezza fissa scollegata dagli spinotti veri).
    """

    ALTEZZA_SPINOTTO = 14

    def __init__(self, sensori: dict, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        contenuto = QWidget()
        contenuto.setStyleSheet("background: transparent;")
        grid = QGridLayout(contenuto)
        grid.setContentsMargins(4, 4, 4, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(0)

        for col, (numero, widget) in enumerate(sensori.items()):
            grid.addWidget(widget, 0, col, alignment=Qt.AlignHCenter | Qt.AlignBottom)

            spinotto = QFrame()
            spinotto.setFixedSize(2, self.ALTEZZA_SPINOTTO)
            spinotto.setStyleSheet(f"background-color: {Palette.TESTO_PRINCIPALE};")
            grid.addWidget(spinotto, 1, col, alignment=Qt.AlignHCenter)

        cavo = QFrame()
        cavo.setFixedHeight(4)
        cavo.setStyleSheet(f"background-color: {Palette.TESTO_PRINCIPALE}; border-radius: 2px;")
        grid.addWidget(cavo, 2, 0, 1, max(len(sensori), 1))

        scroll.setWidget(contenuto)
        layout.addWidget(scroll)


# =============================================================================
# FINESTRA PRINCIPALE
# =============================================================================

class FinestraPrincipale(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sistema di monitoraggio - Knitronix")
        self.setMinimumSize(1000, 460)
        self.setStyleSheet(f"QMainWindow {{ background-color: {Palette.SFONDO}; }}")

        id_attivi = list(range(1, N_SENSORI_ATTIVI + 1))
        self.sorgente = RilevatoreAlarmSource(id_attivi)
        self.sorgente.stato_cambiato.connect(self._on_stato_cambiato)
        self.sorgente.connessione_globale_cambiata.connect(self._on_connessione_globale_cambiata)

        self._costruisci_ui()

        # Stato iniziale: nessun evento ancora ricevuto dal boot, coerente
        # con RilevatoreAlarmSource che parte con connesso=False (vedi sopra).
        for numero in id_attivi:
            self.sensori_widgets[numero].imposta_connesso(False)

        self.sorgente.avvia()

    def _costruisci_ui(self):
        centrale = QWidget()
        self.setCentralWidget(centrale)

        layout_principale = QVBoxLayout(centrale)
        layout_principale.setContentsMargins(36, 32, 36, 32)
        layout_principale.setSpacing(28)

        layout_principale.addWidget(self._crea_header())

        self.sensori_widgets = {}
        for i in range(1, N_SENSORI + 1):
            self.sensori_widgets[i] = SensoreWidget(i, attivo=(i <= N_SENSORI_ATTIVI))

        catena = CatenaSensoriWidget(self.sensori_widgets)
        layout_principale.addWidget(catena, stretch=1)

    def _crea_header(self) -> QWidget:
        header = QFrame()
        header.setStyleSheet(f"border-bottom: 3px solid {Palette.TESTO_PRINCIPALE};")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 18)
        layout.setSpacing(18)

        lbl_logo = QLabel()
        pixmap = QPixmap(str(CARTELLA / "logo-knitronix.png"))
        if not pixmap.isNull():
            lbl_logo.setPixmap(pixmap.scaledToHeight(64, Qt.SmoothTransformation))
        layout.addWidget(lbl_logo)

        blocco_testo = QVBoxLayout()
        blocco_testo.setSpacing(4)

        titolo = QLabel("SISTEMA DI MONITORAGGIO")
        titolo.setFont(QFont("Helvetica", 20, QFont.Black))
        titolo.setStyleSheet(f"color: {Palette.TESTO_PRINCIPALE};")
        blocco_testo.addWidget(titolo)

        sottotitolo = QLabel(
            f"Catena di {N_SENSORI} sensori — i primi {N_SENSORI_ATTIVI} collegati, "
            f"gli altri {N_SENSORI - N_SENSORI_ATTIVI} in attesa di collegamento"
        )
        sottotitolo.setFont(QFont("Helvetica", 11))
        sottotitolo.setStyleSheet(f"color: {Palette.TESTO_SECONDARIO};")
        blocco_testo.addWidget(sottotitolo)

        layout.addLayout(blocco_testo)
        layout.addStretch(1)
        return header

    def _on_stato_cambiato(self, sensor_id: int, stato: StatoSensore):
        widget = self.sensori_widgets.get(sensor_id)
        if widget is not None:
            widget.imposta_stato(stato)

    def _on_connessione_globale_cambiata(self, connesso: bool):
        """Segnale GLOBALE (non per singolo sensore): si applica a tutti i
        sensori attivi insieme - se la catena di sincronizzazione non
        trasmette, nessuno di loro è più affidabile, non solo uno."""
        for numero in range(1, N_SENSORI_ATTIVI + 1):
            self.sensori_widgets[numero].imposta_connesso(connesso)

    def closeEvent(self, event):
        self.sorgente.ferma()
        event.accept()


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    app = QApplication(sys.argv)
    finestra = FinestraPrincipale()
    finestra.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
