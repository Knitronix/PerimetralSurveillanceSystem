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

File: `trasmettitore_f1_f2.ino` (vedi file separato).

Genera f1 e f2 come due toni **realmente simultanei** usando due timer hardware indipendenti (Timer1 su pin 9 per f1, Timer2 su pin 11 per f2), non `tone()` standard — la libreria `tone()` di Arduino usa un solo timer condiviso e non garantisce due frequenze simultanee su due pin diversi.

Comportamento:
- invia marcatore di zero: f1 per `T0_MS`, poi silenzio `GAP_MS`
- per ogni interruttore da 1 a `NUM_SWITCH`: f1 per `T1_MS`; se l'interruttore è chiuso, f2 parte in sovrapposizione per lo stesso `T1_MS`; poi silenzio `GAP_MS`
- ripete da capo

La lettura reale degli interruttori (funzione `readSwitch`) è lasciata come placeholder: va collegata all'hardware reale (shift register / multiplexer per 100 ingressi), non è nello scope di questo file.

---

## 5. Parametri attuali (tarati sul canale reale, punto di partenza)

Misurati sul canale motorino/vibrazione → terreno → fibra → interferometro:
- rumore di fondo: potenza Goertzel normalizzata < 0.00005
- f1 (600 Hz): tipicamente 0.0002–0.0003
- f2 (1500 Hz): tipicamente 0.0014–0.0022 (sistematicamente più forte di f1, ~7x — vedi §6.2)

Default correnti in `main.py`:
- `F1_HZ_DEFAULT = 600`, `F2_HZ_DEFAULT = 1500`
- `T0_MS_DEFAULT = 200`, `T1_MS_DEFAULT = 80`, `TOLLERANZA_MS_DEFAULT = 50`
- soglie: ON/OFF f1 = 0.00015/0.00010, ON/OFF f2 = 0.00080/0.00050

Funzionano sul setup di test attuale, ma **non sono costanti definitive**: frequenze e soglie sono ancora punti di partenza, oggetto dei prossimi step (§6).

Nota su t0/t1 ridotti a 200/80 ms: con `TOLLERANZA_MS_DEFAULT = 50`, la fascia "slot valido" copre [30,130] ms e quella "marcatore zero" [150,250] ms — non si sovrappongono (gap 20 ms), ma 50 ms di tolleranza è il 62% della durata nominale di `t1` (80 ms), più largo in proporzione di quanto lo fosse con t1=150ms. Se in campo capitano falsi "slot" generati da rumore prolungato, stringere la tolleranza è il primo parametro da rivedere.

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

## 6. Prossimi passi

### 6.1 Delta t minimo discernibile dall'interferometro
Capire qual è la separazione temporale minima tra due eventi/impulsi che il sistema (fibra + interferometro + demodulazione) riesce ancora a distinguere come due eventi separati, invece di leggerli come un unico impulso prolungato.

Rilevante per: scegliere `T1_MS`/`T0_MS`/tolleranza al minimo compatibile con la fisica del sistema (cicli più corti → più interruttori multiplexabili in un dato tempo di ciclo), e per sapere qual è il limite fisico oltre il quale comprimere ulteriormente il protocollo non ha senso.

Da misurare sperimentalmente: sequenza di impulsi a distanza temporale decrescente, osservando a che punto il segnale grezzo (o il rilevatore) smette di separarli in due eventi distinti.

### 6.2 Risposta in frequenza del sistema completo
Capire se ci sono frequenze che il sistema "sente meglio" — combinazione di quanto bene l'amplificatore/trasduttore la produce e quanto bene l'interferometro la percepisce — per scegliere f1/f2 nella banda migliore invece dei placeholder attuali (600/1500 Hz).

Il fatto che f2 (1500 Hz) misuri sistematicamente ~7x più potenza di f1 (600 Hz) a parità di condizioni (vedi §5) è già un primo indizio che la risposta non è piatta in frequenza — da approfondire con un test sistematico invece che dedurlo da due soli punti.

Punto di partenza: si può riusare/adattare `CONFRONTO FIBRE/3_analizza_sweep.py` (già esistente, fa confronto fibre banda per banda con segnale sweep e cross-correlazione) per misurare la risposta in frequenza del canale intero, invece che il confronto tra fibre diverse.

---

## 7. Aperti / da decidere

- valore reale di `NUM_SWITCH` da usare nei test (fino a 100)
- durata del `GAP_MS` tra slot, da fissare e allineare tra Arduino e rilevatore
- tolleranze reali su t0/t1 da validare con dati di campo, non solo in laboratorio (collegato a §6.1)
- frequenze esatte f1/f2 da fissare in base ai risultati di §6.2, non più ai placeholder attuali
