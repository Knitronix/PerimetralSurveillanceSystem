"""
2analizzatempiminimi.py
--------------------------
Analisi automatica del delta t minimo affidabile, pensata per lavorare con
la MODALITA' 2 (sweep tempi) di arduino/master/master.ino - selezionata in
automatico da questo script tramite shelly_mode_selector.py (nessuna azione
manuale: lo script cicla da solo l'accensione della Shelly).

NESSUNA dipendenza dalla seriale/USB: una volta installato il master non è
quasi mai raggiungibile via cavo, quindi questo script non apre una porta
seriale e non manda nessun comando "ON" - la modalità 2 parte già sonora
da sola (vedi master.ino). Per sapere a che punto dello sweep siamo, lo
script:
1. aspetta che il tono (sempre a FREQ_TEST_HZ, fissa) compaia sul flusso
   UDP - rilevato dal rapporto tra la potenza a quella frequenza e la
   mediana dello spettro, non da una soglia assoluta (un tono vero,
   anche debole, concentra l'energia in un bin solo: il rapporto è
   enormemente più alto di qualunque punta casuale del rumore - vedi
   analizza_armoniche_sweep.py per la stessa tecnica, misurata dal vivo);
2. da quel momento (tempo zero) replica la STESSA progressione
   deterministica di tempi_avvia_gradino()/tempi_avanza_gradino() in
   master.ino per sapere in quale gradino (fase, ON, GAP) siamo in ogni
   istante - niente bisogno di leggere "STEP;"/"SWEEP_FINITO" dalla
   seriale, la durata di ogni gradino è nota in anticipo.

Idea per l'analisi di ogni gradino: invece di ricostruire finestre
temporali allineate a ogni singolo impulso (fragile, dipende da una
sincronizzazione precisa che qui non abbiamo), si ricalcola la potenza
Goertzel blocco per blocco (20ms, come BLOCCO_GOERTZEL_MS in 3main.py) su
tutto il gradino, e ci si fa passare sopra la STESSA macchina a stati a
isteresi (ON/OFF) usata davvero in 3main.py (MacchinaStatoFrequenza). Si
conta quante transizioni a ON vengono rilevate: se il canale riesce a
seguire il pattern comandato, il conteggio deve combaciare con
RIPETIZIONI_PER_GRADINO (una transizione per ogni impulso). Se le
transizioni rilevate sono meno:
- in FASE A (ON che si accorcia) → gli impulsi più corti probabilmente non
  fanno più in tempo a superare SOGLIA_ON: si sono "persi" (mai contati).
- in FASE B (gap che si accorcia) → i gap più corti probabilmente non fanno
  più in tempo a scendere sotto SOGLIA_OFF: due impulsi consecutivi si
  fondono in una sola transizione invece di due.

IMPORTANTE: FREQ_TEST_HZ, SOGLIA_ON, SOGLIA_OFF, e le durate ON/GAP qui
sotto vanno tenute allineate a mano con TEMPI_FREQ_TEST_HZ/TEMPI_DURATE_*
in arduino/master/master.ino (e le soglie con quelle attuali in 3main.py) -
il risultato vale solo per la combinazione frequenza+soglie con cui è
stato misurato.

Uso: lancia lo script (l'IP della Shelly lo legge da 3main.py). Ctrl+C per
fermare in anticipo (stampa comunque il riepilogo di quanto raccolto finora
e spegne la Shelly); normalmente si ferma da solo a fine sweep.
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import importlib
import time
import socket
import json
from pathlib import Path
from dataclasses import dataclass

import numpy as np

from others.archivio import cartella_taratura
from others.shelly_mode_selector import seleziona_modalita, spegni_master

# IP della Shelly: letto da 3main.py (Config.SMART_PLUG_HOST), cosi' quando il
# DHCP lo cambia si corregge in un posto solo. "import 3main" non e' sintassi
# valida (nome che inizia con un numero): si importa per nome.
SMART_PLUG_HOST = importlib.import_module("3main").Config().SMART_PLUG_HOST
MODALITA_SWEEP_TEMPI = 2  # vedi arduino/master/master.ino

UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000

# Deve restare allineata a mano con TEMPI_FREQ_TEST_HZ in
# arduino/master/master.ino (di default = FREQ2 = 3200Hz, F2 - il canale
# "stato interruttore"). Per testare F1 invece, cambiare entrambe insieme.
FREQ_TEST_HZ = 3200
# Ricalcolate il 19/10/2026 con 0trovafrequenze.py (fibra e mezzo cambiati):
# f2(3200Hz) a PIENA ampiezza = 0.00343 (media di due misure). ATTENZIONE:
# NON sono le stesse soglie di 3main.py - in modalita' 2 il master suona un
# tono singolo a piena ampiezza, in produzione (daisy chain) a meta'
# ampiezza (potenza 1/4, vedi ISR in master.ino). Qui si tiene lo stesso
# RAPPORTO tono/soglia della produzione (ON = 20% del tono, OFF = 65%
# dell'ON), cosi' il test misura i tempi minimi nelle stesse condizioni di
# rilevamento di 3main.py. Regola di conversione: soglia qui = soglia 3main.py x 4.
SOGLIA_ON = 0.000686
SOGLIA_OFF = 0.000446

BLOCCO_MS = 20.0
BLOCCO_CAMPIONI = max(1, round(SAMPLE_RATE * BLOCCO_MS / 1000.0))

# --- Schedule dei gradini - DEVE restare allineata a mano con le costanti
# TEMPI_* in arduino/master/master.ino: replica tempi_avvia_gradino()/
# tempi_avanza_gradino() per sapere in quale gradino siamo dato il tempo
# trascorso dal tempo zero, senza bisogno della seriale. ---
TEMPI_DURATE_ON_MS = [150, 120, 100, 80, 60, 50, 40, 30, 25, 20, 15, 10]
TEMPI_GAP_FISSO_FASE_A_MS = 300
TEMPI_DURATE_GAP_MS = [200, 150, 120, 100, 80, 60, 50, 40, 30, 25, 20, 15, 10]
TEMPI_ON_FISSO_FASE_B_MS = 80
RIPETIZIONI_PER_GRADINO = 20  # deve combaciare con la modalita' 2 dello sketch


def costruisci_schedule():
    """Lista di (fase, on_ms, gap_ms, durata_ms) nell'ordine in cui
    master.ino li genera davvero: tutta la fase A, poi tutta la fase B."""
    schedule = []
    for on_ms in TEMPI_DURATE_ON_MS:
        gap_ms = TEMPI_GAP_FISSO_FASE_A_MS
        schedule.append(("A", on_ms, gap_ms, (on_ms + gap_ms) * RIPETIZIONI_PER_GRADINO))
    for gap_ms in TEMPI_DURATE_GAP_MS:
        on_ms = TEMPI_ON_FISSO_FASE_B_MS
        schedule.append(("B", on_ms, gap_ms, (on_ms + gap_ms) * RIPETIZIONI_PER_GRADINO))
    return schedule


SCHEDULE = costruisci_schedule()


def trova_step_corrente(trascorso_s: float):
    """Ritorna (indice, fase, on_ms, gap_ms) per il gradino attivo al tempo
    trascorso_s dal tempo zero, o None se lo sweep e' finito (equivalente a
    "SWEEP_FINITO" lato Arduino)."""
    cum_ms = 0.0
    trascorso_ms = trascorso_s * 1000.0
    for i, (fase, on_ms, gap_ms, durata_ms) in enumerate(SCHEDULE):
        if trascorso_ms < cum_ms + durata_ms:
            return i, fase, on_ms, gap_ms
        cum_ms += durata_ms
    return None


# --- Rilevamento del tono sul flusso UDP (vedi intestazione) ---
FINESTRA_RILEVAMENTO_S = 0.3
CAMPIONI_PER_FINESTRA_RILEVAMENTO = int(SAMPLE_RATE * FINESTRA_RILEVAMENTO_S)
# Stessa soglia e stesso ragionamento di RAPPORTO_MINIMO_DOMINANTE in
# analizza_armoniche_sweep.py (misurato dal vivo: rumore puro arriva a
# ~19x per natura statistica, un tono vero anche debole supera ~69.000x).
RAPPORTO_MINIMO_TONO = 50.0
CONFERME_CONSECUTIVE_RICHIESTE = 2
TIMEOUT_PRIMO_SEGNALE_S = 20.0

# Ogni prova salva i suoi file nell'archivio della giornata, come
# 0trovafrequenze.py e 1tarasoglie.py: archivio/AAAA-MM-GG/taratura/
#   tempi_HHMMSS.jsonl  un risultato per gradino (impostato in main())
#   tempi_HHMMSS.txt    nota + riepilogo finale
#   tempi_HHMMSS.npz    tutto il segnale ricevuto, per rianalizzarlo
FILE_LOG: Path | None = None


def potenza_normalizzata(spettro_ampiezza, n):
    ampiezza_normalizzata = (2.0 * spettro_ampiezza / n) / 32768.0
    return ampiezza_normalizzata ** 2


def rapporto_tono_presente(campioni) -> float:
    """Rapporto tra la potenza alla frequenza nota (FREQ_TEST_HZ) e la
    mediana dello spettro nella stessa finestra - vedi RAPPORTO_MINIMO_TONO."""
    n = len(campioni)
    finestra = campioni.astype(np.float64) * np.hanning(n)
    spettro = np.abs(np.fft.rfft(finestra))
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    potenze = potenza_normalizzata(spettro, n)
    idx_target = int(np.argmin(np.abs(freqs - FREQ_TEST_HZ)))
    mediana = float(np.median(potenze))
    if mediana <= 0:
        return 0.0
    return float(potenze[idx_target]) / mediana


class GoertzelSingolo:
    """Stesso identico calcolo di RilevatoreGoertzel in 3main.py, riscritto
    qui per non dover importare 3main.py (che all'import avvia un'app Qt)."""

    def __init__(self, frequenza_hz, sample_rate, dimensione_blocco):
        self.dimensione_blocco = dimensione_blocco
        k = round(dimensione_blocco * frequenza_hz / sample_rate)
        omega = 2.0 * np.pi * k / dimensione_blocco
        self.coeff = 2.0 * np.cos(omega)

    def potenza(self, blocco):
        coeff = self.coeff
        q1 = 0.0
        q2 = 0.0
        for x in blocco.tolist():
            q0 = coeff * q1 - q2 + x
            q2 = q1
            q1 = q0
        magnitudine_quadra = q1 * q1 + q2 * q2 - coeff * q1 * q2
        n = self.dimensione_blocco
        return magnitudine_quadra / ((n / 2.0) ** 2) / (32768.0 ** 2)


def potenze_a_blocchi(campioni, goertzel):
    n_blocchi = len(campioni) // BLOCCO_CAMPIONI
    potenze = np.empty(n_blocchi, dtype=np.float64)
    for i in range(n_blocchi):
        blocco = campioni[i * BLOCCO_CAMPIONI: (i + 1) * BLOCCO_CAMPIONI]
        potenze[i] = goertzel.potenza(blocco)
    return potenze


def conta_transizioni_on(potenze, soglia_on, soglia_off):
    """Stessa macchina a stati a isteresi di MacchinaStatoFrequenza in
    3main.py: conta quante volte si passa da OFF ad ON."""
    attivo = False
    conteggio = 0
    for p in potenze:
        if not attivo:
            if p > soglia_on:
                attivo = True
                conteggio += 1
        else:
            if p <= soglia_off:
                attivo = False
    return conteggio


@dataclass
class RisultatoGradino:
    fase: str
    on_ms: float
    gap_ms: float
    transizioni_rilevate: int
    transizioni_attese: int
    n_blocchi: int


def analizza_gradino(campioni, fase, on_ms, gap_ms, goertzel):
    potenze = potenze_a_blocchi(campioni, goertzel)
    rilevate = conta_transizioni_on(potenze, SOGLIA_ON, SOGLIA_OFF)
    return RisultatoGradino(
        fase=fase, on_ms=on_ms, gap_ms=gap_ms,
        transizioni_rilevate=rilevate,
        transizioni_attese=RIPETIZIONI_PER_GRADINO,
        n_blocchi=len(potenze),
    )


def stampa_risultato(r):
    ok = r.transizioni_rilevate >= r.transizioni_attese
    simbolo = "OK " if ok else "PERSE"
    print(
        f"[fase {r.fase}] ON={r.on_ms:5.0f}ms GAP={r.gap_ms:5.0f}ms  "
        f"transizioni {r.transizioni_rilevate:2d}/{r.transizioni_attese:2d}  {simbolo}"
    )


def scrivi_log(r):
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fase": r.fase, "on_ms": r.on_ms, "gap_ms": r.gap_ms,
        "transizioni_rilevate": r.transizioni_rilevate,
        "transizioni_attese": r.transizioni_attese,
    }
    with open(FILE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def riga_risultato(r) -> str:
    ok = r.transizioni_rilevate >= r.transizioni_attese
    return (f"[fase {r.fase}] ON={r.on_ms:5.0f}ms GAP={r.gap_ms:5.0f}ms  "
            f"transizioni {r.transizioni_rilevate:2d}/{r.transizioni_attese:2d}  {'OK ' if ok else 'PERSE'}")


def riepilogo_finale(risultati) -> str:
    """Stampa il riepilogo e lo ritorna come testo (va anche nel .txt)."""
    righe = ["=" * 70, "RIEPILOGO FINALE", "=" * 70]

    for fase, etichetta, variabile in (("A", "durata ON minima (gap largo fisso)", "on_ms"),
                                        ("B", "durata GAP minima (ON sicuro fisso)", "gap_ms")):
        gradini_fase = [r for r in risultati if r.fase == fase]
        if not gradini_fase:
            continue
        gradini_fase.sort(key=lambda r: getattr(r, variabile))
        buoni = [r for r in gradini_fase if r.transizioni_rilevate >= r.transizioni_attese]
        righe.append(f"\n[Fase {fase}] {etichetta}:")
        righe += [riga_risultato(r) for r in gradini_fase]
        if buoni:
            minimo = min(getattr(r, variabile) for r in buoni)
            righe.append(f"  --> minimo affidabile trovato in questo sweep: {minimo:.0f}ms")
            righe.append(f"      (consiglio: usa almeno {minimo * 1.5:.0f}ms in produzione, margine di sicurezza)")
        else:
            righe.append("  --> nessun gradino testato ha funzionato pienamente: prova valori anche più larghi")

    testo = "\n".join(righe)
    print("\n" + testo)
    print(f"\nDettaglio per gradino salvato in: {FILE_LOG}")
    return testo


def main():
    global FILE_LOG
    # Nota facoltativa, mai chiesta: python 2analizzatempiminimi.py "fibra 2, giardino"
    nota = " ".join(sys.argv[1:]).strip()
    base = cartella_taratura() / time.strftime("tempi_%H%M%S")
    FILE_LOG = base.with_suffix(".jsonl")
    print(f"I file di questa prova andranno in: {base.parent}\n")

    # Porta il master in modalita' sweep tempi ciclando la Shelly
    # (arduino/master/master.ino intestazione, shelly_mode_selector.py).
    seleziona_modalita(SMART_PLUG_HOST, MODALITA_SWEEP_TEMPI)

    goertzel = GoertzelSingolo(FREQ_TEST_HZ, SAMPLE_RATE, BLOCCO_CAMPIONI)
    # Tutto il segnale ricevuto, dall'inizio dell'ascolto: salvato a fine
    # prova in base.npz. campione_t0 = dove e' stato rilevato il tempo zero.
    registrazione: list[np.ndarray] = []
    campione_t0 = None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(0.5)
    bytes_attesi = CAMPIONI_PER_PACCHETTO * 2

    print(f"In ascolto UDP su porta {UDP_PORT}, in attesa che il tono ({FREQ_TEST_HZ}Hz) compaia...")

    # --- Fase 1: aspetta il tempo zero (vedi intestazione del file) ---
    t0 = None
    buffer_rilevamento = []
    campioni_accumulati = 0
    candidato_presente = False
    conferme = 0
    t_inizio_attesa = time.time()
    while t0 is None:
        try:
            data, _addr = sock.recvfrom(4096)
        except socket.timeout:
            if time.time() - t_inizio_attesa > TIMEOUT_PRIMO_SEGNALE_S:
                print(f"ERRORE: nessun segnale rilevato entro {TIMEOUT_PRIMO_SEGNALE_S:.0f}s - "
                      "controlla che la Shelly si sia accesa e che il master sia collegato al trasduttore.")
                spegni_master(SMART_PLUG_HOST)
                sock.close()
                return
            continue
        if len(data) != bytes_attesi:
            continue
        registrazione.append(np.frombuffer(data, dtype=np.int16))
        buffer_rilevamento.append(registrazione[-1])
        campioni_accumulati += CAMPIONI_PER_PACCHETTO
        if campioni_accumulati < CAMPIONI_PER_FINESTRA_RILEVAMENTO:
            continue
        coda = np.concatenate(buffer_rilevamento)[-CAMPIONI_PER_FINESTRA_RILEVAMENTO:]
        buffer_rilevamento = []
        campioni_accumulati = 0
        rapporto = rapporto_tono_presente(coda)
        presente = rapporto >= RAPPORTO_MINIMO_TONO
        if presente and presente == candidato_presente:
            conferme += 1
        else:
            candidato_presente = presente
            conferme = 1 if presente else 0
        if presente and conferme >= CONFERME_CONSECUTIVE_RICHIESTE:
            t0 = time.time()
            campione_t0 = len(registrazione) * CAMPIONI_PER_PACCHETTO
            print(f"Tono rilevato (rapporto={rapporto:.0f}x) - tempo zero impostato, inizio analisi.\n")

    # --- Fase 2: segui lo schedule deterministico, accumula e analizza ogni gradino ---
    risultati = []
    step_corrente = None  # (indice, fase, on_ms, gap_ms)
    buffer_gradino = []

    def chiudi_gradino_corrente():
        nonlocal buffer_gradino
        if step_corrente is None or not buffer_gradino:
            buffer_gradino = []
            return
        _idx, fase, on_ms, gap_ms = step_corrente
        campioni = np.concatenate(buffer_gradino)
        durata_attesa_ms = (on_ms + gap_ms) * RIPETIZIONI_PER_GRADINO
        if len(campioni) < SAMPLE_RATE * (durata_attesa_ms / 1000.0) * 0.5:
            print(f"[fase {fase}] ON={on_ms:.0f} GAP={gap_ms:.0f}: troppo pochi campioni, saltato")
        else:
            r = analizza_gradino(campioni, fase, on_ms, gap_ms, goertzel)
            stampa_risultato(r)
            scrivi_log(r)
            risultati.append(r)
        buffer_gradino = []

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if len(data) != bytes_attesi:
                continue

            trascorso_s = time.time() - t0
            step = trova_step_corrente(trascorso_s)
            if step is None:
                print("\nSweep finito (tutti i gradini completati).")
                break

            if step_corrente is None or step[0] != step_corrente[0]:
                chiudi_gradino_corrente()
                step_corrente = step

            registrazione.append(np.frombuffer(data, dtype=np.int16))
            buffer_gradino.append(registrazione[-1])

        chiudi_gradino_corrente()

    except KeyboardInterrupt:
        print("\n\nInterrotto dall'utente, chiudo l'ultimo gradino in corso...")
        chiudi_gradino_corrente()
    finally:
        spegni_master(SMART_PLUG_HOST)
        sock.close()
        if registrazione:
            np.savez_compressed(base.with_suffix(".npz"), x=np.concatenate(registrazione),
                                campione_t0=-1 if campione_t0 is None else campione_t0,
                                sample_rate=SAMPLE_RATE, freq_test_hz=FREQ_TEST_HZ,
                                soglia_on=SOGLIA_ON, soglia_off=SOGLIA_OFF, nota=nota)
            print(f"Registrazione salvata in: {base.with_suffix('.npz')}")

    testo = riepilogo_finale(risultati)
    base.with_suffix(".txt").write_text(f"Nota: {nota or '(nessuna)'}\n\n{testo}\n", encoding="utf-8")
    print(f"Riepilogo salvato in: {base.with_suffix('.txt')}")


if __name__ == "__main__":
    main()
