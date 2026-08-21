"""
audio_features.py Usato sia dal training sia dall'inferenza
------------------
Estrazione feature da segmenti audio (eventi catturati dalla fibra Sagnac).
Usato sia da train_classifier.py (sui file WAV del dataset) sia da
realtime_inference.py (su un buffer numpy già in memoria, per l'inferenza
dal vivo dentro registratore_stalta.py) — stessa funzione, stesso ordine
delle feature, per non avere mai disallineamento tra training e inferenza.

Scelta progettuale: poche feature (circa 20), interpretabili, non centinaia.
Con un dataset che nelle prime settimane avrà poche decine/centinaia di
esempi a classe, un numero alto di feature (es. MFCC + delta + delta-delta +
spettrogramma completo, che fanno gia' centinaia di valori) rischia
overfitting quasi certo con Random Forest/SVM. Meglio iniziare piccoli e
aggiungere feature quando il dataset è abbastanza grande da giustificarle.
"""

import numpy as np
from scipy import signal as sp_signal

SAMPLE_RATE_DEFAULT = 24000

BANDE_FREQUENZA = [
    (0, 50),
    (50, 150),
    (150, 400),
    (400, 1000),
    (1000, 2500),
    (2500, 6000),
]

NOMI_FEATURE = (
    ["rms", "picco", "crest_factor", "zero_crossing_rate", "durata_sopra_soglia_frac"]
    + ["centroide_spettrale", "banda_spettrale", "rolloff_85", "flatness_spettrale"]
    + [f"energia_banda_{lo}_{hi}hz" for lo, hi in BANDE_FREQUENZA]
    + ["skewness", "kurtosis", "n_picchi_locali"]
)


def _statistiche_forma_donda(x):
    x = x.astype(np.float64)
    rms = np.sqrt(np.mean(x**2)) + 1e-9
    picco = np.max(np.abs(x))
    crest_factor = picco / rms
    segni = np.sign(x)
    segni[segni == 0] = 1
    zero_crossing_rate = np.mean(segni[:-1] != segni[1:])

    soglia = 0.1 * picco
    durata_sopra_soglia_frac = np.mean(np.abs(x) > soglia)

    media = np.mean(x)
    std = np.std(x) + 1e-9
    skewness = np.mean(((x - media) / std) ** 3)
    kurtosis = np.mean(((x - media) / std) ** 4) - 3.0

    inviluppo = np.abs(sp_signal.hilbert(x)) if len(x) > 10 else np.abs(x)
    picchi, _ = sp_signal.find_peaks(inviluppo, height=soglia, distance=max(1, len(x) // 50))
    n_picchi_locali = len(picchi)

    return [rms, picco, crest_factor, zero_crossing_rate, durata_sopra_soglia_frac,
            skewness, kurtosis, n_picchi_locali]


def _statistiche_spettrali(x, fs):
    x = x.astype(np.float64)
    x = x - np.mean(x)
    n = len(x)
    if n < 8:
        n_feat = 4 + len(BANDE_FREQUENZA)
        return [0.0] * n_feat

    finestra = np.hanning(n)
    spettro = np.abs(np.fft.rfft(x * finestra))
    freq = np.fft.rfftfreq(n, d=1.0 / fs)

    potenza = spettro**2
    potenza_tot = np.sum(potenza) + 1e-12

    centroide = np.sum(freq * potenza) / potenza_tot
    banda = np.sqrt(np.sum(((freq - centroide) ** 2) * potenza) / potenza_tot)

    cumsum = np.cumsum(potenza)
    idx_85 = np.searchsorted(cumsum, 0.85 * cumsum[-1])
    rolloff_85 = freq[min(idx_85, len(freq) - 1)]

    log_potenza = np.log(potenza + 1e-12)
    flatness = np.exp(np.mean(log_potenza)) / (np.mean(potenza) + 1e-12)

    energie_bande = []
    for lo, hi in BANDE_FREQUENZA:
        mask = (freq >= lo) & (freq < hi)
        energie_bande.append(np.sum(potenza[mask]) / potenza_tot)

    return [centroide, banda, rolloff_85, flatness] + energie_bande


def estrai_feature(x, sample_rate=SAMPLE_RATE_DEFAULT):
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 1:
        x = x.flatten()
    if len(x) == 0:
        return np.zeros(len(NOMI_FEATURE))

    feat_tempo = _statistiche_forma_donda(x)
    feat_freq = _statistiche_spettrali(x, sample_rate)

    rms, picco, crest, zcr, durata_frac, skew, kurt, npicchi = feat_tempo
    centroide, banda, rolloff, flatness, *energie_bande = feat_freq

    vettore = [rms, picco, crest, zcr, durata_frac,
               centroide, banda, rolloff, flatness,
               *energie_bande,
               skew, kurt, npicchi]
    return np.array(vettore, dtype=np.float64)


def estrai_feature_da_wav(path_wav):
    import wave
    with wave.open(path_wav, "rb") as wf:
        fs = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    x = np.frombuffer(raw, dtype=np.int16)
    return estrai_feature(x, sample_rate=fs)