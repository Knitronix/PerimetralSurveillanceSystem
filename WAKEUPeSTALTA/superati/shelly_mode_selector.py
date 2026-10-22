"""
others/shelly_mode_selector.py
--------------------------------
Sceglie la modalita' attiva sul master (arduino/master/master.ino, che
gestisce 3 modalita': 0=daisy chain produzione, 1=sweep frequenze,
2=sweep tempi) pilotando SOLO l'accensione/spegnimento della Shelly che lo
alimenta - nessun'altra connessione, nessun comando seriale per la scelta.
Il meccanismo e' descritto nell'intestazione di master.ino: un ciclo di
accensione piu' breve di SOGLIA_SESSIONE_LUNGA_MS (2.5s lato Arduino) fa
avanzare un contatore di modalita' in EEPROM, un'accensione lasciata piu'
lunga la confema. Per selezionare la modalita' K servono K cicli brevi
seguiti da un'accensione finale lasciata attiva.

Duplica (non importa) le due chiamate RPC minime di others/shelly_control.py:
stesso motivo di tutte le altre duplicazioni in questo progetto (vedi
CLAUDE.md) - restare autonomi dal resto del repository invece di importare
attraverso i flussi.

Uso:
    from others.shelly_mode_selector import seleziona_modalita
    seleziona_modalita(SMART_PLUG_HOST, modalita=1)  # 1 = sweep frequenze
"""

from __future__ import annotations

import time

import requests

TIMEOUT_RPC_S = 5

# Deve restare ABBONDANTEMENTE sotto SOGLIA_SESSIONE_LUNGA_MS (2.5s) in
# master.ino, con margine per la latenza reale della chiamata RPC e del
# boot dell'Arduino.
DURATA_CICLO_BREVE_S = 1.0
# Tempo spento tra un ciclo e il successivo: deve bastare perche' l'Arduino
# si scarichi davvero e riparta da un boot pulito al prossimo accensione.
DURATA_SPENTO_S = 1.5
# Margine oltre SOGLIA_SESSIONE_LUNGA_MS (2.5s) prima di considerare la
# modalita' finale attiva e passare oltre (handshake seriale, UDP, ecc.).
MARGINE_CONFERMA_S = 1.0
SOGLIA_SESSIONE_LUNGA_S = 2.5


def _rpc(host: str, metodo: str, params: dict | None = None) -> dict:
    url = f"http://{host}/rpc/{metodo}"
    resp = requests.get(url, params=params or {}, timeout=TIMEOUT_RPC_S)
    resp.raise_for_status()
    return resp.json()


def _accendi(host: str, canale: int) -> None:
    _rpc(host, "Switch.Set", {"id": canale, "on": "true"})


def _spegni(host: str, canale: int) -> None:
    _rpc(host, "Switch.Set", {"id": canale, "on": "false"})


def spegni_master(host: str, canale: int = 0) -> None:
    """Spegne la Shelly - da chiamare a fine test invece del vecchio
    comando seriale 'OFF' (le modalita' 1/2 non aspettano piu' nessun
    comando, vedi master.ino: partono gia' sonore)."""
    _spegni(host, canale)


def seleziona_modalita(host: str, modalita: int, canale: int = 0) -> None:
    """Porta il master nella modalita' richiesta (0/1/2, vedi master.ino)
    ciclando l'alimentazione via Shelly. Bloccante: ritorna solo quando il
    master dovrebbe essere acceso e assestato in quella modalita'
    (confermata lato Arduino). Il momento esatto in cui l'audio comincia
    davvero non e' questo - c'e' latenza variabile di rele'/boot - va
    rilevato dal chiamante sul flusso UDP stesso (vedi onset nei due
    script analizza_*), non assunto uguale a quando questa funzione
    ritorna."""
    print("[shelly_mode_selector] spengo per partire da uno stato pulito...")
    _spegni(host, canale)
    time.sleep(DURATA_SPENTO_S)

    for i in range(modalita):
        print(f"[shelly_mode_selector] ciclo breve {i + 1}/{modalita} (avanza il contatore di modalita')")
        _accendi(host, canale)
        time.sleep(DURATA_CICLO_BREVE_S)
        _spegni(host, canale)
        time.sleep(DURATA_SPENTO_S)

    attesa_conferma_s = SOGLIA_SESSIONE_LUNGA_S + MARGINE_CONFERMA_S
    print(f"[shelly_mode_selector] accensione finale, modalita' {modalita} - attendo {attesa_conferma_s:.1f}s che si assesti")
    _accendi(host, canale)
    time.sleep(attesa_conferma_s)
    print("[shelly_mode_selector] modalita' selezionata, procedo")
