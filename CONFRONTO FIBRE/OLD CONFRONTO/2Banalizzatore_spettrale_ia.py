import os
import numpy as np
from scipy.io import wavfile
import matplotlib.pyplot as plt

CARTELLA_DATASET = "dataset_sensori_24kHz"
SAMPLE_RATE = 24000

def estrai_spettro_medio(cartella_classe):
    """Legge tutti i file WAV di una cartella e calcola lo spettro di frequenza medio"""
    percorso_completo = os.path.join(CARTELLA_DATASET, cartella_classe)
    if not os.path.exists(percorso_completo):
        return None, None
    
    file_wav = [f for f in os.listdir(percorso_completo) if f.endswith('.wav')]
    if not file_wav:
        print(f"⚠️ Nessun file trovato in {cartella_classe}")
        return None, None
    
    spettri = []
    frequenze = None
    
    for nome_file in file_wav:
        # Carica il file audio a 24kHz
        sr, dati = wavfile.read(os.path.join(percorso_completo, nome_file))
        
        # Se il file è vuoto o corrotto salta
        if len(dati) == 0:
            continue
            
        # Calcola la Trasformata di Fourier Veloce (FFT) Magnitudo
        # Usiamo rfft perché i nostri dati sono reali (non complessi)
        fft_vettore = np.abs(np.fft.rfft(dati))
        
        # Calcola l'asse delle frequenze corrispondente (una volta sola)
        if frequenze is None:
            frequenze = np.fft.rfftfreq(len(dati), d=1.0/SAMPLE_RATE)
            
        spettri.append(fft_vettore)
        
    if not spettri:
        return None, None
        
    # Fa la media matematica di tutte le registrazioni di questa classe
    spettro_medio = np.mean(spettri, axis=0)
    return frequenze, spettro_medio

def analizza_e_disegna():
    plt.figure(figsize=(12, 6))
    plt.style.use('seaborn-v0_8-darkgrid' if 'seaborn-v0_8-darkgrid' in plt.style.available else 'default')
    
    # Cerchiamo le sottocartelle nel nostro dataset
    classi = [d for d in os.listdir(CARTELLA_DATASET) if os.path.isdir(os.path.join(CARTELLA_DATASET, d))]
    
    colori = {'BATTITO_MANI': '#ff4757', 'FISCHIO': '#1e90ff', 'RUMORE_AMBIENTALE': '#2ed573', 'CORSA': '#ffa502'}
    
    print("--- ESTRATTORE DI FEATURE PROJECT Q ---")
    for classe in classi:
        freq, spettro = estrai_spettro_medio(classe)
        if freq is not None:
            print(f"🧬 Classe '{classe}': Elaborati {len(os.listdir(os.path.join(CARTELLA_DATASET, classe)))} file WAV.")
            
            # Normalizziamo lo spettro per poterli confrontare equamente
            spettro_norm = spettro / (np.max(spettro) + 1e-6)
            
            colore = colori.get(classe, None) # Usa colore predefinito o casuale
            plt.plot(freq, spettro_norm, label=classe, alpha=0.8, linewidth=2, color=colore)

    plt.title("Impronte Digitali Spettrali dei Segnali sulla Fibra Ottica", fontsize=14, fontweight='bold')
    plt.xlabel("Frequenza (Hz)", fontsize=12)
    plt.ylabel("Intensità Spettrale Normalizzata", fontsize=12)
    plt.xlim(0, 8000) # La maggior parte dei rumori acustici umani sta sotto gli 8kHz
    plt.legend(fontsize=12)
    plt.grid(True, linestyle='--', alpha=0.6)
    
    print("\n📊 Generazione grafico in corso... Controlla la finestra pop-up.")
    plt.show()

if __name__ == '__main__':
    analizza_e_disegna()