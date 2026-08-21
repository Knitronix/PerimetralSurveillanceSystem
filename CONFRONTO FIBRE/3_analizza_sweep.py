"""
Analizza le registrazioni di uno sweep in frequenza per costruire una curva
di risposta in frequenza per ciascuna fibra ottica, e la incrocia con il
rumore di fondo della stessa fibra per ottenere un SNR per banda di
frequenza (non un singolo numero come per gli eventi brevi, ma "quanto
sente bene ogni fibra a ciascuna frequenza").

Si aspetta la stessa struttura di 2_confronta_fibre.py:

    dataset_sensori_24kHz/<id_fibra>/<classe_sweep>/campione_*.wav
    dataset_sensori_24kHz/<id_fibra>/RUMORE_AMBIENTALE/campione_*.wav

REQUISITI SUL PROTOCOLLO DI REGISTRAZIONE:
- Registra lo sweep con la modalità "durata fissa senza soglia" del
  registratore (checkbox "Attendi soglia" disattivata), premendo Avvia
  Registrazione ESATTAMENTE quando parte la riproduzione dello sweep: questo
  script assume che l'inizio del file corrisponda a t=0 dello sweep.
  Un errore di qualche centinaio di ms nel trigger manuale è normale e
  previsto: per questo esiste rileva_offset_sweep(), che ritrova l'esatto
  inizio via cross-correlazione. Non serve (e non è possibile) premere REC
  in perfetta sincronia.
- Configura qui sotto F_MIN_SWEEP, F_MAX_SWEEP, DURATA_SWEEP_S e TIPO_SWEEP
  in modo che corrispondano ESATTAMENTE al file audio che riproduci. Se non
  corrispondono, la mappatura tempo->frequenza è sbagliata e il risultato
  non ha senso.

Uso:
    python 3_analizza_sweep.py

Output:
    - tabella SNR per banda di frequenza, per fibra
    - verdetto con copertura utile e fibra migliore
    - confronto_sweep.csv con il dettaglio banda per banda

MODIFICA vs versione precedente:
    La confidenza di allineamento era calcolata come picco di correlazione
    diviso la MEDIANA della correlazione sulla finestra di ricerca. Questo
    rapporto non è normalizzato per l'energia del segnale: se una
    registrazione ha lunghi tratti di silenzio attorno allo sweep, la
    mediana crolla verso zero e il rapporto esplode (valori a 3 cifre),
    anche se l'allineamento non è affatto migliore in senso assoluto - è un
    artefatto della metrica, non un allineamento più pulito. Corretto usando
    una cross-correlazione normalizzata (coefficiente stile Pearson, range
    0-1 teorico), confrontabile realmente tra registrazioni e fibre diverse
    indipendentemente da quanto silenzio le circonda.
"""

import os
import glob
import wave
import csv
import warnings
import numpy as np

CARTELLA_DATASET = "dataset_sensori_24kHz"
CLASSE_SWEEP = "SWEEP"                    # deve esistere in config_soglie.json
CLASSE_RUMORE_DI_FONDO = "RUMORE_AMBIENTALE"
FILE_CSV_OUTPUT = "confronto_sweep.csv"

# --- PARAMETRI DELLO SWEEP: DEVONO CORRISPONDERE AL TUO FILE AUDIO ---
F_MIN_SWEEP = 20.0        # Hz, frequenza di partenza dello sweep
F_MAX_SWEEP = 6000.0      # Hz, frequenza finale dello sweep
DURATA_SWEEP_S = 12.0     # secondi, durata dello sweep (non della registrazione)
TIPO_SWEEP = "log"        # "log" (chirp logaritmico, il più comune) o "lineare"

# --- PARAMETRI DI ANALISI ---
NPERSEG_ANALISI = 1024    # campioni per finestra FFT (~43ms a 24kHz)
HOP_ANALISI = 256         # avanzamento tra una finestra e la successiva
N_BANDE = 20              # bande log-spaziate tra F_MIN_SWEEP e F_MAX_SWEEP
SNR_MINIMO_UTILE_DB = 6.0  # sotto questa soglia una banda è considerata "non coperta"
STD_SOSPETTA_DB = 3.0     # sopra questa deviazione standard tra le registrazioni, la banda è da verificare
SOGLIA_CLIPPING_CAMPIONE = 32760  # vicino al fondo scala int16 (32767)
EPS_DBFS = 1e-9

# NOTA: qui sotto ci sono DUE metriche distinte per l'allineamento, che
# rispondono a domande diverse:
#
# 1) "confidenza" (0-1 teorico): quanto la FORMA d'onda della registrazione
#    somiglia punto-per-punto al riferimento sintetico. Il riferimento ha
#    ampiezza costante (è un seno ideale); la registrazione reale no, perché
#    la risposta in frequenza del sistema e il rumore la modulano nel tempo.
#    Per questo motivo la confidenza resta STRUTTURALMENTE bassa (es. 0.02-0.15)
#    anche per un allineamento corretto: non esiste una soglia assoluta
#    universale sopra cui "è sicuramente giusto". Va letta in modo relativo,
#    confrontando fibre/registrazioni tra loro, non contro un numero fisso.
#
# 2) "significativita" (z-score, tipicamente utile da ~3-5 in su): quanto il
#    picco di correlazione si distingue dal rumore di fondo della funzione di
#    correlazione stessa, escludendo apposta l'intorno del picco dal calcolo
#    di quel rumore di fondo. Risponde alla vera domanda "l'offset trovato è
#    un rilevamento affidabile o potrebbe essere casuale?", indipendentemente
#    da quanto è alta la confidenza assoluta. Questa è la metrica giusta su
#    cui basare un avviso di "allineamento incerto".
SIGNIFICATIVITA_MINIMA_AFFIDABILE = 5.0
FINESTRA_ESCLUSIONE_PICCO_S = 0.05  # 50ms attorno al picco esclusi dal calcolo del rumore di fondo


def leggi_wav_come_array(percorso):
    with wave.open(percorso, "rb") as wf:
        n_campioni = wf.getnframes()
        sample_rate = wf.getframerate()
        dati = wf.readframes(n_campioni)
    campioni = np.frombuffer(dati, dtype=np.int16).astype(np.float64)
    return campioni, sample_rate


def frequenza_istantanea(t):
    """Legge di sweep: frequenza attesa al tempo t (secondi dall'inizio)."""
    frazione = t / DURATA_SWEEP_S
    if TIPO_SWEEP == "log":
        return F_MIN_SWEEP * (F_MAX_SWEEP / F_MIN_SWEEP) ** frazione
    return F_MIN_SWEEP + (F_MAX_SWEEP - F_MIN_SWEEP) * frazione


def sintetizza_sweep_riferimento(sample_rate):
    """Ricrea localmente la stessa forma d'onda dello sweep riprodotto,
    usando gli stessi parametri configurati sopra. Serve come 'impronta'
    per ritrovare l'esatto istante di inizio dentro la registrazione."""
    n = int(round(DURATA_SWEEP_S * sample_rate))
    t = np.arange(n) / sample_rate
    if TIPO_SWEEP == "log":
        k = (F_MAX_SWEEP / F_MIN_SWEEP) ** (1.0 / DURATA_SWEEP_S)
        fase = 2 * np.pi * F_MIN_SWEEP * (k ** t - 1) / np.log(k)
    else:
        fase = 2 * np.pi * (F_MIN_SWEEP * t + (F_MAX_SWEEP - F_MIN_SWEEP) * t ** 2 / (2 * DURATA_SWEEP_S))
    return np.sin(fase)


def rileva_offset_sweep(campioni_registrati, sample_rate):
    """Trova il punto della registrazione in cui lo sweep inizia davvero,
    tramite cross-correlazione con una replica sintetica. Compensa il fatto
    che premere 'Avvia Registrazione' in perfetta sincronia con l'audio è
    umanamente impossibile al millisecondo - anche 200-300ms di ritardo, su
    uno sweep di pochi secondi, bastano a rovinare la mappatura tempo->banda
    se non corretti. Questo è esattamente il comportamento atteso di un
    trigger manuale, non un difetto da correggere a monte: per questo la
    correzione va fatta qui, via software.

    Restituisce (offset_in_campioni, confidenza, significativita). Vedi il
    commento sopra SIGNIFICATIVITA_MINIMA_AFFIDABILE per la differenza tra le
    due metriche: la confidenza misura la somiglianza di forma (bassa per
    natura su segnali reali), la significativita misura se il picco trovato
    è statisticamente distinguibile dal rumore della correlazione stessa
    (quella giusta per decidere se fidarsi dell'offset).
    """
    riferimento = sintetizza_sweep_riferimento(sample_rate)
    n_rif = len(riferimento)
    n_reg = len(campioni_registrati)
    margine_campioni = n_reg - n_rif

    if margine_campioni <= 0:
        # Nessun margine per cercare l'offset: la registrazione dura quanto
        # (o meno di) lo sweep di riferimento, quindi non c'è nulla su cui
        # far scorrere la correlazione. Va distinto da una vera bassa
        # confidenza: qui il rilevamento non è stato proprio possibile.
        return 0, -1.0, float("nan")

    x = campioni_registrati.astype(np.float64)
    x = x - np.mean(x)
    y = riferimento - np.mean(riferimento)
    norma_y = np.sqrt(np.sum(y ** 2))

    n_fft = 1
    while n_fft < n_reg + n_rif:
        n_fft *= 2
    X = np.fft.rfft(x, n_fft)
    Y = np.fft.rfft(y, n_fft)
    corr = np.fft.irfft(X * np.conj(Y), n_fft)
    corr_valida = corr[: margine_campioni + 1]

    # Energia locale di x in una finestra scorrevole lunga quanto il
    # riferimento, calcolata con somma cumulativa per restare O(n) invece di
    # ricalcolare la somma da zero ad ogni offset.
    x_quadro_cum = np.concatenate(([0.0], np.cumsum(x ** 2)))
    energia_locale = (
        x_quadro_cum[n_rif: n_rif + margine_campioni + 1]
        - x_quadro_cum[0: margine_campioni + 1]
    )
    norma_locale = np.sqrt(np.maximum(energia_locale, 1e-12))

    # Normalizzando per l'energia locale di x e per l'energia di y otteniamo
    # un vero coefficiente di correlazione (indipendente dal volume assoluto
    # della registrazione e da quanto silenzio la circonda) invece del
    # rapporto picco/mediana della versione precedente, che si gonfiava
    # artificialmente nelle registrazioni con tratti di silenzio lunghi.
    corr_normalizzata = corr_valida / (norma_locale * norma_y + 1e-12)

    offset = int(np.argmax(np.abs(corr_normalizzata)))
    confidenza = float(np.abs(corr_normalizzata[offset]))

    # Significatività statistica del picco: quanto si distingue dal "rumore
    # di fondo" della funzione di correlazione stessa, ESCLUDENDO apposta una
    # finestra attorno al picco dal calcolo di quel rumore (altrimenti il
    # picco stesso alzerebbe la stima del rumore e si autoinquinerebbe).
    # Usiamo mediana e MAD (invece di media/deviazione standard classiche)
    # perché sono robuste: pochi altri picchi secondari nella funzione di
    # correlazione non falsano la stima del rumore di fondo tipico.
    ampiezza_corr = np.abs(corr_normalizzata)
    finestra_esclusione = int(FINESTRA_ESCLUSIONE_PICCO_S * sample_rate)
    maschera = np.ones(len(ampiezza_corr), dtype=bool)
    lo = max(0, offset - finestra_esclusione)
    hi = min(len(ampiezza_corr), offset + finestra_esclusione + 1)
    maschera[lo:hi] = False
    fondo = ampiezza_corr[maschera]
    if len(fondo) > 10:
        mediana_fondo = np.median(fondo)
        mad_fondo = np.median(np.abs(fondo - mediana_fondo)) * 1.4826 + 1e-12
        significativita = float((confidenza - mediana_fondo) / mad_fondo)
    else:
        significativita = float("nan")

    return offset, confidenza, significativita


def bordi_bande():
    return np.geomspace(F_MIN_SWEEP, F_MAX_SWEEP, N_BANDE + 1)


def etichette_bande(bordi):
    etichette = []
    for i in range(len(bordi) - 1):
        etichette.append(f"{bordi[i]:.0f}-{bordi[i+1]:.0f}Hz")
    return etichette


def risposta_sweep_per_banda(campioni, sample_rate):
    """Segue lo sweep nel tempo, estraendo ad ogni finestra l'ampiezza (dB)
    alla frequenza attesa in quell'istante, e aggrega il risultato nelle
    bande di frequenza. Restituisce un array (N_BANDE,) di dB medi, con NaN
    dove non ci sono dati (fuori dalla durata attesa dello sweep).

    NOTA SULLO SMEARING SPETTRALE: in uno sweep log la frequenza istantanea
    si muove più velocemente (in Hz/s) quanto più è alta la frequenza
    stessa. Durante una singola finestra di analisi, ad alta frequenza lo
    sweep può attraversare diversi bin FFT (con questi parametri, ~5 bin a
    6000Hz contro <1 bin a 500Hz) - prendere un solo bin sottostimerebbe
    sistematicamente l'energia proprio nella zona alta dello sweep. Per
    questo integriamo l'energia su una finestra di bin proporzionata alla
    velocità istantanea dello sweep in quel punto, non su un bin fisso."""
    finestra = np.hanning(NPERSEG_ANALISI)
    bordi = bordi_bande()
    somma_db = np.zeros(N_BANDE)
    conteggio = np.zeros(N_BANDE)
    risoluzione_bin_hz = sample_rate / NPERSEG_ANALISI
    durata_finestra_s = NPERSEG_ANALISI / sample_rate

    n_frame = 1 + (len(campioni) - NPERSEG_ANALISI) // HOP_ANALISI
    if n_frame < 1:
        return np.full(N_BANDE, np.nan)

    for i in range(n_frame):
        inizio = i * HOP_ANALISI
        t_centro = (inizio + NPERSEG_ANALISI / 2) / sample_rate
        if t_centro > DURATA_SWEEP_S:
            break
        f_atteso = frequenza_istantanea(t_centro)
        if not (F_MIN_SWEEP <= f_atteso <= min(F_MAX_SWEEP, sample_rate / 2)):
            continue

        # Velocità istantanea dello sweep in quel punto (Hz/s), e quanti bin
        # FFT attraversa durante la durata di questa finestra di analisi.
        if TIPO_SWEEP == "log":
            velocita_hz_s = f_atteso * np.log(F_MAX_SWEEP / F_MIN_SWEEP) / DURATA_SWEEP_S
        else:
            velocita_hz_s = (F_MAX_SWEEP - F_MIN_SWEEP) / DURATA_SWEEP_S
        deriva_hz = velocita_hz_s * durata_finestra_s
        # +1 bin di margine per il lobo principale della finestra di Hann
        # (che "sporca" comunque 1-2 bin anche per un tono perfettamente fermo)
        mezza_larghezza = max(1, int(np.ceil(deriva_hz / risoluzione_bin_hz / 2)) + 1)

        segmento = campioni[inizio: inizio + NPERSEG_ANALISI] * finestra
        spettro = np.abs(np.fft.rfft(segmento))
        bin_centrale = int(round(f_atteso * NPERSEG_ANALISI / sample_rate))
        bin_min = max(0, bin_centrale - mezza_larghezza)
        bin_max = min(len(spettro) - 1, bin_centrale + mezza_larghezza)
        if bin_min > bin_max:
            continue

        # Sommiamo la POTENZA (quadrato dell'ampiezza) sui bin coinvolti -
        # è il modo fisicamente corretto di aggregare energia distribuita su
        # più bin - poi torniamo a un'ampiezza equivalente con la radice.
        energia = np.sum(spettro[bin_min: bin_max + 1] ** 2)
        ampiezza_equivalente = np.sqrt(energia) / NPERSEG_ANALISI
        db = 20.0 * np.log10(ampiezza_equivalente / 32768.0 + EPS_DBFS)

        banda_idx = int(np.clip(np.searchsorted(bordi, f_atteso) - 1, 0, N_BANDE - 1))
        somma_db[banda_idx] += db
        conteggio[banda_idx] += 1

    with np.errstate(invalid="ignore"):
        return np.where(conteggio > 0, somma_db / np.maximum(conteggio, 1), np.nan)


def spettro_medio_rumore_per_banda(campioni, sample_rate):
    """Spettro medio (stile Welch, senza overlap per semplicità) del rumore
    ambientale, aggregato nelle stesse bande di frequenza dello sweep, per
    poterlo usare come riferimento di rumore banda per banda."""
    finestra = np.hanning(NPERSEG_ANALISI)
    n_frame = len(campioni) // NPERSEG_ANALISI
    if n_frame < 1:
        return np.full(N_BANDE, np.nan)

    accumulo = np.zeros(NPERSEG_ANALISI // 2 + 1)
    for i in range(n_frame):
        segmento = campioni[i * NPERSEG_ANALISI:(i + 1) * NPERSEG_ANALISI] * finestra
        accumulo += np.abs(np.fft.rfft(segmento))
    accumulo /= n_frame

    freq_bin = np.fft.rfftfreq(NPERSEG_ANALISI, d=1.0 / sample_rate)
    db_bin = 20.0 * np.log10(accumulo / NPERSEG_ANALISI / 32768.0 + EPS_DBFS)

    bordi = bordi_bande()
    bande_db = np.full(N_BANDE, np.nan)
    for i in range(N_BANDE):
        mask = (freq_bin >= bordi[i]) & (freq_bin < bordi[i + 1])
        if np.any(mask):
            bande_db[i] = np.mean(db_bin[mask])
    return bande_db


def scansiona_sweep_e_rumore():
    """Restituisce { id_fibra: {"sweep": [array_bande, ...], "rumore": [array_bande, ...]} }"""
    risultati = {}
    if not os.path.isdir(CARTELLA_DATASET):
        print(f"Cartella '{CARTELLA_DATASET}' non trovata. Esegui prima il registratore.")
        return risultati

    for id_fibra in sorted(os.listdir(CARTELLA_DATASET)):
        percorso_fibra = os.path.join(CARTELLA_DATASET, id_fibra)
        if not os.path.isdir(percorso_fibra):
            continue

        percorso_sweep = os.path.join(percorso_fibra, CLASSE_SWEEP)
        percorso_rumore = os.path.join(percorso_fibra, CLASSE_RUMORE_DI_FONDO)

        sweep_bande = []
        n_escluse_clip = 0
        if os.path.isdir(percorso_sweep):
            for f in sorted(glob.glob(os.path.join(percorso_sweep, "*.wav"))):
                try:
                    campioni, sr = leggi_wav_come_array(f)
                    durata_file = len(campioni) / sr
                    if durata_file < DURATA_SWEEP_S * 0.9:
                        print(
                            f"  ⚠ {id_fibra}/{CLASSE_SWEEP}/{os.path.basename(f)}: "
                            f"dura solo {durata_file:.1f}s, attesi {DURATA_SWEEP_S}s - "
                            f"probabile cattura incompleta, la ignoro"
                        )
                        continue

                    offset, confidenza, significativita = rileva_offset_sweep(campioni, sr)

                    if confidenza < 0:
                        print(
                            f"  ⚠ {id_fibra}/{CLASSE_SWEEP}/{os.path.basename(f)}: "
                            f"registrazione troppo corta rispetto allo sweep configurato "
                            f"({durata_file:.2f}s vs {DURATA_SWEEP_S:.2f}s attesi) - nessun "
                            f"margine per cercare l'allineamento. Ripeti la registrazione "
                            f"impostando 'Durata cattura' ad almeno "
                            f"{DURATA_SWEEP_S + 1.5:.1f}s nel registratore. La ignoro."
                        )
                        continue

                    offset_ms = 1000.0 * offset / sr
                    campioni_allineati = campioni[offset:]

                    clipping_pct = 100.0 * np.count_nonzero(
                        np.abs(campioni_allineati[: int(DURATA_SWEEP_S * sr)]) >= SOGLIA_CLIPPING_CAMPIONE
                    ) / max(1, min(len(campioni_allineati), int(DURATA_SWEEP_S * sr)))

                    if clipping_pct > 0:
                        print(
                            f"  ❌ {id_fibra}/{CLASSE_SWEEP}/{os.path.basename(f)}: "
                            f"SATURA ({clipping_pct:.2f}% campioni a fondo scala) - "
                            f"picco reale sconosciuto, la escludo dal confronto. "
                            f"Riduci il guadagno e ripeti questa registrazione."
                        )
                        n_escluse_clip += 1
                        continue

                    if np.isnan(significativita):
                        avviso_confidenza = "  ⚠ significatività non calcolabile (finestra troppo corta)"
                    elif significativita < SIGNIFICATIVITA_MINIMA_AFFIDABILE:
                        avviso_confidenza = f"  ⚠ allineamento incerto (significatività {significativita:.1f})"
                    else:
                        avviso_confidenza = ""
                    print(
                        f"  {id_fibra}/{CLASSE_SWEEP}/{os.path.basename(f)}: "
                        f"inizio {offset_ms:.0f}ms, confidenza {confidenza:.3f}, "
                        f"significatività {significativita:.1f}, "
                        f"clip {clipping_pct:.2f}%{avviso_confidenza}"
                    )

                    sweep_bande.append({
                        "file": os.path.basename(f),
                        "bande": risposta_sweep_per_banda(campioni_allineati, sr),
                        "confidenza": confidenza,
                        "significativita": significativita,
                        "offset_ms": offset_ms,
                        "clipping_pct": clipping_pct,
                    })
                except Exception as e:
                    print(f"  ! Errore leggendo {f}: {e}")

        rumore_bande = []
        if os.path.isdir(percorso_rumore):
            for f in sorted(glob.glob(os.path.join(percorso_rumore, "*.wav"))):
                try:
                    campioni, sr = leggi_wav_come_array(f)
                    rumore_bande.append(spettro_medio_rumore_per_banda(campioni, sr))
                except Exception as e:
                    print(f"  ! Errore leggendo {f}: {e}")

        if sweep_bande or rumore_bande:
            risultati[id_fibra] = {
                "sweep": sweep_bande,
                "rumore": rumore_bande,
                "escluse_clip": n_escluse_clip,
            }

    return risultati


def stampa_report(risultati):
    bordi = bordi_bande()
    etichette = etichette_bande(bordi)

    fibre_con_sweep = {
        id_fibra: d for id_fibra, d in risultati.items() if d["sweep"] and d["rumore"]
    }
    if not fibre_con_sweep:
        print(f"\nNessuna fibra ha sia '{CLASSE_SWEEP}' che '{CLASSE_RUMORE_DI_FONDO}' registrati.")
        print("Servono entrambi per calcolare l'SNR per banda di frequenza.")
        return

    snr_per_fibra = {}
    std_per_fibra = {}
    risposta_media_per_fibra = {}
    rumore_medio_per_fibra = {}
    n_prese_per_fibra = {}
    outlier_per_fibra = {}  # id_fibra -> {banda_idx: nome_file_più_anomalo}
    riepilogo_per_fibra = {}  # id_fibra -> statistiche aggregate (confidenza, verdetto, ecc.)

    for id_fibra, d in fibre_con_sweep.items():
        prese_sweep = d["sweep"]  # lista di {"file":..., "bande": array, "confidenza":..., "offset_ms":..., "clipping_pct":...}
        matrice_sweep = np.array([p["bande"] for p in prese_sweep])  # (n_take, N_BANDE)
        nomi_file = [p["file"] for p in prese_sweep]
        matrice_rumore = np.array(d["rumore"])  # (n_take_rumore, N_BANDE)
        n_prese = matrice_sweep.shape[0]
        n_prese_per_fibra[id_fibra] = n_prese

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            # MEDIANA invece di media: alcune fibre (es. con accoppiamento
            # meccanico incostante) possono avere metà delle registrazioni
            # forti e metà deboli, senza un gruppo isolato "chiaramente
            # rotto" da poter scartare con un criterio oggettivo (vedi
            # fibra1/fibra5). In quei casi non c'è un modo giustificabile di
            # decidere quali registrazioni buttare - la mediana evita il
            # problema: ignora automaticamente i valori estremi, in
            # entrambe le direzioni, senza bisogno di scegliere a priori
            # quale metà è "vera".
            risposta_media = np.nanmedian(matrice_sweep, axis=0)
            rumore_medio = np.nanmedian(matrice_rumore, axis=0)
            risposta_std = np.nanstd(matrice_sweep, axis=0) if n_prese > 1 else np.full(N_BANDE, np.nan)
        snr_bande = risposta_media - rumore_medio
        snr_per_fibra[id_fibra] = snr_bande
        std_per_fibra[id_fibra] = risposta_std
        risposta_media_per_fibra[id_fibra] = risposta_media
        rumore_medio_per_fibra[id_fibra] = rumore_medio

        # Per ogni banda poco ripetibile, troviamo QUALE registrazione si discosta
        # di più dalla mediana: è il file da controllare per primo.
        outlier_per_fibra[id_fibra] = {}
        if n_prese > 1:
            mediana_bande = np.nanmedian(matrice_sweep, axis=0)
            for i in range(N_BANDE):
                if np.isnan(risposta_std[i]) or risposta_std[i] < STD_SOSPETTA_DB:
                    continue
                scostamenti = np.abs(matrice_sweep[:, i] - mediana_bande[i])
                if np.all(np.isnan(scostamenti)):
                    continue
                idx_peggiore = int(np.nanargmax(scostamenti))
                outlier_per_fibra[id_fibra][i] = nomi_file[idx_peggiore]

        # Statistiche di confidenza/offset/clipping (usate nel riepilogo e nel CSV).
        # La dispersione degli offset (std_offset_ms) è un secondo indicatore
        # indipendente di quanto è stabile l'allineamento per questa fibra:
        # se gli offset rilevati ballano molto tra una presa e l'altra a parità
        # di protocollo di registrazione, è un segnale che vale la pena
        # controllare quelle prese singolarmente, oltre alla confidenza.
        confidenze = [p["confidenza"] for p in prese_sweep]
        significativita_lista = [p["significativita"] for p in prese_sweep if not np.isnan(p["significativita"])]
        offset_ms_lista = [p["offset_ms"] for p in prese_sweep]
        n_escluse = d["escluse_clip"]

        # Statistiche di verdetto (copertura, SNR medio/minimo, banda debole),
        # calcolate qui una volta sola e riusate sia per la stampa sia per il CSV
        valide = snr_bande[~np.isnan(snr_bande)]
        if len(valide) > 0:
            copertura_pct = 100.0 * np.sum(valide >= SNR_MINIMO_UTILE_DB) / N_BANDE
            snr_medio = float(np.mean(valide))
            snr_minimo = float(np.min(valide))
            banda_debole = etichette[int(np.nanargmin(snr_bande))]
        else:
            copertura_pct = snr_medio = snr_minimo = float("nan")
            banda_debole = "n/d"

        riepilogo_per_fibra[id_fibra] = {
            "confidenza_min": min(confidenze) if confidenze else float("nan"),
            "confidenza_media": float(np.mean(confidenze)) if confidenze else float("nan"),
            "confidenza_max": max(confidenze) if confidenze else float("nan"),
            "significativita_min": min(significativita_lista) if significativita_lista else float("nan"),
            "significativita_media": float(np.mean(significativita_lista)) if significativita_lista else float("nan"),
            "n_allineamenti_incerti": int(sum(1 for s in significativita_lista if s < SIGNIFICATIVITA_MINIMA_AFFIDABILE)),
            "std_offset_ms": float(np.std(offset_ms_lista)) if len(offset_ms_lista) > 1 else float("nan"),
            "n_escluse_clip": n_escluse,
            "copertura_pct": copertura_pct,
            "snr_medio": snr_medio,
            "snr_minimo": snr_minimo,
            "banda_debole": banda_debole,
        }

    # --- TABELLA RIEPILOGATIVA PER FIBRA: registrazioni, esclusioni, confidenza ---
    print("=" * 100)
    print("RIEPILOGO PER FIBRA")
    print("=" * 100)
    print(
        f"{'Fibra':<14} {'N reg.':>7} {'Escluse':>8}   "
        f"{'Confidenza (min/media/max)':<28} {'Signif. (min/media)':<20} {'Incerti':>8} {'Std offset(ms)':>15}"
    )
    print("-" * 100)
    for id_fibra, d in fibre_con_sweep.items():
        r = riepilogo_per_fibra[id_fibra]
        if not np.isnan(r["confidenza_media"]):
            conf_txt = f"{r['confidenza_min']:.3f}/{r['confidenza_media']:.3f}/{r['confidenza_max']:.3f}"
        else:
            conf_txt = "n/d"
        if not np.isnan(r["significativita_media"]):
            sig_txt = f"{r['significativita_min']:.1f}/{r['significativita_media']:.1f}"
        else:
            sig_txt = "n/d"
        incerti_txt = str(r["n_allineamenti_incerti"])
        std_off_txt = f"{r['std_offset_ms']:.0f}" if not np.isnan(r["std_offset_ms"]) else "n/d"
        n_escluse = r["n_escluse_clip"]
        escluse_txt = str(n_escluse) if n_escluse == 0 else f"⚠{n_escluse}"
        print(
            f"{id_fibra:<14} {n_prese_per_fibra[id_fibra]:>7} {escluse_txt:>8}   "
            f"{conf_txt:<28} {sig_txt:<20} {incerti_txt:>8} {std_off_txt:>15}"
        )
    print("-" * 100)
    print("Confidenza: coefficiente di correlazione normalizzata (0-1), somiglianza di FORMA tra la")
    print("registrazione e lo sweep di riferimento. È strutturalmente bassa anche per allineamenti")
    print("corretti (il riferimento sintetico ha ampiezza costante, la registrazione reale no) - va")
    print("letta in modo RELATIVO tra fibre/registrazioni, non contro una soglia assoluta.")
    print("Significatività: z-score robusto del picco di correlazione rispetto al rumore di fondo")
    print(f"della correlazione stessa. Sotto {SIGNIFICATIVITA_MINIMA_AFFIDABILE:.0f} l'offset trovato è statisticamente incerto:")
    print("è QUESTA la metrica su cui fidarsi per giudicare l'allineamento, non la confidenza assoluta.")
    print("'Incerti': quante registrazioni di questa fibra hanno significatività sotto soglia.")
    print("Std offset: quanto variano nel tempo (in ms) gli inizi rilevati tra le registrazioni della")
    print("stessa fibra. Un valore alto indica prese poco confrontabili tra loro, da controllare.")
    print("'Escluse': registrazioni scartate perché sature (clipping>0), non incluse in nessun calcolo sotto.")


    intestazione = f"{'Banda':<14}" + "".join(f"{fibra:>16}" for fibra in snr_per_fibra)
    print("\n" + "=" * len(intestazione))
    print(f"RISPOSTA IN FREQUENZA - SNR ± deviazione standard tra le registrazioni (dB), sweep {TIPO_SWEEP} "
          f"{F_MIN_SWEEP:.0f}-{F_MAX_SWEEP:.0f}Hz")
    print("=" * len(intestazione))
    print(intestazione)
    print("-" * len(intestazione))
    for i, etichetta in enumerate(etichette):
        riga = f"{etichetta:<14}"
        for fibra, snr_bande in snr_per_fibra.items():
            valore = snr_bande[i]
            std_val = std_per_fibra[fibra][i]
            if np.isnan(valore):
                testo = "n/d"
            elif n_prese_per_fibra[fibra] <= 1:
                testo = f"{valore:+.1f}(1 registrazione)"
            elif np.isnan(std_val):
                testo = f"{valore:+.1f}"
            else:
                testo = f"{valore:+.1f}±{std_val:.1f}"
                if std_val >= STD_SOSPETTA_DB:
                    testo += "⚠"
            riga += f"{testo:>16}"
        print(riga)
    print("=" * len(intestazione))
    print(f"Formato: SNR±deviazione_standard. ⚠ = deviazione standard ≥{STD_SOSPETTA_DB:.0f}dB tra le")
    print("registrazioni in quella banda: vedi 'RIPETIBILITÀ' più sotto per scoprire quale registrazione la causa.")

    print("\nNote:")
    print("- SNR per banda = risposta media allo sweep meno rumore ambientale medio,")
    print("  entrambi nella stessa banda di frequenza, per la stessa fibra.")
    print(f"- Una banda con SNR < {SNR_MINIMO_UTILE_DB:.0f}dB è considerata 'non coperta':")
    print("  il segnale in quella banda è troppo vicino al rumore per essere affidabile.")
    print("- 'n/d' = nessun dato per quella banda (lo sweep potrebbe non averla attraversata,")
    print("  o il rumore ambientale non copre quella durata/banda).")

    print("\n" + "=" * len(intestazione))
    print("RIPETIBILITÀ (deviazione standard della risposta tra le registrazioni, dB)")
    print("=" * len(intestazione))
    for id_fibra in snr_per_fibra:
        n_prese = n_prese_per_fibra[id_fibra]
        if n_prese <= 1:
            print(f"\n{id_fibra}: solo {n_prese} registrazione disponibile - ripetibilità non verificabile.")
            print("  Registra almeno 2-3 registrazioni per poter distinguere un valore vero da un caso isolato.")
            continue

        print(f"\n{id_fibra} ({n_prese} registrazioni):")
        std_bande = std_per_fibra[id_fibra]
        problemi = outlier_per_fibra[id_fibra]
        if not problemi:
            print(f"  Tutte le bande hanno std < {STD_SOSPETTA_DB:.0f}dB tra le registrazioni: risultati coerenti.")
        else:
            for banda_idx, nome_file in problemi.items():
                print(
                    f"  ⚠ {etichette[banda_idx]}: std={std_bande[banda_idx]:.1f}dB tra le registrazioni - "
                    f"registrazione più anomala: {nome_file} (controllala singolarmente prima di "
                    f"fidarti del valore medio in questa banda)"
                )
    print("=" * len(intestazione))
    print("\nCome leggere questa sezione: se una banda ha std alta, il valore medio nella")
    print("tabella sopra potrebbe essere dominato da una sola registrazione anomala (rumore esterno")
    print("casuale capitato durante quella registrazione, o un allineamento sbagliato - controlla anche")
    print("la confidenza e l'offset di quel file specifico nel log di scansione) invece che da una vera")
    print("caratteristica della fibra (es. una risonanza reale). Riascolta o ricontrolla il file segnalato:")
    print("se anche le altre registrazioni mostrano un valore simile, il picco/calo è probabilmente reale.")

    print("\n" + "=" * len(intestazione))
    print("VERDETTO")
    print("=" * len(intestazione))
    classifica = []
    for fibra in snr_per_fibra:
        r = riepilogo_per_fibra[fibra]
        if np.isnan(r["snr_medio"]):
            continue
        classifica.append((fibra, r["copertura_pct"], r["snr_medio"], r["snr_minimo"], r["banda_debole"]))

    # Ordiniamo prima per copertura (quante bande sono effettivamente
    # utilizzabili), poi per SNR medio come spareggio: una fibra che copre
    # più banda è preferibile a una con SNR di picco più alto ma bucata.
    classifica.sort(key=lambda r: (r[1], r[2]), reverse=True)

    for pos, (fibra, copertura, snr_medio, snr_minimo, banda_debole) in enumerate(classifica, start=1):
        corona = "  ⭐ MIGLIORE" if pos == 1 else ""
        print(
            f"  {pos}. {fibra}: copertura utile {copertura:.0f}% delle bande, "
            f"SNR medio {snr_medio:+.1f}dB, punto più debole {banda_debole} "
            f"({snr_minimo:+.1f}dB){corona}"
        )
    print("=" * len(intestazione))
    print("\nCriterio: prima la fibra che copre più banda utile (SNR >= "
          f"{SNR_MINIMO_UTILE_DB:.0f}dB), poi a parità di copertura quella con SNR medio più alto.")
    print("Una fibra 'a picco alto ma stretto' può perdere contro una più uniforme:")
    print("se ti serve solo una banda specifica, guarda la tabella riga per riga invece del verdetto.")

    # --- CSV COMPLETO: una riga per banda/fibra, con TUTTO quello che calcoliamo ---
    # Colonne per-banda (variano riga per riga) + colonne per-fibra (ripetute
    # su ogni riga della stessa fibra - comodo per pivot/filtri in Excel).
    righe_csv = []
    for id_fibra in snr_per_fibra:
        r = riepilogo_per_fibra[id_fibra]
        snr_bande = snr_per_fibra[id_fibra]
        std_bande = std_per_fibra[id_fibra]
        risposta_bande = risposta_media_per_fibra[id_fibra]
        rumore_bande = rumore_medio_per_fibra[id_fibra]
        outlier_bande = outlier_per_fibra[id_fibra]

        for i, etichetta in enumerate(etichette):
            std_val = std_bande[i]
            righe_csv.append({
                # --- per banda ---
                "id_fibra": id_fibra,
                "banda_hz": etichetta,
                "banda_min_hz": bordi[i],
                "banda_max_hz": bordi[i + 1],
                "risposta_dbfs": risposta_bande[i],
                "rumore_dbfs": rumore_bande[i],
                "snr_db": snr_bande[i],
                "banda_coperta": bool(not np.isnan(snr_bande[i]) and snr_bande[i] >= SNR_MINIMO_UTILE_DB),
                "risposta_std_db": std_val,
                "std_sospetta": bool(not np.isnan(std_val) and std_val >= STD_SOSPETTA_DB),
                "registrazione_sospetta": outlier_bande.get(i, ""),
                # --- per fibra (ripetuti su ogni riga) ---
                "n_registrazioni_sweep": n_prese_per_fibra[id_fibra],
                "n_registrazioni_escluse_clip": r["n_escluse_clip"],
                "confidenza_allineamento_min": r["confidenza_min"],
                "confidenza_allineamento_media": r["confidenza_media"],
                "confidenza_allineamento_max": r["confidenza_max"],
                "significativita_min_fibra": r["significativita_min"],
                "significativita_media_fibra": r["significativita_media"],
                "n_allineamenti_incerti_fibra": r["n_allineamenti_incerti"],
                "std_offset_ms_fibra": r["std_offset_ms"],
                "copertura_pct_fibra": r["copertura_pct"],
                "snr_medio_fibra": r["snr_medio"],
                "snr_minimo_fibra": r["snr_minimo"],
                "banda_piu_debole_fibra": r["banda_debole"],
            })

    if righe_csv:
        with open(FILE_CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(righe_csv[0].keys()))
            writer.writeheader()
            writer.writerows(righe_csv)
        print(f"\nDettaglio completo (tutte le metriche, per banda e per fibra) salvato in: {FILE_CSV_OUTPUT}")


if __name__ == "__main__":
    risultati = scansiona_sweep_e_rumore()
    if not risultati:
        print("Nessun dato trovato da analizzare.")
    else:
        stampa_report(risultati)