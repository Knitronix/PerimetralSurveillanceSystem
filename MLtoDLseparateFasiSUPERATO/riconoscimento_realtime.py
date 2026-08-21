"""
riconoscimento_realtime.py
----------------------------
Ascolto e classificazione continua, come processo SEPARATO dal registratore
(registratore_stalta.py). Nessuna interfaccia grafica, nessuno spettrogramma,
nessun plotting — solo ricezione UDP, rilevamento STA/LTA e classificazione,
per un carico sulla CPU molto più leggero del registratore completo.

Perché un processo separato invece di una modalità dentro l'app esistente:
- Puoi farlo girare in parallelo al registratore (o da solo) senza che i due
  si rallentino a vicenda: l'interfaccia grafica del registratore (grafici,
  spettrogramma) è la parte più pesante per la CPU, e qui non c'è.
- Non serve scegliere una classe dal menu per etichettare: ascolta e basta,
  utile quando vuoi solo vedere cosa il sistema riconosce, senza gestire
  l'etichettatura manuale in parallelo.
- L'output è una singola riga per evento riconosciuto, non mischiato al log
  denso del registratore (salvataggi, avvisi di rete, ecc.).

Come condivide la porta UDP con il registratore: il dispositivo trasmette in
broadcast (non a un singolo destinatario), quindi più processi possono
ricevere la stessa copia dei pacchetti collegandosi allo stesso indirizzo
con SO_REUSEADDR — infatti puoi far girare questo script CONTEMPORANEAMENTE
al registratore, oppure da solo. Non sono in conflitto tra loro.

Le soglie STA/LTA vengono lette all'avvio da soglie_stalta.json (scritto dal
registratore ad ogni ricalibrazione) — se il file non esiste, usa i default.
Rilancia questo script dopo una ricalibrazione per usare le soglie nuove
(non le rilegge mentre gira, di proposito, per restare semplice).

Uso:
    python riconoscimento_realtime.py
    (Ctrl+C per fermare)
"""

import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field

import numpy as np

from realtime_inference import ClassificatoreEventi

# --- Costanti del protocollo dati: DEVONO combaciare con registratore_stalta.py ---
UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000
ID_FIBRA_DEFAULT = "fibra1"

# --- File di scambio con il registratore ---
FILE_SOGLIE_STALTA = "soglie_stalta.json"
PATH_MODELLO_DEFAULT = "modello_classificatore.pkl"

# --- Parametri STA/LTA: stessi default del registratore, sovrascritti da
# soglie_stalta.json se presente ---
STA_FINESTRA_SEC = 0.1
LTA_FINESTRA_SEC = 3.0
STA_LTA_SOGLIA_ON_DEFAULT = 4.0
STA_LTA_SOGLIA_OFF_DEFAULT = 1.5
PRE_TRIGGER_SEC = 0.3
ISTERESI_CHIUSURA_SEC = 0.25
DURATA_MASSIMA_EVENTO_SEC = 4.0
TEMPO_RIARMO_SEC = 1.0
SOGLIA_CLIPPING = 32000

CARTELLA_EVENTI_RICONOSCIUTI = "eventi_riconosciuti"
FILE_LOG_RICONOSCIMENTI = os.path.join(CARTELLA_EVENTI_RICONOSCIUTI, "riconoscimenti_log.jsonl")


def carica_soglie():
    """Legge soglia_on/soglia_off da soglie_stalta.json se esiste (scritto
    dal registratore), altrimenti usa i default definiti sopra."""
    if os.path.exists(FILE_SOGLIE_STALTA):
        try:
            with open(FILE_SOGLIE_STALTA, "r", encoding="utf-8") as f:
                dati = json.load(f)
            return dati["soglia_on"], dati["soglia_off"]
        except Exception as e:
            print(f"⚠ Impossibile leggere {FILE_SOGLIE_STALTA} ({e}), uso i default.")
    else:
        print(f"⚠ {FILE_SOGLIE_STALTA} non trovato, uso soglie di default "
              f"(ON={STA_LTA_SOGLIA_ON_DEFAULT}, OFF={STA_LTA_SOGLIA_OFF_DEFAULT}).")
    return STA_LTA_SOGLIA_ON_DEFAULT, STA_LTA_SOGLIA_OFF_DEFAULT


class RilevatoreStaLta:
    """
    Stessa logica del rilevatore in registratore_stalta.py (duplicata qui
    intenzionalmente, non importata, per mantenere questo script davvero
    indipendente e senza dipendenze dal modulo Qt del registratore). Se in
    futuro modifichi la logica STA/LTA in un posto, ricordati di
    riportarla anche qui.
    """

    def __init__(self, sample_rate, campioni_per_pacchetto, sta_sec, lta_sec, soglia_on, soglia_off):
        durata_pacchetto = campioni_per_pacchetto / sample_rate
        self.alpha_sta = min(1.0, durata_pacchetto / sta_sec)
        self.alpha_lta = min(1.0, durata_pacchetto / lta_sec)
        self.soglia_on = soglia_on
        self.soglia_off = soglia_off
        self._floor_potenza = 1e-6
        self.sta = 0.0
        self.lta = self._floor_potenza
        self.in_evento = False
        self.tempo_sotto_soglia_off = 0.0
        self.durata_pacchetto = durata_pacchetto
        self.ultimo_rapporto = 0.0

    def aggiorna(self, pacchetto_campioni):
        potenza = float(np.mean(pacchetto_campioni.astype(np.float64) ** 2))
        self.sta += self.alpha_sta * (potenza - self.sta)
        self.lta += self.alpha_lta * (potenza - self.lta)
        self.lta = max(self.lta, self._floor_potenza)
        rapporto = self.sta / self.lta
        self.ultimo_rapporto = rapporto

        evento_iniziato = False
        evento_finito = False
        if not self.in_evento:
            if rapporto > self.soglia_on:
                self.in_evento = True
                self.tempo_sotto_soglia_off = 0.0
                evento_iniziato = True
        else:
            if rapporto < self.soglia_off:
                self.tempo_sotto_soglia_off += self.durata_pacchetto
                if self.tempo_sotto_soglia_off >= ISTERESI_CHIUSURA_SEC:
                    self.in_evento = False
                    evento_finito = True
            else:
                self.tempo_sotto_soglia_off = 0.0
        return evento_iniziato, evento_finito


@dataclass
class BufferCircolarePreTrigger:
    """Tiene gli ultimi PRE_TRIGGER_SEC secondi di segnale, per includerli
    nell'evento catturato quando lo STA/LTA scatta (altrimenti si perde
    l'inizio dell'evento, catturato solo dal momento del trigger in poi)."""
    n_campioni: int
    buffer: np.ndarray = field(init=False)
    pos: int = field(default=0, init=False)

    def __post_init__(self):
        self.buffer = np.zeros(self.n_campioni, dtype=np.int16)

    def scrivi(self, nuovi_campioni):
        n = len(nuovi_campioni)
        fine = self.pos + n
        if fine <= self.n_campioni:
            self.buffer[self.pos:fine] = nuovi_campioni
        else:
            primo = self.n_campioni - self.pos
            self.buffer[self.pos:] = nuovi_campioni[:primo]
            self.buffer[: n - primo] = nuovi_campioni[primo:]
        self.pos = fine % self.n_campioni

    def estrai_ultimi(self, n):
        inizio = (self.pos - n) % self.n_campioni
        if inizio < self.pos:
            return self.buffer[inizio:self.pos]
        return np.concatenate((self.buffer[inizio:], self.buffer[:self.pos]))


def main():
    if not os.path.exists(PATH_MODELLO_DEFAULT):
        print(f"✗ Modello non trovato: {PATH_MODELLO_DEFAULT}")
        print("  Allena prima un modello con: python train_classifier.py")
        sys.exit(1)

    print(f"Carico il modello: {PATH_MODELLO_DEFAULT} ...")
    classificatore = ClassificatoreEventi(PATH_MODELLO_DEFAULT)
    print(f"Modello caricato (tipo: {classificatore.tipo_modello}).\n")

    soglia_on, soglia_off = carica_soglie()
    print(f"Soglie STA/LTA: ON={soglia_on}  OFF={soglia_off}\n")

    rilevatore = RilevatoreStaLta(
        SAMPLE_RATE, CAMPIONI_PER_PACCHETTO, STA_FINESTRA_SEC, LTA_FINESTRA_SEC, soglia_on, soglia_off
    )

    campioni_pre_trigger = int(SAMPLE_RATE * PRE_TRIGGER_SEC)
    campioni_massimi_evento = int(SAMPLE_RATE * DURATA_MASSIMA_EVENTO_SEC) + campioni_pre_trigger
    pre_roll_buffer = BufferCircolarePreTrigger(n_campioni=max(campioni_pre_trigger, CAMPIONI_PER_PACCHETTO * 4))

    os.makedirs(CARTELLA_EVENTI_RICONOSCIUTI, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(0.5)
    bytes_attesi = CAMPIONI_PER_PACCHETTO * 2

    catturando = False
    buffer_evento = None
    pos_evento = 0
    tempo_ultima_chiusura = -1e9

    print("In ascolto... (Ctrl+C per fermare)\n")
    print(f"{'Ora':<10} {'Classe':<26} {'Confidenza':<12}")
    print("-" * 50)

    try:
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue

            if len(data) != bytes_attesi:
                continue

            campioni = np.frombuffer(data, dtype=np.int16)
            pre_roll_buffer.scrivi(campioni)

            evento_iniziato, evento_finito = rilevatore.aggiorna(campioni)

            if not catturando:
                tempo_da_ultima_chiusura = time.monotonic() - tempo_ultima_chiusura
                if evento_iniziato and tempo_da_ultima_chiusura >= TEMPO_RIARMO_SEC:
                    catturando = True
                    buffer_evento = np.zeros(campioni_massimi_evento, dtype=np.int16)
                    pre_roll = pre_roll_buffer.estrai_ultimi(min(campioni_pre_trigger, pre_roll_buffer.n_campioni))
                    buffer_evento[: len(pre_roll)] = pre_roll
                    pos_evento = len(pre_roll)
            else:
                n = len(campioni)
                fine = min(pos_evento + n, campioni_massimi_evento)
                quanti = fine - pos_evento
                if quanti > 0:
                    buffer_evento[pos_evento:fine] = campioni[:quanti]
                    pos_evento = fine
                pieno = pos_evento >= campioni_massimi_evento

                if evento_finito or pieno:
                    dati_validi = buffer_evento[:pos_evento]
                    etichetta, confidenza, _ = classificatore.classifica(dati_validi, SAMPLE_RATE)

                    ora_str = time.strftime("%H:%M:%S")
                    print(f"{ora_str:<10} {etichetta:<26} {confidenza:.0%}")

                    picco = int(np.max(np.abs(dati_validi))) if len(dati_validi) else 0
                    timestamp_ms = int(time.time() * 1000)
                    nome_file = f"evento_{timestamp_ms}.wav"
                    try:
                        import wave
                        with wave.open(os.path.join(CARTELLA_EVENTI_RICONOSCIUTI, nome_file), "wb") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(SAMPLE_RATE)
                            wf.writeframes(dati_validi.tobytes())
                        with open(FILE_LOG_RICONOSCIMENTI, "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "fibra_id": ID_FIBRA_DEFAULT,
                                "classe_predetta": etichetta,
                                "confidenza": round(confidenza, 3),
                                "durata_s": round(pos_evento / SAMPLE_RATE, 3),
                                "picco": picco,
                                "clipping": picco >= SOGLIA_CLIPPING,
                                "file_audio": nome_file,
                                "modello": os.path.basename(PATH_MODELLO_DEFAULT),
                            }, ensure_ascii=False) + "\n")
                    except Exception as e:
                        print(f"  ⚠ Errore salvataggio evento: {e}")

                    catturando = False
                    pos_evento = 0
                    tempo_ultima_chiusura = time.monotonic()

    except KeyboardInterrupt:
        print("\nFermato dall'utente.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()