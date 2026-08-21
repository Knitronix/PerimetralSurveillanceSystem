# Guida operativa — Sistema di riconoscimento eventi su fibra Sagnac

File necessari, tutti nella **stessa cartella**:

```
registratore_stalta.py
audio_features.py
train_classifier.py
realtime_inference.py
config_soglie_ia.json
```

`registratore_stalta.py` ha **due modalità** selezionabili da un menu a tendina in alto a destra: *Dataset* (etichettatura manuale, come prima) e *Riconoscimento* (classificazione automatica, senza etichetta manuale). In modalità Riconoscimento, spettrogramma e grafico storico STA/LTA si disattivano — non solo si nascondono: si ferma anche il calcolo che li genera — per ridurre il carico sulla CPU quando non ti servono.

---

## 0. Installazione (una tantum)

```bash
python -m pip install numpy scipy scikit-learn joblib PySide6 pyqtgraph
```

Nessuna libreria audio esterna (niente librosa): tutto basato su NumPy/SciPy, per ridurre i problemi di compatibilità tra versioni Python.

---

## PARTE 1 — Uso del registratore STA/LTA (passo-passo)

**1. Avvio**
```bash
python registratore_stalta.py
```
Al primo avvio crea automaticamente le cartelle `dataset_sensori_24kHz/` e `registrazioni_audio/`, e un file `full_streaming_<timestamp>.wav` con lo streaming grezzo continuo (utile come backup/debug, indipendente dagli eventi ritagliati).

**2. Verifica che i dati arrivino**
Controlla la label "Rete" in basso a sinistra: deve mostrare un numero di pacchetti/secondo stabile. **Nota**: il dispositivo invia dati a raffiche (~16 pacchetti quasi simultanei ogni ~169ms), non in flusso uniforme — è normale e non un problema di rete, l'app è calibrata su questo comportamento. Preoccupati solo se vedi crescere "anomalie raffica" molto rapidamente o "secondi con possibile perdita dati" — quelli sì indicano dati mancanti, non solo la normale cadenza a scatti.

**3. Calibra le soglie STA/LTA prima di registrare qualunque cosa**
Con il sistema a riposo (nessun evento in corso), guarda la label "STA/LTA" in tempo reale: dovrebbe oscillare intorno a 1.0 (nessun evento = short-term e long-term average simili). Genera un evento di prova (un colpo secco vicino alla fibra) e osserva quanto sale il rapporto. Regola:
- **Soglia ON**: abbastanza sopra il rumore di fondo a riposo da non scattare per caso, ma sotto il picco che hai osservato durante l'evento di prova. Se a riposo il rapporto oscilla fino a 2, non mettere ON a 2.2 (troppo vicino, scatterà spesso per rumore) — un buon margine è tipicamente 1.5-2× il massimo osservato a riposo.
- **Soglia OFF**: più bassa della ON, ma sopra 1.0. Se troppo vicina alla ON, il trigger si aprirà/chiuderà a scatti sullo stesso evento nonostante l'isteresi.
Per ora: prendi il max di un minuto di rumore ambientale = rumore_max; poi prendi il max del tuo evento più debole possibile (es passo cauto) = evento_min 
ON = rumore_max + 0.3×(evento_min − rumore_max), 
OFF = rumore_max + 0.1×(evento_min − rumore_max)

Non c'è un valore universale corretto: dipende dal tensionamento della tua fibra e dal rumore ambientale reale. Regolala sul tuo sistema, non fidarti dei default.

**4. Scegli la classe** dal menu a tendina ("Cosa stai per registrare?"). La fibra è fissa (`fibra1`, mostrata in sola lettura nel pannello) — non c'è più selezione multi-fibra, dato che è già stata scelta la fibra da usare.

**5. Premi "▶ AVVIA"**
- Se la classe è `RUMORE_AMBIENTALE`: registra a blocchi continui finché non premi Ferma — usala per catturare minuti di silenzio/rumore di fondo reale.
- Per tutte le altre classi: resta in ascolto e cattura automaticamente ogni evento che fa scattare lo STA/LTA, senza bisogno di ripremere il pulsante ogni volta. Genera l'evento fisico (cammina, batti un colpo, ecc.) e guarda il log a destra: ogni cattura salvata appare con durata e un eventuale avviso di clipping.

**6. Cambia classe quando vuoi passare al prossimo tipo di evento** — il sistema si ferma automaticamente e va ripremuto Avvia (evita di mescolare classi per errore).

**7. A fine sessione**, chiudi la finestra: il file WAV continuo viene chiuso correttamente.

---

## PARTE 2 — Modalità Riconoscimento (classificazione automatica)

Dal menu "Modalità" in alto a destra, seleziona *Riconoscimento (automatico, leggero)*. Richiede che `modello_classificatore.pkl` esista già (allenato con `train_classifier.py`) — se manca, l'app te lo segnala nel log e resta in modalità Dataset.

**Cosa cambia rispetto alla modalità Dataset:**
- Spettrogramma e grafico storico STA/LTA scompaiono e smettono di essere calcolati (non solo disegnati) — più leggero per la CPU.
- Non scegli più una classe: il sistema classifica da solo ogni evento catturato dal trigger STA/LTA.
- Ogni evento riconosciuto viene salvato in `eventi_riconosciuti/<classe_predetta>/` (WAV + log strutturato `riconoscimenti_log.jsonl`), separato dal dataset di training — questi non sono campioni etichettati a mano, sono predizioni del modello.
- Sotto il 75% di confidenza, l'evento va nella cartella `SCONOSCIUTO_DA_VERIFICARE` invece che forzato in una classe nota.

Premi **"▶ AVVIA"** come al solito — da qui in poi ascolta e classifica senza altro intervento. Nel log vedrai una riga per evento, tipo:
```
🔎 14:32:07  COLPO_IMPATTO              91%
```

Cambiare le soglie STA/LTA (spinbox ON/OFF) funziona anche in questa modalità, con lo stesso effetto immediato.

## PARTE 3 — Codici per training e classificazione (cosa fa ognuno)

| File | Ruolo |
|---|---|
| `audio_features.py` | Estrae ~20 feature (RMS, picco, zero-crossing, centroide spettrale, energia per banda, ecc.) da un evento audio. Usato sia dal training sia dall'inferenza — stesso codice, garanzia che non ci sia disallineamento. |
| `train_classifier.py` | Legge tutti i WAV in `dataset_sensori_24kHz/`, estrae le feature, allena **sia** Random Forest **sia** SVM RBF (con grid search sugli iperparametri), e salva il migliore dei due per F1 macro in `modello_classificatore.pkl`. Esclude automaticamente le classi con meno di 8 esempi. |
| `realtime_inference.py` | Carica `modello_classificatore.pkl` e classifica un buffer audio in tempo reale. Sotto il 75% di confidenza restituisce `SCONOSCIUTO_DA_VERIFICARE` invece di forzare una classe nota. Usato dalla modalità Riconoscimento di `registratore_stalta.py`. |

> **Nota**: esiste anche `riconoscimento_realtime.py`, uno script indipendente creato in un'iterazione precedente (processo separato, senza interfaccia grafica). È stato superato dalla modalità Riconoscimento integrata sopra, che risolve lo stesso problema (leggerezza) senza duplicare la logica STA/LTA in due file diversi. Puoi tenerlo se in futuro ti serve davvero un processo indipendente dal registratore (es. su un'altra macchina), altrimenti la modalità integrata è la via consigliata.

---

## PARTE 4 — Flusso completo, dall'inizio

**Fase A — Raccolta dati (laboratorio, poi campo)**
1. Installa le dipendenze (Parte 0).
2. Avvia `registratore_stalta.py` in modalità Dataset, calibra STA/LTA (Parte 1, punto 3).
3. Registra un primo giro di prova di tutte le classi in `config_soglie_ia.json`, giusto per verificare che tutto funzioni end-to-end (qualche decina di esempi totali bastano per questo test).

**Fase B — Primo training di prova**
```bash
python train_classifier.py --dataset dataset_sensori_24kHz --out modello_classificatore.pkl
```
Con pochi esempi il risultato sarà debole — è normale e atteso, serve solo a verificare che lo script giri senza errori e a leggere il report (precision/recall per classe, matrice di confusione) per farsi un'idea di quali classi si confondono.

**Fase C — Test della classificazione dal vivo**
Passa a modalità Riconoscimento dal menu a tendina (Parte 2), premi Avvia, genera un evento e verifica che appaia una riga con classe e confidenza nel log.

**Fase D — Reset e passaggio al campo (come deciso)**
Quando la pipeline sopra funziona senza errori:
```bash
rm -rf dataset_sensori_24kHz eventi_riconosciuti
rm -f modello_classificatore.pkl
```
(o semplicemente cancella/svuota manualmente quelle cartelle/file) e riparti con la raccolta vera sul campo, seguendo la roadmap in `planning_progetto_fibra.md`.

**Fase E — Ciclo settimanale in campo**
1. Raccogli dati con `registratore_stalta.py` in modalità Dataset (Parte 1).
2. Ogni settimana circa, riallena: `python train_classifier.py`.
3. Guarda il report (F1 per classe, matrice di confusione, quale dei due modelli ha vinto) per capire dove il dataset è debole.
4. Passa a modalità Riconoscimento per scovare esempi ambigui o predizioni sbagliate da rivedere.
5. Ripeti, orientando la raccolta successiva verso le classi/condizioni più deboli.

**Fase F — Quando il dataset è abbastanza grande**
Passaggio alla CNN su spettrogramma log-mel con transfer learning, come da roadmap (settimane 5-8) — codice da preparare quando ci arriviamo, perché l'architettura dipenderà dai numeri reali del dataset a quel punto.