# Progetto: Sistema di ascolto e classificazione eventi su fibra ottica (interferometro Sagnac)

## Obiettivo del progetto
Sistema che ascolta e classifica eventi (intrusioni, passi, rumori) rilevati da una fibra ottica
collegata a un interferometro. Il segnale digitale è già acquisito e pronto da elaborare.
Obiettivo: alte prestazioni con approccio deep learning, bassa tolleranza a perdere eventi reali
(anche a costo di più falsi allarmi). Il modello girerà in produzione sullo stesso PC/server usato
per il training (nessun vincolo embedded).

## Struttura del progetto

- `CONFRONTO FIBRE/` — flusso di confronto oggettivo tra le fibre ottiche (quale fibra "sente"
  meglio), indipendente dal flusso ML/DL. Cartella "pulita", attiva.
  - `1_registratore.py` — GUI (PySide6 + pyqtgraph) che riceve il segnale via UDP (24kHz, pacchetti
    da 254 campioni int16, porta 12345), mostra forma d'onda e spettrogramma in tempo reale,
    registra dataset WAV etichettati per classe/fibra (cattura a soglia RMS o a durata fissa).
  - `2_confronta_fibre.py` — confronto fibre su eventi brevi (RMS, crest factor, clipping, SNR
    per banda, punteggio composito).
  - `3_analizza_sweep.py` — confronto fibre banda per banda con segnale sweep (allineamento via
    cross-correlazione, SNR per banda, ripetibilità).
  - `readme.md` / `ISTRUZIONI FLUSSO` — documentazione tecnica e flusso operativo di questi 3 script.
  - `OLD CONFRONTO/` — training IA vecchio (addestratore Random Forest, analizzatore spettrale,
    classificatore live, modello .pkl). ARCHIVIATO: non toccare, sostituito dal flusso in `MLtoDL/`.
- `MLtoDL/` — pipeline ML/DL attiva per il riconoscimento eventi:
  - `0registratoreStalta.py` — script unico con due modalità selezionabili da interfaccia:
    - **Dataset**: raccolta ed etichettatura campioni, trigger STA/LTA (sostituisce la vecchia
      soglia RMS fissa) con isteresi ON/OFF, tempo di riarmo, controllo clipping, log strutturato
      (`eventi_log.jsonl`).
    - **Riconoscimento**: classificazione automatica in tempo reale; spettrogramma e grafico
      storico STA/LTA disattivati (calcolo fermato, non solo nascosto) per alleggerire la CPU.
  - `audio_features.py` — estrazione feature audio, condivisa da training e inferenza.
  - `train_classifier.py` — training Random Forest + SVM (grid search), salva
    `modello_classificatore.pkl`.
  - `realtime_inference.py` — inferenza in tempo reale usata dalla modalità Riconoscimento.
  - `plan.md` — planning di progetto a 3 fasi (Laboratorio → Pretest campo → SAFE).
  - `guidaoperativa.md` — manuale d'uso passo-passo di tutta la pipeline.
  - Dataset (`dataset_sensori_24kHz/`), modello (`modello_classificatore.pkl`) e log operativi
    (`eventi_log.jsonl`, `riconoscimenti_log.jsonl`) vivono qui; gli script KPI in `KPI/` li
    leggono da qui ma non li possiedono.
- `MLtoDLseparateFasiSUPERATO/` — ARCHIVIATO: versione precedente della pipeline sopra, con la
  modalità di riconoscimento realizzata come script separato (`riconoscimento_realtime.py`)
  invece che integrata nel registratore. Non toccare, se non serve davvero un processo di
  riconoscimento indipendente dal registratore (es. su un'altra macchina).
- `KPI/` — tutta la logica di valutazione KPI, oltre al tracking: `sensor_integrity_verification.py`,
  `calcola_kpi.py`, `curva_apprendimento.py`, `valuta_modello.py` (ognuno calcola i propri percorsi
  in base alla posizione del file, non alla cwd, e legge dataset/modello da `../MLtoDL/`), oltre a
  `kpi_calibration_log.jsonl` (scritto direttamente qui da `MLtoDL/0registratoreStalta.py`),
  `kpi_tracking.xlsx` (valori misurati per fase), `PROTOCOLLOkpi.MD` (procedura dettagliata per
  ciascun KPI), `listaScriptUtiliPerKPI.md` (mappa KPI → script).

> Nessuna cartella `arduino/` è attualmente presente nel repository (multiplexing tappeti/vibratori
> — da aggiungere qui se/quando il codice viene versionato).

## Fasi del progetto
1. **Laboratorio**: segnali/energie non rappresentativi, serve solo a testare l'apparato ML.
2. **Pretest in campo** (giardino dell'utente ora, campo reale a ottobre con fibra più lunga).
3. **Test ufficiale al centro SAFE**: qui la parte ML deve essere matura/affidabile; per la parte
   DL bastano step e limiti chiari anche se non completa.

Integrazione ML in due fasi:
- **TRAINING 1**: tappeto singolo, senza multiplexing. Riconosce: passi, motore, vibratore1.
- **TRAINING 2**: a valle di ML + multiplexing Arduino, identifica quale tappeto (1, 2, 3...) ha
  rilevato l'evento in base al segnale Arduino.

## Classi di eventi
- Classi di test attuali (da rivedere: alcune da tenere, altre da cambiare): battito mani, fischio,
  corsa, passi lenti, rumore ambientale.
- Nuova classe aggiunta ai pre-test: "gruppo che passeggia/corre".

## Note tecniche importanti (da NON perdere)
- Il dispositivo/interrogatore non invia un flusso UDP uniforme: bufferizza e invia a raffiche di
  ~16 pacchetti quasi simultanei, con pausa strutturale di ~169ms tra una raffica e l'altra.
  Comportamento del dispositivo, non modificabile — va gestito lato software.
- Problema noto, da NON risolvere ora ma tenere presente: i passi lenti non danno un picco alto in
  ampiezza (energia concentrata nelle basse frequenze). Con STA/LTA a banda larga, rumore di fondo
  e passi lenti possono avere rapporti simili, non separabili con nessuna soglia.
- Test di confronto fibre: motorino vibrante a 5m — fibra1 lo rilevava già con soglia a 2000,
  sulle altre fibre soglia abbassata a 250 per rilevarlo.

## Convenzioni e regole per Claude Code
- NON modificare nulla dentro `CONFRONTO FIBRE/OLD CONFRONTO/` o `MLtoDLseparateFasiSUPERATO/`:
  è materiale archiviato, sostituito da versioni più recenti nelle rispettive cartelle attive.
- Prima di modifiche strutturali, fare commit git dello stato funzionante corrente.
- Mantenere separati il flusso `CONFRONTO FIBRE/` e il flusso `MLtoDL/` (training/ML): non
  mescolare logica o file tra le due aree.
- Il file `MLtoDL/0registratoreStalta.py` è unico con due modalità (Dataset/Riconoscimento):
  evitare di duplicare la logica STA/LTA in script separati.
- Gli script KPI (`sensor_integrity_verification.py`, `calcola_kpi.py`, `curva_apprendimento.py`,
  `valuta_modello.py`) vivono in `KPI/`, non in `MLtoDL/`. Se modifichi i loro percorsi di default,
  mantieni il pattern `Path(__file__).resolve().parent` (mai percorsi relativi alla cwd) — è quello
  che li rende eseguibili da qualunque cartella.

## Da completare
- [ ] Correggere i percorsi reali delle cartelle sopra elencate
- [ ] Aggiungere requirements.txt / dipendenze (Python, librerie usate)
- [ ] Aggiungere comando per avviare l'app di acquisizione e lo script di training