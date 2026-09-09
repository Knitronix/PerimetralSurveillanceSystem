"""
analizza_armoniche_sweep.py
----------------------------
Analisi automatica delle armoniche/spurie, pensata per lavorare insieme a
`trasmettitore/sweep_gradini.ino` (SOLO quello sketch, non trasmettitore.ino
- qui serve un tono singolo pulito a gradini, non il protocollo f1/f2).

Non serve guardare lo spettrogramma a occhio: questo script riceve lo stesso
stream UDP di main.py (24kHz, pacchetti da 254 campioni int16, porta 12345),
legge dalla seriale dell'Arduino quando cambia gradino e a quale frequenza
(righe "STEP;<millis>;<freq_hz>" stampate da sweep_gradini.ino), fa una FFT
su ogni gradino e riporta in automatico:
- la potenza della fondamentale (dovrebbe combaciare col gradino comandato)
- tutte le altre righe spettrali significative (spurie), con frequenza,
  potenza, rapporto rispetto alla fondamentale, differenza e rapporto di
  frequenza rispetto alla fondamentale
Alla fine (Ctrl+C per fermare, lo sweep su Arduino gira in loop) confronta le
spurie viste in gradini diversi e prova a classificarle da solo:
- differenza (spuria - fondamentale) costante su più gradini diversi ->
  probabile intermodulazione additiva con un riferimento fisso a
  quella differenza
- frequenza assoluta della spuria costante su più gradini diversi ->
  probabile risonanza/rumore fisso indipendente dalla fondamentale
- rapporto (spuria / fondamentale) vicino a un intero su più gradini ->
  probabile armonica vera della fondamentale

Uso: imposta SERIAL_PORT qui sotto con la porta COM dell'Arduino, chiudi il
Serial Monitor dell'IDE Arduino se e' aperto (la porta seriale la puo' tenere
aperta un solo programma alla volta) e lancia lo script. Il comando "ON" lo
manda lui in automatico un paio di secondi dopo l'apertura della seriale
(serve aspettare che l'Arduino finisca di riavviarsi - aprire la seriale lo
resetta, come fa anche l'IDE); manda anche "OFF" da solo alla chiusura
(Ctrl+C). Non c'e' bisogno di scrivere niente a mano da nessuna parte.
"""

import sys

# Console Windows di default spesso non e' UTF-8: un carattere accentato o
# un simbolo fuori dal set cp1252 in un print() manderebbe in crash lo
# script a meta' di uno sweep lungo, perdendo l'analisi fatta fino a li'.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import time
import socket
import json
import threading
import queue
from pathlib import Path
from dataclasses import dataclass, field

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Manca il pacchetto pyserial. Installa con: pip install pyserial")
    sys.exit(1)

# --- Configurazione: modifica qui la porta seriale dell'Arduino ---
SERIAL_PORT = 'COM4'  # <-- CAMBIA QUI con la porta giusta (vedi elenco stampato se sbagliata)
SERIAL_BAUD = 9600

UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000

# Scarta l'inizio e la fine di ogni gradino: transitori di accensione del
# gradino precedente/nuovo possono sporcare l'analisi se non tagliati via.
GUARDIA_INIZIO_SEC = 0.4
GUARDIA_FINE_SEC = 0.2

# Una riga spettrale conta come "spuria significativa" solo se la sua
# potenza supera SIA questa frazione della fondamentale SIA un multiplo del
# rumore di fondo stimato sullo spettro (mediana di tutti i bin): usare solo
# la frazione della fondamentale non basta, se la fondamentale stessa è
# quasi zero (es. nessun segnale vero, solo rumore) la soglia crolla a quasi
# zero e finisce per contare come "picco" ogni minima increspatura casuale.
RAPPORTO_MINIMO_SPURIA = 0.01  # 1% della fondamentale
FATTORE_MINIMO_RUMORE = 20.0  # almeno 20x la mediana dello spettro

# Due picchi più vicini di così vengono considerati lo stesso lobo spettrale
# (capita che un singolo picco reale "sbordi" su bin adjacenti e venga
# altrimenti contato più volte): si tiene solo il più forte dei due.
SEPARAZIONE_MINIMA_HZ = 15.0

# Tetto alle spurie tenute per gradino (le più forti): evita file di log
# enormi quando lo spettro è comunque rumoroso nonostante i filtri sopra.
MAX_SPURIE_TENUTE = 15

# Tolleranza per considerare uguali due valori tra gradini diversi, quando
# si cerca un pattern ricorrente (delta fisso, frequenza assoluta fissa,
# rapporto armonico intero).
TOLLERANZA_HZ = 40.0
TOLLERANZA_RAPPORTO_ARMONICA = 0.05

FILE_LOG = Path(__file__).resolve().parent / "sweep_armoniche_log.jsonl"


def potenza_normalizzata(spettro_ampiezza, n):
    """Stessa normalizzazione (0..1 = tono a piena scala int16) usata dal
    Goertzel in main.py, per poter confrontare i numeri con le soglie
    SOGLIA_ON/OFF già in uso li'."""
    ampiezza_normalizzata = (2.0 * spettro_ampiezza / n) / 32768.0
    return ampiezza_normalizzata ** 2


@dataclass
class RigaSpettrale:
    freq_hz: float
    potenza: float


@dataclass
class RisultatoGradino:
    freq_comandata: float
    freq_fondamentale: float
    potenza_fondamentale: float
    spurie: list = field(default_factory=list)  # list[RigaSpettrale]
    n_campioni: int = 0


def trova_picchi(freqs, potenze, potenza_minima):
    """Massimi locali sopra potenza_minima. Ritorna lista di (freq, potenza),
    ordinata per potenza decrescente. Niente librerie di picchi esterne:
    bastano vicini immediati per uno spettro già abbastanza pulito come
    questo (tono/i noti, non segnale generico rumoroso)."""
    picchi = []
    n = len(potenze)
    for i in range(1, n - 1):
        if potenze[i] < potenza_minima:
            continue
        if potenze[i] >= potenze[i - 1] and potenze[i] >= potenze[i + 1]:
            picchi.append((freqs[i], potenze[i]))
    picchi.sort(key=lambda p: p[1], reverse=True)
    return picchi


def analizza_finestra(campioni, freq_comandata):
    n = len(campioni)
    finestra = campioni.astype(np.float64) * np.hanning(n)
    spettro = np.fft.rfft(finestra)
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    ampiezza = np.abs(spettro)
    potenze = potenza_normalizzata(ampiezza, n)

    # La fondamentale e' il picco piu' vicino alla frequenza comandata
    # dall'Arduino (entro una finestra di tolleranza), non necessariamente
    # il piu' forte in assoluto - anche se di norma coincidono.
    maschera_vicino = np.abs(freqs - freq_comandata) <= max(TOLLERANZA_HZ, freq_comandata * 0.05)
    if not np.any(maschera_vicino):
        idx_fondamentale = int(np.argmax(potenze))
    else:
        indici_vicini = np.where(maschera_vicino)[0]
        idx_fondamentale = indici_vicini[np.argmax(potenze[indici_vicini])]

    freq_fondamentale = float(freqs[idx_fondamentale])
    potenza_fondamentale = float(potenze[idx_fondamentale])

    rumore_di_fondo = float(np.median(potenze))
    potenza_minima_spuria = max(
        potenza_fondamentale * RAPPORTO_MINIMO_SPURIA,
        rumore_di_fondo * FATTORE_MINIMO_RUMORE,
        1e-9,
    )
    tutti_i_picchi = trova_picchi(freqs, potenze, potenza_minima_spuria)

    # Soppressione dei picchi troppo vicini (stesso lobo spettrale contato
    # più volte): tutti_i_picchi è già ordinato per potenza decrescente,
    # quindi accettare greedily tenendo solo il primo di ogni gruppo vicino
    # equivale a tenere il più forte.
    spurie = []
    for f, p in tutti_i_picchi:
        if abs(f - freq_fondamentale) <= TOLLERANZA_HZ:
            continue  # e' la fondamentale stessa, non una spuria
        if any(abs(f - s.freq_hz) <= SEPARAZIONE_MINIMA_HZ for s in spurie):
            continue  # troppo vicina a una spuria già tenuta, più forte
        spurie.append(RigaSpettrale(freq_hz=f, potenza=p))
        if len(spurie) >= MAX_SPURIE_TENUTE:
            break

    return RisultatoGradino(
        freq_comandata=freq_comandata,
        freq_fondamentale=freq_fondamentale,
        potenza_fondamentale=potenza_fondamentale,
        spurie=spurie,
        n_campioni=n,
    )


def stampa_risultato_gradino(risultato):
    print(
        f"\n[gradino {risultato.freq_comandata:.0f}Hz] "
        f"fondamentale reale {risultato.freq_fondamentale:.1f}Hz, "
        f"potenza {risultato.potenza_fondamentale:.6f} "
        f"({risultato.n_campioni} campioni)"
    )
    if not risultato.spurie:
        print("  nessuna spuria significativa")
        return
    for s in sorted(risultato.spurie, key=lambda r: r.potenza, reverse=True)[:8]:
        rapporto = s.potenza / risultato.potenza_fondamentale if risultato.potenza_fondamentale > 0 else float("nan")
        delta = s.freq_hz - risultato.freq_fondamentale
        rapporto_freq = s.freq_hz / risultato.freq_fondamentale if risultato.freq_fondamentale > 0 else float("nan")
        print(
            f"  spuria {s.freq_hz:8.1f}Hz  potenza {s.potenza:.6f}  "
            f"({rapporto*100:5.1f}% della fondamentale)  "
            f"delta {delta:+8.1f}Hz  rapporto {rapporto_freq:6.3f}x"
        )


def scrivi_log(risultato):
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "freq_comandata": risultato.freq_comandata,
        "freq_fondamentale": risultato.freq_fondamentale,
        "potenza_fondamentale": risultato.potenza_fondamentale,
        "spurie": [{"freq_hz": s.freq_hz, "potenza": s.potenza} for s in risultato.spurie],
    }
    with open(FILE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def raggruppa_per_prossimita(valori, tolleranza):
    """Raggruppa una lista di valori scalari in cluster: ogni valore entra
    nel cluster esistente piu' vicino se e' entro `tolleranza`, altrimenti
    ne apre uno nuovo. Usato sia per raggruppare frequenze assolute che
    delta, con lo stesso criterio semplice (non serve altro: i valori attesi
    sono pochi cluster ben separati, non una nuvola densa)."""
    cluster = []  # list of [somma, conteggio, elementi]
    for v in sorted(valori):
        if cluster and abs(v - cluster[-1][0] / cluster[-1][1]) <= tolleranza:
            cluster[-1][0] += v
            cluster[-1][1] += 1
            cluster[-1][2].append(v)
        else:
            cluster.append([v, 1, [v]])
    return [(somma / conteggio, conteggio, elementi) for somma, conteggio, elementi in cluster]


def analisi_finale(risultati):
    print("\n" + "=" * 70)
    print(f"RIEPILOGO FINALE - {len(risultati)} gradini analizzati")
    print("=" * 70)

    if len(risultati) < 2:
        print("Troppi pochi gradini per un'analisi incrociata (serve almeno 2).")
        return

    # Ogni spuria vista, con a fianco la fondamentale del suo gradino.
    coppie = []  # (freq_spuria, freq_fondamentale, potenza_spuria)
    for r in risultati:
        for s in r.spurie:
            coppie.append((s.freq_hz, r.freq_fondamentale, s.potenza))

    if not coppie:
        print("Nessuna spuria significativa vista in nessun gradino: segnale pulito su tutto lo sweep.")
        return

    # --- Ipotesi 1: frequenza assoluta fissa (risonanza/rumore indipendente da f1) ---
    cluster_assoluti = raggruppa_per_prossimita([c[0] for c in coppie], TOLLERANZA_HZ)
    candidati_assoluti = [c for c in cluster_assoluti if c[1] >= 3]

    # --- Ipotesi 2: delta (spuria - fondamentale) fisso (intermodulazione additiva) ---
    cluster_delta = raggruppa_per_prossimita([c[0] - c[1] for c in coppie], TOLLERANZA_HZ)
    # tieni solo i delta osservati su fondamentali DIVERSE (altrimenti e' solo
    # lo stesso gradino contato piu' volte, non un pattern che regge nel tempo)
    candidati_delta = []
    for media, conteggio, elementi in cluster_delta:
        fondamentali_coinvolte = {round(f_fond) for f_spuria, f_fond, _ in coppie
                                   if abs((f_spuria - f_fond) - media) <= TOLLERANZA_HZ}
        if conteggio >= 3 and len(fondamentali_coinvolte) >= 2:
            candidati_delta.append((media, conteggio, fondamentali_coinvolte))

    # --- Ipotesi 3: rapporto vicino a un intero (armonica vera) ---
    cluster_rapporti = []
    for f_spuria, f_fond, _ in coppie:
        if f_fond <= 0:
            continue
        rapporto = f_spuria / f_fond
        vicino_intero = round(rapporto)
        if vicino_intero >= 2 and abs(rapporto - vicino_intero) <= TOLLERANZA_RAPPORTO_ARMONICA:
            cluster_rapporti.append(vicino_intero)
    conteggio_rapporti = {}
    for v in cluster_rapporti:
        conteggio_rapporti[v] = conteggio_rapporti.get(v, 0) + 1
    candidati_armonici = [(n, c) for n, c in conteggio_rapporti.items() if c >= 3]

    stampato_qualcosa = False

    if candidati_assoluti:
        stampato_qualcosa = True
        print("\n[Risonanza/rumore a frequenza FISSA, indipendente dalla fondamentale]")
        for media, conteggio, elementi in sorted(candidati_assoluti, key=lambda c: -c[1]):
            print(f"  ~{media:.0f}Hz  (vista {conteggio} volte, es. {[f'{e:.0f}' for e in elementi[:5]]})")

    if candidati_delta:
        stampato_qualcosa = True
        print("\n[Intermodulazione additiva: spuria = fondamentale +/- offset FISSO]")
        for media, conteggio, fondamentali in sorted(candidati_delta, key=lambda c: -c[1]):
            print(
                f"  offset {media:+.0f}Hz  (vista {conteggio} volte, su fondamentali diverse: "
                f"{sorted(fondamentali)[:8]})"
            )

    if candidati_armonici:
        stampato_qualcosa = True
        print("\n[Armoniche vere della fondamentale (spuria ~ N x fondamentale)]")
        for n, c in sorted(candidati_armonici, key=lambda x: -x[1]):
            print(f"  {n}x  (vista {c} volte)")

    if not stampato_qualcosa:
        print("\nSpurie viste, ma nessun pattern chiaro e ricorrente su più gradini diversi")
        print("(potrebbero essere rumore occasionale non sistematico - controlla il log jsonl per i dettagli).")

    print(f"\nDettaglio completo per gradino salvato in: {FILE_LOG}")


class LettoreSeriale(threading.Thread):
    """Legge le righe 'STEP;<millis>;<freq_hz>' dall'Arduino e le mette in
    coda. Thread separato: la lettura seriale bloccante non deve fermare la
    ricezione UDP nel thread principale."""

    def __init__(self, porta, baud, coda_eventi):
        super().__init__(daemon=True)
        self.coda_eventi = coda_eventi
        self.running = True
        self.pronto = threading.Event()
        try:
            self.ser = serial.Serial(porta, baud, timeout=1)
        except serial.SerialException as e:
            porte = [p.device for p in serial.tools.list_ports.comports()]
            print(f"Impossibile aprire {porta}: {e}")
            print(f"Porte seriali disponibili: {porte or 'nessuna trovata'}")
            print("Modifica SERIAL_PORT in cima allo script e riprova.")
            sys.exit(1)

    def run(self):
        while self.running:
            try:
                riga = self.ser.readline().decode("utf-8", errors="ignore").strip()
            except serial.SerialException:
                break
            if not riga:
                continue
            print(f"[arduino] {riga}")
            if "Pronto" in riga:
                # Segnala che setup() e' finito e l'Arduino sta davvero
                # leggendo la seriale: prima di questo punto un "ON"
                # mandato da Python puo' arrivare mentre la board sta
                # ancora riavviandosi (dopo il reset via DTR all'apertura
                # della porta) e andare perso - e' quello che sembra essere
                # successo nel giro precedente (~100s di silenzio prima che
                # i dati diventassero buoni: un'attesa fissa di 2s non
                # basta se il boot e' più lento del previsto).
                self.pronto.set()
            if riga.startswith("STEP;"):
                try:
                    _, _millis_arduino, freq_str = riga.split(";")
                    freq_hz = float(freq_str)
                except ValueError:
                    continue
                # Timestamp lato Python (non quello dell'Arduino): evita
                # problemi di sincronizzazione tra i due orologi, il
                # ritardo della seriale e' trascurabile su gradini di
                # diversi secondi.
                self.coda_eventi.put((time.time(), freq_hz))

    def invia_comando(self, comando):
        try:
            self.ser.write((comando + "\n").encode("utf-8"))
        except Exception as e:
            print(f"Errore inviando '{comando}' sulla seriale: {e}")

    def stop(self):
        self.running = False
        try:
            self.ser.close()
        except Exception:
            pass


def archivia_log_precedente():
    """Il log si scrive in append: senza archiviare quello di una sessione
    precedente, i dati vecchi si mischierebbero con quelli nuovi (successo
    per davvero: un run con ~100s di silenzio iniziale rimasto nel file
    insieme ai dati buoni del giro successivo). Ogni avvio parte pulito."""
    if not FILE_LOG.exists():
        return
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    destinazione = FILE_LOG.with_name(f"{FILE_LOG.stem}_{timestamp}{FILE_LOG.suffix}")
    FILE_LOG.rename(destinazione)
    print(f"Log precedente archiviato in: {destinazione}")


def main():
    archivia_log_precedente()
    coda_eventi = queue.Queue()
    lettore = LettoreSeriale(SERIAL_PORT, SERIAL_BAUD, coda_eventi)
    lettore.start()

    # Aprire la seriale da Python resetta l'Arduino (via DTR, come fa anche
    # l'IDE): serve aspettare che setup() sia finito prima di mandare "ON",
    # altrimenti il comando arriva mentre la board sta ancora riavviandosi
    # e va perso (visto succedere per davvero: ~100s di silenzio in un giro
    # precedente, con un'attesa fissa troppo corta). Invece di indovinare
    # quanti secondi bastano, si aspetta il segnale vero: la riga "Pronto"
    # che l'Arduino stampa alla fine di setup(), quando legge davvero la
    # seriale. Lo script tiene lui la seriale aperta per tutta la durata
    # (non serve più il Serial Monitor dell'IDE, anzi va chiuso perché la
    # porta la può tenere aperta un solo programma alla volta): "ON" glielo
    # manda questo script da solo, non c'è un altro posto dove scriverlo.
    print("Aspetto che l'Arduino finisca il riavvio (riga 'Pronto')...")
    if not lettore.pronto.wait(timeout=15.0):
        print("ATTENZIONE: non ho visto 'Pronto' entro 15s, mando 'ON' comunque ma verifica la porta/il cavo.")
    lettore.invia_comando("ON")
    print("Comando 'ON' inviato: lo sweep dovrebbe partire adesso.")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(0.5)
    bytes_attesi = CAMPIONI_PER_PACCHETTO * 2

    print(f"In ascolto UDP su porta {UDP_PORT}, seriale su {SERIAL_PORT}.")
    print("Ctrl+C per fermare ed avere il riepilogo finale.\n")

    gradino_corrente = None  # (freq_comandata, t_inizio)
    buffer_corrente = []
    risultati = []

    def chiudi_gradino_corrente(t_fine):
        nonlocal buffer_corrente
        if gradino_corrente is None or not buffer_corrente:
            buffer_corrente = []
            return
        freq_comandata, t_inizio = gradino_corrente
        campioni = np.concatenate(buffer_corrente)
        inizio_taglio = int(GUARDIA_INIZIO_SEC * SAMPLE_RATE)
        fine_taglio = int(GUARDIA_FINE_SEC * SAMPLE_RATE)
        campioni_validi = campioni[inizio_taglio: len(campioni) - fine_taglio] if len(campioni) > (inizio_taglio + fine_taglio) else campioni
        if len(campioni_validi) < SAMPLE_RATE * 0.5:
            print(f"[gradino {freq_comandata:.0f}Hz] troppo pochi campioni validi, saltato")
        else:
            risultato = analizza_finestra(campioni_validi, freq_comandata)
            stampa_risultato_gradino(risultato)
            scrivi_log(risultato)
            risultati.append(risultato)
        buffer_corrente = []

    try:
        while True:
            try:
                nuovo_evento = coda_eventi.get_nowait()
            except queue.Empty:
                nuovo_evento = None

            if nuovo_evento is not None:
                t_evento, freq_hz = nuovo_evento
                chiudi_gradino_corrente(t_evento)
                gradino_corrente = (freq_hz, t_evento)

            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                continue

            if len(data) != bytes_attesi or gradino_corrente is None:
                continue

            buffer_corrente.append(np.frombuffer(data, dtype=np.int16))

    except KeyboardInterrupt:
        print("\n\nInterrotto dall'utente, chiudo l'ultimo gradino in corso...")
        chiudi_gradino_corrente(time.time())
    finally:
        lettore.invia_comando("OFF")
        time.sleep(0.2)  # da' tempo al comando di partire prima di chiudere la porta
        lettore.stop()
        sock.close()

    analisi_finale(risultati)


if __name__ == "__main__":
    main()
