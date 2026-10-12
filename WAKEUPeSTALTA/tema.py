"""
tema.py — WAKEUPeSTALTA
-----------------------
Un solo design system per main.py, pannello_timer.py e vista_impianto.py:
palette, scala tipografica, spaziature, QSS globale e i pochi componenti
riusati ovunque (card con titolo, stepper −/+, stile dei grafici).

Regole che tengono insieme le tre tab:
  - superfici: sfondo app BG -> card BG_ELEVATO (bordo 1px, raggio 12) ->
    riquadri incassati (grafici, log) BG_INSET
  - testo: 4 taglie sole -> 11 (etichette maiuscole), 13 (testo), 15
    (valori), 26 (numeri grandi)
  - spazi: multipli di 4 -> 8 / 12 / 16
  - colori di stato: ACCENTO turchese = inattività / azione primaria,
    AMBRA = tetto massimo / storico STA-LTA, ROSSO = allarme / soglia ON,
    BLU = soglia OFF / reset, VERDE = ascolto / tappeto aperto
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# ----------------------------------------------------------------------
# Palette
# ----------------------------------------------------------------------
COLORI = {
    "bg": "#14161a",
    "bg_elevato": "#1c1f24",
    "bg_input": "#232730",
    "bg_lista": "#111317",      # superfici incassate: grafici, log
    "bordo": "#2b2f37",
    "bordo_forte": "#3a3f48",
    "testo": "#e4e6eb",
    "testo_tenue": "#8b92a0",
    "testo_spento": "#5b616c",
    "accento": "#00d2c4",
    "accento_hover": "#33ddd1",
    "accento_testo_su": "#0a1210",
    "pericolo": "#ff4757",
    "ambra": "#e8a33d",
    "blu": "#5eb1ff",
    "verde": "#1fbf75",
}

BG = COLORI["bg"]
BG_ELEVATO = COLORI["bg_elevato"]
BG_INPUT = COLORI["bg_input"]
BG_INSET = COLORI["bg_lista"]
BORDO = COLORI["bordo"]
BORDO_FORTE = COLORI["bordo_forte"]
TESTO = COLORI["testo"]
TESTO_TENUE = COLORI["testo_tenue"]
TESTO_SPENTO = COLORI["testo_spento"]
ACCENTO = COLORI["accento"]
AMBRA = COLORI["ambra"]
ROSSO = COLORI["pericolo"]
BLU = COLORI["blu"]
VERDE = COLORI["verde"]

# ----------------------------------------------------------------------
# Tipografia e spazi
# ----------------------------------------------------------------------
FONT = "'Segoe UI', 'Inter', sans-serif"
FONT_MONO = "'Cascadia Mono', 'Consolas', monospace"
FONT_NOME = "Segoe UI"

T_ETICHETTA = 11   # etichette maiuscole (eyebrow)
T_TESTO = 13
T_VALORE = 15
T_GRANDE = 26

SPAZIO = 12                     # tra card
MARGINI_CARD = (16, 14, 16, 14)
MARGINI_CARD_COMPATTA = (14, 10, 14, 10)   # colonne di controlli che non devono scrollare
RAGGIO_CARD = 12


def rgba(colore: str, alfa: int) -> str:
    c = QtGui.QColor(colore)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alfa})"


def stile_chip(colore: str) -> str:
    """Pillola colorata (stato, motivo del riarmo, legende)."""
    return (
        f"color: {colore}; background: {rgba(colore, 32)}; border: 1px solid {rgba(colore, 110)};"
        f"border-radius: 11px; padding: 3px 10px; font-size: {T_ETICHETTA + 1}px; font-weight: 700;"
    )


# ----------------------------------------------------------------------
# QSS globale
# ----------------------------------------------------------------------
STYLE_QSS = f"""
/* Testo e font ovunque, ma lo sfondo SOLO sulla radice: un QWidget
   contenitore dentro una card non deve dipingere una toppa scura. */
QWidget {{
    color: {TESTO};
    font-family: {FONT};
    font-size: {T_TESTO}px;
}}
QMainWindow, QWidget#Radice {{ background-color: {BG}; }}
QLabel {{ background: transparent; }}
QToolTip {{
    background-color: {BG_INPUT}; color: {TESTO};
    border: 1px solid {BORDO_FORTE}; border-radius: 6px; padding: 6px 8px;
}}

/* ---- superfici ---- */
QFrame#Card, QFrame#Header {{
    background-color: {BG_ELEVATO};
    border: 1px solid {BORDO};
    border-radius: {RAGGIO_CARD}px;
}}
QFrame#Separatore {{ background-color: {BORDO}; border: none; max-height: 1px; min-height: 1px; }}
QFrame#SeparatoreV {{ background-color: {BORDO}; border: none; max-width: 1px; min-width: 1px; }}

/* ---- testo ---- */
QLabel#Eyebrow {{
    color: {ACCENTO}; font-size: {T_ETICHETTA}px; font-weight: 800; letter-spacing: 1px;
}}
QLabel#EyebrowTenue {{
    color: {TESTO_TENUE}; font-size: {T_ETICHETTA}px; font-weight: 800; letter-spacing: 1px;
}}
QLabel#Nota {{ color: {TESTO_TENUE}; font-size: 12px; }}
QLabel#Mono {{ color: {TESTO_TENUE}; font-family: {FONT_MONO}; font-size: 12px; }}
QLabel#Campo {{ color: {TESTO}; font-size: {T_TESTO}px; font-weight: 600; }}

/* ---- tab ---- */
QTabWidget::pane {{ border: none; background: transparent; top: 0px; }}
QTabWidget::tab-bar {{ left: 0px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {TESTO_TENUE};
    padding: 7px 18px;
    margin: 0 6px 8px 0;
    border: 1px solid transparent;
    border-radius: 9px;
    font-size: {T_TESTO}px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    background-color: {BG_ELEVATO};
    color: {ACCENTO};
    border: 1px solid {BORDO};
}}
QTabBar::tab:hover:!selected {{ color: {TESTO}; background-color: {rgba(BG_ELEVATO, 140)}; }}
QTabBar::tab:disabled {{ color: {TESTO_SPENTO}; }}

/* ---- pulsanti ---- */
QPushButton {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDO_FORTE};
    border-radius: 8px;
    padding: 8px 16px;
    color: {TESTO};
    font-size: {T_TESTO}px;
    font-weight: 600;
}}
QPushButton:hover {{ background-color: #2a2f38; border-color: {ACCENTO}; color: {ACCENTO}; }}
QPushButton:pressed {{ background-color: {BG_ELEVATO}; }}
QPushButton:disabled {{ color: {TESTO_SPENTO}; border-color: {BORDO}; background-color: {BG_ELEVATO}; }}

QPushButton#Primario {{
    background-color: {ACCENTO}; color: {COLORI['accento_testo_su']};
    border: none; font-size: 14px; font-weight: 800; padding: 11px 16px;
}}
QPushButton#Primario:hover {{ background-color: {COLORI['accento_hover']}; color: {COLORI['accento_testo_su']}; }}

QPushButton#Accento {{
    background-color: transparent; border: 1px solid {rgba(ACCENTO, 150)};
    color: {ACCENTO}; font-weight: 700;
}}
QPushButton#Accento:hover {{ background-color: {rgba(ACCENTO, 28)}; border-color: {COLORI['accento_hover']}; }}

QPushButton#Piatto {{
    background: transparent; border: none; padding: 4px 8px;
    color: {TESTO_TENUE}; font-size: 12px; font-weight: 600;
}}
QPushButton#Piatto:hover {{ color: {ACCENTO}; background: {rgba(ACCENTO, 20)}; }}

/* ---- campi ---- */
QDoubleSpinBox, QLineEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDO};
    border-radius: 8px;
    padding: 6px 10px;
    color: {TESTO};
    font-size: 14px;
    font-weight: 600;
    selection-background-color: {ACCENTO};
    selection-color: {COLORI['accento_testo_su']};
}}
QDoubleSpinBox:hover, QLineEdit:hover {{ border-color: {BORDO_FORTE}; }}
QDoubleSpinBox:focus, QLineEdit:focus {{ border-color: {ACCENTO}; }}
QDoubleSpinBox:disabled, QLineEdit:disabled {{ color: {TESTO_SPENTO}; background-color: {BG_ELEVATO}; }}

QToolButton#Passo {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDO_FORTE};
    border-radius: 15px;
    color: {TESTO};
    font-size: 16px;
    font-weight: 700;
}}
QToolButton#Passo:hover {{ border-color: {ACCENTO}; color: {ACCENTO}; background-color: {rgba(ACCENTO, 28)}; }}
QToolButton#Passo:pressed {{ background-color: {rgba(ACCENTO, 50)}; }}
QToolButton#Passo:disabled {{ color: {TESTO_SPENTO}; border-color: {BORDO}; background-color: {BG_ELEVATO}; }}

/* ---- log ---- */
QListWidget {{
    background-color: {BG_INSET};
    border: 1px solid {BORDO};
    border-radius: 8px;
    padding: 4px;
    font-family: {FONT_MONO};
    font-size: 12px;
    outline: none;
}}
QListWidget::item {{
    color: {TESTO_TENUE}; padding: 6px 8px; border: none;
    border-bottom: 1px solid {rgba(BORDO, 160)};
}}
QListWidget::item:hover {{ background-color: {BG_ELEVATO}; color: {TESTO}; }}
QListWidget::item:selected {{ background-color: {rgba(ACCENTO, 30)}; color: {TESTO}; }}

/* ---- scroll ---- */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDO_FORTE}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {TESTO_SPENTO}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""


def applica_palette(app: QtWidgets.QApplication) -> None:
    """Fusion + palette scura: i widget che non hanno una regola QSS propria
    (viewport delle scroll area, popup, menu contestuali) non devono
    ricadere sul grigio chiaro di default."""
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for ruolo, colore in (
        (QtGui.QPalette.Window, BG), (QtGui.QPalette.WindowText, TESTO),
        (QtGui.QPalette.Base, BG_INPUT), (QtGui.QPalette.AlternateBase, BG_ELEVATO),
        (QtGui.QPalette.Text, TESTO), (QtGui.QPalette.Button, BG_INPUT),
        (QtGui.QPalette.ButtonText, TESTO), (QtGui.QPalette.Highlight, ACCENTO),
        (QtGui.QPalette.HighlightedText, COLORI["accento_testo_su"]),
        (QtGui.QPalette.ToolTipBase, BG_INPUT), (QtGui.QPalette.ToolTipText, TESTO),
        (QtGui.QPalette.PlaceholderText, TESTO_SPENTO),
    ):
        pal.setColor(ruolo, QtGui.QColor(colore))
    app.setPalette(pal)


# ----------------------------------------------------------------------
# Componenti
# ----------------------------------------------------------------------
def crea_card(titolo: str | None = None, colore_titolo: str | None = None, spaziatura: int = 10,
              compatta: bool = False) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout, QtWidgets.QHBoxLayout | None]:
    """Card standard: ritorna (frame, layout del contenuto, riga testata).
    La riga testata ha già il titolo e uno stretch: ci si aggiungono a
    destra legende, chip, pulsanti piatti."""
    card = QtWidgets.QFrame()
    card.setObjectName("Card")
    layout = QtWidgets.QVBoxLayout(card)
    layout.setContentsMargins(*(MARGINI_CARD_COMPATTA if compatta else MARGINI_CARD))
    layout.setSpacing(spaziatura)
    testata = None
    if titolo:
        testata = QtWidgets.QHBoxLayout()
        testata.setSpacing(8)
        etichetta = QtWidgets.QLabel(titolo.upper())
        etichetta.setObjectName("Eyebrow")
        if colore_titolo:
            etichetta.setStyleSheet(f"color: {colore_titolo};")
        testata.addWidget(etichetta)
        testata.addStretch(1)
        layout.addLayout(testata)
    return card, layout, testata


def separatore(verticale: bool = False) -> QtWidgets.QFrame:
    s = QtWidgets.QFrame()
    s.setObjectName("SeparatoreV" if verticale else "Separatore")
    return s


def etichetta(testo: str, nome_oggetto: str = "Campo") -> QtWidgets.QLabel:
    e = QtWidgets.QLabel(testo)
    e.setObjectName(nome_oggetto)
    return e


def legenda_linea(testo: str, colore: str, tratteggiata: bool = True) -> QtWidgets.QLabel:
    """Piccola legenda "— ON" dello stesso colore della linea nel grafico."""
    trattino = "╌╌" if tratteggiata else "──"
    e = QtWidgets.QLabel(f"{trattino}  {testo}")
    e.setStyleSheet(f"color: {colore}; font-size: 12px; font-weight: 700;")
    return e


class SpinSenzaRotella(QtWidgets.QDoubleSpinBox):
    """Ignora la rotella: scorrendo il pannello non si cambiano soglie per
    sbaglio senza nemmeno il focus/click."""

    def wheelEvent(self, event):
        event.ignore()


class Stepper(QtWidgets.QWidget):
    """−  [ valore ]  +   — stesso controllo numerico in tutte e tre le tab.
    Il valore resta scrivibile a mano; `.spin` è il QDoubleSpinBox vero
    (value(), valueChanged, setValue...). Disabilitare `.spin` disabilita
    anche i pulsanti −/+ (altrimenti potrebbero ancora cambiare il valore)."""

    def __init__(self, minimo: float, massimo: float, valore: float, passo: float,
                 decimali: int = 2, suffisso: str = "", larghezza: int = 96, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.spin = SpinSenzaRotella()
        self.spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.spin.setAlignment(QtCore.Qt.AlignCenter)
        self.spin.setRange(minimo, massimo)
        self.spin.setSingleStep(passo)
        self.spin.setDecimals(decimali)
        if suffisso:
            self.spin.setSuffix(suffisso)
        self.spin.setValue(valore)
        self.spin.setFixedWidth(larghezza)
        self.spin.installEventFilter(self)

        self._pulsanti = []
        for simbolo, azione in (("−", self.spin.stepDown), ("+", self.spin.stepUp)):
            b = QtWidgets.QToolButton()
            b.setObjectName("Passo")
            b.setText(simbolo)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(350)
            b.setFixedSize(30, 30)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.clicked.connect(azione)
            self._pulsanti.append(b)
        layout.addWidget(self._pulsanti[0])
        layout.addWidget(self.spin)
        layout.addWidget(self._pulsanti[1])

    def eventFilter(self, oggetto, evento):
        if oggetto is self.spin and evento.type() == QtCore.QEvent.EnabledChange:
            for b in self._pulsanti:
                b.setEnabled(self.spin.isEnabled())
        return super().eventFilter(oggetto, evento)

    def setEnabled(self, abilitato: bool) -> None:  # noqa: N802 (API Qt)
        self.spin.setEnabled(abilitato)

    def valore(self) -> float:
        return self.spin.value()


def stile_plot(plot, etichetta_y: str | None = None, etichetta_x: str | None = "Tempo (s)") -> None:
    """Grafici pyqtgraph coerenti con le card: fondo incassato, assi tenui,
    nessun titolo interno (il titolo sta nella testata della card)."""
    plot.setBackground(BG_INSET)
    plot.showGrid(x=True, y=True, alpha=0.12)
    font_tick = QtGui.QFont(FONT_NOME)
    font_tick.setPixelSize(11)
    stile_label = {"color": TESTO_TENUE, "font-size": "11px"}
    for nome in ("left", "bottom"):
        asse = plot.getAxis(nome)
        asse.setPen(QtGui.QColor(BORDO_FORTE))
        asse.setTextPen(QtGui.QColor(TESTO_TENUE))
        asse.setStyle(tickFont=font_tick, tickTextOffset=6)
    if etichetta_y:
        plot.setLabel("left", etichetta_y, **stile_label)
    if etichetta_x:
        plot.setLabel("bottom", etichetta_x, **stile_label)
    plot.getPlotItem().layout.setContentsMargins(4, 6, 8, 4)
    plot.setFrameShape(QtWidgets.QFrame.NoFrame)