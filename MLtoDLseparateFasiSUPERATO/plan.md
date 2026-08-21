# Sistema di riconoscimento eventi su fibra ottica (interferometro Sagnac)
## Planning di progetto — 3 fasi, entro 8 settimane

---

## 1. Obiettivo e vincoli di partenza

**Obiettivo:** costruire un sistema che ascolta il segnale proveniente dalla fibra ottica interrata (via interferometro Sagnac) e classifica automaticamente gli eventi rilevati (intrusioni, passi, rumori), con prestazioni di riconoscimento molto elevate.

**Vincoli e priorità dichiarati, che guidano ogni scelta successiva:**

| Vincolo | Implicazione |
|---|---|
| Fibra **interrata** | Definisce quali eventi sono fisicamente plausibili (niente "taglio recinzione", sì "scavo", "passi", "veicoli pesanti") |
| **2 mesi** di tempo | Non abbastanza per accumulare un dataset enorme da zero, ma sufficiente se la raccolta è strutturata fin dal giorno 1 |
| Inferenza su **PC/server a potenza piena** | Nessun vincolo di compressione/quantizzazione del modello: si può usare un'architettura grande e accurata |
| **Bassa tolleranza a mancare eventi reali** (falsi negativi), anche a costo di più falsi allarmi | La metrica guida non è l'accuratezza generica ma il **recall per classe di intrusione**; soglie di decisione e loss vanno sbilanciate di conseguenza |
| **Qualche ora al giorno** dedicabile alla raccolta sul campo | Volume di dati moderato ma non enorme: 2 mesi × ~2h/giorno ≈ 100-110 ore di raccolta attiva. Va usato bene, non sprecato in ripetizioni ridondanti |
| Vuole **deep learning**, non solo ML classico | Accettato come obiettivo finale, ma richiede una strategia per compensare la scarsità di dati nelle prime settimane (vedi §4) |

---

## 1bis. Struttura del progetto in 3 fasi, con criteri di uscita distinti

Il progetto è organizzato in **3 fasi con obiettivi e criteri di maturità diversi**, non un'unica progressione lineare. Questo è il criterio guida per ogni decisione su cosa fare/rimandare in ciascun momento.

### Fase 1 — Laboratorio
**Obiettivo:** validare tecnicamente l'intera catena (acquisizione → rilevamento evento → salvataggio dataset → estrazione feature → training → inferenza), **non** produrre dati per il modello finale. Segnali ed energie osservati in laboratorio non sono rappresentativi delle condizioni reali (accoppiamento della fibra, rumore di fondo, superfici diversi dal campo).
**Cosa NON è questa fase:** non è raccolta dataset definitiva. I dati di laboratorio vengono azzerati prima di iniziare la Fase 2 (vedi §4bis).
**Criterio di uscita:** la pipeline gira end-to-end senza errori — un evento generato in laboratorio viene catturato, salvato, usato per allenare un modello di prova, e classificato correttamente in modalità di riconoscimento automatico. Non serve accuratezza alta in questa fase, serve che il meccanismo funzioni.

### Fase 2 — Pretest in campo
**Obiettivo:** raccolta dati reale (quella che conta per il modello) nelle condizioni operative effettive (fibra interrata, ambiente esterno reale), e primo addestramento vero sia del modello ML classico sia, quando il volume lo consente, del modello DL.
**Criterio di uscita:** dataset sufficientemente ampio e vario (più sessioni, condizioni diverse — vedi rischi §8), primo ciclo di training/valutazione completato con metriche misurate (non stimate), limiti noti del sistema identificati e documentati (es. la questione passi lenti, §4quater).

### Fase 3 — Test ufficiale al centro SAFE
**Obiettivo:** validazione formale del sistema. Questa fase ha un **criterio di maturità differenziato tra ML e DL**, esplicitamente stabilito:
- **La parte ML (classica) deve essere matura e affidabile**: prestazioni misurate e riproducibili, gestione della classe sconosciuta, soglie di decisione tarate, tasso di falsi allarmi noto e documentato. Non deve essere un prototipo — deve reggere una valutazione formale.
- **La parte DL può non essere completa**, ma deve avere: architettura scelta e motivata, step di sviluppo chiari (cosa è stato fatto, cosa resta), limiti noti dichiarati esplicitamente (non scoperti durante il test). È accettabile presentare DL come "lavoro in corso con roadmap chiara", non è accettabile presentarlo come opaco o non testato.

Questo criterio conferma la strategia già adottata (§2): usare ML classico come primo prodotto solido e DL come obiettivo a maturazione progressiva è esattamente ciò che questo criterio richiede, non un ripiego.

---

## 2. Perché non si parte subito con una rete profonda

Con un dataset che parte da **zero campioni** e cresce di poche ore al giorno, allenare una CNN da zero nelle prime settimane produrrebbe un modello che memorizza il rumore invece di imparare il pattern (overfitting), perché le reti profonde hanno bisogno di molti esempi per generalizzare. Questo non è un ripiego rispetto all'obiettivo "deep learning": è il percorso che permette di *arrivarci* con un modello che funziona davvero, invece che uno che sembra funzionare solo sui dati di test.

La soluzione adottata è un **percorso a due binari**, non due fasi sequenziali:

- Un modello **classico (Random Forest)**, riallenato ogni settimana sui dati accumulati fino a quel momento, usato come **strumento di lavoro** durante la raccolta: segnala quali classi si confondono, quali esempi sono ambigui, dove il dataset è debole. Non è il prodotto finale.
- Il modello **deep (CNN su spettrogramma + transfer learning)**, introdotto quando il volume di dati lo giustifica (indicativamente dalla settimana 5), che diventa il sistema di produzione.

---

## 3. Tassonomia delle classi

Motivazione: per una fibra interrata in ottica di sicurezza perimetrale, la tassonomia deve coprire sia gli **eventi target** (cosa vogliamo riconoscere) sia i **disturbi** (cosa deve imparare a ignorare, altrimenti genera falsi allarmi continui). Le classi "battito mani"/"fischio" del sistema di test attuale sono utili per calibrazione ma non sono eventi realistici da intrusione: vengono sostituite.

**Classi target (eventi di interesse):**
- `PASSI_LENTI` — camminata cauta/furtiva
- `PASSI_CORSA` — passi veloci/corsa
- `SCAVO` — attività di scavo nei pressi della fibra
- `VEICOLO_TRANSITO` — passaggio di veicolo sopra o vicino al tracciato
- `COLPO_IMPATTO` — impatto singolo (es. oggetto lasciato cadere, colpo di attrezzo)
- `MANIPOLAZIONE_FIBRA` — manomissione diretta della fibra/pozzetto, evento di sicurezza ad alta priorità
- `VOCE` — voci umane nei pressi della fibra (spesso correlate a presenza di persone, va distinta dal semplice rumore)

**Classi di disturbo (da imparare a ignorare):**
- `RUMORE_AMBIENTALE` — silenzio/rumore di fondo
- `VENTO_PIOGGIA` — condizioni meteo
- `ANIMALE` — passaggio di animali di piccola/media taglia

**Classe di sicurezza:**
- `SCONOSCIUTO_DA_VERIFICARE` — catch-all per eventi che superano la soglia di rilevamento ma non assomigliano con sufficiente confidenza a nessuna classe nota. Coerente con la priorità "non perdere eventi": invece di forzare una classificazione tra le classi note (rischiando di scartare come rumore un evento mai visto), il sistema segnala per revisione umana.

Questa lista è un punto di partenza per la settimana 1, non definitiva: verrà raffinata sulla base di cosa il modello classico settimanale mostra confondersi.

---

## 4. Perché il trigger di cattura va cambiato (soglia fissa → STA/LTA)

Il sistema attuale cattura un evento quando l'RMS del segnale supera una soglia fissa impostata manualmente. Questo è in **diretto conflitto con la priorità "non perdere eventi reali"**: un evento debole (es. passi cauti, a distanza dalla fibra) potrebbe non superare mai una soglia assoluta, e in tal caso non arriva nemmeno al classificatore — indipendentemente da quanto sia bravo il modello a valle. Inoltre una soglia fissa non si adatta a un rumore di fondo che cambia nel tempo (vento, traffico, condizioni meteo).

La soluzione proposta è **STA/LTA (Short-Term/Long-Term Average)**, tecnica standard in sismologia per il rilevamento di eventi transienti deboli su rumore di fondo variabile:
- si calcola una media mobile a breve termine (STA, es. 100ms) e una a lungo termine (LTA, es. 3s) della potenza del segnale
- il rapporto STA/LTA si alza rapidamente quando arriva un evento, indipendentemente dal livello assoluto del rumore di fondo
- un trigger scatta quando il rapporto supera una soglia "on", e si chiude quando scende sotto una soglia "off" più bassa (isteresi, evita che il trigger si apra/chiuda a scatti sullo stesso evento)

Questo sostituisce la soglia RMS fissa **solo per la modalità di cattura a soglia**; la modalità "durata fissa" (usata per gli sweep di calibrazione) resta invariata perché serve a uno scopo diverso.

---

## 4bis. Fase 1 (laboratorio): solo validazione tecnica, non dataset finale

Coerente con la definizione di Fase 1 in §1bis: i dati di laboratorio non entrano nel dataset finale. Nessuna gestione di metadati "laboratorio vs campo": quando il codice è verificato, la cartella `dataset_sensori_24kHz/` (e il log eventi) viene svuotata e si riparte da zero con la sola raccolta sul campo (Fase 2). Motivazione: un modello allenato anche parzialmente su dati di laboratorio rischia di imparare caratteristiche dell'ambiente di laboratorio (accoppiamento della fibra, rumore di fondo, superfici) che non si ritrovano nel campo reale — più semplice ripartire puliti che pesare/filtrare due popolazioni di dati miste.

## 4ter. Miglioramenti aggiuntivi integrati nel registratore

Oltre al trigger STA/LTA, sono stati integrati nello script (`registratore_stalta.py`):
- **Tempo di riarmo** (`TEMPO_RIARMO_SEC`): dopo la chiusura di un evento, i nuovi trigger vengono ignorati per un breve periodo, per evitare che lo stesso evento fisico prolungato generi più catture separate.
- **Controllo clipping**: ogni evento salvato viene controllato per campioni vicini al fondo scala (±32768); il WAV grezzo non viene mai alterato, ma l'evento viene segnalato nel log strutturato per essere escluso o rivisto in fase di training.
- **Log strutturato degli eventi** (`eventi_log.jsonl`): un record JSON per ogni evento catturato (timestamp, fibra, classe, durata, RMS, picco, clipping, percorso file), utile sia per analisi successive sia come base per il futuro campo "confidenza/modello" quando sarà integrato il classificatore.

## 4quater. Limitazioni note e miglioramenti futuri (non da risolvere ora)

**STA/LTA a banda larga non separa i passi lenti dal rumore di fondo.** Verificato empiricamente: il livello di rumore a riposo e il livello raggiunto da un passo cauto/lento possono risultare quasi uguali nel rapporto STA/LTA calcolato sull'energia a banda larga (tutte le frequenze insieme). Non è un problema di calibrazione delle soglie: un passo lento non produce un picco alto in ampiezza come un colpo, la sua energia si concentra piuttosto in una banda di frequenze basse specifica — mescolata con tutte le altre frequenze nel calcolo a banda larga, il contributo del passo si "annega" nel resto del rumore.

**Direzione di soluzione futura, non implementata ora:** invece di calcolare STA/LTA sull'energia dell'intero segnale, calcolarlo su una versione **filtrata in banda** (filtro passa-banda alle frequenze dove si concentra l'energia dei passi lenti — da determinare empiricamente, verosimilmente nella parte bassa dello spettro visibile nello spettrogramma già presente nell'app). Questo isolerebbe il contributo specifico dei passi lenti dal rumore a banda larga, rendendo il rapporto STA/LTA più discriminante per questa classe specifica. Implicazione architetturale: potrebbe servire più di un rilevatore STA/LTA in parallelo, ciascuno calibrato su una banda diversa per classi di eventi con firme in frequenza diverse (es. uno a banda larga per eventi impulsivi come colpi, uno a banda bassa per passi lenti/scavo), invece di un unico rilevatore generico.

**Perché non risolverlo ora:** aggiunge complessità (filtraggio in banda, calibrazione di più rilevatori) prima di sapere se serve davvero — con il dataset ancora da raccogliere, non è chiaro se in pratica questo limite impatta la raccolta reale o resta un caso limite di laboratorio. Da rivalutare quando il dataset reale mostrerà se i passi lenti vengono effettivamente persi o catturati a sufficienza con il compromesso attuale (soglia bassa, più falsi allarmi accettati).

## 5. Architettura tecnica del sistema (target finale)

```
Fibra + interferometro Sagnac
        │
        ▼
Acquisizione UDP (esistente, invariata)
        │
        ▼
Rilevamento evento — STA/LTA (nuovo, sostituisce soglia RMS fissa)
        │  con pre-roll (contesto prima del trigger, dal buffer circolare già esistente)
        ▼
Buffer evento ritagliato
        │
        ├──► Salvataggio dataset etichettato (esistente, per continuare a raccogliere)
        │
        └──► Classificatore
                 Settimane 1-4: Random Forest (strumento diagnostico)
                 Settimane 5-8: CNN su spettrogramma log-mel + transfer learning
                       │
                       ▼
              Decisione con soglia orientata al recall
              (classe + confidenza; sotto una certa confidenza → SCONOSCIUTO_DA_VERIFICARE)
```

---

## 6. Roadmap, mappata sulle 3 fasi

| Fase | Settimana indicativa | Attività | Criterio di uscita |
|---|---|---|---|
| **1 — Laboratorio** | 1 (durata variabile: si esce solo a validazione riuscita, non per calendario) | Tassonomia definitiva, trigger STA/LTA integrato e calibrato, verifica end-to-end (cattura → training di prova → riconoscimento) | Pipeline funzionante, dataset di laboratorio azzerato |
| **2 — Pretest in campo** | 2-3 | Raccolta dati intensiva su classi target primarie (passi, corsa, scavo), prime sessioni di disturbo (rumore ambientale, vento) | Primo dataset reale utilizzabile |
| **2 — Pretest in campo** | 4 | Primo training Random Forest/SVM, analisi confusioni, identificazione classi/condizioni sotto-rappresentate | Report diagnostico, piano raccolta correttivo |
| **2 — Pretest in campo** | 5-6 | Raccolta mirata sulle debolezze emerse, ampliamento condizioni (giorno/notte, meteo diverso), prime prove CNN + transfer learning | Modello ML maturo in valutazione; modello DL in validazione iniziale |
| **3 — Preparazione SAFE** | 7 | Validazione ML su sessioni "nuove" (non viste in training) per stima realistica delle prestazioni, tuning soglia di decisione per il recall target; DL: consolidamento di step e limiti noti | ML: metriche di prestazione realistiche e riproducibili. DL: roadmap e limiti documentati |
| **3 — Preparazione SAFE** | 8 | Hardening ML (gestione classe sconosciuta, tasso di falsi allarmi giornaliero), documentazione finale per entrambi i modelli | Sistema pronto per il test ufficiale: ML maturo, DL con step/limiti chiari anche se non finale |

---

## 7. Metriche di successo, differenziate per fase 3 (SAFE)

Data la priorità dichiarata, la metrica principale non è l'accuratezza complessiva ma:
- **Recall per classe target** (in primis eventi di intrusione): quota di eventi reali correttamente rilevati, la metrica critica da massimizzare
- **Precision** monitorata ma secondaria: si accetta un tasso più alto di falsi allarmi pur di non perdere eventi
- **Tasso di falsi allarmi/giorno** in condizioni di solo rumore di fondo, per capire il costo operativo della scelta di privilegiare il recall
- **Matrice di confusione** per capire quali classi si scambiano tra loro, non solo un numero aggregato

**Per la Fase 3 (SAFE) nello specifico, coerente con §1bis:**
- **Modello ML**: tutte le metriche sopra devono essere misurate su dati di campo reali (non di laboratorio), riproducibili, e accompagnate da soglie di decisione tarate e documentate.
- **Modello DL**: non è richiesto che raggiunga le stesse metriche del ML per superare la fase — è richiesto che l'architettura, gli step compiuti, e i limiti noti (es. quantità di dati ancora insufficiente per una certa classe) siano dichiarati esplicitamente e chiaramente prima del test, non scoperti durante.

---

## 8. Rischi principali

- **Dataset sbilanciato o poco vario**: se la raccolta si concentra su poche condizioni (es. solo di giorno, solo asciutto), il modello generalizzerà male. Mitigazione: protocollo di raccolta che impone varietà fin dalla Fase 2, non solo quantità.
- **Overfitting con CNN su dataset ancora piccolo**: mitigato da transfer learning + data augmentation (SpecAugment) + monitoraggio costante su un validation set separato per sessione (non per singolo campione, per evitare che campioni della stessa sessione finiscano sia in train che in test).
- **STA/LTA troppo sensibile**: con soglie troppo basse si rischiano moltissimi falsi trigger su rumore di fondo. Va calibrato empiricamente in Fase 1 prima della raccolta massiva, altrimenti si accumula dataset rumoroso.
- **Confondere i criteri di maturità di ML e DL alla Fase 3**: rischio organizzativo, non tecnico — se il DL viene presentato al SAFE con lo stesso livello di aspettativa del ML (invece che come "roadmap chiara, limiti noti"), il criterio di successo della fase non è quello concordato. Va comunicato con chiarezza prima del test, non durante.