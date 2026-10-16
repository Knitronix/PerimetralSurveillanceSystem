"""
analizza_armoniche_sweep.py
----------------------------
Analisi automatica delle armoniche/spurie, pensata per lavorare con la
MODALITA' 1 (sweep frequenze) di arduino/master/master.ino - selezionata
in automatico da questo script tramite others/shelly_mode_selector.py
(nessuna azione manuale: lo script cicla da solo l'accensione della Shelly).

NESSUNA dipendenza dalla seriale/USB: una volta installato il master non è
quasi mai raggiungibile via cavo, quindi questo script non apre una porta
seriale e non manda nessun comando "ON" - la modalità 1 parte già sonora
da sola (vedi master.ino).

Come sa QUALE frequenza sta suonando in ogni istante (16/10/2026, riscritto
dopo un bug trovato analizzando un log reale - vedi sotto):
NON rileva piu' la frequenza dominante continuamente dal flusso UDP. Quel
primo approccio sembrava piu' robusto (si auto-calibra sul rumore, niente
assunzioni sul tempo) ma aveva un difetto serio: quando la VERA fondamentale
di un gradino e' debole ma una sua armonica (tipicamente la 3a) e' piu'
forte - capita spesso su questa fibra, che genera armoniche forti - lo
spettro globale ha il picco piu' alto SULL'ARMONICA, non sulla fondamentale.
Il rilevatore allora "snappava" quel picco al gradino noto piu' vicino e
lo etichettava come un gradino comandato diverso da quello vero: su un
log reale, i gradini a 600/800/1400/1600/2000Hz (deboli) venivano quasi
sempre scambiati per gradini a 1800/2400/4200/4800/6000Hz (= le loro 3e
armoniche, molto piu' forti) - un'etichettatura sistematica e ripetibile,
non rumore casuale.

MODALITA' 1 e' pero' completamente deterministica (rampa fissa 200->6000Hz
a passi di 200Hz, SWEEPF_STEP_DURATION_MS per gradino, poi ripete in loop -
vedi master.ino): non serve indovinare QUALE frequenza stiamo sentendo,
basta sapere QUANDO e' iniziato il primo gradino e seguire lo schedule noto
per tempo trascorso - esattamente la stessa tecnica usata in
analizza_tempi_minimi.py.

SECONDO bug trovato (16/10/2026, log reale dopo il primo fix sopra): la
prima versione di questo fix ancorava il "tempo zero" al primo tono
rilevabile dal flusso UDP (stessa tecnica di analizza_tempi_minimi.py).
Ma il PRIMO gradino della rampa e' 200Hz, che su questa fibra e' troppo
debole per superare la soglia di rilevamento nei suoi 4 secondi di
finestra: il primo tono davvero rilevabile arrivava quindi durante il
gradino SUCCESSIVO (400Hz reale), pur venendo etichettato come se fosse
l'inizio del gradino 0 - uno sfasamento di un intero gradino (200Hz),
costante per tutto il resto dello sweep (confermato su un log reale: lo
scostamento tra frequenza comandata e misurata era +190/+193Hz, identico
e con SNR altissimo, su tutti i gradini forti).

Fix: il tempo zero ora si ancora al momento in cui lo script COMINCIA AD
ASCOLTARE (subito dopo che others/shelly_mode_selector.py ha gia' aspettato
boot e conferma modalita'), non al primo tono rilevato - non serve piu'
che il gradino 0 sia rilevabile. Il rilevamento via FFT resta, ma solo
come DIAGNOSTICA non bloccante: se il primo tono rilevabile arriva troppo
tardi (piu' di un gradino intero dopo l'inizio dell'ascolto), lo script
stampa un avviso invece di fidarsi ciecamente - meglio un avviso visibile
che un disallineamento silenzioso come la volta scorsa. Lo scostamento tra
l'orologio di Python e millis() su Arduino su pochi minuti resta
trascurabile rispetto a gradini da 4 secondi (per i gradini da 10-15ms di
analizza_tempi_minimi.py sarebbe stato un problema, qui no).

Novita' di oggi: oltre al dettaglio per-gradino (stampato comunque a
schermo mentre lo sweep procede), lo script stampa anche un blocco
"RISULTATO" leggibile a schermo - una tabella con potenza/valutazione per
ogni frequenza nota e un elenco dei migliori candidati per F1/F2, con
eventuali avvisi di collisione armonica tra loro - cosi' non serve aprire
il file di log jsonl per farsi un'idea. Viene ristampato ad ogni ciclo
completo della rampa (si aggiorna via via) e di nuovo alla fine (Ctrl+C).

Non serve guardare lo spettrogramma a occhio: questo script riceve lo
stesso stream UDP di main.py (24kHz, pacchetti da 254 campioni int16,
porta 12345), fa una FFT su ogni gradino e riporta in automatico:
- la potenza della fondamentale (al gradino comandato, ora sempre corretto)
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
Questa analisi incrociata ORA e' finalmente attendibile, visto che prima
girava su dati con l'etichettatura sbagliata.

Uso: imposta SMART_PLUG_HOST qui sotto e lancia lo script. Pensa lui a
tutto: sceglie la modalità giusta ciclando la Shelly, aspetta che il
segnale compaia sul flusso UDP, poi analizza. Ctrl+C per fermare e avere
il riepilogo finale (spegne anche la Shelly).
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
from pathlib import Path
from dataclasses import dataclass, field

import numpy as np

from others.shelly_mode_selector import seleziona_modalita, spegni_master

# --- Configurazione: modifica qui l'IP della Shelly che alimenta il master ---
SMART_PLUG_HOST = "192.168.1.9"  # <-- CAMBIA QUI se la Shelly ha un altro IP (il DHCP lo cambia di tanto in tanto)
MODALITA_SWEEP_FREQUENZE = 1  # vedi arduino/master/master.ino

UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000

# --- Schedule dei gradini - DEVE restare allineata a mano con le costanti
# SWEEPF_* in arduino/master/master.ino: rampa fissa, nota in anticipo, non
# serve "indovinarla" dal segnale (vedi intestazione del file). ---
SWEEPF_FREQ_MIN = 200
SWEEPF_FREQ_MAX = 6000
SWEEPF_FREQ_STEP = 200
SWEEPF_STEP_DURATION_MS = 4000
N_GRADINI = int((SWEEPF_FREQ_MAX - SWEEPF_FREQ_MIN) / SWEEPF_FREQ_STEP) + 1
SCHEDULE = [SWEEPF_FREQ_MIN + i * SWEEPF_FREQ_STEP for i in range(N_GRADINI)]


def indice_gradino_corrente(trascorso_s: float) -> int:
    """Indice (0..N_GRADINI-1) del gradino attivo al tempo trascorso_s dal
    tempo zero (vedi intestazione). La rampa ripete all'infinito, quindi
    non ritorna mai None - a differenza dello schedule finito di
    analizza_tempi_minimi.py."""
    trascorso_ms = trascorso_s * 1000.0
    return int(trascorso_ms // SWEEPF_STEP_DURATION_MS) % N_GRADINI


def freq_del_gradino(indice: int) -> int:
    return SCHEDULE[indice]


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
# rapporto armonico intero, o una collisione armonica tra due candidati).
TOLLERANZA_HZ = 40.0
TOLLERANZA_RAPPORTO_ARMONICA = 0.05

# --- Rilevamento del tempo zero (vedi intestazione) - usato SOLO una volta
# all'inizio, non piu' continuamente durante lo sweep. ---
FINESTRA_RILEVAMENTO_S = 0.5
CAMPIONI_PER_FINESTRA_RILEVAMENTO = int(SAMPLE_RATE * FINESTRA_RILEVAMENTO_S)
# Il picco dominante nella finestra deve superare questo multiplo della
# MEDIANA della finestra stessa per essere considerato "c'e' un tono", non
# rumore - relativo al rumore di QUELLA finestra, non un numero fisso.
# Misurato dal vivo (300 finestre di rumore puro sintetico): il rapporto
# dominante/mediana del SOLO rumore arriva fino a ~19x per la sua stessa
# natura statistica (spettro "a punte", chi-quadro) - un tono vero, anche
# debolissimo (ampiezza 50/32768), da' invece ~69.000x. 50 lascia un
# margine enorme su entrambi i lati.
RAPPORTO_MINIMO_DOMINANTE = 50.0
CONFERME_CONSECUTIVE_RICHIESTE = 2
TIMEOUT_PRIMO_SEGNALE_S = 20.0

FILE_LOG = Path(__file__).resolve().parent / "sweep_armoniche_log.jsonl"


def potenza_normalizzata(spettro_ampiezza, n):
    """Stessa normalizzazione (0..1 = tono a piena scala int16) usata dal
    Goertzel in main.py, per poter confrontare i numeri con le soglie
    SOGLIA_ON/OFF già in uso li'."""
    ampiezza_normalizzata = (2.0 * spettro_ampiezza / n) / 32768.0
    return ampiezza_normalizzata ** 2


def frequenza_dominante_o_none(campioni):
    """Frequenza del picco piu' forte nello spettro, SOLO se supera
    RAPPORTO_MINIMO_DOMINANTE volte la mediana dello spettro. None se nella
    finestra non c'e' nulla che spicchi chiaramente dal rumore. Usata SOLO
    per il tempo zero iniziale (vedi intestazione) - la frequenza esatta
    ritornata non viene piu' usata per etichettare i gradini, conta solo
    "c'e' un tono o no"."""
    n = len(campioni)
    finestra = campioni.astype(np.float64) * np.hanning(n)
    spettro = np.abs(np.fft.rfft(finestra))
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    potenze = potenza_normalizzata(spettro, n)
    idx_max = int(np.argmax(potenze))
    mediana = float(np.median(potenze))
    if mediana <= 0 or potenze[idx_max] < mediana * RAPPORTO_MINIMO_DOMINANTE:
        return None
    return float(freqs[idx_max])


@dataclass
class RigaSpettrale:
    freq_hz: float
    potenza: float


@dataclass
class RisultatoGradino:
    freq_comandata: float
    freq_fondamentale: float
    potenza_fondamentale: float
    rumore_di_fondo: float = 0.0
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
    # dall'Arduino (entro una finestra di tolleranza, ora sempre quella
    # VERA: freq_comandata viene dallo schedule deterministico, non piu'
    # indovinata dal segnale - vedi intestazione del file).
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
        rumore_di_fondo=rumore_di_fondo,
        spurie=spurie,
        n_campioni=n,
    )


def stampa_risultato_gradino(risultato):
    snr = (risultato.potenza_fondamentale / risultato.rumore_di_fondo
           if risultato.rumore_di_fondo > 0 else float("inf"))
    print(
        f"\n[gradino {risultato.freq_comandata:.0f}Hz] "
        f"fondamentale reale {risultato.freq_fondamentale:.1f}Hz, "
        f"potenza {risultato.potenza_fondamentale:.6f} (SNR {snr:.0f}x) "
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
        "rumore_di_fondo": risultato.rumore_di_fondo,
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
    print(f"ANALISI SPURIE INCROCIATA - {len(risultati)} gradini analizzati")
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


def classifica_potenza(potenza, massimo):
    """Valutazione RELATIVA al gradino piu' forte visto in questo sweep, non
    un numero assoluto fisso - si auto-calibra su qualunque fibra/hardware,
    stessa logica (relativo, non assoluto) di RAPPORTO_MINIMO_DOMINANTE."""
    if massimo <= 0:
        return "N/D"
    rapporto = potenza / massimo
    if rapporto >= 0.5:
        return "OTTIMA"
    if rapporto >= 0.1:
        return "BUONA"
    if rapporto >= 0.01:
        return "DEBOLE"
    return "QUASI ASSENTE"


def rileva_collisioni_armoniche(frequenze, max_armonica=6):
    """Tra i candidati forti, segnala le coppie dove uno e' vicino a un
    multiplo intero piccolo dell'altro: su questa fibra le armoniche basse
    (soprattutto la 3a) possono essere piu' forti della fondamentale di
    un'altra frequenza - se F1 e F2 cadono in questa relazione, il
    rilevatore Goertzel di main.py rischia di confondere i due canali."""
    collisioni = []
    frequenze = sorted(set(frequenze))
    for f_base in frequenze:
        for n in range(2, max_armonica + 1):
            attesa = f_base * n
            for f_alta in frequenze:
                if f_alta <= f_base:
                    continue
                if abs(f_alta - attesa) <= TOLLERANZA_HZ:
                    collisioni.append((f_base, f_alta, n))
    return collisioni


def stampa_risultato_candidati(risultati):
    """Il blocco 'RISULTATO' pensato per essere letto direttamente a
    schermo, senza dover apire il file di log jsonl: una riga per ogni
    frequenza nota vista almeno una volta (potenza mediana tra le volte in
    cui e' stata vista, piu' robusta di una singola lettura), una
    valutazione relativa, le eventuali collisioni armoniche tra i
    candidati migliori, e un elenco finale ordinato pronto da usare per
    scegliere F1/F2."""
    if not risultati:
        print("\nNessun gradino ancora analizzato, nessun risultato da mostrare.")
        return

    per_freq = {}
    for r in risultati:
        per_freq.setdefault(r.freq_comandata, []).append(r)

    righe = []
    for freq in sorted(per_freq):
        potenze = [r.potenza_fondamentale for r in per_freq[freq]]
        righe.append((freq, float(np.median(potenze)), len(potenze)))

    massimo = max(pot for _, pot, _ in righe)

    print("\n" + "=" * 70)
    print(f"RISULTATO - {len(righe)}/{N_GRADINI} frequenze note viste almeno una volta "
          f"({len(risultati)} gradini raccolti in totale)")
    print("=" * 70)
    print(f"{'freq(Hz)':>9}  {'potenza mediana':>16}  {'valutazione':<16}{'viste'}")
    for freq, pot, n in righe:
        print(f"{freq:9.0f}  {pot:16.3e}  {classifica_potenza(pot, massimo):<16}{n}")

    mancanti = [f for f in SCHEDULE if f not in per_freq]
    if mancanti:
        print(f"\nFrequenze non ancora viste in questo sweep: {mancanti}")

    forti = [freq for freq, pot, _ in righe if classifica_potenza(pot, massimo) in ("OTTIMA", "BUONA")]
    collisioni = rileva_collisioni_armoniche(forti)
    if collisioni:
        print("\nATTENZIONE - possibili collisioni armoniche tra candidati forti:")
        for f_base, f_alta, n in collisioni:
            print(f"  {f_alta:.0f}Hz ~= {n} x {f_base:.0f}Hz -> rischio di confusione nel rilevatore Goertzel")

    migliori = sorted(
        ((freq, pot) for freq, pot, _ in righe if classifica_potenza(pot, massimo) in ("OTTIMA", "BUONA")),
        key=lambda x: -x[1],
    )
    if migliori:
        print("\nCandidati migliori per F1/F2 (forti e puliti, dal piu' al meno forte):")
        for freq, pot in migliori[:10]:
            avviso = " [vedi collisione armonica sopra]" if any(freq in (f_base, f_alta) for f_base, f_alta, _ in collisioni) else ""
            print(f"  {freq:.0f}Hz  (potenza mediana {pot:.3e}){avviso}")
    else:
        print("\nNessun candidato forte/pulito ancora - aspetta che lo sweep raccolga piu' gradini.")

    print(f"\n(dettaglio completo per gradino anche in: {FILE_LOG})")


def archivia_log_precedente():
    """Il log si scrive in append: senza archiviare quello di una sessione
    precedente, i dati vecchi si mischierebbero con quelli nuovi. Ogni
    avvio parte pulito."""
    if not FILE_LOG.exists():
        return
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    destinazione = FILE_LOG.with_name(f"{FILE_LOG.stem}_{timestamp}{FILE_LOG.suffix}")
    FILE_LOG.rename(destinazione)
    print(f"Log precedente archiviato in: {destinazione}")


def main():
    # Porta il master in modalita' sweep frequenze ciclando la Shelly
    # (arduino/master/master.ino intestazione, others/shelly_mode_selector.py) -
    # prima di questo punto, qualunque modalita' potesse girare non ci
    # interessa.
    seleziona_modalita(SMART_PLUG_HOST, MODALITA_SWEEP_FREQUENZE)

    archivia_log_precedente()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(0.5)
    bytes_attesi = CAMPIONI_PER_PACCHETTO * 2

    print(f"In ascolto UDP su porta {UDP_PORT} (rampa {SWEEPF_FREQ_MIN}-{SWEEPF_FREQ_MAX}Hz, "
          f"{N_GRADINI} gradini da {SWEEPF_STEP_DURATION_MS/1000:.0f}s)...")
    print("Ctrl+C per fermare ed avere il riepilogo finale.\n")

    risultati = []
    cicli_completati = 0
    buffer_gradino = []

    def chiudi_gradino_corrente(indice):
        nonlocal buffer_gradino
        if indice is None or not buffer_gradino:
            buffer_gradino = []
            return
        freq_comandata = freq_del_gradino(indice)
        campioni = np.concatenate(buffer_gradino)
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
        buffer_gradino = []

    # Tempo zero ANCORATO ALL'INIZIO DELL'ASCOLTO (vedi intestazione del
    # file - fix del 16/10/2026): others/shelly_mode_selector.py ha gia'
    # aspettato boot e conferma modalita' prima di ritornare, quindi il
    # gradino 0 (200Hz) e' gia' in corso o appena iniziato a questo punto -
    # non serve (e non e' affidabile) aspettare di rilevarlo sul segnale.
    t0 = time.time()
    indice_precedente = 0
    primo_segnale_rilevato = False
    buffer_rilevamento = []
    campioni_accumulati_rilevamento = 0
    candidato_presente = False
    conferme = 0

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                if not primo_segnale_rilevato and (time.time() - t0) > TIMEOUT_PRIMO_SEGNALE_S:
                    print(f"ERRORE: nessun segnale rilevato entro {TIMEOUT_PRIMO_SEGNALE_S:.0f}s - "
                          "controlla che la Shelly si sia accesa e che il master sia collegato al trasduttore.")
                    spegni_master(SMART_PLUG_HOST)
                    sock.close()
                    return
                continue
            if len(data) != bytes_attesi:
                continue

            trascorso_s = time.time() - t0
            indice = indice_gradino_corrente(trascorso_s)
            if indice != indice_precedente:
                chiudi_gradino_corrente(indice_precedente)
                if indice < indice_precedente:
                    cicli_completati += 1
                    print(f"\n>>> Ciclo {cicli_completati} della rampa completato <<<")
                    stampa_risultato_candidati(risultati)
                indice_precedente = indice

            campioni = np.frombuffer(data, dtype=np.int16)
            buffer_gradino.append(campioni)

            # Diagnostica non bloccante (vedi intestazione): segnala se il
            # primo tono rilevabile arriva sospettosamente tardi, senza
            # dipenderci per lo schedule (che e' gia' partito da t0).
            if not primo_segnale_rilevato:
                buffer_rilevamento.append(campioni)
                campioni_accumulati_rilevamento += len(campioni)
                if campioni_accumulati_rilevamento >= CAMPIONI_PER_FINESTRA_RILEVAMENTO:
                    coda = np.concatenate(buffer_rilevamento)[-CAMPIONI_PER_FINESTRA_RILEVAMENTO:]
                    buffer_rilevamento = []
                    campioni_accumulati_rilevamento = 0
                    presente = frequenza_dominante_o_none(coda) is not None
                    if presente and presente == candidato_presente:
                        conferme += 1
                    else:
                        candidato_presente = presente
                        conferme = 1 if presente else 0
                    if presente and conferme >= CONFERME_CONSECUTIVE_RICHIESTE:
                        primo_segnale_rilevato = True
                        if trascorso_s > (SWEEPF_STEP_DURATION_MS / 1000.0) * 1.5:
                            print(f"\nATTENZIONE: il primo segnale rilevabile e' apparso solo dopo "
                                  f"{trascorso_s:.1f}s dall'inizio dell'ascolto (oltre un gradino intero, "
                                  f"{SWEEPF_STEP_DURATION_MS/1000:.0f}s) - lo schedule potrebbe essere "
                                  "disallineato (il gradino iniziale e' probabilmente troppo debole su "
                                  "questa fibra). Controlla i primi risultati con occhio critico.\n")
                        else:
                            print(f"\nPrimo segnale rilevabile dopo {trascorso_s:.1f}s dall'inizio "
                                  "dell'ascolto - allineamento plausibile.\n")

    except KeyboardInterrupt:
        print("\n\nInterrotto dall'utente, chiudo l'ultimo gradino in corso...")
        chiudi_gradino_corrente(indice_precedente)
    finally:
        spegni_master(SMART_PLUG_HOST)
        sock.close()

    analisi_finale(risultati)
    stampa_risultato_candidati(risultati)


if __name__ == "__main__":
    main()
