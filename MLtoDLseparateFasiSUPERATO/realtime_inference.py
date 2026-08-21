"""
realtime_inference.py
----------------------
Carica il modello allenato da train_classifier.py e lo usa per classificare
un buffer di campioni appena catturato da registratore_stalta.py.

Sotto una soglia di confidenza minima, restituisce "SCONOSCIUTO_DA_VERIFICARE"
invece della classe con probabilità più alta: coerente con la priorità del
progetto (non perdere/mascherare eventi mai visti forzandoli in una classe
nota) — vedi planning_progetto_fibra.md, tassonomia.
"""

import joblib
import numpy as np

from audio_features import estrai_feature

CLASSE_SCONOSCIUTA = "SCONOSCIUTO_DA_VERIFICARE"
SOGLIA_CONFIDENZA_DEFAULT = 0.75


class ClassificatoreEventi:
    def __init__(self, path_modello, soglia_confidenza=SOGLIA_CONFIDENZA_DEFAULT):
        pacchetto = joblib.load(path_modello)
        self.modello = pacchetto["modello"]
        self.tipo_modello = pacchetto.get("tipo_modello", "sconosciuto")
        self.scaler = pacchetto["scaler"]
        self.encoder = pacchetto["encoder"]
        self.nomi_feature = pacchetto["nomi_feature"]
        self.soglia_confidenza = soglia_confidenza

    def classifica(self, buffer_campioni, sample_rate):
        """
        Ritorna (etichetta, confidenza, probabilita_per_classe).
        etichetta è CLASSE_SCONOSCIUTA se la confidenza della predizione
        migliore è sotto self.soglia_confidenza.
        """
        feat = estrai_feature(buffer_campioni, sample_rate=sample_rate)
        feat_scaled = self.scaler.transform(feat.reshape(1, -1))

        probs = self.modello.predict_proba(feat_scaled)[0]
        idx_max = int(np.argmax(probs))

        etichetta_grezza = self.encoder.classes_[idx_max]
        confidenza = float(probs[idx_max])

        etichetta = etichetta_grezza if confidenza >= self.soglia_confidenza else CLASSE_SCONOSCIUTA

        probabilita = {classe: float(p) for classe, p in zip(self.encoder.classes_, probs)}
        return etichetta, confidenza, probabilita