#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor Allarme - GUI PySide6 (Knitronix)
==========================================
Interfaccia grafica per il monitoraggio di una catena di 10 sensori di
pressione collegati in serie da un unico cavo. Per ora solo i primi 2
sensori sono fisicamente collegati; gli altri 8 sono predisposti per un
collegamento futuro e restano visualizzati come "Non collegato".

STATO ATTUALE: usa dati SIMULATI (vedi classe SimulatedAlarmSource).
Per collegare i dati reali, sostituire SimulatedAlarmSource con una classe
che implementa la stessa interfaccia (segnale `stato_cambiato`) e che legge
lo stato reale dai sensori (seriale, GPIO, MQTT, ecc.).

Esegui con:  python alarm_monitor_gui.py
Richiede:    pip install PySide6
"""

import sys
import random
from datetime import datetime
from enum import Enum

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QColor, QPixmap, QFontMetrics
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QFrame, QPushButton, QCheckBox, QScrollArea, QSizePolicy, QGraphicsDropShadowEffect,
)

N_SENSORI = 10
N_SENSORI_ATTIVI = 2  # i primi N_SENSORI_ATTIVI sono fisicamente collegati


# =============================================================================
# MODELLO DATI
# =============================================================================

class StatoSensore(Enum):
    LIBERO = "libero"
    ALLARME = "allarme"


class SimulatedAlarmSource(QObject):
    """
    Sorgente dati SIMULATA per lo stato dei sensori attivi.

    Espone il contratto che dovrà avere la futura sorgente reale:
    - segnale `stato_cambiato(sensor_id: int, nuovo_stato: StatoSensore)`
    - metodo `imposta_stato(sensor_id, stato)`

    Per collegare i sensori reali: creare una classe con la stessa interfaccia
    che, invece di generare dati random, legge da porta seriale / GPIO / rete
    e chiama `imposta_stato(...)` quando riceve un cambiamento reale.
    """

    stato_cambiato = Signal(int, object)  # (sensor_id, StatoSensore)
    # Segnale GLOBALE, non per singolo sensore: True = il sistema sta
    # ricevendo dati validi (marcatore/slot riconosciuti), False = nessun
    # conteggio - tipicamente gli Arduino della catena di sincronizzazione
    # non sono collegati/alimentati. Qui simulato con un checkbox nel
    # pannello di test; nella sorgente reale (gui.py) e' un vero watchdog a
    # tempo sull'ultimo evento di protocollo riconosciuto.
    connessione_globale_cambiata = Signal(bool)

    def __init__(self, id_sensori_attivi, parent=None):
        super().__init__(parent)
        self._stati = {i: StatoSensore.LIBERO for i in id_sensori_attivi}
        self._connesso_globale = True

        self._timer_auto = QTimer(self)
        self._timer_auto.setInterval(3500)
        self._timer_auto.timeout.connect(self._simula_evento_casuale)

    def avvia_simulazione_automatica(self):
        self._timer_auto.start()

    def ferma_simulazione_automatica(self):
        self._timer_auto.stop()

    def stato_corrente(self, sensor_id: int) -> StatoSensore:
        return self._stati[sensor_id]

    def imposta_stato(self, sensor_id: int, stato: StatoSensore):
        if self._stati.get(sensor_id) != stato:
            self._stati[sensor_id] = stato
            self.stato_cambiato.emit(sensor_id, stato)

    def commuta_stato(self, sensor_id: int):
        nuovo = (StatoSensore.LIBERO if self._stati[sensor_id] == StatoSensore.ALLARME
                  else StatoSensore.ALLARME)
        self.imposta_stato(sensor_id, nuovo)

    def _simula_evento_casuale(self):
        sensor_id = random.choice(list(self._stati.keys()))
        self.commuta_stato(sensor_id)

    def imposta_connessione_globale(self, connesso: bool):
        if self._connesso_globale != connesso:
            self._connesso_globale = connesso
            self.connessione_globale_cambiata.emit(connesso)


# =============================================================================
# PALETTE COLORI - coerente col logo Knitronix (rosso/nero/bianco)
# =============================================================================

class Palette:
    SFONDO = "#F7F5F4"
    CARD_SFONDO = "#FFFFFF"
    TESTO_PRINCIPALE = "#1A1A1A"
    TESTO_SECONDARIO = "#6B6560"
    BORDO = "#DCD8D4"
    BORDO_TRATTEGGIATO = "#BFBAB6"

    LIBERO_BG = "#E9E7E5"
    LIBERO_ACCENTO = "#1A1A1A"
    ALLARME_BG = "#FBE2DF"
    ALLARME_ACCENTO = "#E13A2E"
    NON_COLLEGATO_BG = "#EDEBE9"
    NON_COLLEGATO_ACCENTO = "#BFBAB6"
    # Ambra: avviso distinto sia dal grigio "non collegato" (placeholder
    # statico, sensore mai cablato) sia dal rosso "allarme" (pressione
    # rilevata) - segnala "non so cosa sta succedendo qui ora", non
    # "tutto ok" ne' "allarme confermato".
    DISCONNESSO_BG = "#FDF3DC"
    DISCONNESSO_ACCENTO = "#C98A1D"


# =============================================================================
# WIDGET: singolo sensore
# =============================================================================

class SensoreWidget(QFrame):
    """
    Rappresenta visivamente un singolo sensore della catena:
    - nome ("Sensore N")
    - blocco colorato che riflette lo stato
    - etichetta testuale di stato
    - timestamp dell'ultimo cambio (solo se attivo)
    - "spinotto" verticale che lo collega visivamente al cavo comune
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
        larghezza = 100 if attivo else 75
        padding = 10 if attivo else 8
        self._dim_blocco = (60, 85) if attivo else (40, 60)

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
        # Altezza minima riservata per 2 righe, calcolata dalla metrica REALE
        # del font invece che indovinata a mano (un valore indovinato non
        # bastava: dentro la QScrollArea annidata di CatenaSensoriWidget il
        # calcolo automatico di heightForWidth per questa label non riserva
        # spazio a sufficienza da solo, le due righe di "Non collegato"/
        # "⚠ Segnale assente" venivano disegnate sovrapposte invece che
        # impilate - lineSpacing() e' la vera altezza riga a riga di QUESTO
        # font su QUESTO sistema, non una stima).
        metriche_stato = QFontMetrics(self.lbl_stato.font())
        self.lbl_stato.setMinimumHeight(metriche_stato.lineSpacing() * 2 + 4)
        layout.addWidget(self.lbl_stato)

        self.lbl_timestamp = QLabel()
        self.lbl_timestamp.setAlignment(Qt.AlignCenter)
        self.lbl_timestamp.setWordWrap(True)
        self.lbl_timestamp.setFont(QFont("Helvetica", 8))
        self.lbl_timestamp.setStyleSheet(f"color: {Palette.TESTO_SECONDARIO};")
        metriche_ts = QFontMetrics(self.lbl_timestamp.font())
        self.lbl_timestamp.setMinimumHeight(metriche_ts.lineSpacing() * 2 + 4)
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
    """Mostra la fila di sensori con una linea (cavo) che li collega visivamente."""

    def __init__(self, sensori: dict, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # Contenitore scrollabile orizzontale per la fila di card
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        contenuto = QWidget()
        contenuto.setStyleSheet("background: transparent;")
        riga = QHBoxLayout(contenuto)
        riga.setSpacing(8)
        riga.setContentsMargins(4, 4, 4, 10)
        riga.setAlignment(Qt.AlignLeft | Qt.AlignBottom)

        for numero, widget in sensori.items():
            colonna = QVBoxLayout()
            colonna.setSpacing(0)
            colonna.setAlignment(Qt.AlignHCenter)
            colonna.addWidget(widget)

            spinotto = QFrame()
            spinotto.setFixedSize(2, 16)
            spinotto.setStyleSheet(f"background-color: {Palette.TESTO_PRINCIPALE};")
            colonna.addWidget(spinotto, alignment=Qt.AlignHCenter)

            wrapper = QWidget()
            wrapper.setLayout(colonna)
            riga.addWidget(wrapper)

        scroll.setWidget(contenuto)
        layout.addWidget(scroll)

        caption = QLabel("— cavo unico che collega la catena di sensori")
        caption.setFont(QFont("Helvetica", 11))
        caption.setStyleSheet(f"color: {Palette.TESTO_SECONDARIO};")
        layout.addWidget(caption)

    def paintEvent(self, event):
        """Disegna la linea del cavo dietro le card, a un'altezza fissa dal top."""
        from PySide6.QtGui import QPainter, QPen
        painter = QPainter(self)
        pen = QPen(QColor(Palette.TESTO_PRINCIPALE))
        pen.setWidth(4)
        painter.setPen(pen)
        y = 88  # altezza approssimativa del centro dei blocchi colore
        painter.drawLine(20, y, self.width() - 20, y)
        super().paintEvent(event)


# =============================================================================
# FINESTRA PRINCIPALE
# =============================================================================

class FinestraPrincipale(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sistema di monitoraggio - Knitronix")
        self.setMinimumSize(1000, 560)
        self.setStyleSheet(f"QMainWindow {{ background-color: {Palette.SFONDO}; }}")

        id_attivi = list(range(1, N_SENSORI_ATTIVI + 1))
        self.sorgente = SimulatedAlarmSource(id_attivi)
        self.sorgente.stato_cambiato.connect(self._on_stato_cambiato)
        self.sorgente.connessione_globale_cambiata.connect(self._on_connessione_globale_cambiata)

        self._costruisci_ui()
        self.sorgente.avvia_simulazione_automatica()

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

        layout_principale.addWidget(self._crea_pannello_test())

    def _crea_header(self) -> QWidget:
        header = QFrame()
        header.setStyleSheet(f"border-bottom: 3px solid {Palette.TESTO_PRINCIPALE};")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 18)
        layout.setSpacing(18)

        lbl_logo = QLabel()
        pixmap = QPixmap("logo-knitronix.png")
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

    def _crea_pannello_test(self) -> QWidget:
        """Pannello di controllo per generare eventi di test (dati simulati)."""
        pannello = QFrame()
        pannello.setStyleSheet(f"""
            QFrame {{
                background-color: {Palette.CARD_SFONDO};
                border: 1px dashed {Palette.BORDO_TRATTEGGIATO};
                border-radius: 14px;
            }}
        """)
        layout = QHBoxLayout(pannello)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(16)

        etichetta = QLabel("SIMULAZIONE (SOLO PER TEST):")
        etichetta.setStyleSheet(f"color: {Palette.TESTO_SECONDARIO};")
        etichetta.setFont(QFont("Helvetica", 12, QFont.DemiBold))
        layout.addWidget(etichetta)

        colori_bottoni = [Palette.TESTO_PRINCIPALE, Palette.ALLARME_ACCENTO]
        for idx, sensor_id in enumerate(range(1, N_SENSORI_ATTIVI + 1)):
            colore = colori_bottoni[idx % len(colori_bottoni)]
            bottone = QPushButton(f"Commuta Sensore {sensor_id}")
            bottone.setCursor(Qt.PointingHandCursor)
            bottone.setStyleSheet(f"""
                QPushButton {{
                    background-color: {colore};
                    border: 1px solid {colore};
                    border-radius: 10px;
                    padding: 8px 16px;
                    color: white;
                    font-weight: bold;
                    font-size: 13px;
                }}
                QPushButton:hover {{ opacity: 0.9; }}
            """)
            bottone.clicked.connect(lambda _, s=sensor_id: self.sorgente.commuta_stato(s))
            layout.addWidget(bottone)

        layout.addStretch(1)

        checkbox_connessione = QCheckBox("Simula: nessun segnale dai sensori")
        checkbox_connessione.setFont(QFont("Helvetica", 12))
        checkbox_connessione.toggled.connect(
            lambda spento: self.sorgente.imposta_connessione_globale(not spento)
        )
        layout.addWidget(checkbox_connessione)

        return pannello

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
