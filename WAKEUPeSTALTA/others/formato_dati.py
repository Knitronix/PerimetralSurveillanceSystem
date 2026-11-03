"""
others/formato_dati.py — WAKEUPeSTALTA
---------------------------------------
Formato dei pacchetti UDP dell'interrogatore, in un posto solo.

Verificato con Wireshark il 02/11/2026: ogni pacchetto (508 byte, nessuna
intestazione) contiene 127 campioni int32 little-endian, 94.5 pacchetti/s
= 12000 campioni/s. Fino al 02/11 tutti gli script leggevano 254 campioni
int16 a 24kHz: i tempi tornavano lo stesso (stessi byte al secondo), ma
ogni campione vero diventava una coppia (parte bassa, segno), i segnali
oltre +-32767 si "riavvolgevano" in rumore a tutte le frequenze (auto sulla
fibra = tappeti finti chiusi) e ogni tono aveva una copia a 12000-f.
Dettagli: report/REPORT_2026-10-28_29.md, §4.

Scala: i valori int32 non sono limitati a +-32768 (a riposo arrivano gia' a
~+-300000). Le potenze normalizzate restano divise per 32768^2 come prima,
cosi' i numeri dei segnali piccoli restano confrontabili con lo storico: un
evento forte supera semplicemente 1.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 12000
CAMPIONI_PER_PACCHETTO = 127
DTYPE_CAMPIONI = np.dtype("<i4")
BYTE_PER_CAMPIONE = DTYPE_CAMPIONI.itemsize
BYTE_PER_PACCHETTO = CAMPIONI_PER_PACCHETTO * BYTE_PER_CAMPIONE  # 508


def decodifica_pacchetto(dati: bytes) -> np.ndarray | None:
    """Campioni int32 di un pacchetto UDP, o None se la lunghezza non torna."""
    if len(dati) != BYTE_PER_PACCHETTO:
        return None
    return np.frombuffer(dati, dtype=DTYPE_CAMPIONI)
