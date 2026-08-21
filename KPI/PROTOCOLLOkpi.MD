# Protocollo di misurazione KPI — riutilizzabile a fine Fase 1 / Fase 2 / Fase 3

## Come leggere questo documento

Ogni KPI ha: **cosa misura**, **procedura passo-passo**, **strumento** (script, se automatizzato), **quando è valido ripeterlo**, **come interpretare il numero**. La procedura è identica in ogni fase — cambia solo l'ambiente (laboratorio, campo, SAFE) e quindi il significato del numero ottenuto, non il modo di ottenerlo.

**Principio guida**: un KPI misurato in laboratorio (Fase 1) valida che *la procedura di misura funzioni*, non che *il sistema sia pronto*. Non confrontare mai un numero di Fase 1 con un requisito operativo — solo Fase 2/3 hanno segnali rappresentativi.

---

## 4.1 — Calibration time

**Cosa misura**: tempo dall'avvio del sistema al momento in cui è dichiarato "pronto" (calibrazione STA/LTA completata, sistema stabile).

**Ricorda che  dalla calibrazione STALTA dipende anche la probability of Detection 5.1....**

Per ora: prendi il max di un minuto di rumore ambientale = rumore_max;
poi prendi il max del tuo evento più debole possibile (es passo cauto) = evento_min.

ON = rumore_max + 0.3×(evento_min − rumore_max);

OFF = rumore_max + 0.1×(evento_min − rumore_max)


**Procedura**:
1. Avvia `registratore_stalta.py`.
2. Calibra le soglie STA/LTA come di consueto (osservazione min/max, eventi di prova).
3. Quando ritieni la calibrazione completa, premi il pulsante **"✅ Sistema Pronto"** (aggiunto nell'interfaccia — vedi sotto).
4. Chiudi l'app, ripeti da capo. **N = 10 ripetizioni**, in condizioni realistiche per la fase in corso (non velocizzare artificialmente la calibrazione).

**Strumento**: il pulsante logga automaticamente il tempo trascorso dall'avvio in `kpi_calibration_log.jsonl`. Al termine delle 10 ripetizioni:
```bash
python calcola_kpi.py calibration-time
```

**Output**: tempo medio ± deviazione standard, in secondi.

**Ripetibilità tra fasi**: identica. In Fase 2/3 la calibrazione sarà più complessa (condizioni di campo reali), quindi aspettati tempi più lunghi e più variabili — non è un peggioramento, è un dato più realistico.

---

## 4.3 — Sensor integrity verification

**Cosa misura**: continuità del segnale, livello di rumore, ampiezza — un self-test automatico prima di ogni sessione operativa.

**Procedura**:
1. Con il sistema collegato alla fibra e a riposo (nessun evento), lancia:
```bash
python sensor_integrity_verification.py --durata 30
```
2. Lo script ascolta per 30 secondi e verifica: pacchetti in arrivo senza interruzioni anomale, rumore di fondo entro un range plausibile (non saturo, non silenzio digitale sospetto = fibra scollegata), nessun clipping.

**Output**: PASS/FAIL per ciascun controllo + tempo di verifica totale.

**Quando ripeterlo**: prima di ogni sessione di raccolta/test, in ogni fase — è un controllo operativo di routine, non solo un KPI una tantum.

---

## 4.4 — AI baseline learning duration

**Cosa misura**: tempo di training e numero di campioni necessari perché le prestazioni del modello smettano di migliorare significativamente (plateau).

**Procedura**:
1. Con un dataset rappresentativo della fase corrente (**non** quello di laboratorio per le fasi successive — dataset reale), lancia:
```bash
python curva_apprendimento.py --dataset dataset_sensori_24kHz
```
2. Lo script allena il modello su frazioni crescenti del dataset (10%, 20%, ... 100%), misura F1 macro ad ogni step, e individua il punto di plateau (prima frazione oltre la quale il miglioramento scende sotto una soglia, di default 1%).

**Output**: ore di training totali + numero di campioni al plateau + grafico della curva (salvato come immagine).

**Nota per la Fase 1**: con il dataset di laboratorio (poche decine di esempi) il plateau non è significativo — serve solo a verificare che lo script funzioni. Il numero reale arriva in Fase 2, quando il dataset cresce settimana per settimana.

---

## 4.5 — Mission Ready time

**Cosa misura**: tempo totale dalla posa della fibra al sistema operativo e verificato.

**Formula**: `4.1 (calibrazione) + 4.3 (self-test) + tempo di posa fisica della fibra`

**Procedura**:
1. Cronometra manualmente il tempo di posa fisica (dalla prima operazione sul terreno alla fibra collegata e pronta all'accensione).
2. Somma con i risultati di 4.1 e 4.3 già misurati.
```bash
python calcola_kpi.py mission-ready --tempo-posa-minuti 45
```

**Output**: tempo totale end-to-end.

**Valido solo per**: PRE-TEST + SAFE (non ha senso in laboratorio, dove non c'è una vera posa sul terreno).

---

## 5.1 / 5.2 / 5.3 — Probability of Detection (persone / UGV / veicoli)

**Cosa misura**: quota di eventi reali generati che il sistema rileva effettivamente.

**Procedura**:
1. Pianifica una campagna di eventi controllati: per ogni evento, un operatore annota categoria, timestamp di inizio/fine, in un file `eventi_pianificati.csv` (formato sotto).
2. Genera fisicamente gli eventi pianificati sul/vicino al tracciato della fibra, con il sistema in acquisizione (modalità Dataset o Riconoscimento, non importa per questo KPI — quello che conta è che gli eventi vengano *catturati*, cioè che scatti il trigger STA/LTA).
3. A campagna conclusa:
```bash
python calcola_kpi.py pd --pianificati eventi_pianificati.csv --log dataset_sensori_24kHz/eventi_log.jsonl
```

**Formato `eventi_pianificati.csv`**:
```csv
event_id,categoria,timestamp_inizio,timestamp_fine
1,persona,2026-09-10T09:12:00,2026-09-10T09:12:08
2,veicolo,2026-09-10T09:15:30,2026-09-10T09:15:45
```

**Output**: `PD = eventi rilevati / eventi totali`, per categoria, con l'elenco degli eventi mancati (utile per capire *quali* condizioni sfuggono, non solo il numero aggregato).

**Valido solo per**: PRE-TEST + SAFE — in laboratorio gli eventi non sono abbastanza realistici perché il numero significhi qualcosa, ma la procedura va comunque provata almeno una volta in Fase 1 per verificare che lo script funzioni prima di andare sul campo.

---

## 5.5 — False Alarm Rate (FAR)

**Cosa misura**: quanti falsi allarmi al sistema in assenza di eventi reali.

**Procedura**:
1. Sistema in acquisizione passiva (armato, nessun evento reale generato) per un periodo noto e sufficientemente lungo (consigliato: almeno 2-4 ore per una stima stabile, non pochi minuti).
2. Annota inizio e fine del periodo "silenzioso".
3. Al termine:
```bash
python calcola_kpi.py far --log dataset_sensori_24kHz/eventi_log.jsonl --inizio 2026-09-10T14:00:00 --fine 2026-09-10T18:00:00
```

**Output**: numero di falsi allarmi/ora (ogni evento catturato durante il periodo silenzioso è per definizione un falso allarme).

**Valido solo per**: PRE-TEST + SAFE — in laboratorio il rumore di fondo non rappresenta le condizioni reali (vento, traffico, animali), quindi il numero non è trasferibile, ma di nuovo: prova la procedura già ora.

---

## 7.1 — Personnel classification accuracy

**Cosa misura**: accuracy/precision/recall per classe, su un test set tenuto separato dal training.

**Perchè?**
7.1 risponde alla domanda: "quando il sistema dice che un evento è di classe X, quanto spesso ha ragione — e quanti eventi di classe X reali riesce effettivamente a beccare?" Sono due domande diverse, ed è per questo che il KPI chiede tre numeri distinti per ogni classe (accuracy, precision, recall), non uno solo.

**Procedura**: già prodotto automaticamente da `train_classifier.py` ad ogni riaddestramento (classification report + matrice di confusione stampati a console). Per salvarlo come report formale riutilizzabile:
```bash
python valuta_modello.py --modello modello_classificatore.pkl --dataset dataset_sensori_24kHz
```

**Output**: file `report_kpi_7_1.json` con accuracy/precision/recall/F1 per classe e matrice di confusione.

**Nota importante**: il test set deve essere tenuto separato **per sessione**, non per singolo campione (vedi planning, rischio dataset) — altrimenti il numero è ottimisticamente falsato.

---

## 7.6 — Classification confidence (calibration curve)

**Cosa misura**: se la confidenza dichiarata dal modello corrisponde davvero alla probabilità di avere ragione (es. "quando dico 90%, ho ragione davvero il 90% delle volte?").

**Procedura**: stesso script di 7.1, stesso comando:
```bash
python valuta_modello.py --modello modello_classificatore.pkl --dataset dataset_sensori_24kHz
```

**Output**: file `report_kpi_7_6.json` con la curva di calibrazione (bin di confidenza vs accuratezza reale in quel bin) + grafico `calibration_curve.png`. Una curva vicina alla diagonale = modello ben calibrato; sistematicamente sopra la diagonale = il modello è più sicuro di quanto dovrebbe (rischioso, soprattutto vicino alla soglia di 75% usata per "sconosciuto").

---

## Checklist di fine fase (da eseguire identica ogni volta)

1. Verificare che tutti gli script KPI girino senza errori sul dataset/modello della fase appena conclusa.
2. Eseguire tutte le procedure applicabili alla fase (vedi colonna "valido per" di ciascun KPI sopra).
3. Registrare tutti i valori in `kpi_tracking.xlsx`, nella colonna della fase corrente.
4. Confrontare con la fase precedente (dove applicabile) e annotare variazioni significative nel foglio "Note" del tracker.
5. Se un KPI non è misurabile per mancanza di dati sufficienti, annotarlo esplicitamente come "N/D — motivo", non lasciarlo vuoto senza spiegazione.