"""
others/archivio.py — WAKEUPeSTALTA
-----------------------------------
Archivio di sessione: tutto quello che 3main.py sente e fa, salvato per poterlo
ricontrollare a posteriori ("quando e' passata una persona avevo questa forma
d'onda, quando ne sono passate 5 quest'altra").

Una cartella per giornata, archivio/AAAA-MM-GG/:
  main_HHMMSS/      una sessione = un avvio di 3main.py
    sessione.json   fotografia della configurazione all'avvio
    audio/          segnale continuo della fibra, un WAV ogni DURATA_FILE_S
                    (24kHz, 16 bit, mono - si apre con Audacity)
    eventi.jsonl    una riga per evento: ora, file audio e secondo dentro
                    quel file in cui ritrovarlo, piu' i dati dell'evento
                    (comprese le prove etichettate: prova_inizio/prova_fine)
    audio_prove/    un WAV per ogni prova della barra PROVE, solo quella prova
                    (+1s prima e dopo): HHMMSS_<titolo>.wav
  taratura/         report e registrazioni di 0trovafrequenze.py e
                    1tarasoglie.py della stessa giornata (cartella_taratura())

Regola fissa: l'archivio non deve MAI fermare la rilevazione. Qualunque
errore di disco (pieno, cartella non scrivibile) lo disattiva e viene
segnalato tramite on_errore - 3main.py continua a funzionare.
"""

from __future__ import annotations

import json
import re
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

DURATA_FILE_S = 600  # 10 minuti per file WAV
MARGINE_PROVA_S = 1.0  # secondi tenuti prima e dopo ogni prova nel suo WAV
CARTELLA_ARCHIVIO = Path(__file__).resolve().parent.parent / "archivio"


def cartella_giornata(base: Path = CARTELLA_ARCHIVIO) -> Path:
    return Path(base) / time.strftime("%Y-%m-%d")


def cartella_taratura(base: Path = CARTELLA_ARCHIVIO) -> Path:
    """Dove 0trovafrequenze.py e 1tarasoglie.py salvano le loro misure:
    accanto alle sessioni di 3main.py della stessa giornata."""
    cartella = cartella_giornata(base) / "taratura"
    cartella.mkdir(parents=True, exist_ok=True)
    return cartella


def _nome_file(testo: str) -> str:
    """Titolo della prova -> nome di file valido su Windows."""
    return re.sub(r'[\\/:*?"<>|]+', "_", testo).strip(" .")[:60] or "prova"


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

        # Prove: l'ultimo MARGINE_PROVA_S di audio tenuto in memoria (per il
        # secondo "prima"), e la prova in corso - o in attesa del secondo
        # "dopo" - con i suoi pezzi di audio.
        self._pre: list[np.ndarray] = []
        self._prova: dict | None = None

    # ------------------------------------------------------------------
    # Prove (barra PROVE di 3main.py)
    # ------------------------------------------------------------------
    def inizia_prova(self, etichetta: str) -> None:
        if not self.attivo:
            return
        if self._prova is not None:  # la precedente aspettava ancora il secondo "dopo"
            self._salva_prova()
        nome = f"audio_prove/{time.strftime('%H%M%S')}_{_nome_file(etichetta)}.wav"
        self._prova = {"etichetta": etichetta, "nome": nome, "pezzi": list(self._pre), "fine": None}
        self.evento("prova_inizio", etichetta=etichetta)

    def fine_prova(self, **dati) -> str | None:
        """Chiude la prova: il suo WAV viene scritto appena arriva anche il
        secondo "dopo". Ritorna il nome del file (relativo alla sessione)."""
        if not self.attivo or self._prova is None:
            return None
        self._prova["fine"] = self.campioni_totali + int(MARGINE_PROVA_S * self.sample_rate)
        self.evento("prova_fine", etichetta=self._prova["etichetta"], file_prova=self._prova["nome"], **dati)
        return self._prova["nome"]

    def _salva_prova(self) -> None:
        prova, self._prova = self._prova, None
        try:
            percorso = self.cartella / prova["nome"]
            percorso.parent.mkdir(exist_ok=True)
            with wave.open(str(percorso), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.sample_rate)
                if prova["pezzi"]:
                    w.writeframes(np.concatenate(prova["pezzi"]).astype("<i2").tobytes())
            self.evento("prova_wav_salvato", etichetta=prova["etichetta"], file_prova=prova["nome"])
        except (OSError, wave.Error) as e:
            # un WAV di prova non riuscito non deve spegnere l'archivio
            self.evento("prova_wav_errore", etichetta=prova["etichetta"], errore=str(e))

    def _aggiorna_prova(self, pezzo: np.ndarray) -> None:
        self._pre.append(pezzo)
        margine = int(MARGINE_PROVA_S * self.sample_rate)
        while sum(len(p) for p in self._pre[1:]) >= margine:
            self._pre.pop(0)
        if self._prova is not None:
            self._prova["pezzi"].append(pezzo)
            if self._prova["fine"] is not None and self.campioni_totali >= self._prova["fine"]:
                self._salva_prova()

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
                # scrittura: il file resta leggibile anche se 3main.py si chiude
                # male (crash, task manager).
                self._wav.writeframes(campioni[inizio:inizio + n].astype("<i2").tobytes())
                self._campioni_file += n
                self.campioni_totali += n
                inizio += n
        except (OSError, wave.Error) as e:
            self._disattiva(e)
            return
        self._aggiorna_prova(campioni)

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
        if self._prova is not None:  # prova appena chiusa: salva senza aspettare il secondo "dopo"
            self._salva_prova()
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
