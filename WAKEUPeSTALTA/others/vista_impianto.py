"""
others/vista_impianto.py — WAKEUPeSTALTA
-----------------------------------------
Vista grafica dell'impianto: zona circolare (fibra), tappeti 1 e 2, lati
grigi, BASE al centro e pulsante RESET in alto a destra.

    ASCOLTO   (= Stato.AMBIENTE)  anello verde, tappeti 1/2 grigi
    RISVEGLIO                     anello rosso che pulsa, tappeti grigi
    ARMATO                        anello grigio, tappeti 1/2 verde (aperto)
                                  o rosso (chiuso) secondo stato_tappeti;
                                  grigi finché il sensore non è ancora visto

Widget autonomo, nessun import da main.py (niente import circolari): riceve
il nome dello stato come stringa e il dict stato_tappeti così com'è.

API:
    vista = VistaImpianto()
    vista.imposta_stato("AMBIENTE" | "ASCOLTO" | "RISVEGLIO" | "ARMATO")
    vista.imposta_tappeti({1: False, 2: True})   # counter -> chiuso?
    vista.reset_richiesto.connect(...)            # click su RESET

Lanciato da solo (python others/vista_impianto.py) mostra una demo che cicla gli stati.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .tema import BG, BG_INPUT, BORDO, FONT_NOME, ROSSO, TESTO, VERDE

TESTO_SU_COLORE = "#0a1210"
BG_BASE = BG_INPUT     # la BASE al centro: un gradino sopra la card che contiene la vista
GRIGIO_ANELLO = "#5a606b"
GRIGIO_TAPPETO = "#4a4f58"
GRIGIO_LATI = "#3d424b"

_STATI_ASCOLTO = {"AMBIENTE", "ASCOLTO"}

_DIDASCALIA = {
    "ASCOLTO": "IN ASCOLTO",
    "RISVEGLIO": "RISVEGLIO · attivazione tappeti",
    "ARMATO": "ARMATO · tappeti in lettura",
}


class _ColoreAnimato(QtCore.QObject):
    """Colore che sfuma verso il nuovo valore invece di cambiare di colpo."""

    def __init__(self, colore: str, widget: QtWidgets.QWidget, durata_ms: int = 120):
        super().__init__(widget)
        self._widget = widget
        self.colore = QtGui.QColor(colore)
        self._destinazione = QtGui.QColor(colore)
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(durata_ms)
        self._anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._su_valore)

    def _su_valore(self, valore) -> None:
        self.colore = QtGui.QColor(valore)
        self._widget.update()

    def vai_a(self, colore: str) -> None:
        nuovo = QtGui.QColor(colore)
        if nuovo == self._destinazione:
            return
        self._destinazione = nuovo
        self._anim.stop()
        self._anim.setStartValue(QtGui.QColor(self.colore))
        self._anim.setEndValue(nuovo)
        self._anim.start()


class _PulsanteReset(QtWidgets.QPushButton):
    """RESET con hover "luminoso": gradiente più chiaro + alone che si
    accende con un'animazione morbida all'ingresso del mouse."""

    def __init__(self, parent=None):
        super().__init__("⟲  RESET", parent)
        self.setObjectName("Reset")
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedSize(156, 48)
        self.setToolTip("Spegne la smart plug e torna in ASCOLTO")
        # Stylesheet sul widget stesso: vince sulle regole QPushButton
        # generiche di STYLE_QSS in main.py.
        self.setStyleSheet(f"""
            QPushButton#Reset {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #6dbaff, stop:1 #3d8fe0);
                color: #0b1622;
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 10px;
                font-family: 'Segoe UI', sans-serif;
                font-size: 15px;
                font-weight: 800;
                letter-spacing: 1.5px;
            }}
            QPushButton#Reset:hover {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #9fd2ff, stop:1 #5eb1ff);
                border: 1px solid rgba(255, 255, 255, 110);
            }}
            QPushButton#Reset:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3a86d6, stop:1 #2f6fb3);
                padding-top: 3px;
            }}
            QPushButton#Reset:focus {{ border: 2px solid #e4e6eb; }}
        """)

        self._alone = QtWidgets.QGraphicsDropShadowEffect(self)
        self._alone.setOffset(0, 0)
        self._alone.setColor(QtGui.QColor(94, 177, 255, 200))
        self._alone.setBlurRadius(6)
        self.setGraphicsEffect(self._alone)

        self._anim_alone = QtCore.QPropertyAnimation(self._alone, b"blurRadius", self)
        self._anim_alone.setDuration(220)
        self._anim_alone.setEasingCurve(QtCore.QEasingCurve.OutCubic)

    def _anima_alone(self, raggio: float) -> None:
        self._anim_alone.stop()
        self._anim_alone.setStartValue(self._alone.blurRadius())
        self._anim_alone.setEndValue(raggio)
        self._anim_alone.start()

    def enterEvent(self, event):
        self._anima_alone(34)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anima_alone(6)
        super().leaveEvent(event)


class VistaImpianto(QtWidgets.QWidget):
    reset_richiesto = QtCore.Signal()

    def __init__(self, con_reset: bool = True, con_didascalia: bool = True, parent=None):
        """con_reset=False: nessun pulsante RESET sulla figura (in main.py
        il RESET è nell'header, uguale per tutte le tab). In quel caso
        `pulsante_reset` è None e `reset_richiesto` non viene mai emesso."""
        super().__init__(parent)
        # Minimo piccolo: la figura scala con lo spazio disponibile (raggio
        # = min(larghezza, altezza)) invece di imporre la sua misura alla
        # finestra.
        self.setMinimumSize(240, 260)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._con_reset = con_reset
        self._con_didascalia = con_didascalia  # pillola "ARMATO · tappeti in lettura" sotto l'anello
        # Niente riempimento di sfondo: la vista sta dentro una card (tab 3)
        # e prende il suo colore, come ogni altro contenuto dell'app.

        self._stato = "ASCOLTO"
        self._tappeti: dict[int, bool] = {}

        self._col_anello = _ColoreAnimato(VERDE, self)
        self._col_tappeto = {1: _ColoreAnimato(GRIGIO_TAPPETO, self), 2: _ColoreAnimato(GRIGIO_TAPPETO, self)}

        # Pulsazione dell'anello in RISVEGLIO (0 -> 1 -> 0, in loop)
        self._fase = 0.0
        self._pulsazione = QtCore.QVariantAnimation(self)
        self._pulsazione.setStartValue(0.0)
        self._pulsazione.setKeyValueAt(0.5, 1.0)
        self._pulsazione.setEndValue(0.0)
        self._pulsazione.setDuration(1000)
        self._pulsazione.setLoopCount(-1)
        self._pulsazione.setEasingCurve(QtCore.QEasingCurve.InOutSine)
        self._pulsazione.valueChanged.connect(self._su_fase)

        self.pulsante_reset = None
        if con_reset:
            self.pulsante_reset = _PulsanteReset(self)
            self.pulsante_reset.clicked.connect(self.reset_richiesto.emit)

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------
    def imposta_stato(self, stato: str) -> None:
        stato = "ASCOLTO" if stato.upper() in _STATI_ASCOLTO else stato.upper()
        if stato not in _DIDASCALIA:
            return
        self._stato = stato
        self._col_anello.vai_a({"ASCOLTO": VERDE, "RISVEGLIO": ROSSO, "ARMATO": GRIGIO_ANELLO}[stato])
        if stato == "RISVEGLIO":
            self._pulsazione.start()
        else:
            self._pulsazione.stop()
            self._fase = 0.0
        self._aggiorna_colori_tappeti()
        self.update()

    def imposta_tappeti(self, stato_tappeti: dict[int, bool]) -> None:
        self._tappeti = dict(stato_tappeti)
        self._aggiorna_colori_tappeti()

    # ------------------------------------------------------------------
    def _su_fase(self, valore) -> None:
        self._fase = float(valore)
        self.update()

    def _aggiorna_colori_tappeti(self) -> None:
        for numero, colore in self._col_tappeto.items():
            if self._stato == "ARMATO" and numero in self._tappeti:
                colore.vai_a(ROSSO if self._tappeti[numero] else VERDE)
            else:
                colore.vai_a(GRIGIO_TAPPETO)

    def sizeHint(self):
        return QtCore.QSize(520, 500)

    def resizeEvent(self, event):
        margine = 4
        if self.pulsante_reset is not None:
            self.pulsante_reset.move(self.width() - self.pulsante_reset.width() - margine, margine)
        super().resizeEvent(event)

    # ------------------------------------------------------------------
    # Disegno
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        spazio_didascalia = 56 if self._con_didascalia else 0
        R = max(40.0, min(self.width() * 0.44, (self.height() - spazio_didascalia) * 0.46))
        centro = QtCore.QPointF(self.width() / 2, (self.height() - spazio_didascalia) / 2 + (8 if self._con_didascalia else 0))
        spessore = R * 0.12
        r_anello = R - spessore / 2
        colore = self._col_anello.colore

        # Riflesso interno tenue del colore dell'anello
        gradiente = QtGui.QRadialGradient(centro, r_anello)
        interno = QtGui.QColor(colore); interno.setAlpha(0)
        bordo_int = QtGui.QColor(colore); bordo_int.setAlpha(28 + int(40 * self._fase))
        gradiente.setColorAt(0.0, interno)
        gradiente.setColorAt(0.75, interno)
        gradiente.setColorAt(1.0, bordo_int)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(gradiente)
        p.drawEllipse(centro, r_anello, r_anello)

        # Alone dell'anello (più forte e pulsante in RISVEGLIO)
        p.setBrush(QtCore.Qt.NoBrush)
        intensita = 1.0 + 1.6 * self._fase
        for allargamento, alfa in ((2.2, 14), (1.6, 24), (1.25, 40)):
            c = QtGui.QColor(colore)
            c.setAlpha(min(255, int(alfa * intensita)))
            p.setPen(QtGui.QPen(c, spessore * (allargamento + 0.35 * self._fase)))
            p.drawEllipse(centro, r_anello, r_anello)

        p.setPen(QtGui.QPen(colore, spessore))
        p.drawEllipse(centro, r_anello, r_anello)

        def rettangolo(dx, dy, w, h):
            return QtCore.QRectF(centro.x() + dx * R - w * R / 2,
                                 centro.y() + dy * R - h * R / 2, w * R, h * R)

        raggio_angoli = R * 0.025

        # Lati verticali grigi
        for dx in (-0.51, 0.51):
            r = rettangolo(dx, 0, 0.14, 0.83)
            p.setPen(QtGui.QPen(QtGui.QColor(BORDO), 1))
            p.setBrush(QtGui.QColor(GRIGIO_LATI))
            p.drawRoundedRect(r, raggio_angoli, raggio_angoli)

        # BASE
        r_base = rettangolo(0, 0, 0.67, 0.44)
        p.setPen(QtGui.QPen(QtGui.QColor(BORDO), 1.5))
        p.setBrush(QtGui.QColor(BG_BASE))
        p.drawRoundedRect(r_base, raggio_angoli * 2, raggio_angoli * 2)
        font = QtGui.QFont(FONT_NOME)
        font.setPixelSize(max(12, int(R * 0.11)))
        font.setWeight(QtGui.QFont.Weight.ExtraBold)
        font.setLetterSpacing(QtGui.QFont.PercentageSpacing, 108)
        p.setFont(font)
        p.setPen(QtGui.QColor(TESTO))
        p.drawText(r_base, QtCore.Qt.AlignCenter, "BASE")

        # Tappeti 1 (alto) e 2 (basso)
        for numero, dy in ((1, -0.42), (2, 0.42)):
            r = rettangolo(0, dy, 0.81, 0.25)
            c = self._col_tappeto[numero].colore
            acceso = self._stato == "ARMATO" and numero in self._tappeti
            if acceso:
                for allarg, alfa in ((10, 30), (5, 55)):
                    a = QtGui.QColor(c); a.setAlpha(alfa)
                    p.setPen(QtGui.QPen(a, allarg))
                    p.setBrush(QtCore.Qt.NoBrush)
                    p.drawRoundedRect(r, raggio_angoli, raggio_angoli)
            p.setPen(QtGui.QPen(c.lighter(125), 1))
            p.setBrush(c)
            p.drawRoundedRect(r, raggio_angoli, raggio_angoli)
            font.setPixelSize(max(11, int(R * 0.085)))
            p.setFont(font)
            p.setPen(QtGui.QColor(TESTO_SU_COLORE if acceso else TESTO))
            p.drawText(r, QtCore.Qt.AlignCenter, str(numero))

        # Didascalia di stato sotto l'anello, a pillola
        if not self._con_didascalia:
            p.end()
            return
        testo = _DIDASCALIA[self._stato]
        font_did = QtGui.QFont(FONT_NOME)
        font_did.setPixelSize(13)
        font_did.setWeight(QtGui.QFont.Weight.Bold)
        font_did.setLetterSpacing(QtGui.QFont.PercentageSpacing, 110)
        p.setFont(font_did)
        larghezza = QtGui.QFontMetrics(font_did).horizontalAdvance(testo) + 52
        pillola = QtCore.QRectF(centro.x() - larghezza / 2, centro.y() + R + 14, larghezza, 32)
        sfondo = QtGui.QColor(colore); sfondo.setAlpha(36)
        bordo = QtGui.QColor(colore); bordo.setAlpha(140)
        p.setPen(QtGui.QPen(bordo, 1))
        p.setBrush(sfondo)
        p.drawRoundedRect(pillola, 16, 16)
        p.setBrush(colore.lighter(115))
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(QtCore.QPointF(pillola.left() + 18, pillola.center().y()), 4.5, 4.5)
        p.setPen(colore.lighter(130))
        p.drawText(pillola.adjusted(18, 0, 0, 0), QtCore.Qt.AlignCenter, testo)
        p.end()


# ----------------------------------------------------------------------
# Demo standalone
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import itertools

    from tema import STYLE_QSS, applica_palette

    app = QtWidgets.QApplication([])
    applica_palette(app)
    vista = VistaImpianto()
    vista.setObjectName("Radice")
    vista.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    vista.setStyleSheet(STYLE_QSS)
    vista.resize(900, 760)
    vista.setWindowTitle("VistaImpianto — demo")

    sequenza = itertools.cycle([
        ("ASCOLTO", {}),
        ("RISVEGLIO", {}),
        ("ARMATO", {}),
        ("ARMATO", {1: False, 2: True}),
        ("ARMATO", {1: True, 2: False}),
    ])

    def passo():
        stato, tappeti = next(sequenza)
        vista.imposta_stato(stato)
        vista.imposta_tappeti(tappeti)

    def reset():
        vista.imposta_stato("ASCOLTO")
        vista.imposta_tappeti({})

    vista.reset_richiesto.connect(reset)
    timer = QtCore.QTimer(interval=2500, timeout=passo)
    timer.start()
    passo()
    vista.show()
    app.exec()