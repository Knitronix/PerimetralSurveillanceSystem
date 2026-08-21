"""
Confronta le fibre ottiche testate, leggendo i campioni salvati dal
registratore (1_registratore.py) nella cartella:

    dataset_sensori_24kHz/<id_fibra>/<classe>/campione_*.wav

Uso:
    python 2_confronta_fibre.py

Requisiti: registrare lo STESSO protocollo di test (stessi eventi, stessa
distanza, stesso ambiente) per ciascun microfono/fibra, cambiando solo l'ID
fibra nell'interfaccia del registratore. Il confronto ha senso solo se lo
stimolo fisico è comparabile tra una fibra e l'altra.

Output:
    - tre tabelle comparative stampate a schermo (metriche base + metriche
      spettrali/statistiche estese + SNR per banda di frequenza)
    - verdetto con punteggio composito (SNR, instabilità, clipping)
    - confronto_fibre.csv con TUTTE le metriche calcolate, una riga per file,
      per analisi ulteriori (Excel, pivot, grafici)
"""

import os
import glob
import wave
import csv
import numpy as np

CARTELLA_DATASET = "dataset_sensori_24kHz"
CLASSE_RUMORE_DI_FONDO = "RUMORE_AMBIENTALE"
SOGLIA_CLIPPING_CAMPIONE = 32760
EPS_DBFS = 1e-9
EPS_POTENZA = 1e-12
FILE_CSV_OUTPUT = "confronto_fibre.csv"

# --- PESI DEL PUNTEGGIO COMPOSITO ---
# Criteri standard di valutazione di un trasduttore/sensore acustico:
# 1) Sensibilità (SNR): quanto più forte è l'evento rispetto al rumore di
#    fondo, a parità di stimolo fisico. È il criterio primario.
# 2) Ripetibilità: un sensore che oscilla molto misura per misura è meno
#    affidabile anche se in media sembra sensibile. Penalizziamo l'SNR con
#    la deviazione standard tra le prove (in dB).
# 3) Validità del dato: se satura (clipping), il picco reale è sconosciuto
#    (l'ADC lo ha tagliato) - il dato non è utilizzabile per un confronto
#    onesto, quindi va SQUALIFICATO dal punteggio, non solo penalizzato.
PESO_PENALITA_INSTABILITA = 0.5  # dB di punteggio persi per ogni dB di std

# Bande grezze per la ripartizione di energia (Hz). Scelta generica
# bassa/media/alta, non legata a nessuna teoria specifica - utile come
# indicatore rapido di "dove" si concentra il contenuto in frequenza,
# senza bisogno di conoscere una legge di sweep (a differenza di
# 3_analizza_sweep.py, qui il segnale non è controllato/noto).
BORDI_BANDA_ENERGIA_HZ = [0.0, 500.0, 2000.0, float("inf")]
ETICHETTE_BANDA_ENERGIA = ["bassa(<500Hz)", "media(500-2000Hz)", "alta(>2000Hz)"]


def leggi_wav_come_array(percorso):
    with wave.open(percorso, "rb") as wf:
        n_campioni = wf.getnframes()
        sample_rate = wf.getframerate()
        dati = wf.readframes(n_campioni)
    campioni = np.frombuffer(dati, dtype=np.int16).astype(np.float64)
    return campioni, sample_rate


def spettro_completo(campioni, sample_rate):
    """FFT singola con finestra di Hann su tutto il file. Restituisce
    (frequenze, magnitudo) o (None, None) se il file è troppo corto."""
    if len(campioni) < 8:
        return None, None
    finestra = np.hanning(len(campioni))
    spettro = np.abs(np.fft.rfft(campioni * finestra))
    freq = np.fft.rfftfreq(len(campioni), d=1.0 / sample_rate)
    return freq, spettro


def baricentro_e_banda_spettrale(freq, spettro):
    """Baricentro (centroide, Hz) e banda (deviazione standard spettrale,
    Hz) attorno al baricentro. Il baricentro dice 'dove' si concentra
    mediamente l'energia; la banda dice quanto è 'larga' quella
    concentrazione - due fibre con lo stesso baricentro ma banda molto
    diversa hanno una forma spettrale diversa (una più tonale, una più
    diffusa)."""
    energia_totale = np.sum(spettro)
    if energia_totale <= 0:
        return float("nan"), float("nan")
    baricentro = float(np.sum(freq * spettro) / energia_totale)
    banda = float(np.sqrt(np.sum(((freq - baricentro) ** 2) * spettro) / energia_totale))
    return baricentro, banda


def flatness_spettrale(spettro):
    """Spectral flatness (Wiener entropy): media geometrica / media
    aritmetica della densità di potenza. Vicino a 1 = spettro piatto,
    simile a rumore bianco (poco strutturato). Vicino a 0 = spettro
    concentrato in poche frequenze (segnale tonale/strutturato). Utile per
    capire se una fibra restituisce un segnale più 'pulito/tonale' o più
    'rumoroso/diffuso' a parità di evento."""
    potenza = spettro.astype(np.float64) ** 2 + EPS_POTENZA
    media_geometrica = float(np.exp(np.mean(np.log(potenza))))
    media_aritmetica = float(np.mean(potenza))
    if media_aritmetica <= 0:
        return float("nan")
    return media_geometrica / media_aritmetica


def rolloff_spettrale(freq, spettro, percentuale=0.85):
    """Frequenza sotto la quale si concentra 'percentuale' dell'energia
    totale. Un rolloff più basso indica energia concentrata verso il
    basso, più alto indica un segnale più ricco di alte frequenze."""
    potenza = spettro.astype(np.float64) ** 2
    energia_totale = np.sum(potenza)
    if energia_totale <= 0:
        return float("nan")
    cumulativa = np.cumsum(potenza)
    soglia = percentuale * energia_totale
    idx = int(np.searchsorted(cumulativa, soglia))
    idx = min(idx, len(freq) - 1)
    return float(freq[idx])


def picco_spettrale(freq, spettro):
    """Frequenza dominante: dove sta il singolo bin con più energia."""
    if len(spettro) == 0:
        return float("nan")
    return float(freq[int(np.argmax(spettro))])


def energia_per_banda(freq, spettro, bordi):
    """Percentuale di energia (potenza) in ciascuna delle bande definite
    da 'bordi' (lista di N+1 confini per N bande)."""
    potenza = spettro.astype(np.float64) ** 2
    totale = np.sum(potenza)
    n_bande = len(bordi) - 1
    if totale <= 0:
        return [float("nan")] * n_bande
    frazioni = []
    for i in range(n_bande):
        mask = (freq >= bordi[i]) & (freq < bordi[i + 1])
        frazioni.append(100.0 * float(np.sum(potenza[mask])) / totale)
    return frazioni


def livello_per_banda_db(freq, spettro, n_campioni, bordi):
    """Livello di potenza per banda, normalizzato per il numero di
    campioni del file (indipendente dalla durata della registrazione),
    espresso in dB.

    NON è una calibrazione fisica assoluta (non ha senso confrontarlo con
    uno strumento esterno): serve a confrontare la STESSA banda tra un
    evento e il rumore ambientale della STESSA fibra, per ottenere un SNR
    per banda invece di un solo SNR a banda larga. Questo colma il limite
    per cui l'RMS a banda larga non distingue bene eventi a bassa energia
    concentrata in poche frequenze (es. passi lenti) dal rumore di fondo:
    con il livello per banda, anche se l'RMS totale è simile, la banda
    interessata può mostrare comunque un SNR positivo chiaro."""
    if n_campioni <= 0:
        return [float("nan")] * (len(bordi) - 1)
    potenza = spettro.astype(np.float64) ** 2
    livelli = []
    for i in range(len(bordi) - 1):
        mask = (freq >= bordi[i]) & (freq < bordi[i + 1])
        p_media = float(np.sum(potenza[mask])) / n_campioni
        livelli.append(10.0 * np.log10(p_media + EPS_POTENZA))
    return livelli


def kurtosis_eccesso(campioni):
    """Kurtosis in eccesso (0 = distribuzione gaussiana). Valori alti
    indicano un segnale 'impulsivo' (pochi picchi molto più grandi del
    resto) - è una metrica classica nell'analisi vibrazionale/acustica per
    rilevare eventi impulsivi, complementare al crest factor."""
    x = campioni.astype(np.float64) - np.mean(campioni)
    m2 = np.mean(x ** 2)
    if m2 <= 0:
        return float("nan")
    m4 = np.mean(x ** 4)
    return float(m4 / (m2 ** 2) - 3.0)


def asimmetria(campioni):
    """Skewness: asimmetria della distribuzione dei campioni attorno alla
    media. 0 = simmetrica. Un segnale fortemente asimmetrico può indicare
    una non linearità del sensore (risposta diversa a compressione ed
    espansione)."""
    x = campioni.astype(np.float64) - np.mean(campioni)
    m2 = np.mean(x ** 2)
    if m2 <= 0:
        return float("nan")
    m3 = np.mean(x ** 3)
    return float(m3 / (m2 ** 1.5))


def zero_crossing_rate(campioni, sample_rate):
    """Attraversamenti dello zero al secondo. Indicatore grezzo e
    velocissimo da calcolare del contenuto in frequenza dominante/della
    rumorosità del segnale, indipendente dalla FFT."""
    segni = np.sign(campioni)
    segni[segni == 0] = 1
    attraversamenti = np.count_nonzero(segni[:-1] != segni[1:])
    durata = len(campioni) / sample_rate
    if durata <= 0:
        return float("nan")
    return float(attraversamenti / durata)


def calcola_metriche_file(percorso):
    campioni, sample_rate = leggi_wav_come_array(percorso)
    if len(campioni) == 0:
        return None

    rms_lineare = np.sqrt(np.mean(campioni ** 2))
    picco_lineare = np.max(np.abs(campioni))
    rms_dbfs = 20.0 * np.log10(rms_lineare / 32768.0 + EPS_DBFS)
    picco_dbfs = 20.0 * np.log10(picco_lineare / 32768.0 + EPS_DBFS)
    crest_db = picco_dbfs - rms_dbfs
    clipping_pct = 100.0 * np.count_nonzero(np.abs(campioni) >= SOGLIA_CLIPPING_CAMPIONE) / len(campioni)
    durata_s = len(campioni) / sample_rate

    freq, spettro = spettro_completo(campioni, sample_rate)
    if freq is not None:
        baricentro_hz, banda_hz = baricentro_e_banda_spettrale(freq, spettro)
        flatness = flatness_spettrale(spettro)
        rolloff_85_hz = rolloff_spettrale(freq, spettro, 0.85)
        rolloff_95_hz = rolloff_spettrale(freq, spettro, 0.95)
        picco_freq_hz = picco_spettrale(freq, spettro)
        e_bassa, e_media, e_alta = energia_per_banda(freq, spettro, BORDI_BANDA_ENERGIA_HZ)
        liv_bassa, liv_media, liv_alta = livello_per_banda_db(
            freq, spettro, len(campioni), BORDI_BANDA_ENERGIA_HZ
        )
    else:
        baricentro_hz = banda_hz = flatness = float("nan")
        rolloff_85_hz = rolloff_95_hz = picco_freq_hz = float("nan")
        e_bassa = e_media = e_alta = float("nan")
        liv_bassa = liv_media = liv_alta = float("nan")

    return {
        "file": os.path.basename(percorso),
        "durata_s": durata_s,
        "rms_dbfs": rms_dbfs,
        "picco_dbfs": picco_dbfs,
        "crest_db": crest_db,
        "clipping_pct": clipping_pct,
        "baricentro_hz": baricentro_hz,
        "banda_spettrale_hz": banda_hz,
        "flatness_spettrale": flatness,
        "rolloff_85_hz": rolloff_85_hz,
        "rolloff_95_hz": rolloff_95_hz,
        "picco_spettrale_hz": picco_freq_hz,
        "energia_bassa_pct": e_bassa,
        "energia_media_pct": e_media,
        "energia_alta_pct": e_alta,
        "livello_banda_bassa_db": liv_bassa,
        "livello_banda_media_db": liv_media,
        "livello_banda_alta_db": liv_alta,
        "kurtosis": kurtosis_eccesso(campioni),
        "skewness": asimmetria(campioni),
        "zero_crossing_rate_hz": zero_crossing_rate(campioni, sample_rate),
    }


def scansiona_dataset():
    """Restituisce { id_fibra: { classe: [metriche_file, ...] } }"""
    risultati = {}
    if not os.path.isdir(CARTELLA_DATASET):
        print(f"Cartella '{CARTELLA_DATASET}' non trovata. Esegui prima il registratore.")
        return risultati

    for id_fibra in sorted(os.listdir(CARTELLA_DATASET)):
        percorso_fibra = os.path.join(CARTELLA_DATASET, id_fibra)
        if not os.path.isdir(percorso_fibra):
            continue
        risultati[id_fibra] = {}
        for classe in sorted(os.listdir(percorso_fibra)):
            percorso_classe = os.path.join(percorso_fibra, classe)
            if not os.path.isdir(percorso_classe):
                continue
            file_wav = sorted(glob.glob(os.path.join(percorso_classe, "*.wav")))
            metriche = []
            for f in file_wav:
                try:
                    m = calcola_metriche_file(f)
                    if m is not None:
                        metriche.append(m)
                except Exception as e:
                    print(f"  ! Errore leggendo {f}: {e}")
            if metriche:
                risultati[id_fibra][classe] = metriche
    return risultati


def stampa_tabella_comparativa(risultati):
    # ============================================================
    # TABELLA 1: metriche base (livello, dinamica, SNR) - come prima
    # ============================================================
    intestazione = (
        f"{'Fibra':<12}{'Classe':<20}{'N':>4}{'RMS medio':>12}{'Picco medio':>13}"
        f"{'Crest':>8}{'Clip%':>8}{'Baric.Hz':>10}{'RMS std':>10}{'SNR vs rumore':>16}"
    )
    print("=" * len(intestazione))
    print("CONFRONTO FIBRE OTTICHE - METRICHE BASE")
    print("=" * len(intestazione))
    print(intestazione)
    print("-" * len(intestazione))

    righe_csv = []
    dati_per_fibra = {}  # id_fibra -> {"snr": [...], "std": [...], "clipping_max": float}

    for id_fibra, classi in risultati.items():
        rms_rumore = None
        if CLASSE_RUMORE_DI_FONDO in classi:
            valori = [m["rms_dbfs"] for m in classi[CLASSE_RUMORE_DI_FONDO]]
            rms_rumore = float(np.mean(valori))

        dati_per_fibra.setdefault(id_fibra, {"snr": [], "std": [], "clipping_max": 0.0})

        for classe, metriche in classi.items():
            rms_vals = np.array([m["rms_dbfs"] for m in metriche])
            picco_vals = np.array([m["picco_dbfs"] for m in metriche])
            crest_vals = np.array([m["crest_db"] for m in metriche])
            clip_vals = np.array([m["clipping_pct"] for m in metriche])
            baric_vals = np.array([m["baricentro_hz"] for m in metriche])

            rms_medio = float(np.mean(rms_vals))
            rms_std = float(np.std(rms_vals))
            clip_massimo_classe = float(np.max(clip_vals))
            dati_per_fibra[id_fibra]["clipping_max"] = max(
                dati_per_fibra[id_fibra]["clipping_max"], clip_massimo_classe
            )

            snr_txt = "--"
            if rms_rumore is not None and classe != CLASSE_RUMORE_DI_FONDO:
                snr_db = rms_medio - rms_rumore
                snr_txt = f"{snr_db:+.1f} dB"
                dati_per_fibra[id_fibra]["snr"].append(snr_db)
                dati_per_fibra[id_fibra]["std"].append(rms_std)

            print(
                f"{id_fibra:<12}{classe:<20}{len(metriche):>4}"
                f"{rms_medio:>11.1f}d{np.mean(picco_vals):>12.1f}d"
                f"{np.mean(crest_vals):>7.1f}d{np.mean(clip_vals):>7.2f}%"
                f"{np.nanmean(baric_vals):>9.0f}h{rms_std:>9.2f}d{snr_txt:>16}"
            )

            for m in metriche:
                righe_csv.append({"id_fibra": id_fibra, "classe": classe, **m})

    print("=" * len(intestazione))
    print("\nNote tabella 1:")
    print("- RMS/Picco in dBFS (0dB = fondo scala). Più vicino a 0 = segnale più forte.")
    print("- Crest = Picco - RMS: quanto è 'impulsivo' il segnale (dB).")
    print("- RMS std: deviazione standard tra i vari campioni della stessa classe/fibra")
    print("  (ripetibilità: più basso = fibra più stabile/costante sullo stesso stimolo).")
    print(f"- SNR vs rumore: RMS medio della classe meno RMS medio di '{CLASSE_RUMORE_DI_FONDO}'")
    print("  per la stessa fibra. Più alto = fibra più sensibile a parità di stimolo fisico.")
    print("- Clip%: percentuale di campioni saturi. Deve essere 0% per una misura affidabile.")

    # ============================================================
    # TABELLA 2: metriche spettrali e statistiche estese
    # ============================================================
    intestazione2 = (
        f"{'Fibra':<12}{'Classe':<18}{'Banda':>8}{'Flat':>7}{'Roll85':>8}"
        f"{'PiccoHz':>9}{'E.bas%':>7}{'E.med%':>7}{'E.alt%':>7}{'Kurt':>7}{'Skew':>7}{'ZCR':>8}"
    )
    print("\n" + "=" * len(intestazione2))
    print("CONFRONTO FIBRE OTTICHE - METRICHE SPETTRALI E STATISTICHE ESTESE")
    print("=" * len(intestazione2))
    print(intestazione2)
    print("-" * len(intestazione2))

    for id_fibra, classi in risultati.items():
        for classe, metriche in classi.items():
            banda = np.nanmean([m["banda_spettrale_hz"] for m in metriche])
            flat = np.nanmean([m["flatness_spettrale"] for m in metriche])
            roll85 = np.nanmean([m["rolloff_85_hz"] for m in metriche])
            picco_hz = np.nanmean([m["picco_spettrale_hz"] for m in metriche])
            e_bassa = np.nanmean([m["energia_bassa_pct"] for m in metriche])
            e_media = np.nanmean([m["energia_media_pct"] for m in metriche])
            e_alta = np.nanmean([m["energia_alta_pct"] for m in metriche])
            kurt = np.nanmean([m["kurtosis"] for m in metriche])
            skew = np.nanmean([m["skewness"] for m in metriche])
            zcr = np.nanmean([m["zero_crossing_rate_hz"] for m in metriche])

            print(
                f"{id_fibra:<12}{classe:<18}{banda:>7.0f}h{flat:>7.2f}{roll85:>7.0f}h"
                f"{picco_hz:>8.0f}h{e_bassa:>6.0f}%{e_media:>6.0f}%{e_alta:>6.0f}%"
                f"{kurt:>7.1f}{skew:>7.2f}{zcr:>7.0f}h"
            )

    print("=" * len(intestazione2))
    print("\nNote tabella 2:")
    print("- Banda: deviazione standard spettrale attorno al baricentro (Hz) - quanto è")
    print("  'larga' la distribuzione dell'energia in frequenza.")
    print("- Flat (flatness spettrale, 0-1): vicino a 1 = spettro piatto/rumoroso, vicino")
    print("  a 0 = energia concentrata/tonale.")
    print("- Roll85: frequenza sotto la quale sta l'85% dell'energia del segnale.")
    print("- PiccoHz: frequenza del singolo bin con più energia (frequenza dominante).")
    print("- E.bassa/media/alta%: percentuale di energia in <500Hz / 500-2000Hz / >2000Hz.")
    print("- Kurt (kurtosis in eccesso): 0=gaussiana, valori alti=segnale impulsivo")
    print("  (pochi picchi molto più grandi del resto) - complementare al crest factor.")
    print("- Skew (asimmetria): 0=simmetrica. Valori lontani da 0 possono indicare una")
    print("  non linearità del sensore (risposta diversa a compressione/espansione).")
    print("- ZCR: attraversamenti dello zero al secondo, indicatore grezzo e veloce del")
    print("  contenuto in frequenza dominante, indipendente dalla FFT.")

    # ============================================================
    # TABELLA 3: SNR per banda (bassa/media/alta), non solo a banda larga
    # ============================================================
    # Motivazione: l'SNR "vs rumore" della Tabella 1 è calcolato sull'RMS a
    # banda larga. Un evento con energia concentrata in poche frequenze (es.
    # passi lenti, energia soprattutto sotto i 500Hz) può avere un RMS totale
    # simile al rumore di fondo pur avendo, in QUELLA banda specifica, un
    # segnale ben distinguibile. L'SNR a banda larga da solo non lo vede;
    # qui lo controlliamo banda per banda, con lo stesso principio già usato
    # in 3_analizza_sweep.py per lo sweep in frequenza.
    intestazione3 = (
        f"{'Fibra':<12}{'Classe':<20}{'SNR bassa':>11}{'SNR media':>11}{'SNR alta':>11}"
    )
    print("\n" + "=" * len(intestazione3))
    print("CONFRONTO FIBRE OTTICHE - SNR PER BANDA DI FREQUENZA")
    print("=" * len(intestazione3))
    print(intestazione3)
    print("-" * len(intestazione3))

    for id_fibra, classi in risultati.items():
        rumore_banda = None
        if CLASSE_RUMORE_DI_FONDO in classi:
            metriche_rumore = classi[CLASSE_RUMORE_DI_FONDO]
            rumore_banda = [
                float(np.nanmean([m["livello_banda_bassa_db"] for m in metriche_rumore])),
                float(np.nanmean([m["livello_banda_media_db"] for m in metriche_rumore])),
                float(np.nanmean([m["livello_banda_alta_db"] for m in metriche_rumore])),
            ]

        for classe, metriche in classi.items():
            if classe == CLASSE_RUMORE_DI_FONDO:
                continue

            evento_banda = [
                float(np.nanmean([m["livello_banda_bassa_db"] for m in metriche])),
                float(np.nanmean([m["livello_banda_media_db"] for m in metriche])),
                float(np.nanmean([m["livello_banda_alta_db"] for m in metriche])),
            ]

            if rumore_banda is None:
                snr_txt = ["n/d"] * 3
            else:
                snr_txt = [f"{e - r:+.1f} dB" for e, r in zip(evento_banda, rumore_banda)]

            print(
                f"{id_fibra:<12}{classe:<20}{snr_txt[0]:>11}{snr_txt[1]:>11}{snr_txt[2]:>11}"
            )

    print("=" * len(intestazione3))
    print("\nNote tabella 3:")
    print("- SNR bassa/media/alta: livello medio della classe meno livello medio del")
    print(f"  rumore ('{CLASSE_RUMORE_DI_FONDO}'), calcolato separatamente in ciascuna delle")
    print("  bande <500Hz / 500-2000Hz / >2000Hz, per la stessa fibra.")
    print("- 'n/d': quella fibra non ha una registrazione di rumore ambientale, quindi")
    print("  l'SNR per banda non è calcolabile (manca il riferimento).")
    print("- Il livello per banda è normalizzato per numero di campioni (indipendente")
    print("  dalla durata del file) ma NON è calibrato in assoluto: ha senso solo per")
    print("  confrontare evento e rumore della STESSA fibra, non tra fibre diverse in")
    print("  valore assoluto (per quello usa comunque l'SNR a banda larga di Tabella 1).")
    print("- Utile soprattutto per eventi a bassa energia/bassa frequenza (es. passi")
    print("  lenti): possono avere SNR a banda larga poco chiaro pur avendo un SNR")
    print("  netto nella banda bassa. Questa tabella è informativa: NON entra nel")
    print("  punteggio composito del verdetto qui sotto.")

    # ============================================================
    # VERDETTO (punteggio composito, come prima: SNR - instabilità, squalifica clipping)
    # ============================================================
    if dati_per_fibra:
        intestazione_verdetto = (
            f"{'Fibra':<12}{'SNR medio':>12}{'Instabilità':>13}{'Penalità':>10}"
            f"{'Punteggio':>11}{'Stato':>18}"
        )
        print("\n" + "=" * len(intestazione_verdetto))
        print("VERDETTO SECONDO CRITERI ACUSTICI STANDARD")
        print("(sensibilità SNR, penalizzata per instabilità, squalifica se satura)")
        print("=" * len(intestazione_verdetto))
        print(intestazione_verdetto)
        print("-" * len(intestazione_verdetto))

        classifica = []
        for id_fibra, d in dati_per_fibra.items():
            if not d["snr"]:
                continue
            snr_medio = float(np.mean(d["snr"]))
            instabilita_media = float(np.mean(d["std"]))
            penalita = PESO_PENALITA_INSTABILITA * instabilita_media
            squalificata = d["clipping_max"] > 0.0

            if squalificata:
                punteggio = float("-inf")
                stato = "❌ SATURA (non valido)"
            else:
                punteggio = snr_medio - penalita
                stato = "✓ valido"

            classifica.append((id_fibra, snr_medio, instabilita_media, penalita, punteggio, stato, squalificata))

        classifica.sort(key=lambda riga: riga[4], reverse=True)

        for pos, (id_fibra, snr_medio, instabilita, penalita, punteggio, stato, squalificata) in enumerate(classifica, start=1):
            punteggio_txt = "N/D" if squalificata else f"{punteggio:+.1f}"
            corona = ""
            if pos == 1 and not squalificata:
                corona = "  ⭐ MIGLIORE"
            print(
                f"{id_fibra:<12}{snr_medio:>+11.1f}d{instabilita:>12.2f}d"
                f"{penalita:>9.2f}d{punteggio_txt:>11} {stato:>17}{corona}"
            )

        print("=" * len(intestazione_verdetto))
        print("\nCome si legge il punteggio:")
        print("  Punteggio = SNR medio - (0.5 × instabilità media tra le prove)")
        print("  Una fibra che satura (clipping) viene sempre esclusa dal podio.")
        print("  Le metriche spettrali estese (tabella 2) NON entrano nel punteggio: sono")
        print("  informative, per capire QUALE differenza c'è tra le fibre, non solo quanto")
        print("  è grande. Usale per un giudizio applicativo che il punteggio da solo non può dare.")

        migliori_valide = [r for r in classifica if not r[6]]
        if not migliori_valide:
            print("\n⚠ ATTENZIONE: tutte le fibre risultano sature in almeno una misura.")
            print("  Nessun risultato è affidabile: ripeti i test con un guadagno più basso.")
        elif len(migliori_valide) < len(classifica):
            escluse = [r[0] for r in classifica if r[6]]
            print(f"\n⚠ Escluse dal podio per saturazione: {', '.join(escluse)}")

    if righe_csv:
        with open(FILE_CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(righe_csv[0].keys()))
            writer.writeheader()
            writer.writerows(righe_csv)
        print(f"\nDettaglio completo per-file (tutte le metriche) salvato in: {FILE_CSV_OUTPUT}")


if __name__ == "__main__":
    risultati = scansiona_dataset()
    if not risultati:
        print("Nessun dato trovato da analizzare.")
    else:
        stampa_tabella_comparativa(risultati)