# Project Q — Confronto Fibre Ottiche via Interferometro

## 1. Obiettivo

Un interferometro interroga fibre ottiche usate come sensori acustici/vibrazionali. Lo scopo è stabilire **quale fibra "sente" meglio**, confrontandole in modo oggettivo e ripetibile: si attacca una fibra alla volta, si registra lo stesso protocollo di test per ciascuna, poi si confrontano i risultati.

Tre programmi, tre compiti separati:

| File | Compito |
|---|---|
| `1_registratore.py` | Interfaccia grafica: riceve i dati dall'interferometro via UDP, li mostra in tempo reale (forma d'onda + spettrogramma), li salva su disco catalogati per fibra e tipo di segnale |
| `2_confronta_fibre.py` | Analisi da terminale: confronta le fibre su eventi brevi (battiti, fischi, rumore ambientale) |
| `3_analizza_sweep.py` | Analisi da terminale: confronta le fibre banda di frequenza per banda di frequenza, usando uno sweep come segnale di test |

---

## 2. `1_registratore.py`

### 2.1 Cosa fa

Riceve pacchetti UDP dall'interferometro (int16, 24kHz), li mostra in tempo reale su due grafici sincronizzati (forma d'onda e spettrogramma), e permette di registrare e catalogare campioni per fibra e classe di segnale (rumore ambientale, eventi a soglia, sweep a durata fissa).

### 2.2 Architettura interna

- **`NetworkWorker`** (thread separato): riceve i pacchetti UDP, li scrive nel wav grezzo continuo, li inoltra alla GUI. Non fa mai calcoli pesanti — solo I/O.
- **`SagnacDatasetBuilder`** (finestra principale, thread GUI): riceve i pacchetti dal worker, aggiorna i buffer, disegna i grafici, gestisce la cattura e il salvataggio.

### 2.3 Parametri di rete

| Parametro | Valore | Perché |
|---|---|---|
| `UDP_IP` / `UDP_PORT` | `0.0.0.0` / `12345` | Ascolta su tutte le interfacce, porta fissa dell'interferometro |
| `CAMPIONI_PER_PACCHETTO` | 254 | Campioni int16 per pacchetto UDP, definito dal firmware dell'interferometro |
| `SAMPLE_RATE` | 24000 | Frequenza di campionamento dell'interferometro |
| `PAUSA_TRA_RAFFICHE_S` | 0.169 s | Pausa strutturale nota tra una raffica di pacchetti e la successiva (misurata con Wireshark: il dispositivo bufferizza e invia a raffiche di ~16 pacchetti quasi simultanei, non un flusso uniforme) |
| `FATTORE_GAP_SOSPETTO` | 2.0 | Se il tempo tra due pacchetti supera 2× la pausa strutturale nota (quindi ~338ms), si sospetta una perdita di pacchetti reale e viene contata come "gap sospetto" nelle statistiche di rete a schermo |

**Correzione**: in una versione precedente la soglia era calcolata come 3× l'intervallo teorico tra un singolo pacchetto e il successivo (~254/24000s ≈ 10.6ms → soglia ~31.7ms), come se il flusso fosse uniforme. Non lo è: il dispositivo invia a raffiche con una pausa strutturale di ~169ms tra una raffica e l'altra, quindi quella soglia scattava un falso "gap sospetto" ad ogni singola pausa normale, anche in condizioni di rete perfette. La soglia ora è calibrata sulla pausa strutturale nota (misurata con Wireshark), non sull'intervallo teorico tra singoli pacchetti — un gap anomalo reale (es. 500ms) resta comunque rilevato.

### 2.4 Buffer di visualizzazione (forma d'onda)

- **`SECONDI_VISIBILI = 15`**: quanti secondi di storico mostra il grafico.
- **`STORICO_CAMPIONI = SAMPLE_RATE × SECONDI_VISIBILI`** (360.000 campioni): dimensione del buffer.
- Il buffer è un array numpy fisso con un **puntatore di scrittura circolare** (`write_pos`): ogni pacchetto scrive solo i propri 254 campioni nella posizione corrente, invece di ricopiare l'intero array (come farebbe `np.roll`). La "linearizzazione" per il disegno avviene solo al momento del refresh (**`FPS_GRAFICO = 20`** volte al secondo), non ad ogni pacchetto (~94 volte al secondo) — è la scelta che rende fluida la visualizzazione.

### 2.5 Spettrogramma

| Parametro | Valore | Perché / come si calcola |
|---|---|---|
| `NPERSEG_SPETTROGRAMMA` | `SAMPLE_RATE` (24000) | Finestra FFT di 1 secondo intero → risoluzione in frequenza = SAMPLE_RATE/NPERSEG = **esattamente 1Hz** |
| `MAX_FREQ_VISUALIZZATA` | 6000 Hz | Range mostrato sull'asse Y |
| `HOP_SPETTROGRAMMA_CAMPIONI` | 2400 (100ms) | Ogni quanti campioni nuovi si calcola una colonna. Una FFT da 24.000 campioni costa troppo per farla ad ogni pacchetto (~94/s) ma è banale farla ~10 volte al secondo — overlap ~94% tra una finestra e la successiva, per uno spettrogramma "morbido" |
| `DINAMICA_DB_DEFAULT` | 60 dB | Il colore va dal picco corrente (99° percentile dei valori visibili, non il massimo assoluto, per non farsi rovinare da un singolo valore anomalo) fino a `picco − dinamica`. Non è una soglia assoluta in dB: dipende dalla scala reale dell'ADC, che non è nota a priori, quindi si auto-adatta ad ogni frame |
| `VALORE_INIZIALE_DB` | -300 | Riempimento iniziale dell'immagine, ben sotto qualunque segnale reale, in attesa dei primi dati |

Il calcolo di una colonna aspetta di avere **un secondo intero di dati reali** prima di partire (`campioni_totali_ricevuti >= NPERSEG_SPETTROGRAMMA`): calcolare la FFT su una finestra che contiene ancora zeri di riempimento genererebbe un salto artificiale (energia falsa su tutte le frequenze) che dominerebbe la scala dei colori.

Il rect di posizionamento dell'immagine (`QRectF`) viene passato **ad ogni `setImage()`**, non impostato una sola volta: impostarlo prima che esista un'immagine può produrre un bounding box scorretto per l'auto-range di pyqtgraph.

### 2.6 Cattura e salvataggio

**Stato**: un solo interruttore logico governa tutto.

- **`armato`** (bool): il pulsante è "Ferma Registrazione" quando `True`. Quando `False`, nessuna cattura avviene, qualunque sia il livello del segnale.
- **`catturando_evento`** (bool): rilevante solo per le classi diverse da `RUMORE_AMBIENTALE`, indica se in questo momento si sta accumulando un evento scattato.

**Due modalità di cattura**, selezionabili dal checkbox "Attendi soglia prima di catturare":

1. **A soglia** (`usa_soglia=True`, default): mentre `armato`, resta in ascolto e cattura automaticamente ogni evento la cui RMS supera `soglia_trigger` — ripetutamente, senza dover ripremere il pulsante per ogni evento. Adatta a eventi brevi e imprevedibili (battiti, passi).
2. **Durata fissa** (`usa_soglia=False`): appena armato, la cattura parte subito senza aspettare nessuna soglia, per `campioni_da_raccogliere` campioni, poi salva e **si ridisarma da sola** (una presa per pressione del pulsante). Pensata per segnali lunghi e controllati come uno sweep: aspettare una soglia rischierebbe di tagliare l'inizio, specialmente se il segnale parte debole.

Per `RUMORE_AMBIENTALE`, il comportamento è sempre "a blocchi continui": mentre armato, registra e salva blocchi di `campioni_da_raccogliere` campioni ripetutamente, senza concetto di soglia.

**`campioni_da_raccogliere`**: quanti campioni compone una cattura, impostabile da interfaccia ("Durata cattura", 0.5-30s, default 1.5s). È condiviso da tutte le classi — se lo si alza per lo sweep e si torna su un'altra classe senza riabbassarlo, quella classe erediterà la stessa durata.

**Buffer di cattura**: array numpy preallocato (`buffer_impulso_array`) con puntatore di scrittura (`pos_impulso`), stesso principio del buffer di visualizzazione — non una lista Python con `.extend()`, che con catture fino a 30 secondi (fino a ~700.000 campioni) sarebbe stata inefficiente.

**Cambiare classe, soglia o durata mentre una cattura è in corso** ferma sempre la cattura corrente (stato altrimenti ambiguo: un evento che parte con un target e finisce con un altro).

### 2.7 Catalogazione per fibra

**`id_fibra`** (default `"fibra1"`, modificabile da un campo di testo): ogni file salvato finisce in `dataset_sensori_24kHz/<id_fibra>/<classe>/campione_<timestamp>.wav`. Il cambio di fibra è una semplice stringa sanificata (solo alfanumerici, `-`, `_`) e non richiede nessuna interazione con il thread di rete: `salva_file()` gira interamente nel thread della GUI, quindi non ci sono race condition da gestire.

### 2.8 Classi di segnale

Definite in `config_soglie.json` (chiave = nome classe, valore = soglia di default suggerita); se il file manca, si usa `CONFIG_DEFAULT` (RUMORE_AMBIENTALE, BATTITO_MANI, FISCHIO, CORSA, PASSI_LENTI). Per un test con sweep va aggiunta una classe (es. `SWEEP`) — il nome deve corrispondere esattamente a `CLASSE_SWEEP` in `3_analizza_sweep.py`.

---

## 3. `2_confronta_fibre.py`

### 3.1 Cosa fa

Legge tutti i campioni salvati (`dataset_sensori_24kHz/<fibra>/<classe>/*.wav`), calcola metriche per file, le aggrega per fibra/classe, e produce un punteggio composito di confronto.

### 3.2 Metriche calcolate per ogni file

| Metrica | Come si calcola | Cosa significa |
|---|---|---|
| RMS (dBFS) | `20·log10(rms_lineare/32768)` | Livello medio del segnale. 0dB = fondo scala |
| Picco (dBFS) | `20·log10(picco_lineare/32768)` | Valore massimo raggiunto |
| Crest factor (dB) | Picco − RMS | Quanto è "impulsivo" il segnale |
| Clipping (%) | % campioni con `\|valore\| ≥ 32760` | Se >0%, il segnale ha saturato l'ADC: il vero picco è sconosciuto |
| Baricentro spettrale (Hz) | Media delle frequenze pesata per l'energia dello spettro (FFT sull'intero file) | Indicatore grezzo di "dove" si concentra il contenuto in frequenza |

### 3.3 Punteggio composito (verdetto)

Per ogni fibra, aggregando su tutte le classi evento (esclusa `RUMORE_AMBIENTALE`, usata come riferimento di rumore):

```
SNR_classe   = RMS_medio_classe − RMS_medio_rumore_ambientale     (stessa fibra)
SNR_medio    = media di SNR_classe su tutte le classi evento
instabilità  = media della deviazione standard di RMS tra le prove, su tutte le classi
punteggio    = SNR_medio − 0.5 × instabilità
```

**Squalifica automatica**: se una fibra ha clipping >0% in una qualsiasi misura, il suo punteggio diventa `-inf` (esclusa dal podio) — un dato saturo non è confrontabile onestamente, indipendentemente da quanto sembri buono l'SNR grezzo.

Il peso `0.5` sull'instabilità (`PESO_PENALITA_INSTABILITA`) è una scelta di bilanciamento: penalizza la scarsa ripetibilità senza farla dominare completamente sulla sensibilità media.

### 3.4 SNR per banda (bassa/media/alta)

Oltre all'SNR a banda larga (§3.3), per ogni file viene calcolato anche un **livello per banda** in tre bande grezze (stessi confini di `BORDI_BANDA_ENERGIA_HZ`: <500Hz, 500-2000Hz, >2000Hz):

```
livello_banda = 10·log10( Σ(potenza_spettro_in_banda) / n_campioni_file )
```

Normalizzato per il numero di campioni (indipendente dalla durata della registrazione), ma **non calibrato in assoluto** — ha senso solo confrontando evento e rumore della stessa fibra, non tra fibre diverse in valore assoluto.

```
SNR_banda = livello_banda_medio_classe − livello_banda_medio_rumore     (stessa fibra, stessa banda)
```

**Perché serve**: un evento con energia concentrata in poche frequenze basse (es. passi lenti, che spesso non danno un picco alto in ampiezza ma un contenuto concentrato sotto i 500Hz) può avere un RMS a banda larga simile al rumore di fondo, pur avendo in quella banda specifica un segnale ben distinguibile. L'SNR a banda larga da solo non lo vede; l'SNR per banda sì.

Stampato come Tabella 3 dell'output (`confronto_fibre.csv` contiene anche i valori grezzi, colonne `livello_banda_bassa_db` / `_media_db` / `_alta_db`). **È informativo, non entra nel punteggio composito del verdetto** — stesso principio già in uso per le metriche spettrali estese di Tabella 2.

---

## 4. `3_analizza_sweep.py`

### 4.1 Cosa fa

Usa una registrazione di sweep in frequenza (registrata in modalità "durata fissa" nel registratore) per costruire una curva di **risposta in frequenza per banda**, incrociata con il rumore ambientale della stessa fibra per ottenere un **SNR per banda** — non un singolo numero, ma una curva su tutto lo spettro testato.

### 4.2 Parametri dello sweep (da configurare, devono corrispondere esattamente al file audio riprodotto)

| Parametro | Significato |
|---|---|
| `F_MIN_SWEEP`, `F_MAX_SWEEP` | Frequenza di partenza e arrivo dello sweep (Hz) |
| `DURATA_SWEEP_S` | Durata dello sweep vero e proprio (non della registrazione, che può essere più lunga) |
| `TIPO_SWEEP` | `"log"` (chirp logaritmico — il più comune per misure acustiche, stesso tempo per ogni ottava) o `"lineare"` |

Da questi si deriva la **legge di sweep**, `frequenza_istantanea(t)`: a quale frequenza si trova lo sweep al tempo `t` dall'inizio.

### 4.3 Allineamento automatico (cross-correlazione)

Premere "Avvia Registrazione" in sincronia perfetta con l'audio è umanamente impossibile al millisecondo; un errore anche di 200-300ms sposta la mappatura tempo→frequenza. `sintetizza_sweep_riferimento()` ricrea localmente la stessa forma d'onda (dagli stessi 4 parametri sopra); `rileva_offset_sweep()` la cross-correla con la registrazione reale per trovare l'esatto istante di inizio, tramite FFT (`np.fft.rfft`/`irfft`, più veloce della correlazione diretta per segnali lunghi).

- **Richiede margine**: la registrazione deve durare più a lungo dello sweep di riferimento, altrimenti non c'è nulla su cui "far scorrere" la ricerca (in questo caso la funzione restituisce confidenza `-1`, distinta da una bassa confidenza vera).
- **Confidenza** = picco di correlazione diviso la mediana del resto della curva di correlazione. Sotto ~3 l'allineamento è considerato incerto.

### 4.4 Calcolo della risposta per banda

- **`bordi_bande()`**: `N_BANDE` (20) bande **log-spaziate** tra `F_MIN_SWEEP` e `F_MAX_SWEEP` (spaziatura logaritmica = stessa "larghezza" percentuale per ogni banda, coerente con come funziona la percezione in frequenza).
- **`risposta_sweep_per_banda()`**: per ogni finestra di analisi (`NPERSEG_ANALISI = 1024` campioni, ~43ms; avanzamento `HOP_ANALISI = 256` campioni), calcola la frequenza attesa in quell'istante ed estrae l'energia lì.
  - **Correzione per smearing spettrale**: in uno sweep log la velocità di variazione della frequenza (Hz/s) cresce con la frequenza stessa — ad alta frequenza lo sweep attraversa più bin FFT durante una singola finestra di analisi. Invece di prendere un solo bin, si integra la **potenza** (somma dei quadrati delle ampiezze, poi radice quadrata per tornare a un'ampiezza equivalente) su una finestra di bin proporzionata alla velocità istantanea in quel punto (`mezza_larghezza`, con +1 bin di margine per il lobo principale della finestra di Hann).
- **`spettro_medio_rumore_per_banda()`**: spettro medio (stile Welch, segmenti non sovrapposti) della registrazione di rumore ambientale, aggregato nelle stesse bande — riferimento di rumore per il calcolo dell'SNR.

### 4.5 Controllo qualità delle registrazioni

- **Durata insufficiente**: se un file sweep dura meno del 90% di `DURATA_SWEEP_S`, viene ignorato (probabile cattura incompleta).
- **Clipping**: se una registrazione ha clipping >0% (stessa soglia `SOGLIA_CLIPPING_CAMPIONE = 32760` di `2_confronta_fibre.py`), viene **esclusa** dal confronto — il picco reale non è noto, non è un dato utilizzabile.
- **Confidenza di allineamento bassa**: la registrazione viene comunque usata, ma segnalata con un avviso durante la scansione.

### 4.6 Ripetibilità

Con più registrazioni sweep per la stessa fibra, per ogni banda si calcola media e **deviazione standard** (`STD_SOSPETTA_DB = 3.0` come soglia di attenzione). Se una banda supera la soglia, si individua **quale registrazione specifica** si discosta di più dalla mediana (probabile causa dell'instabilità), così da poterla controllare singolarmente invece di fidarsi ciecamente della media.

### 4.7 SNR, copertura e verdetto

```
SNR_banda = risposta_media_sweep_banda − rumore_medio_banda      (stessa fibra, stessa banda)
```

- **`SNR_MINIMO_UTILE_DB = 6.0`**: sotto questa soglia una banda è considerata "non coperta" (segnale troppo vicino al rumore).
- **Copertura (%)** = quante bande hanno SNR ≥ soglia, diviso il numero **totale fisso** di bande (`N_BANDE = 20`) — non solo quelle con dato. Un `n/d` in una banda (nessun dato disponibile, tipicamente per allineamento impreciso che taglia l'inizio dello sweep, dove passano le frequenze più basse) conta correttamente come "non coperta": è un'informazione reale sulla qualità di quella misura specifica, non un caso da escludere dal conteggio.
- **Verdetto**: ordina le fibre prima per copertura, poi per SNR medio come spareggio (una fibra che copre più banda è preferibile a una con un picco isolato ma "bucata" altrove).

### 4.8 Output

- Tabella riepilogativa per fibra (N registrazioni, escluse per clipping, confidenza di allineamento min/media/max)
- Tabella SNR±deviazione standard per banda, una colonna per fibra
- Sezione ripetibilità con le registrazioni sospette da controllare
- Verdetto finale
- `confronto_sweep.csv` con il dettaglio completo banda per banda (incluse std e numero di registrazioni), per analisi ulteriori

---

## 5. Flusso operativo sintetico

### 5.1 Preparazione (una volta sola)

1. In `config_soglie.json`, assicurarsi che siano presenti le classi che verranno usate, incluse `RUMORE_AMBIENTALE` e `SWEEP`.
2. Preparare il file audio dello sweep da riprodurre, annotando frequenza minima, massima, durata esatta e tipo (log/lineare).

### 5.2 Per ogni fibra da testare

1. Attaccare la fibra all'interferometro, avviare `1_registratore.py`.
2. Impostare l'**ID Fibra** nel campo dedicato, premere Applica.
3. **Rumore ambientale**: classe `RUMORE_AMBIENTALE`, Avvia Registrazione, lasciare qualche decina di secondi in quiete, Ferma.
4. **Sweep**: classe `SWEEP`, deselezionare "Attendi soglia", impostare "Durata cattura" più lunga dello sweep reale (margine per l'allineamento automatico), Avvia Registrazione esattamente all'inizio della riproduzione, ripetere almeno 2-3 volte.
5. (Opzionale) Altri eventi: classe relativa, soglia impostata, Avvia Registrazione, ripetere l'evento più volte, Ferma.
6. Staccare la fibra, ripetere dal punto 1 per la fibra successiva — **stesso identico protocollo**.

### 5.3 Analisi

1. `python 2_confronta_fibre.py` → confronto su eventi brevi e rumore ambientale.
2. In `3_analizza_sweep.py`, verificare che `F_MIN_SWEEP`, `F_MAX_SWEEP`, `DURATA_SWEEP_S`, `TIPO_SWEEP` corrispondano esattamente al file audio usato.
3. `python 3_analizza_sweep.py` → curva SNR per banda, ripetibilità, verdetto.
4. Controllare sempre gli avvisi (`⚠`, esclusioni per clipping, confidenza di allineamento) prima di trarre conclusioni da un singolo valore.

---

## 6. File generati

- `dataset_sensori_24kHz/<id_fibra>/<classe>/campione_*.wav` — campioni catalogati
- `registrazioni_audio/full_streaming_*.wav` — copia grezza e continua dell'intera sessione (non catalogata per fibra)
- `confronto_fibre.csv` — dettaglio per-file dell'analisi eventi brevi
- `confronto_sweep.csv` — dettaglio banda-per-banda dell'analisi sweep

---

## 7. Da rivedere (non urgente, appuntato per dopo)

Osservazione emersa lavorando sul machine learning applicato all'interferometro: il rumore di fondo e i **passi lenti** hanno spesso energie/RMS simili a banda larga (l'energia dei passi lenti si concentra nelle basse frequenze invece di dare un picco alto in ampiezza), quindi con un trigger a banda larga i due non sono separabili con nessuna soglia. Un punto degli script di confronto fibre soffre ancora di questo principio:

- **`1_registratore.py`, cattura "a soglia"**: si basa sull'RMS a banda larga del pacchetto grezzo. Per classi a bassa energia/bassa frequenza (es. passi lenti) rischia di non catturare l'evento, o di catturarlo solo nei colpi più forti/vicini — introducendo un bias silenzioso nel dataset (si finisce per confrontare le fibre solo sugli eventi "facili"). Da valutare un trigger tipo STA/LTA al posto della soglia RMS pura, come già deciso per `registratore_stalta.py` sul lato interferometro — qui è probabilmente ancora più importante, perché se lo strumento di raccolta è cieco su un tipo di evento, quell'evento viene escluso dal confronto fibre senza che ce ne si accorga.

**Fatto** (non più da rivedere): `2_confronta_fibre.py` ora calcola anche un **SNR per banda** (bassa/media/alta, Tabella 3 dell'output) accanto all'SNR a banda larga, proprio per non perdere eventi a bassa energia/bassa frequenza come i passi lenti — vedi §3.4. `3_analizza_sweep.py` non aveva questo problema fin dall'inizio, dato che l'SNR era già calcolato banda per banda.

## 8. Limiti noti

- **Rumore misurato "a parte"**: il rumore ambientale viene registrato in un momento diverso dallo sweep. Se la sorgente sonora stessa introduce rumore aggiuntivo durante la riproduzione (vibrazioni, rumore elettrico), l'SNR calcolato può risultare più ottimistico della realtà.
- **Risoluzione 1Hz dello spettrogramma live**: richiede una finestra da 1 secondo intero (limite fisico di Gabor-Heisenberg — risoluzione fine in frequenza e in tempo sono in conflitto). Un evento breve apparirà "spalmato" nel tempo sullo spettrogramma.
- **Verdetti automatici**: sono una sintesi utile, non un oracolo. Non conoscono esigenze applicative specifiche (banda richiesta, vincoli di costo/robustezza) — vanno sempre incrociati con la tabella completa.