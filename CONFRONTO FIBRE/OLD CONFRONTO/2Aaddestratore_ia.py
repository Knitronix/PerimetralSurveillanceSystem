# Questo script analizzerà tutti i file contenuti nelle tue cartelle 
# (BATTITO_MANI, FISCHIO, RUMORE_AMBIENTALE), estrarrà da ciascuno di essi 
# un vettore di 64 parametri spettrali (le "features"), addestrerà l'algoritmo
# Random Forest e salverà il cervello della macchina in un file chiamato 
# modello_ia_project_q.pkl.

import os
import numpy as np
import pickle
from scipy.io import wavfile
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report

CARTELLA_DATASET = "dataset_sensori_24kHz"
SAMPLE_RATE = 24000
NUM_BINS = 64  # Dividiamo lo spettro in 64 bande di frequenza (le nostre feature)
FILE_MODELLO = "modello_ia_project_q.pkl"

def estrai_features_da_file(percorso_file):
    """Legge un file WAV, calcola la FFT e la comprime in 64 bin di frequenza normalizzati"""
    sr, dati = wavfile.read(percorso_file)
    if len(dati) == 0:
        return None
    
    # 1. Calcolo Trasformata di Fourier (Magnitudo)
    fft_vettore = np.abs(np.fft.rfft(dati))
    frequenze = np.fft.rfftfreq(len(dati), d=1.0/SAMPLE_RATE)
    
    # 2. Tagliamo lo spettro fino a 8000 Hz (inutile andare oltre per questi rumori)
    idx_8k = np.where(frequenze <= 8000)[0][-1]
    fft_tagliata = fft_vettore[:idx_8k]
    
    # 3. Raggruppiamo lo spettro in NUM_BINS (64) sotto-bande mediate
    blocchi = np.array_split(fft_tagliata, NUM_BINS)
    features = [np.mean(b) if len(b) > 0 else 0.0 for b in blocchi]
    
    # 4. Normalizzazione locale (indipendente dal volume del colpo)
    features = np.array(features)
    max_val = np.max(features)
    if max_val > 0:
        features = features / max_val
        
    return features

def addestra_cervello():
    X = []  # Vettore delle caratteristiche (64 bin per ogni file)
    y = []  # Etichette delle classi (es. "FISCHIO", "BATTITO_MANI")
    
    if not os.path.exists(CARTELLA_DATASET):
        print(f"❌ Cartella {CARTELLA_DATASET} non trovata!")
        return

    classi = [d for d in os.listdir(CARTELLA_DATASET) if os.path.isdir(os.path.join(CARTELLA_DATASET, d))]
    
    print("🧠 [IA] Caricamento del dataset ed estrazione delle impronte digitali...")
    for classe in classi:
        percorso_classe = os.path.join(CARTELLA_DATASET, classe)
        file_wav = [f for f in os.listdir(percorso_classe) if f.endswith('.wav')]
        
        print(f" -> Elaborazione classe '{classe}': trovati {len(file_wav)} campioni.")
        for nome_file in file_wav:
            features = estrai_features_da_file(os.path.join(percorso_classe, nome_file))
            if features is not None:
                X.append(features)
                y.append(classe)
                
    X = np.array(X)
    y = np.array(y)
    
    if len(X) == 0:
        print("❌ Dataset vuoto! Registra prima dei file audio.")
        return

    # Dividiamo in Training Set (80%) e Test Set (20%) per valutare l'IA
    # Usiamo stratify per mantenere le proporzioni delle classi stabili
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    
    print(f"\n📊 [IA] Dati di addestramento: {len(X_train)} campioni | Dati di test: {len(X_test)} campioni.")
    
    # Inizializziamo il classificatore Random Forest (Foresta di alberi decisionali)
    modello = RandomForestClassifier(n_estimators=100, random_state=42)
    modello.fit(X_train, y_train)
    
    # Validazione
    y_pred = modello.predict(X_test)
    accuratezza = accuracy_score(y_test, y_pred)
    print(f"🎯 [IA] Accuratezza del modello sul Test Set: {accuratezza * 100:.2f}%")
    
    # Addestramento finale su TUTTI i dati per la massima precisione in diretta
    print("🚀 [IA] Addestramento definitivo sull'intero dataset...")
    modello.fit(X, y)
    
    # Salvataggio del modello su disco
    dati_da_salvare = {
        "modello": modello,
        "num_bins": NUM_BINS,
        "sample_rate": SAMPLE_RATE
    }
    with open(FILE_MODELLO, 'wb') as f:
        pickle.dump(dati_da_salvare, f)
        
    print(f"💾 [IA] Cervello salvato con successo nel file: '{FILE_MODELLO}'!")

if __name__ == '__main__':
    addestra_cervello()