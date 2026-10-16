"""
others/widget_timer.py — WAKEUPeSTALTA
---------------------------------------
Pannello dei timer di riarmo (tab 2 orizzontale, tab 3 compatto in colonna).

Due timer riportano il sistema in ASCOLTO (la logica resta in main.py,
controlla_transizioni_temporali - qui si mostra e si regola soltanto):

  INATTIVITÀ    prioritario. Si azzera ad ogni chiusura: scade quando i
                tappeti restano fermi per N minuti.
  TETTO MASSIMO scorre sempre dall'ingresso in ARMATO e non si azzera mai:
                entra in gioco con attività continua. "Mai" = nessun tetto.

Il timer che sta davvero comandando il riarmo è evidenziato (bordo e alone
del suo colore); l'altro resta visibile ma in secondo piano.

API:
    widget = WidgetTimer(2.0, 5.0, mai=False, compatto=False)
    widget.timeout_inattivita_cambiato.connect(...)   # float, minuti
    widget.timeout_massimo_cambiato.connect(...)      # float, minuti (inf se "Mai")
    widget.segna_allarme()                            # all'ingresso in RISVEGLIO
    widget.aggiorna(stato, ora, ultima_chiusura_s, inizio_armato_s,
                     inizio_risveglio_s, attesa_boot_s)  # ogni tick (200 ms)
    widget.stepper_inattivita.spin / stepper_massimo.spin / switch_mai
                                                        # per sincronizzare 2 istanze

Lanciato da solo (python others/widget_timer.py) mostra una demo simulata.
"""

from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from .tema import (ACCENTO, AMBRA, BG_ELEVATO, BLU, BORDO, BORDO_FORTE, FONT_NOME, MARGINI_CARD_COMPATTA,
                   RAGGIO_CARD, ROSSO, T_ETICHETTA, T_GRANDE, TESTO, TESTO_TENUE, VERDE,
                   Stepper, rgba, separatore, stile_chip)

SOGLIA_ATTIVITA_IN_CORSO_S = 1.5  # come in main.py: chiusura più recente di così = attività continua


def _mmss(secondi: float) -> str:
    secondi = max(0, int(math.ceil(secondi)))
    minuti, sec = divmod(secondi, 60)
    return f"{minuti:02d}:{sec:02d}"


# ======================================================================
# Mattoncini
# ======================================================================
class _AnelloTimer(QtWidgets.QWidget):
    """Countdown circolare: l'arco si consuma in senso orario."""

    def __init__(self, colore: str, lato: int = 120, parent=None):
        super().__init__(parent)
        self.setFixedSize(lato, lato)
        self.colore = QtGui.QColor(colore)
        self.frazione = 1.0
        self.testo = "--:--"
        self.sotto = ""
        self.modo = "idle"   # idle | attivo | pausa | infinito

    def imposta(self, frazione: float, testo: str, sotto: str, modo: str) -> None:
        frazione = max(0.0, min(1.0, frazione))
        if (frazione, testo, sotto, modo) != (self.frazione, self.testo, self.sotto, self.modo):
            self.frazione, self.testo, self.sotto, self.modo = frazione, testo, sotto, modo
            self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        lato = self.width()
        spessore = max(6.0, lato * 0.065)
        margine = spessore + 4
        r = QtCore.QRectF(margine, margine, lato - 2 * margine, lato - 2 * margine)

        p.setPen(QtGui.QPen(QtGui.QColor(BORDO_FORTE), spessore))
        p.drawEllipse(r)

        colore = QtGui.QColor(self.colore)
        frazione = self.frazione
        if self.modo == "infinito":
            colore.setAlpha(70)
            frazione = 1.0
        elif self.modo == "pausa":
            colore.setAlpha(90)
        elif self.modo == "idle":
            colore.setAlpha(120)

        if frazione > 0:
            inizio, ampiezza = 90 * 16, -int(frazione * 360 * 16)
            if self.modo == "attivo":
                alone = QtGui.QColor(colore); alone.setAlpha(55)
                penna = QtGui.QPen(alone, spessore + 7)
                penna.setCapStyle(QtCore.Qt.RoundCap)
                p.setPen(penna)
                p.drawArc(r, inizio, ampiezza)
            penna = QtGui.QPen(colore, spessore)
            penna.setCapStyle(QtCore.Qt.RoundCap)
            if self.modo == "pausa":
                penna.setStyle(QtCore.Qt.DotLine)
            p.setPen(penna)
            p.drawArc(r, inizio, ampiezza)

            if self.modo == "attivo" and frazione < 1.0:
                angolo = math.radians(90 - frazione * 360)
                cx = r.center().x() + r.width() / 2 * math.cos(angolo)
                cy = r.center().y() - r.height() / 2 * math.sin(angolo)
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(QtGui.QColor("white"))
                p.drawEllipse(QtCore.QPointF(cx, cy), 3.2, 3.2)

        font = QtGui.QFont(FONT_NOME)
        font.setWeight(QtGui.QFont.Weight.Bold)
        font.setPixelSize(int(lato * (0.27 if self.modo == "infinito" else 0.19)))
        p.setFont(font)
        p.setPen(QtGui.QColor(TESTO if self.modo == "attivo" else TESTO_TENUE))
        p.drawText(QtCore.QRectF(r.left(), r.center().y() - lato * 0.17, r.width(), lato * 0.24),
                   QtCore.Qt.AlignCenter, self.testo)

        font.setPixelSize(max(9, int(lato * 0.08)))
        font.setLetterSpacing(QtGui.QFont.PercentageSpacing, 110)
        p.setFont(font)
        p.setPen(QtGui.QColor(self.colore if self.modo in ("attivo", "pausa") else TESTO_TENUE))
        p.drawText(QtCore.QRectF(r.left() + 6, r.center().y() + lato * 0.08, r.width() - 12, lato * 0.13),
                   QtCore.Qt.AlignCenter, self.sotto)
        p.end()


class _Interruttore(QtWidgets.QAbstractButton):
    """Switch a pillola con pomello animato."""

    def __init__(self, colore: str, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._colore = QtGui.QColor(colore)
        self._pos = 0.0
        self._hover = False
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._su_pos)
        self.toggled.connect(self._anima)

    def _su_pos(self, v):
        self._pos = float(v)
        self.update()

    def _anima(self, acceso: bool):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if acceso else 0.0)
        self._anim.start()

    def enterEvent(self, e):
        self._hover = True; self.update(); super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False; self.update(); super().leaveEvent(e)

    def sizeHint(self):
        return QtCore.QSize(44, 24)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        spento = QtGui.QColor(BORDO_FORTE).lighter(118 if self._hover else 100)
        acceso = QtGui.QColor(self._colore).lighter(112 if self._hover else 100)
        t = self._pos
        traccia = QtGui.QColor(
            int(spento.red() + (acceso.red() - spento.red()) * t),
            int(spento.green() + (acceso.green() - spento.green()) * t),
            int(spento.blue() + (acceso.blue() - spento.blue()) * t),
        )
        r = QtCore.QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(traccia)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = 3 + t * (r.width() - d - 6)
        p.setBrush(QtGui.QColor("white"))
        p.drawEllipse(QtCore.QRectF(x + 0.5, 3.5, d, d))
        p.end()


class _Badge(QtWidgets.QWidget):
    """Pillola disegnata a mano: dentro una card con QGraphicsEffect il
    border-radius del QSS viene perso (angoli squadrati)."""

    def __init__(self, testo: str, colore: str, parent=None):
        super().__init__(parent)
        self._testo = testo
        self._colore = QtGui.QColor(colore)
        self._font = QtGui.QFont(FONT_NOME)
        self._font.setPixelSize(10)
        self._font.setWeight(QtGui.QFont.Weight.ExtraBold)
        self._font.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.0)
        larghezza = QtGui.QFontMetrics(self._font).horizontalAdvance(testo) + 18
        self.setFixedSize(larghezza, 18)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        sfondo = QtGui.QColor(self._colore); sfondo.setAlpha(30)
        bordo = QtGui.QColor(self._colore); bordo.setAlpha(110)
        p.setPen(QtGui.QPen(bordo, 1))
        p.setBrush(sfondo)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setFont(self._font)
        p.setPen(self._colore)
        p.drawText(r, QtCore.Qt.AlignCenter, self._testo)
        p.end()


class _CardTimer(QtWidgets.QFrame):
    def __init__(self, titolo: str, colore: str, descrizione: str, badge: str | None = None,
                 lato_anello: int = 120, parent=None):
        super().__init__(parent)
        self.setObjectName("CardTimer")
        self.colore = colore
        self._attiva = None
        # Minimum, non Maximum: con la descrizione che va a capo Qt
        # sottostima l'altezza e "Maximum" taglierebbe lo stepper.
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)

        esterno = QtWidgets.QVBoxLayout(self)
        # Margini compatti: in tab 2 il pannello sta sotto al grafico e con
        # schermi scalati (125-150%) l'altezza logica è poca.
        esterno.setContentsMargins(*MARGINI_CARD_COMPATTA)
        esterno.setSpacing(6)

        testata = QtWidgets.QHBoxLayout()
        testata.setSpacing(8)
        t = QtWidgets.QLabel(titolo)
        t.setStyleSheet(f"color: {colore}; font-size: {T_ETICHETTA}px; font-weight: 800; letter-spacing: 1px;")
        testata.addWidget(t)
        if badge:
            testata.addWidget(_Badge(badge, colore))
        testata.addStretch(1)
        self.label_stato = QtWidgets.QLabel("")
        testata.addWidget(self.label_stato)
        esterno.addLayout(testata)

        corpo = QtWidgets.QHBoxLayout()
        corpo.setSpacing(16)
        self.anello = _AnelloTimer(colore, lato_anello)
        corpo.addWidget(self.anello, 0, QtCore.Qt.AlignVCenter)

        destra = QtWidgets.QVBoxLayout()
        destra.setSpacing(8)
        destra.addStretch(1)
        desc = QtWidgets.QLabel(descrizione)
        desc.setWordWrap(True)
        desc.setObjectName("Nota")
        destra.addWidget(desc)
        self.area_controlli = QtWidgets.QVBoxLayout()
        self.area_controlli.setSpacing(8)
        destra.addLayout(self.area_controlli)
        destra.addStretch(1)
        corpo.addLayout(destra, stretch=1)
        esterno.addLayout(corpo)

        self._alone = QtWidgets.QGraphicsDropShadowEffect(self)
        self._alone.setOffset(0, 0)
        self._alone.setColor(QtGui.QColor(colore))
        self._alone.setBlurRadius(0)
        self.setGraphicsEffect(self._alone)
        self._anim_alone = QtCore.QPropertyAnimation(self._alone, b"blurRadius", self)
        self._anim_alone.setDuration(300)
        self._anim_alone.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self.imposta_attiva(False, "")

    def imposta_attiva(self, attiva: bool, testo_stato: str) -> None:
        self.label_stato.setText(testo_stato)
        if attiva == self._attiva:
            return
        self._attiva = attiva
        bordo = rgba(self.colore, 200) if attiva else BORDO
        self.setStyleSheet(
            f"QFrame#CardTimer {{ background-color: {BG_ELEVATO}; border: 1px solid {bordo};"
            f" border-radius: {RAGGIO_CARD}px; }}"
        )
        self.label_stato.setStyleSheet(
            f"color: {self.colore if attiva else TESTO_TENUE}; font-size: {T_ETICHETTA}px;"
            " font-weight: 800; letter-spacing: 0.5px;"
        )
        self._anim_alone.stop()
        self._anim_alone.setStartValue(self._alone.blurRadius())
        self._anim_alone.setEndValue(24 if attiva else 0)
        self._anim_alone.start()


class _BloccoHero(QtWidgets.QVBoxLayout):
    """Etichetta maiuscola + numero grande + dettaglio a destra."""

    def __init__(self, titolo: str):
        super().__init__()
        self.setSpacing(2)
        e = QtWidgets.QLabel(titolo)
        e.setObjectName("EyebrowTenue")
        self.addWidget(e)
        riga = QtWidgets.QHBoxLayout()
        riga.setSpacing(10)
        self.valore = QtWidgets.QLabel("--:--")
        riga.addWidget(self.valore)
        self.dettaglio = QtWidgets.QLabel("")
        riga.addWidget(self.dettaglio, 0, QtCore.Qt.AlignVCenter)
        riga.addStretch(1)
        self.addLayout(riga)

    def colora(self, colore: str) -> None:
        self.valore.setStyleSheet(f"color: {colore}; font-size: {T_GRANDE}px; font-weight: 800;")


# ======================================================================
# Pannello
# ======================================================================
class WidgetTimer(QtWidgets.QWidget):
    timeout_inattivita_cambiato = QtCore.Signal(float)
    timeout_massimo_cambiato = QtCore.Signal(float)   # inf = "Mai"

    def __init__(self, timeout_inattivita_min: float = 2.0, timeout_massimo_min: float = 5.0,
                 mai: bool = False, compatto: bool = False, parent=None):
        super().__init__(parent)
        if math.isinf(timeout_massimo_min):
            mai, timeout_massimo_min = True, 5.0
        self.timeout_inattivita_min = timeout_inattivita_min
        self.timeout_massimo_min = timeout_massimo_min
        self.mai = mai
        self._allarme_epoch: float | None = None
        self._stile_hero = None
        lato = 100 if compatto else 104

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._crea_hero(compatto))

        contenitore_card = QtWidgets.QVBoxLayout() if compatto else QtWidgets.QHBoxLayout()
        contenitore_card.setSpacing(10)

        self.card_inattivita = _CardTimer(
            "TIMER DI INATTIVITÀ", ACCENTO,
            "Riarma quando i tappeti restano inattivi: mai premuti. Riparte da capo ad ogni attività sul tappeto.",
            badge="PRIORITARIO", lato_anello=lato,
        )
        self.stepper_inattivita = Stepper(0.5, 120.0, timeout_inattivita_min, 0.5, 1, " min", 92)
        self.stepper_inattivita.spin.valueChanged.connect(self._su_inattivita)
        self.card_inattivita.area_controlli.addWidget(self.stepper_inattivita, 0, QtCore.Qt.AlignLeft)
        contenitore_card.addWidget(self.card_inattivita, stretch=1)

        self.card_massimo = _CardTimer(
            "TIMER MASSIMO", AMBRA,
            "Timer di sicurezza: scorre sempre e non si riavvia, neanche con attività continua.",
            lato_anello=lato,
        )
        self.stepper_massimo = Stepper(0.5, 120.0, timeout_massimo_min, 0.5, 1, " min", 92)
        self.stepper_massimo.spin.valueChanged.connect(self._su_massimo)
        self.card_massimo.area_controlli.addWidget(self.stepper_massimo, 0, QtCore.Qt.AlignLeft)
        riga_mai = QtWidgets.QHBoxLayout()
        riga_mai.setSpacing(10)
        self.switch_mai = _Interruttore(AMBRA)
        self.switch_mai.setToolTip("Nessun tetto: resta armato finché c'è attività o fino a RESET")
        riga_mai.addWidget(self.switch_mai)
        etichetta_mai = QtWidgets.QLabel("Mai · nessun tetto")
        etichetta_mai.setObjectName("Campo")
        riga_mai.addWidget(etichetta_mai)
        riga_mai.addStretch(1)
        self.card_massimo.area_controlli.addLayout(riga_mai)
        self.switch_mai.setChecked(mai)
        self.stepper_massimo.setEnabled(not mai)
        self.switch_mai.toggled.connect(self._su_mai)
        contenitore_card.addWidget(self.card_massimo, stretch=1)

        layout.addLayout(contenitore_card)
        if compatto:
            layout.addStretch(1)
        self.aggiorna("AMBIENTE", time.monotonic(), 0, 0, 0, 0)

    def _crea_hero(self, compatto: bool) -> QtWidgets.QFrame:
        hero = QtWidgets.QFrame()
        hero.setObjectName("Card")
        hero.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        l = QtWidgets.QVBoxLayout(hero) if compatto else QtWidgets.QHBoxLayout(hero)
        l.setContentsMargins(*MARGINI_CARD_COMPATTA)
        l.setSpacing(10 if compatto else 20)

        self.blocco_allarme = _BloccoHero("ULTIMO ALLARME")
        self.blocco_allarme.colora(TESTO_TENUE)
        self.blocco_allarme.valore.setText("--:--:--")
        self.blocco_allarme.dettaglio.setText("nessuno finora")
        self.blocco_allarme.dettaglio.setObjectName("Nota")
        l.addLayout(self.blocco_allarme, stretch=1)
        l.addWidget(separatore(verticale=not compatto))
        self.blocco_riarmo = _BloccoHero("RITORNO IN ASCOLTO")
        l.addLayout(self.blocco_riarmo, stretch=1)

        # alias usati da chi legge il pannello dall'esterno
        self.label_allarme_ora = self.blocco_allarme.valore
        self.label_allarme_fa = self.blocco_allarme.dettaglio
        self.label_riarmo = self.blocco_riarmo.valore
        self.chip_motivo = self.blocco_riarmo.dettaglio
        return hero

    # ------------------------------------------------------------------
    # Controlli
    # ------------------------------------------------------------------
    def _su_inattivita(self, v: float) -> None:
        self.timeout_inattivita_min = v
        self.timeout_inattivita_cambiato.emit(v)

    def _su_massimo(self, v: float) -> None:
        self.timeout_massimo_min = v
        if not self.mai:
            self.timeout_massimo_cambiato.emit(v)

    def _su_mai(self, mai: bool) -> None:
        if mai == self.mai:
            return
        self.mai = mai
        self.stepper_massimo.setEnabled(not mai)
        self.timeout_massimo_cambiato.emit(float("inf") if mai else self.timeout_massimo_min)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def segna_allarme(self) -> None:
        self._allarme_epoch = time.time()
        self.label_allarme_ora.setText(time.strftime("%H:%M:%S"))
        self.blocco_allarme.colora(ROSSO)

    def _imposta_hero(self, testo: str, colore: str, chip: str) -> None:
        self.label_riarmo.setText(testo)
        chiave = (colore, chip)
        if chiave != self._stile_hero:
            self._stile_hero = chiave
            self.blocco_riarmo.colora(colore)
            self.chip_motivo.setText(chip)
            self.chip_motivo.setStyleSheet(stile_chip(colore))

    def _aggiorna_allarme_relativo(self) -> None:
        if self._allarme_epoch is None:
            return
        trascorsi = int(time.time() - self._allarme_epoch)
        if trascorsi < 10:
            testo = "adesso"
        elif trascorsi < 60:
            testo = f"{trascorsi} s fa"
        elif trascorsi < 3600:
            testo = f"{trascorsi // 60} min fa"
        else:
            testo = f"{trascorsi // 3600} h fa"
        self.label_allarme_fa.setText(testo)

    def aggiorna(self, stato: str, ora: float, ultima_chiusura_s: float, inizio_armato_s: float,
                 inizio_risveglio_s: float, attesa_boot_s: float) -> None:
        """Da chiamare ad ogni tick di controlla_transizioni_temporali.
        `stato` è Stato.name (AMBIENTE / RISVEGLIO / ARMATO), i tempi sono
        time.monotonic() come in main.py."""
        self._aggiorna_allarme_relativo()
        t_in = self.timeout_inattivita_min * 60
        t_max = self.timeout_massimo_min * 60
        stato = stato.upper()

        if stato == "ARMATO":
            da_chiusura = ora - ultima_chiusura_s
            rim_in = max(0.0, t_in - da_chiusura)
            rim_max = math.inf if self.mai else max(0.0, t_max - (ora - inizio_armato_s))
            attivita = da_chiusura < SOGLIA_ATTIVITA_IN_CORSO_S

            if attivita:
                comanda = None if self.mai else "max"
            else:
                comanda = "in" if rim_in <= rim_max else "max"

            if attivita:
                self.card_inattivita.anello.imposta(1.0, _mmss(t_in), "IN PAUSA", "pausa")
                self.card_inattivita.imposta_attiva(False, "● attività sui tappeti")
            else:
                self.card_inattivita.anello.imposta(rim_in / t_in, _mmss(rim_in), "AL RIARMO",
                                                    "attivo" if comanda == "in" else "idle")
                self.card_inattivita.imposta_attiva(comanda == "in", "IN COMANDO" if comanda == "in" else "")

            if self.mai:
                self.card_massimo.anello.imposta(1.0, "∞", "NESSUN TETTO", "infinito")
                self.card_massimo.imposta_attiva(False, "disattivato")
            else:
                self.card_massimo.anello.imposta(rim_max / t_max, _mmss(rim_max), "AL RIARMO",
                                                 "attivo" if comanda == "max" else "idle")
                self.card_massimo.imposta_attiva(comanda == "max",
                                                 "IN COMANDO" if comanda == "max" else "in sottofondo")

            if comanda is None:
                self._imposta_hero("∞", AMBRA, "attività continua · resta armato")
            elif comanda == "in":
                self._imposta_hero(_mmss(rim_in), ACCENTO, "per inattività")
            else:
                self._imposta_hero(_mmss(rim_max), AMBRA,
                                   "tetto massimo · attività continua" if attivita else "per tetto massimo")
            return

        # AMBIENTE / RISVEGLIO: timer fermi, mostrano la durata impostata
        self.card_inattivita.anello.imposta(1.0, _mmss(t_in), "IMPOSTATO", "idle")
        self.card_inattivita.imposta_attiva(False, "")
        if self.mai:
            self.card_massimo.anello.imposta(1.0, "∞", "NESSUN TETTO", "infinito")
            self.card_massimo.imposta_attiva(False, "disattivato")
        else:
            self.card_massimo.anello.imposta(1.0, _mmss(t_max), "IMPOSTATO", "idle")
            self.card_massimo.imposta_attiva(False, "")

        if stato == "RISVEGLIO":
            rimanenti = max(0.0, attesa_boot_s - (ora - inizio_risveglio_s))
            self._imposta_hero(f"{rimanenti:.1f} s", BLU, "sincronizzazione tappeti")
        else:
            self._imposta_hero("--:--", VERDE, "in ascolto · timer fermi")


# ----------------------------------------------------------------------
# Demo standalone: allarme, attività continua per 15 s, poi silenzio
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from tema import STYLE_QSS, applica_palette

    app = QtWidgets.QApplication([])
    applica_palette(app)
    finestra = QtWidgets.QWidget()
    finestra.setObjectName("Radice")
    finestra.setStyleSheet(STYLE_QSS)
    lay = QtWidgets.QHBoxLayout(finestra)
    pannello = WidgetTimer(0.5, 1.0)
    compatto = WidgetTimer(0.5, 1.0, compatto=True)
    compatto.setFixedWidth(380)
    lay.addWidget(pannello, stretch=1)
    lay.addWidget(compatto)
    finestra.resize(1400, 640)

    sim = {"stato": "AMBIENTE", "t0": time.monotonic(), "chiusura": 0.0, "armato": 0.0}

    def tick():
        ora = time.monotonic()
        dt = ora - sim["t0"]
        if sim["stato"] == "AMBIENTE" and dt > 3:
            sim.update(stato="RISVEGLIO", t0=ora)
            pannello.segna_allarme(); compatto.segna_allarme()
        elif sim["stato"] == "RISVEGLIO" and dt > 2:
            sim.update(stato="ARMATO", t0=ora, chiusura=ora, armato=ora)
        elif sim["stato"] == "ARMATO":
            if dt < 15:
                sim["chiusura"] = ora
            if ora - sim["chiusura"] >= pannello.timeout_inattivita_min * 60 or \
               ora - sim["armato"] >= pannello.timeout_massimo_min * 60:
                sim.update(stato="AMBIENTE", t0=ora)
        for p in (pannello, compatto):
            p.aggiorna(sim["stato"], ora, sim["chiusura"], sim["armato"], sim["t0"], 2.0)

    t = QtCore.QTimer(interval=200, timeout=tick)
    t.start()
    finestra.show()
    app.exec()