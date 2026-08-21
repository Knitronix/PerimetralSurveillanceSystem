# Guida operativa — Sistema di riconoscimento eventi su fibra Sagnac

File necessari, tutti nella **stessa cartella**:

```
registratore_stalta.py
audio_features.py
train_classifier.py
realtime_inference.py
config_soglie_ia.json
```

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

Non c'è un valore universale corretto: dipende dal tensionamento della tua fibra e dal rumore ambientale reale. Regolala sul tuo sistema, non fidarti dei default.

**4. Scegli la classe** dal menu a tendina ("Cosa stai per registrare?"). La fibra è fissa (`fibra1`, mostrata in sola lettura nel pannello) — non c'è più selezione multi-fibra, dato che è già stata scelta la fibra da usare.

**5. Premi "▶ AVVIA REGISTRAZIONE"**
- Se la classe è `RUMORE_AMBIENTALE`: registra a blocchi continui finché non premi Ferma — usala per catturare minuti di silenzio/rumore di fondo reale.
- Per tutte le altre classi: resta in ascolto e cattura automaticamente ogni evento che fa scattare lo STA/LTA, senza bisogno di ripremere il pulsante ogni volta. Genera l'evento fisico (cammina, batti un colpo, ecc.) e guarda il log a destra: ogni cattura salvata appare con durata e un eventuale avviso di clipping.

**6. Cambia classe quando vuoi passare al prossimo tipo di evento** — il sistema si ferma automaticamente e va ripremuto Avvia (evita di mescolare classi per errore).

**7. A fine sessione**, chiudi la finestra: il file WAV continuo viene chiuso correttamente.

---

## PARTE 2 — Riconoscimento in tempo reale come processo separato

`riconoscimento_realtime.py` è uno script indipendente, senza interfaccia grafica: ascolta il segnale, rileva eventi con STA/LTA e li classifica, stampando solo una riga per evento riconosciuto. Pensato per essere più leggero del registratore completo (niente spettrogramma/plotting) e per non richiedere di scegliere una classe manualmente.

**Può girare insieme al registratore o da solo** — il dispositivo trasmette in broadcast, quindi entrambi i processi possono ricevere gli stessi dati senza conflitti sulla porta.

**Uso:**
```bash
python riconoscimento_realtime.py
```
Richiede che `modello_classificatore.pkl` esista già (allenato con `train_classifier.py`). Legge le soglie STA/LTA da `soglie_stalta.json` — file scritto automaticamente dal registratore ogni volta che ricalibri ON/OFF lì. Se ricalibri le soglie nel registratore mentre questo script è già in esecuzione, **riavvialo** per usare i valori aggiornati (non li rilegge mentre gira).

Ogni evento riconosciuto viene salvato in `eventi_riconosciuti/` (WAV + log strutturato `riconoscimenti_log.jsonl`), separato dal dataset di training — questi non sono campioni etichettati manualmente, sono predizioni del modello.

Ferma con Ctrl+C.

## PARTE 3 — Codici per training e classificazione (cosa fa ognuno)

| File | Ruolo |
|---|---|
| `audio_features.py` | Estrae ~20 feature (RMS, picco, zero-crossing, centroide spettrale, energia per banda, ecc.) da un evento audio. Usato sia dal training sia dall'inferenza — stesso codice, garanzia che non ci sia disallineamento. |
| `train_classifier.py` | Legge tutti i WAV in `dataset_sensori_24kHz/`, estrae le feature, allena **sia** Random Forest **sia** SVM RBF (con grid search sugli iperparametri), e salva il migliore dei due per F1 macro in `modello_classificatore.pkl`. Esclude automaticamente le classi con meno di 8 esempi. |
| `realtime_inference.py` | Carica `modello_classificatore.pkl` e classifica un buffer audio in tempo reale. Sotto il 75% di confidenza restituisce `SCONOSCIUTO_DA_VERIFICARE` invece di forzare una classe nota. |
| `riconoscimento_realtime.py` | Processo separato e leggero (nessuna interfaccia grafica): ascolta, rileva eventi con STA/LTA e li classifica in automatico, senza bisogno di scegliere una classe. Vedi Parte 2 — è l'unico modo per classificare in tempo reale, il registratore non lo fa più. |

---

## PARTE 4 — Flusso completo, dall'inizio

**Fase A — Raccolta dati (laboratorio, poi campo)**
1. Installa le dipendenze (Parte 0).
2. Avvia `registratore_stalta.py`, calibra STA/LTA (Parte 1, punto 3).
3. Registra un primo giro di prova di tutte le classi in `config_soglie_ia.json`, giusto per verificare che tutto funzioni end-to-end (qualche decina di esempi totali bastano per questo test).

**Fase B — Primo training di prova**
```bash
python train_classifier.py --dataset dataset_sensori_24kHz --out modello_classificatore.pkl
```
Con pochi esempi il risultato sarà debole — è normale e atteso, serve solo a verificare che lo script giri senza errori e a leggere il report (precision/recall per classe, matrice di confusione) per farsi un'idea di quali classi si confondono.

**Fase C — Test della classificazione dal vivo**
```bash
python riconoscimento_realtime.py
```
Ascolto e classificazione senza etichettatura manuale (vedi Parte 2). Genera un evento e verifica che appaia una riga con classe e confidenza.

**Fase D — Reset e passaggio al campo (come deciso)**
Quando la pipeline sopra funziona senza errori:
```bash
rm -rf dataset_sensori_24kHz
rm -f dataset_sensori_24kHz/eventi_log.jsonl modello_classificatore.pkl
```
(o semplicemente cancella/svuota manualmente quelle cartelle/file) e riparti con la raccolta vera sul campo, seguendo la roadmap dell'8 settimane in `planning_progetto_fibra.md`.

**Fase E — Ciclo settimanale in campo**
1. Raccogli dati con `registratore_stalta.py` (Parte 1).
2. Ogni settimana circa, riallena: `python train_classifier.py`.
3. Guarda il report (F1 per classe, matrice di confusione, quale dei due modelli ha vinto) per capire dove il dataset è debole.
4. Attiva la classificazione automatica dal vivo per scovare esempi ambigui o predizioni sbagliate da rivedere.
5. Ripeti, orientando la raccolta successiva verso le classi/condizioni più deboli.

**Fase F — Quando il dataset è abbastanza grande**
Passaggio alla CNN su spettrogramma log-mel con transfer learning, come da roadmap (settimane 5-8) — codice da preparare quando ci arriviamo, perché l'architettura dipenderà dai numeri reali del dataset a quel punto.