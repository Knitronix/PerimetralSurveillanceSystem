# SPECS — Interrogatore & Riconoscimento Doppia Frequenza (f1/f2)

> Documento di riferimento persistente per Claude Code. Da tenere aggiornato ad ogni modifica di architettura/protocollo. Il progetto vive nella cartella `FREQUENCY DETECTION/`, separata dalla vecchia pipeline `MLtoDL/` (non toccata, vedi CLAUDE.md).

---

## 0. Stato del progetto

Si è ripartiti da zero rispetto alla vecchia pipeline MLtoDL (dataset recording, feature extraction, training, classificazione): qui non c'è registrazione, non c'è training, non c'è classificazione.

**Milestone raggiunta:** il metodo di riconoscimento a doppia frequenza (Goertzel + isteresi + macchina a stati sulla durata, vedi §3) **funziona sul canale reale** (motorino/vibrazione → terreno → fibra → interferometro), dopo la taratura delle soglie sui livelli di segnale effettivamente misurati. L'approccio è validato e non più in discussione — i prossimi step (§6) sono di caratterizzazione fisica del canale, non di revisione del metodo.

---

## 1. Architettura

```
main.py     -> UNICO entry point del progetto: interrogatore (ricezione UDP,
               bufferizza), visualizzatore (forma d'onda + spettrogramma
               opzionale) e rilevatore protocollo f1/f2, nello stesso
               file/processo/finestra Qt
```

`main.py` accorpa in un solo file/finestra Qt:
- **Interrogatore** (`NetworkWorker`): ricezione UDP grezza a 24kHz, pacchetti da 254 campioni int16, porta 12345. Nessuna scrittura su disco (nessuna registrazione).
- **Visualizzatore**: forma d'onda sempre attiva; spettrogramma disattivabile da checkbox (default OFF — sia il grafico che il calcolo FFT si fermano quando disattivato, non solo il disegno).
- **Rilevatore f1/f2** (`RilevatoreGoertzel`, `MacchinaStatoFrequenza`, `RilevatoreProtocolloF1F2`), agganciato nel metodo `gestisci_dati()`. Non esiste un file `rilevatore_freq.py` separato: la logica del protocollo (§2-3) vive dentro `main.py`.

Pattern riusati da `MLtoDL/0registratoreStalta.py` (senza modificarlo, resta la pipeline di training/dataset archiviata): `NetworkWorker` per la ricezione UDP, il pannello "pesante" attivabile/disattivabile a runtime (qui per lo spettrogramma), lo schema a soglie con isteresi regolabile da spinbox a runtime (qui sulla potenza Goertzel invece che sul rapporto STA/LTA).

**Output:** ogni evento (marcatore di zero, coppie counter/stato sia aperto che chiuso) è sempre loggato per intero in console e in `eventi_f1_f2_log.jsonl` accanto a `main.py` — record completo per analisi successive. A schermo, per lettura rapida, il pannello di log (`lista_log`, QListWidget con una riga-card per evento: badge numero, testo, orario, tetto `MAX_RIGHE_LOG`) mostra **solo le chiusure** — l'unica informazione che serve leggere al volo è quale interruttore è appena stato chiuso; marcatori di zero e slot "aperto" restano solo nel log tecnico. I controlli di taratura (frequenze/durate/soglie) sono raccolti in un pannello nascosto di default (`widget_parametri_avanzati`, apribile con un bottone): ora che il sistema è tarato e stabile non serve vederli sempre, e nasconderli lascia molto più spazio verticale al log.

**Design (v2):** tema scuro coerente per tutta la finestra (`STYLE_QSS`, applicato a livello di `QApplication`), pannello destro riorganizzato in "card" (blocchi con sfondo e bordo arrotondato) invece di label sciolte — coerente con lo sfondo nero dei grafici pyqtgraph, che prima contrastava con il pannello chiaro di default.

**Verifica interruttori attesi (non bloccante):** campo `spin_numero_atteso` in cui l'operatore inserisce quanti interruttori si aspetta per ciclo (0 = verifica disattivata). A ogni marcatore di zero, il conteggio del ciclo appena concluso viene confrontato con questo valore; una discrepanza produce solo un avviso a schermo (`label_verifica_ciclo`) e un `log.warning` in console — nessuna eccezione, nessuna interruzione del rilevamento. Serve perché il sistema non ha un limite hardcoded al numero di interruttori (vedi §1 architettura): un singolo slot perso o mal classificato sfasa il counter di 1 per tutto il resto del ciclo, e questo avviso è il modo per accorgersene.

**Altri controlli in UI:** "Azzera picchi" (max-hold su potenza f1/f2, per tarare le soglie a occhio, §5.1); card "Ripetibilità ciclo" (SOLO per test col ciclo Arduino fisso: confronta ogni ciclo completo con un riferimento catturato al primo, segnala slot diversi); bottone per svuotare la lista log a schermo senza toccare il file `.jsonl`.

**Cartelle di supporto** (non toccano `main.py`/`trasmettitore.ino` se non per applicarne i risultati): `TEST ARMONICHE/` e `TEST TEMPI/` (vedi §6), `config_condivisa.py`/`aggiorna_config_ino.py` (vedi §4/§5).

---

## 2. Protocollo logico f1 / f2

**f1 — "clock/counter":** una singola frequenza nota, emessa con due durate possibili:
- `t0` (200 ms) → marcatore di **zero** (inizio ciclo / reset del contatore)
- `t1` (80 ms) → **slot** incrementale (conta 1, 2, 3, ... N)

**f2 — "stato interruttore":** una seconda frequenza nota, durata sempre `t1`, emessa **in sovrapposizione temporale** a uno slot `t1` di f1 **solo se** l'interruttore corrispondente a quello slot è chiuso.

**Ciclo:**
```
[t0] [slot1: f1(t1) + f2(t1)?] [slot2: f1(t1) + f2(t1)?] ... [slotN] -> ripete da [t0]
```

**Risultato per ogni slot:** coppia `(counter, stato)` con
- `stato = chiuso` se f2 è presente nello slot
- `stato = aperto` se f2 è assente nello slot

`counter` = numero di interruttore, sincronizzato dal marcatore di zero di f1.

**Il `GAP_MS` (silenzio tra uno slot e il successivo, vedi §4) non è misurato né usato dal rilevatore**: la macchina a stati (§3) classifica solo le durate degli impulsi ON, non gli intervalli OFF tra di loro. L'unico vincolo è che il gap sia abbastanza lungo da far scendere la potenza sotto `soglia_off` per almeno un blocco Goertzel (20 ms) tra un impulso e il successivo — altrimenti due impulsi consecutivi si fondono in un unico ON più lungo, letto come rumore. Oltre quel minimo, la durata esatta del gap è irrilevante per il conteggio: conta solo per il tempo di ciclo totale (più gap = ciclo più lento, non più preciso).

---

## 3. Metodo di riconoscimento

**Perché non FFT/spettrogramma classico:** le frequenze sono note a priori e non serve stimarle, serve solo decidere *presenza* e *durata*. L'algoritmo usato è il **filtro di Goertzel** (stesso principio del riconoscimento toni DTMF): calcola in modo efficiente la potenza a una singola frequenza nota, molto più economico di una FFT completa per questo scopo.

**Pipeline (implementata in `main.py`):**

1. Due filtri di Goertzel indipendenti in parallelo, uno su f1 e uno su f2, calcolati sugli stessi blocchi di campioni dello stream a 24 kHz.
2. Blocco da 20 ms (480 campioni) → 4 blocchi per slot `t1` (80 ms), 10 blocchi per marcatore `t0` (200 ms).
3. Per ogni blocco: potenza Goertzel normalizzata, confrontata con **doppia soglia (isteresi)** per decidere lo stato ON/OFF di quella frequenza, evitando sfarfallio sul confine di soglia.
4. Macchina a stati per ciascuna frequenza: misura la durata continuativa di ON; quando torna OFF, classifica la durata:
   - vicino a `t0` (± tolleranza) → marcatore di zero
   - vicino a `t1` (± tolleranza) → slot valido
   - altrimenti → scarta come rumore/glitch
5. Il counter incrementa ad ogni slot valido di f1 dopo l'ultimo marcatore di zero; si resetta a 0 al marcatore successivo.
6. Per ogni slot f1 valido, verifica la sovrapposizione temporale con eventuali impulsi f2 nella stessa finestra: sovrapposizione → chiuso, altrimenti → aperto.
7. Output: sequenza di coppie `(counter, stato)` per ogni ciclo (vedi §1 per dove va a finire).

**Nota di taratura (dal test sul canale reale):** i primi default assoluti (soglie tarate per un tono a piena scala) non funzionavano affatto sul canale reale, dove i livelli sono molto più bassi del previsto. Vedi §5 per i valori attuali. Restano soglie **assolute**: sensibili al guadagno del canale, quindi da rivalidare a ogni cambio di fibra/installazione — non è stato necessario finora passare a una misura relativa (tipo STA/LTA), ma è l'alternativa da tenere in mente se la sensibilità a soglie assolute diventasse un problema pratico.

---

## 4. Codice Arduino (trasmettitore)

File: `trasmettitore/trasmettitore.ino`.

Genera f1 e f2 come due toni **realmente simultanei sullo stesso pin** (12, OC1B) tramite sintesi DDS (tabella seno a 256 valori, phase accumulator) sommata e mandata in PWM (Timer1 Fast PWM 8 bit, portante 62.5kHz; Timer2 CTC come clock del DDS a `SAMPLE_RATE=40000`Hz). Non usa due timer/pin separati come in una versione precedente di questo documento: un solo pin, i due toni sommati e poi filtrati con un RC passa-basso.

Comportamento:
- invia marcatore di zero: f1 per `T0_MS`, poi silenzio (nessun `GAP_MS` esplicito dopo il marcatore in questa versione, vedi codice)
- per ogni interruttore da 1 a `NUM_SWITCH`: f1 per `T1_MS`; se l'interruttore è chiuso, f2 parte in sovrapposizione per lo stesso `T1_MS`; poi silenzio `GAP_MS`
- ripete da capo

`CANALE1_ABILITATO`/`CANALE2_ABILITATO` permettono di silenziare un canale intero per isolarlo nei test (usati dalla procedura di taratura soglie, §5.1, e dagli sweep di caratterizzazione, §6). `SIMULA_INTERRUTTORI_CASUALI` genera stati random per test senza hardware reale collegato. La lettura reale degli interruttori (funzione `readSwitch`) resta un placeholder da collegare all'hardware reale (shift register/multiplexer).

**Frequenze e timing non sono più costanti locali indipendenti**: vivono in `config_condivisa.py` (letto direttamente da `main.py`) e vengono scritte dentro `trasmettitore.ino` da `python aggiorna_config_ino.py` — un header C++ condiviso non ha funzionato (il toolchain Arduino non segue `#include` con percorsi relativi fuori dalla cartella dello sketch). Flusso per cambiare F1/F2/T0/T1/GAP: modifica `config_condivisa.py` → `python aggiorna_config_ino.py` → riflasha. Le soglie restano fuori da questo meccanismo (vedi §5.1).

---

## 5. Parametri attuali

Fonte di verità: `config_condivisa.py` per frequenze/timing, cima di `main.py` per le soglie — non duplicare i valori qui, per non andare fuori sincronia (già successo una volta: `T0_MS` a 180 su Arduino contro 200 atteso da `main.py`, corretto con `aggiorna_config_ino.py`).

- **Frequenze** (`F1_HZ`/`F2_HZ`): scelte con lo sweep di §6.2 — 4600Hz/3200Hz, sotto il limite di banda del sistema (~5400Hz), senza cross-talk misurabile tra loro.
- **Timing** (`T0_MS`/`T1_MS`/`GAP_MS`): validati con lo sweep di §6.1 — T0=200ms (marcatore), T1=20ms (uno slot, il minimo teorico = un blocco Goertzel), GAP=50ms.
- **Tolleranza** (`TOLLERANZA_MS_DEFAULT` in `main.py`, non nel config condiviso): 10ms — stretta apposta perché T1 è già al minimo rappresentabile (vedi commento in `main.py`).
- **Soglie**: derivate dai picchi misurati durante gli sweep (procedura §5.1), non da un nuovo test dedicato a 3 punti. Restano **assolute**: da rivalidare a ogni cambio di fibra/installazione/accoppiamento (banco di prova vs campo).

---

## 5.1 Come ritarare le soglie da soli (senza dover richiedere aiuto)

Le soglie (`SOGLIA_ON_F1/F2`, `SOGLIA_OFF_F1/F2`) vanno ricalcolate ogni volta che cambia qualcosa che altera il livello del segnale reale: fibra diversa, installazione diversa, frequenze f1/f2 diverse, accoppiamento diverso (banco di prova vs campo). Procedura, usando solo gli strumenti già in `main.py` e i flag di isolamento in `trasmettitore.ino`:

**1. Misura il rumore puro** (nessun tono trasmesso):
- In `trasmettitore.ino` metti `CANALE1_ABILITATO = false` e `CANALE2_ABILITATO = false`, riflasha.
- In `main.py` premi "⟲ Azzera picchi", aspetta qualche secondo.
- Leggi `picco f1` e `picco f2`: quello è il rumore di fondo per ciascuna frequenza.

**2. Misura il segnale vero, un canale alla volta — e leggi SEMPRE entrambi i picchi, non solo quello che ti interessa** (per non farli sporcare a vicenda, e per scoprire se invece si sporcano davvero):
- Per f1: `CANALE1_ABILITATO = true`, `CANALE2_ABILITATO = false`, riflasha, azzera picchi, aspetta almeno un ciclo intero, leggi **sia** `picco f1` (il segnale vero di f1) **sia** `picco f2` (quanto f1 sporca il filtro di f2 — cross-talk).
- Per f2: `CANALE1_ABILITATO = false`, `CANALE2_ABILITATO = true`, riflasha, azzera picchi, aspetta almeno un ciclo intero (serve che capiti almeno uno slot "chiuso" per vedere il vero picco f2), leggi **sia** `picco f2` (segnale vero) **sia** `picco f1` (cross-talk di f2 su f1).
- Rimetti entrambi i flag a `true` a fine test — sono solo per l'isolamento, non per l'uso normale.

**3. Il "pavimento" da battere per ogni soglia è il PIÙ ALTO tra rumore puro (punto 1) e cross-talk dall'altro canale (punto 2)**, non solo il rumore. Esempio reale riscontrato: rumore puro di f2 ~0.000015, ma f1 (che è sempre acceso ad ogni slot) filtrato nel bin di f2 dava 0.0016 — un pavimento ~100x più alto del rumore, e oltre metà del segnale vero di f2 (0.003). Se il cross-talk è il pavimento dominante:
- rapporto segnale/pavimento oltre ~10x → soglia ancora tranquilla da piazzare.
- rapporto sotto ~3x → soglia intrinsecamente fragile: il problema non si risolve scegliendo il numero giusto, va cambiata la frequenza che sta facendo cross-talk (di solito la più debole delle due, o quella la cui armonica cade vicino all'altra) — riprova questa stessa procedura a 3 punti su un paio di frequenze alternative e tieni quella con il rapporto leakage/segnale più basso.

**4. Calcola le soglie** rispetto al pavimento più alto trovato al punto 3 (non solo al rumore):
- `SOGLIA_ON ≈ 20% del picco segnale`, ma mai sotto ~1.5-2x il pavimento
- `SOGLIA_OFF` a metà strada tra pavimento e `SOGLIA_ON` (non necessariamente 65% fisso: se il margine è stretto, l'isteresi va compressa di conseguenza, non ignorata)

**5. Applica i nuovi valori**: per una prova rapida bastano gli spinbox in "Parametri avanzati" (si applicano subito, non serve "Applica parametri protocollo" — quel bottone serve solo per f1/f2/t0/t1/tolleranza). Per farli diventare il default all'avvio, aggiorna le costanti `SOGLIA_ON_F1_DEFAULT`/`SOGLIA_OFF_F1_DEFAULT`/`SOGLIA_ON_F2_DEFAULT`/`SOGLIA_OFF_F2_DEFAULT` in cima a `main.py` (i valori attuali lì sono la fonte di verità, non duplicarli qui in questo documento per evitare che vadano fuori sincronia).

**Promemoria importante:** le soglie tarate sul banco di prova (accoppiamento diretto/forte) NON sono valide per il campo (accoppiamento debole tramite terreno, vedi §5) — vanno rifatte da capo con questa stessa procedura ogni volta che si cambia contesto di misura.

---

## 6. Caratterizzazione del canale (risolto in laboratorio)

Entrambi i punti sotto erano "prossimi passi" aperti, ora risolti con strumenti dedicati in cartelle a parte (non toccano `main.py`/`trasmettitore.ino` se non per il risultato finale già applicato in §5).

### 6.1 Delta t minimo — `TEST TEMPI/`
`sweep_tempi/sweep_tempi.ino` genera un tono a durata decrescente in due fasi (ON più corto a gap fisso largo, poi gap più corto a ON fisso sicuro); `analizza_tempi_minimi.py` rigioca sui dati la stessa macchina a stati a isteresi di `main.py` e conta le transizioni rilevate contro quelle attese, per trovare il limite reale senza bisogno di leggere uno spettrogramma a occhio. Risultato applicato: T1=20ms (il minimo teorico, un blocco Goertzel), GAP=50ms.

### 6.2 Risposta in frequenza — `TEST ARMONICHE/`
`sweep_gradini/sweep_gradini.ino` spazza un tono a gradini di frequenza (200-6000Hz); `analizza_armoniche_sweep.py` fa FFT per gradino e cerca automaticamente pattern di cross-talk (armoniche, intermodulazione a offset fisso, risonanze assolute). Risultati: il sistema smette di rispondere sopra ~5400Hz; **4600Hz/3200Hz** sono risultate le due frequenze con segnale più forte, meno armoniche proprie e senza cross-talk misurabile tra loro (applicate in §5).

---

## 7. Aperti / da decidere

- valore reale di `NUM_SWITCH` da usare nei test (fino a 100)
- tolleranze t0/t1 validate finora solo in laboratorio (banco di prova): da riverificare con dati di campo, dove il canale è più debole (motorino/vibrazione → terreno → fibra, non l'accoppiamento diretto del banco)
- soglie da §5.1 derivate dagli sweep di caratterizzazione, non da un test a 3 punti dedicato con protocollo f1/f2 completo attivo — da confermare con "Azzera picchi" dal vivo prima del pretest in campo
