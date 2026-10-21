"""
others/archivio.py — WAKEUPeSTALTA
-----------------------------------
Archivio di sessione: tutto quello che main.py sente e fa, salvato per poterlo
ricontrollare a posteriori ("quando e' passata una persona avevo questa forma
d'onda, quando ne sono passate 5 quest'altra").

Una cartella per giornata, archivio/AAAA-MM-GG/:
  main_HHMMSS/      una sessione = un avvio di main.py
    sessione.json   fotografia della configurazione all'avvio + "note"
                    (luogo, fibra, mezzo... modificabili dalla barra PROVE)
    audio/          segnale continuo della fibra, un WAV ogni DURATA_FILE_S
                    (24kHz, 16 bit, mono - si apre con Audacity)
    eventi.jsonl    una riga per evento: ora, file audio e secondo dentro
                    quel file in cui ritrovarlo, piu' i dati dell'evento
                    (comprese le prove etichettate: prova_inizio/prova_fine)
  taratura/         report e registrazioni di trova_frequenze.py e
                    tara_soglie.py della stessa giornata (cartella_taratura())

Per riguardare e confrontare: rivedi_archivio.py.

Regola fissa: l'archivio non deve MAI fermare la rilevazione. Qualunque
errore di disco (pieno, cartella non scrivibile) lo disattiva e viene
segnalato tramite on_errore - main.py continua a funzionare.
"""

from __future__ import annotations

import json
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

DURATA_FILE_S = 600  # 10 minuti per file WAV
CARTELLA_ARCHIVIO = Path(__file__).resolve().parent.parent / "archivio"


def cartella_giornata(base: Path = CARTELLA_ARCHIVIO) -> Path:
    return Path(base) / time.strftime("%Y-%m-%d")


def cartella_taratura(base: Path = CARTELLA_ARCHIVIO) -> Path:
    """Dove trova_frequenze.py e tara_soglie.py salvano le loro misure:
    accanto alle sessioni di main.py della stessa giornata."""
    cartella = cartella_giornata(base) / "taratura"
    cartella.mkdir(parents=True, exist_ok=True)
    return cartella


def _json_default(valore):
    """numpy e tipi non standard -> valori JSON normali."""
    if isinstance(valore, np.generic):
        return valore.item()
    if isinstance(valore, Path):
        return str(valore)
    return str(valore)


class ArchivioSessione:
    def __init__(self, cartella_base: Path, sample_rate: int, configurazione: dict,
                 on_errore: Callable[[str], None] | None = None):
        self.sample_rate = sample_rate
        self.on_errore = on_errore
        self.attivo = True
        self.errore: str | None = None
        self.campioni_totali = 0
        self._campioni_per_file = int(DURATA_FILE_S * sample_rate)
        self._wav: wave.Wave_write | None = None
        self._nome_wav: str | None = None
        self._campioni_file = 0
        self._eventi = None
        self.cartella = cartella_giornata(cartella_base) / time.strftime("main_%H%M%S")
        self._sessione = {
            "inizio": datetime.now().isoformat(timespec="seconds"),
            "note": "",  # luogo, fibra, mezzo, meteo... (barra PROVE di main.py)
            "sample_rate": sample_rate,
            "durata_file_audio_s": DURATA_FILE_S,
            "configurazione": configurazione,
        }
        try:
            (self.cartella / "audio").mkdir(parents=True, exist_ok=False)
            self._scrivi_sessione()
            self._eventi = open(self.cartella / "eventi.jsonl", "a", encoding="utf-8")
        except OSError as e:
            self._disattiva(e)

    def aggiorna_note(self, note: str) -> None:
        """Note della sessione (luogo, fibra, mezzo...): riscrive
        sessione.json e lascia traccia nel log di quando sono cambiate."""
        if not self.attivo:
            return
        self._sessione["note"] = note
        try:
            self._scrivi_sessione()
        except OSError as e:
            self._disattiva(e)
            return
        self.evento("note_sessione", note=note)

    def _scrivi_sessione(self) -> None:
        (self.cartella / "sessione.json").write_text(
            json.dumps(self._sessione, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")

    # ------------------------------------------------------------------
    def scrivi_audio(self, campioni: np.ndarray) -> None:
        if not self.attivo:
            return
        try:
            inizio = 0
            while inizio < len(campioni):
                if self._wav is None or self._campioni_file >= self._campioni_per_file:
                    self._apri_nuovo_wav()
                n = min(len(campioni) - inizio, self._campioni_per_file - self._campioni_file)
                # writeframes aggiorna anche l'intestazione del WAV ad ogni
                # scrittura: il file resta leggibile anche se main.py si chiude
                # male (crash, task manager).
                self._wav.writeframes(campioni[inizio:inizio + n].astype("<i2").tobytes())
                self._campioni_file += n
                self.campioni_totali += n
                inizio += n
        except (OSError, wave.Error) as e:
            self._disattiva(e)

    def evento(self, tipo: str, **dati) -> None:
        """Un evento, posizionato alla fine dell'audio scritto finora (cioe'
        sull'ultimo pacchetto ricevuto)."""
        if not self.attivo or self._eventi is None:
            return
        riga = {
            "ora": datetime.now().isoformat(timespec="milliseconds"),
            "tipo": tipo,
            "file_audio": self._nome_wav,
            "secondo_nel_file": round(self._campioni_file / self.sample_rate, 3),
            "campione_totale": self.campioni_totali,
            **dati,
        }
        try:
            self._eventi.write(json.dumps(riga, ensure_ascii=False, default=_json_default) + "\n")
            self._eventi.flush()
        except OSError as e:
            self._disattiva(e)

    def chiudi(self) -> None:
        self.evento("chiusura", campioni_totali=self.campioni_totali,
                    durata_s=round(self.campioni_totali / self.sample_rate, 1))
        for chiudibile in (self._wav, self._eventi):
            try:
                if chiudibile is not None:
                    chiudibile.close()
            except (OSError, wave.Error):
                pass
        self._wav = None
        self._eventi = None
        self.attivo = False

    # ------------------------------------------------------------------
    def _apri_nuovo_wav(self) -> None:
        if self._wav is not None:
            self._wav.close()
        self._nome_wav = f"audio/{time.strftime('%H%M%S')}.wav"
        self._wav = wave.open(str(self.cartella / self._nome_wav), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(self.sample_rate)
        self._campioni_file = 0
        self.evento("nuovo_file_audio")

    def _disattiva(self, errore: Exception) -> None:
        self.attivo = False
        self.errore = f"ARCHIVIO DISATTIVATO ({type(errore).__name__}: {errore}) — la rilevazione continua"
        for chiudibile in (self._wav, self._eventi):
            try:
                if chiudibile is not None:
                    chiudibile.close()
            except Exception:
                pass
        self._wav = None
        self._eventi = None
        if self.on_errore is not None:
            self.on_errore(self.errore)
