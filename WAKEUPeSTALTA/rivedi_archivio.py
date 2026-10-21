"""
rivedi_archivio.py — WAKEUPeSTALTA
-----------------------------------
Riguarda e confronta le sessioni registrate da main.py (others/archivio.py):
"quando passava una persona avevo questa forma d'onda, quando ne passavano 5
quest'altra".

  - in alto si sceglie la sessione (archivio/AAAA-MM-GG/main_HHMMSS), con
    le sue note
  - a sinistra la lista degli eventi: prove etichettate, trigger STA/LTA,
    chiusure dei tappeti, avvisi, log (filtrabili)
  - si seleziona un evento e lo si manda nel pannello A o B: forma d'onda +
    spettrogramma, con lo zero dell'asse sull'inizio della prova / sul
    momento dell'evento, cosi' A e B si confrontano a colpo d'occhio
  - "Salva WAV" esporta il tratto mostrato, per ascoltarlo con Audacity

Solo lettura: non modifica mai l'archivio.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from others.archivio import CARTELLA_ARCHIVIO
from others.tema import COLORI, FONT_MONO, STYLE_QSS, applica_palette, crea_card, etichetta, stile_plot

MARGINE_PROVA_S = 1.0     # secondi mostrati prima/dopo una prova
FINESTRA_EVENTO_S = 3.0   # secondi mostrati prima/dopo un evento singolo
MAX_DURATA_S = 180.0      # oltre questo si taglia (grafici e memoria)
FMAX_SPETTRO = 8000.0
NFFT = 1024

FILTRI = {
    "Prove": ("prova",),
    "Trigger STA/LTA": ("stalta_trigger",),
    "Chiusure tappeti": ("f1f2_slot_chiuso",),
    "Impulsi scartati": ("f1f2_rumore",),
    "Avvisi": ("avviso",),
    "Log": ("log", "note_sessione"),
    "Marcatori / slot aperti": ("f1f2_zero", "f1f2_slot_aperto"),
}
FILTRI_ATTIVI_DEFAULT = {"Prove", "Trigger STA/LTA", "Chiusure tappeti", "Impulsi scartati", "Avvisi"}


# ======================================================================
# Lettura dell'archivio (pura)
# ======================================================================
class Sessione:
    def __init__(self, cartella: Path):
        self.cartella = cartella
        self.info = json.loads((cartella / "sessione.json").read_text(encoding="utf-8"))
        self.sr = int(self.info["sample_rate"])
        self.eventi = []
        with open(cartella / "eventi.jsonl", encoding="utf-8") as f:
            for riga in f:
                riga = riga.strip()
                if riga:
                    try:
                        self.eventi.append(json.loads(riga))
                    except json.JSONDecodeError:
                        pass  # ultima riga troncata da una chiusura brusca
        # Indice dei file audio: campione totale da cui inizia ciascuno.
        self.file_audio: list[tuple[int, Path, int]] = []  # (inizio, percorso, n_campioni)
        for e in self.eventi:
            if e["tipo"] == "nuovo_file_audio" and e.get("file_audio"):
                percorso = cartella / e["file_audio"]
                if percorso.exists():
                    with wave.open(str(percorso)) as w:
                        self.file_audio.append((e["campione_totale"], percorso, w.getnframes()))
        self.note = self.info.get("note", "")
        for e in self.eventi:  # le note possono essere state aggiornate durante la sessione
            if e["tipo"] == "note_sessione":
                self.note = e.get("note", "")
        self.prove = self._accoppia_prove()

    def _accoppia_prove(self) -> list[dict]:
        prove, aperta = [], None
        for e in self.eventi:
            if e["tipo"] == "prova_inizio":
                aperta = e
            elif e["tipo"] == "prova_fine" and aperta is not None:
                prove.append({"etichetta": aperta.get("etichetta", ""), "inizio": aperta["campione_totale"],
                              "fine": e["campione_totale"], "ora": aperta["ora"]})
                aperta = None
        if aperta is not None:  # prova mai chiusa (crash)
            prove.append({"etichetta": aperta.get("etichetta", "") + " (non chiusa)",
                          "inizio": aperta["campione_totale"], "fine": aperta["campione_totale"] + 30 * self.sr,
                          "ora": aperta["ora"]})
        return prove

    def leggi_audio(self, inizio: int, fine: int) -> np.ndarray:
        """Campioni [inizio, fine) dell'audio continuo della sessione,
        attraversando i confini tra un file WAV e l'altro."""
        inizio = max(0, inizio)
        pezzi = []
        for f_inizio, percorso, n in self.file_audio:
            a, b = max(inizio, f_inizio), min(fine, f_inizio + n)
            if a >= b:
                continue
            with wave.open(str(percorso)) as w:
                w.setpos(a - f_inizio)
                pezzi.append(np.frombuffer(w.readframes(b - a), dtype="<i2"))
        return np.concatenate(pezzi) if pezzi else np.zeros(0, dtype=np.int16)


def elenco_sessioni(base: Path = CARTELLA_ARCHIVIO) -> list[Path]:
    return sorted((p for p in Path(base).glob("*/main_*") if (p / "eventi.jsonl").exists()), reverse=True)


def descrivi_evento(e: dict) -> tuple[str, str]:
    """(categoria per i filtri, testo leggibile)."""
    t = e["tipo"]
    if t == "stalta_trigger":
        return "stalta_trigger", f"trigger STA/LTA {e.get('rapporto', 0):.2f} (ON {e.get('soglia_on', 0):.2f}) → {e.get('esito', '')}"
    if t == "f1f2_slot":
        stato = e.get("stato", "")
        return f"f1f2_slot_{stato}", f"tappeto {e.get('counter')} {stato}"
    if t == "f1f2_zero":
        return "f1f2_zero", "marcatore"
    if t == "f1f2_rumore":
        return "f1f2_rumore", f"impulso scartato ({e.get('durata_s', 0) * 1000:.0f} ms)"
    if t == "avviso":
        return "avviso", f"avviso {e.get('chiave')}: {e.get('testo') or 'rientrato'}"
    if t in ("log", "note_sessione"):
        return t, e.get("testo") or f"note: {e.get('note', '')}"
    return t, t


# ======================================================================
# GUI
# ======================================================================
class PannelloConfronto(QtWidgets.QWidget):
    """Forma d'onda + spettrogramma di un tratto, con lo zero dell'asse
    sull'inizio della prova / sul momento dell'evento."""

    def __init__(self, nome: str, colore: str):
        super().__init__()
        self.audio = np.zeros(0, dtype=np.int16)
        self.sr = 24000
        self.descrizione = ""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        testata = QtWidgets.QHBoxLayout()
        titolo = QtWidgets.QLabel(nome)
        titolo.setStyleSheet(f"color: {colore}; font-weight: 800; font-size: 14px;")
        testata.addWidget(titolo)
        self.label = QtWidgets.QLabel("— seleziona un evento e premi \"Mostra in " + nome + "\"")
        self.label.setObjectName("Nota")
        testata.addWidget(self.label, 1)
        self.btn_wav = QtWidgets.QPushButton("Salva WAV…")
        self.btn_wav.setObjectName("Piatto")
        self.btn_wav.clicked.connect(self._salva_wav)
        self.btn_wav.setEnabled(False)
        testata.addWidget(self.btn_wav)
        layout.addLayout(testata)

        self.plot_onda = pg.PlotWidget()
        stile_plot(self.plot_onda, "Ampiezza", None)
        self.curva = self.plot_onda.plot(pen=pg.mkPen(colore, width=1))
        try:
            self.curva.setDownsampling(auto=True, method="peak")
        except TypeError:
            self.curva.setDownsampling(auto=True)
        self.curva.setClipToView(True)
        layout.addWidget(self.plot_onda, 1)

        self.plot_spettro = pg.PlotWidget()
        stile_plot(self.plot_spettro, "Hz", "Secondi (0 = inizio prova / evento)")
        self.immagine = pg.ImageItem()
        self.plot_spettro.addItem(self.immagine)
        mappa = pg.colormap.get("viridis")
        self.immagine.setLookupTable(mappa.getLookupTable(nPts=256))
        self.plot_spettro.setXLink(self.plot_onda)
        layout.addWidget(self.plot_spettro, 1)
        self.marcatori = []

    def mostra(self, audio: np.ndarray, sr: int, t_zero_s: float, descrizione: str, eventi_vicini: list):
        """audio inizia a t_zero_s secondi rispetto allo zero (negativo = prima)."""
        self.audio, self.sr, self.descrizione = audio, sr, descrizione
        self.label.setText(descrizione)
        self.btn_wav.setEnabled(len(audio) > 0)
        for m in self.marcatori:
            self.plot_onda.removeItem(m)
        self.marcatori = []
        if len(audio) == 0:
            self.curva.setData([], [])
            self.immagine.clear()
            return
        t = t_zero_s + np.arange(len(audio)) / sr
        self.curva.setData(t, audio.astype(np.float32))
        # spettrogramma (dB), finestre da NFFT con sovrapposizione 50%
        passo = NFFT // 2
        n = max(1, (len(audio) - NFFT) // passo + 1)
        if len(audio) >= NFFT:
            idx = np.arange(NFFT)[None, :] + passo * np.arange(n)[:, None]
            frame = audio.astype(np.float32)[idx] * np.hanning(NFFT).astype(np.float32)
            spettro = np.abs(np.fft.rfft(frame, axis=1)) ** 2
            kmax = int(FMAX_SPETTRO / (sr / NFFT))
            db = 10 * np.log10(spettro[:, :kmax] + 1e-3)
            self.immagine.setImage(db, autoLevels=False,
                                   levels=(np.percentile(db, 5), np.percentile(db, 99.8)))
            self.immagine.setRect(QtCore.QRectF(t_zero_s, 0, n * passo / sr, kmax * sr / NFFT))
        self.plot_onda.setXRange(t[0], t[-1], padding=0)
        self.plot_spettro.setYRange(0, FMAX_SPETTRO, padding=0)
        for t_ev, colore, testo in eventi_vicini:
            linea = pg.InfiniteLine(t_ev, angle=90, pen=pg.mkPen(colore, width=1.5, style=QtCore.Qt.DashLine),
                                    label=testo, labelOpts={"color": colore, "position": 0.92})
            self.plot_onda.addItem(linea)
            self.marcatori.append(linea)

    def _salva_wav(self):
        percorso, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Salva WAV", "tratto.wav", "WAV (*.wav)")
        if not percorso:
            return
        with wave.open(percorso, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(self.audio.astype("<i2").tobytes())


class FinestraRivedi(QtWidgets.QMainWindow):
    def __init__(self, base: Path = CARTELLA_ARCHIVIO):
        super().__init__()
        self.setWindowTitle("Rivedi archivio — WAKEUPeSTALTA")
        self.resize(1500, 900)
        self.setStyleSheet(STYLE_QSS)
        self.base = base
        self.sessione: Sessione | None = None
        self.righe: list[dict] = []  # una per riga della tabella

        radice = QtWidgets.QWidget()
        radice.setObjectName("Radice")
        self.setCentralWidget(radice)
        layout = QtWidgets.QVBoxLayout(radice)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        card, lay, _ = crea_card("Sessione", colore_titolo=COLORI["accento"], compatta=True)
        riga = QtWidgets.QHBoxLayout()
        self.combo = QtWidgets.QComboBox()
        self.combo.currentIndexChanged.connect(self._carica_sessione)
        riga.addWidget(self.combo, 1)
        btn = QtWidgets.QPushButton("Aggiorna elenco")
        btn.setObjectName("Piatto")
        btn.clicked.connect(self._aggiorna_elenco)
        riga.addWidget(btn)
        lay.addLayout(riga)
        self.label_info = QtWidgets.QLabel("")
        self.label_info.setObjectName("Mono")
        self.label_info.setWordWrap(True)
        lay.addWidget(self.label_info)
        layout.addWidget(card)

        corpo = QtWidgets.QHBoxLayout()
        corpo.setSpacing(12)
        card, lay, _ = crea_card("Eventi", colore_titolo=COLORI["blu"])
        filtri = QtWidgets.QGridLayout()
        self.check_filtri = {}
        for i, nome in enumerate(FILTRI):
            c = QtWidgets.QCheckBox(nome)
            c.setChecked(nome in FILTRI_ATTIVI_DEFAULT)
            c.toggled.connect(self._riempi_tabella)
            filtri.addWidget(c, i // 2, i % 2)
            self.check_filtri[nome] = c
        lay.addLayout(filtri)
        self.tabella = QtWidgets.QTableWidget(0, 3)
        self.tabella.setHorizontalHeaderLabels(["ora", "t (s)", "evento"])
        self.tabella.horizontalHeader().setStretchLastSection(True)
        self.tabella.verticalHeader().setVisible(False)
        self.tabella.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabella.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tabella.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tabella.setStyleSheet(f"font-family: {FONT_MONO}; font-size: 12px;")
        lay.addWidget(self.tabella, 1)
        bottoni = QtWidgets.QHBoxLayout()
        for nome in ("A", "B"):
            b = QtWidgets.QPushButton(f"Mostra in {nome}")
            b.setObjectName("Primario" if nome == "A" else "Accento")
            b.clicked.connect(lambda _=False, n=nome: self._mostra(n))
            bottoni.addWidget(b)
        lay.addLayout(bottoni)
        card.setMinimumWidth(460)
        corpo.addWidget(card, 4)

        destra = QtWidgets.QVBoxLayout()
        self.pannelli = {}
        for nome, colore in (("A", COLORI["accento"]), ("B", COLORI["ambra"])):
            card, lay, _ = crea_card(f"Pannello {nome}", colore_titolo=colore, compatta=True)
            self.pannelli[nome] = PannelloConfronto(nome, colore)
            lay.addWidget(self.pannelli[nome])
            destra.addWidget(card, 1)
        corpo.addLayout(destra, 9)
        layout.addLayout(corpo, 1)

        self._aggiorna_elenco()

    # ------------------------------------------------------------------
    def _aggiorna_elenco(self):
        self.combo.blockSignals(True)
        self.combo.clear()
        for p in elenco_sessioni(self.base):
            self.combo.addItem(f"{p.parent.name}  {p.name.replace('main_', '')[:2]}:{p.name[7:9]}:{p.name[9:11]}", p)
        self.combo.blockSignals(False)
        if self.combo.count():
            self._carica_sessione()
        else:
            self.label_info.setText(f"Nessuna sessione in {self.base}")

    def _carica_sessione(self):
        percorso = self.combo.currentData()
        if percorso is None:
            return
        try:
            self.sessione = Sessione(Path(percorso))
        except (OSError, ValueError, KeyError) as e:
            self.sessione = None
            self.label_info.setText(f"Sessione illeggibile: {e}")
            return
        s = self.sessione
        durata = sum(n for _, _, n in s.file_audio) / s.sr
        cfg = s.info.get("configurazione", {})
        sg, st = cfg.get("soglie_goertzel", {}), cfg.get("sta_lta_di_partenza", {})
        self.label_info.setText(
            f"Note: {s.note or '(nessuna)'}\n"
            f"Audio {durata / 60:.1f} min · {len(s.prove)} prove · f1/f2 {cfg.get('protocollo', {}).get('F1_HZ')}/"
            f"{cfg.get('protocollo', {}).get('F2_HZ')} Hz · soglie ON f1/f2 {sg.get('SOGLIA_ON_F1')}/{sg.get('SOGLIA_ON_F2')}"
            f" · STA/LTA {st.get('STA_S')}/{st.get('LTA_S')} s ON {st.get('SOGLIA_ON')}")
        self._riempi_tabella()

    def _riempi_tabella(self):
        if self.sessione is None:
            return
        attive = {c for nome, ch in self.check_filtri.items() if ch.isChecked() for c in FILTRI[nome]}
        s = self.sessione
        self.righe = []
        if "prova" in attive:
            for p in s.prove:
                self.righe.append({"tipo": "prova", "campione": p["inizio"], "ora": p["ora"],
                                   "testo": f"PROVA \"{p['etichetta']}\" ({(p['fine'] - p['inizio']) / s.sr:.1f} s)",
                                   "prova": p})
        for e in s.eventi:
            categoria, testo = descrivi_evento(e)
            if categoria in attive:
                self.righe.append({"tipo": e["tipo"], "campione": e["campione_totale"], "ora": e["ora"],
                                   "testo": testo, "evento": e})
        self.righe.sort(key=lambda r: r["campione"])
        self.tabella.setRowCount(len(self.righe))
        for i, r in enumerate(self.righe):
            valori = (r["ora"][11:23], f"{r['campione'] / s.sr:8.2f}", r["testo"])
            for j, v in enumerate(valori):
                self.tabella.setItem(i, j, QtWidgets.QTableWidgetItem(v))
        self.tabella.resizeColumnsToContents()

    def _mostra(self, nome: str):
        sel = self.tabella.selectionModel().selectedRows()
        if not sel or self.sessione is None:
            return
        r = self.righe[sel[0].row()]
        s = self.sessione
        if "prova" in r:
            zero = r["prova"]["inizio"]
            inizio = zero - int(MARGINE_PROVA_S * s.sr)
            fine = min(r["prova"]["fine"] + int(MARGINE_PROVA_S * s.sr), inizio + int(MAX_DURATA_S * s.sr))
        else:
            e = r["evento"]
            # per un impulso f1/f2 lo zero e' l'inizio dell'impulso, non la fine
            zero = e["campione_totale"] - int(e.get("durata_s", 0) * s.sr) if e["tipo"].startswith("f1f2_") else e["campione_totale"]
            inizio = zero - int(FINESTRA_EVENTO_S * s.sr)
            fine = zero + int(FINESTRA_EVENTO_S * s.sr)
        audio = s.leggi_audio(inizio, fine)
        t_zero = (max(0, inizio) - zero) / s.sr
        vicini = []
        for e in s.eventi:
            if inizio <= e["campione_totale"] <= fine:
                cat, testo = descrivi_evento(e)
                if cat == "stalta_trigger":
                    vicini.append(((e["campione_totale"] - zero) / s.sr, COLORI["pericolo"], "STA/LTA"))
                elif cat == "f1f2_slot_chiuso":
                    vicini.append(((e["campione_totale"] - zero) / s.sr, COLORI["ambra"], f"T{e.get('counter')}"))
                elif e["tipo"] in ("prova_inizio", "prova_fine"):
                    vicini.append(((e["campione_totale"] - zero) / s.sr, COLORI["verde"], e["tipo"][6:]))
        self.pannelli[nome].mostra(audio, s.sr, t_zero, f"{r['ora'][11:19]} · {r['testo']}", vicini)


def main() -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    applica_palette(app)
    finestra = FinestraRivedi()
    finestra.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
