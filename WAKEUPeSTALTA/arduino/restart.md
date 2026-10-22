# Restart — Sincronizzazione daisy chain (open-loop, bus condiviso)

> **Copia di deployment** (vedi `WAKEUPeSTALTA/README.md`): documento di
> riferimento per l'architettura del bus, il wiring e il perché delle scelte
> di timing — utile per chi deve ritoccare `master.ino`/`slave2.ino` sul
> posto.

> **Stato: generazione centralizzata sul master (seconda revisione)**.
> Prima versione (staffetta punto-punto) e seconda versione (bus condiviso,
> ogni nodo genera il proprio tono) erano già state verificate dal vivo.
> Questa terza revisione cambia chi genera i toni — vedi "Generazione
> centralizzata sul master" sotto — per eliminare un problema di fondo della
> versione precedente: se un nodo intermedio non risponde, il contatore
> ORDINALE del ricevitore (`3main.py`, non legato al tempo assoluto) sfasava
> silenziosamente l'identità di tutti i nodi successivi nello stesso ciclo —
> non "nessun dato", ma un **tappeto sbagliato riportato come premuto**. Non
> ancora verificata dal vivo sull'hardware reale: i valori di timing
> dell'interrogazione (sotto) sono di partenza, da confermare sul campo.

## Obiettivo
Sincronizzare N Arduino Mega (oggi 2: MASTER=SLAVE1 e SLAVE2, in futuro SLAVE3...N)
in modo che il sistema sappia sempre, per ogni slot temporale, se il tappeto
corrispondente è premuto — generando però i toni f1/f2 da un **unico punto**
(il master), non da ciascun nodo.

## Architettura: bus condiviso open-loop a tempo fisso
**Non** è una staffetta punto-punto (dove ogni nodo aspetta il trigger dal
precedente e lo rilancia al successivo). È un **bus broadcast**: il master ha
un'unica linea collegata in parallelo a tutti gli slave, e scandisce il tempo
da solo, senza aspettare conferma da nessuno per la TEMPORIZZAZIONE. Nessuno
slave rilancia mai il segnale di indirizzamento: lo ricevono tutti
direttamente dal master, sullo stesso filo.

```
                    ┌──────────────► D2 (SLAVE2, NODE_ID=2)
MASTER D8 ──[R]────┼──────────────► D2 (SLAVE3, NODE_ID=3)   ← futuro
                    └──────────────► D2 (SLAVE N, NODE_ID=N)  ← futuro

GND master ──────────────────────── GND comune a tutti i nodi
```

Stesso filo del diagramma sopra, nessuna linea aggiunta: durante
l'interrogazione (vedi sotto) lo stesso bus viene usato brevemente anche nel
verso slave→master.

- Il master genera N impulsi di indirizzamento consecutivi sul bus, uno ogni
  slot
- Ogni slave conta gli impulsi ricevuti e riconosce il proprio turno quando
  il conteggio coincide col proprio `NODE_ID`
- Il master **sa sempre quando il ciclo è finito** (è lui che lo scandisce),
  quindi non serve nessun filo di ritorno DEDICATO da SLAVE N verso il
  master — la risposta di interrogazione usa il filo già esistente

## Perché bus condiviso invece della staffetta
- Un nodo guasto/scollegato non blocca gli altri (nella staffetta sì: se un
  nodo intermedio non riceve mai il trigger, tutti i successivi restano fermi)
- Il master conosce sempre lo stato di avanzamento del ciclo
- Wiring più semplice a N grande: un solo bus condiviso, non N-1 collegamenti
  punto-punto in cascata
- Aggiungere un nuovo nodo non richiede toccare nessun altro nodo esistente:
  basta collegarlo in parallelo allo stesso bus

## Generazione centralizzata sul master
Il master è l'**unico** nodo che genera f1/f2 su tutta la catena. Gli slave
non hanno più DDS/PWM/trasduttore: hanno solo un interruttore locale da
riferire al master.

1. **f1 per T0_MS** — marcatore di zero/clock a inizio ciclo (impulso
   "conteggio=0" del bus)
2. **f1 per T1_MS, slot del master (NODE_ID=1)** — il master legge il
   proprio interruttore locale, nessuna interrogazione: genera lui stesso.
3. **f1 per T1_MS, slot di un nodo remoto (NODE_ID=k, k>1)** — il master
   indirizza quel nodo (impulso k), poi lo **interroga** (vedi sezione
   sotto) per sapere se è vivo e se il suo interruttore è chiuso, e SOLO
   DOPO genera lui il tono per conto di quello slot.
4. **f2 sovrapposta a f1**, in entrambi i casi sopra, solo se
   l'interruttore di quel nodo (proprio o remoto) risulta chiuso.

**Un nodo remoto che non risponde all'interrogazione NON produce silenzio**:
il master genera comunque f1 per quello slot (senza f2). Questo è
deliberato — è quello che elimina il bug della versione precedente: il
contatore ordinale del ricevitore vede sempre un tono per ogni slot, nella
posizione giusta, a prescindere da quale nodo remoto sia vivo o morto. La
diagnosi "nodo X non risponde" si legge sul **Serial Monitor del master**
(stampata solo sui cambi di stato, non ad ogni ciclo), non è distinguibile
sulla fibra da "nodo X vivo ma aperto" — se in futuro serve anche quella
distinzione sul segnale decodificato, va aggiunto un terzo stato esplicito
sia qui che in `RilevatoreProtocolloF1F2` (`3main.py`), non c'è ancora.

## Interrogazione remota
Come il master scopre se un nodo remoto è vivo e il suo interruttore è
chiuso, SENZA un filo dedicato in più: riusa la stessa linea del bus
(`PIN_BUS`, D8 sul master / D2 su ogni slave), a turno nei due versi.

1. Il master emette l'impulso di indirizzamento (come sempre, OUTPUT per
   `DURATA_IMPULSO_TRIGGER_MS`), poi **rilascia immediatamente la linea**
   (`pinMode(PIN_BUS, INPUT)`) e si mette in ascolto (`attendi_impulso_risposta()`
   in `master.ino`, bloccante con timeout — **non** `pulseIn()`: con
   l'interrupt di Timer2 [il DDS] che spara ogni 25us continuamente, anche
   durante questa attesa, `pulseIn()` misura la durata contando cicli di
   CPU e quel traffico la corrompe al punto da non riconoscere mai
   l'impulso — bug osservato dal vivo nella prima versione di questo
   redesign. `attendi_impulso_risposta()` misura invece con `micros()`
   [Timer0, indipendente da Timer2], tollerante a qualunque interrupt
   concorrente).
2. Il nodo indirizzato (che ha visto l'impulso via interrupt, come sempre)
   aspetta `MARGINE_SICUREZZA_RISPOSTA_MS` — **deve** restare ampiamente più
   lungo di `DURATA_IMPULSO_TRIGGER_MS`, altrimenti risponderebbe mentre il
   master guida ancora lui stesso la linea (contesa: due driver sullo stesso
   filo). Poi disattiva il proprio interrupt (per non autotriggerarsi sul
   proprio impulso in uscita), passa a OUTPUT, pulsa HIGH per una durata che
   codifica lo stato, torna a INPUT e riattiva l'interrupt.
3. La **durata** dell'impulso di risposta codifica lo stato (stessa logica
   delle soglie f1 zero/slot, solo più breve): `DURATA_RISPOSTA_APERTO_MS`
   (corto) = vivo e aperto, `DURATA_RISPOSTA_CHIUSO_MS` (lungo) = vivo e
   chiuso. **Nessun impulso** entro `TIMEOUT_RISPOSTA_MS` = nodo non
   funzionante per questo ciclo (non un guasto permanente dal punto di vista
   del protocollo: si riprova al ciclo successivo).
4. Il master richiude la linea in OUTPUT (pronta per il prossimo impulso di
   indirizzamento) e genera il tono per quello slot in base a quanto appena
   scoperto.

Mai due nodi guidano il bus insieme — garantito dalla disciplina temporale
(margine di sicurezza ampio), non da arbitraggio elettrico: stesso principio
già usato per i toni nella versione precedente, estesa ora anche alla
risposta.

Valori attuali (in entrambi gli sketch, devono restare identici):
`MARGINE_SICUREZZA_RISPOSTA_MS = 15`, `DURATA_RISPOSTA_APERTO_MS = 6`,
`DURATA_RISPOSTA_CHIUSO_MS = 18`, `TIMEOUT_RISPOSTA_MS = 45` (solo sul
master, è lui che applica il timeout). Partenza ragionata (margine ampio
sopra `DURATA_IMPULSO_TRIGGER_MS = 8ms`), non ancora misurata dal vivo.

## Timing
`T1_MS` (durata di ogni slot, master o remoto) e `GAP_MS` (silenzio dopo
ogni tono, per il ricevitore) restano gli stessi di prima e si applicano
identici sia allo slot del master sia a quello generato per conto di un
nodo remoto — dal punto di vista del ricevitore sulla fibra i due casi sono
indistinguibili, come deve essere.

`INTERVALLO_MS`/`MARGINE_SICUREZZA_MS` della versione precedente (l'attesa
"alla cieca" di un intero slot+gap remoto) **non esistono più**: il master
ora sa sempre cosa succede in ogni slot, interrogando invece di aspettare al
buio - vedi "Interrogazione remota" sopra per i nuovi tempi.

## Wiring — bidirezionale a turno, nessun filo in più
A differenza della versione a staffetta (e della versione precedente a
generazione distribuita), la linea del bus è ora usata in **entrambi i
versi**, ma non contemporaneamente:

- **Master**: `PIN_BUS` (D8) normalmente OUTPUT (impulsi di
  indirizzamento), collegato tramite resistore serie 220-330Ω in parallelo
  su tutti i `PIN_BUS` (D2) degli slave (bus condiviso). Passa brevemente a
  INPUT solo durante l'interrogazione di un nodo remoto (vedi sopra).
- **Ogni slave** (2, 3, ..., N): `PIN_BUS` (D2) normalmente INPUT con
  interrupt. Passa brevemente a OUTPUT solo per inviare la propria risposta
  quando interrogato, poi torna a INPUT.
- **Pull-down della linea del bus: 10kΩ tra D8 e GND sul MASTER**
  (obbligatorio, montato il 22/10/2026). Quando il master rilascia la linea
  per ascoltare la risposta di uno slave, e' l'unica cosa che la tiene bassa.
  Sullo slave2 il pull-down sul D2 che questo documento dava per montato
  **non c'e' mai stato** (misurato: ~9MΩ tra D2 e GND). Finche' il cablaggio
  era tranquillo funzionava lo stesso (linea "per aria" ma ferma); dopo
  l'aggiunta dell'ESP e il ricablaggio dell'alimentazione la linea raccoglieva
  disturbi (spilli da ~20us = portante audio a 62.5kHz del pin 12) e il master
  leggeva meta' delle risposte sbagliate (tappeto 2 premuto visto
  "aperto/chiuso" a cicli alterni). Con la 10kΩ sul D8: 38/38 risposte
  corrette. Diagnosi: comando `DEBUG` del master (durata e attesa di ogni
  risposta). Tenere comunque il filo del bus lontano dal filo audio (pin 12)
  e dall'amplificatore.
- `GND` comune a tutti i nodi (obbligatorio)

```cpp
const int PIN_BUS = 8;   // sul master
const int PIN_BUS = 2;   // su ogni slave (interrupt-capable)
```

Il pin 12 (OC1B, uscita audio verso il trasduttore/fibra) e il pin 11
(abilitazione suono) esistono **solo sul master**: gli slave non hanno più
nessun collegamento audio, solo l'interruttore su A0.

## Requisiti software
- Rilevamento impulsi di indirizzamento via `attachInterrupt` (RISING) su
  `PIN_BUS`, mai polling
- ISR minimale: solo flag `volatile` + contatore impulsi + debounce (finestra
  2ms via `micros()`), nessuna logica pesante dentro l'interrupt
- **Prima di guidare `PIN_BUS` in OUTPUT per rispondere, lo slave deve
  disattivare il proprio `attachInterrupt`** su quel pin (altrimenti il suo
  stesso impulso in uscita si autotriggererebbe come un falso impulso del
  master) — lo riattiva subito dopo essere tornato a INPUT.
- Il master usa `attendi_impulso_risposta()` (bloccante, con timeout,
  misura via `micros()` - **non** `pulseIn()`, vedi "Interrogazione remota"
  sopra per il perché) per ascoltare la risposta: va bene bloccare
  brevemente `loop()`, il DDS continua a girare in background via Timer2,
  indipendente da `loop()`.
- **Reset del contatore ad ogni nuovo ciclo**: se non arrivano impulsi per più
  di una soglia di timeout (es. 300ms), lo slave resetta `contaImpulsi = -1`
  in autonomia (self-healing, senza bisogno di un comando di reset esplicito)

## Cosa NON fare
- Non implementare la staffetta punto-punto (superata da questa versione)
- Non far generare toni f1/f2 a nessuno slave: solo il master genera audio,
  vedi "Generazione centralizzata sul master" sopra
- Non introdurre librerie esterne per bus/interrogazione, solo
  `attachInterrupt`/`micros()` nativi
- Non usare `pulseIn()` per misurare la risposta sul master: con Timer2
  (DDS) attivo in background la sua misura a conteggio di cicli non è
  affidabile - usare `attendi_impulso_risposta()` (basata su `micros()`)
- Non far rispondere uno slave senza prima disattivare il proprio interrupt
  sullo stesso pin (autotrigger sul proprio impulso in uscita)
- Non lasciare `MARGINE_SICUREZZA_RISPOSTA_MS` troppo vicino a
  `DURATA_IMPULSO_TRIGGER_MS`: deve restare ampiamente più lungo, altrimenti
  rischio di contesa sul bus (vedi "Interrogazione remota")

## Interruttore locale: latch, non lettura istantanea
Ogni nodo legge il proprio interruttore su `A0` (chiuso = HIGH, pull-down 10kΩ
esterno verso GND, switch verso 5V/VCC - stesso schema del bus di trigger).
La lettura NON è un semplice `digitalRead` usato al volo: è gestita con due
livelli, per non perdere una chiusura breve avvenuta tra un ciclo e l'altro.

1. **Debounce software** (`DEBOUNCE_INTERRUTTORE_MS = 30`, `aggiorna_interruttore()`,
   chiamata ad ogni giro di `loop()`): una transizione del pin è accettata solo
   se resta stabile per almeno questo tempo, per non scambiare un rimbalzo
   meccanico per una pressione vera.
2. **Latch** (`interruttoreChiuso`): va a `true` ad OGNI giro in cui il pin
   risulta stabilmente chiuso (non solo alla prima transizione - un
   interruttore tenuto chiuso per più cicli deve continuare a essere
   riportato chiuso ad ogni ciclo, non solo al primo). Torna a `false` in UN
   SOLO punto per sketch: dentro `TONO_SLOT_PROPRIO` sul master (consumato
   per il proprio `canale2_on`), dentro `invia_risposta()` sullo slave
   (consumato per la durata della risposta). Nessun altro punto del codice
   deve scriverci.

Il controllo seriale (`ON`/`OFF` del suono) esiste **solo sul master** ora
(gli slave non generano più audio) e parte già attivo al boot.

## Nota per estensione futura a N nodi
Aggiungere un nodo richiede: un nuovo sketch `slaveN` (copia di `slave2.ino`
con `NODE_ID_PROPRIO` cambiato — è già minimale, nessun DDS da copiare),
collegare il suo `PIN_BUS` in parallelo allo stesso bus condiviso (con
pull-down 10kΩ locale), e incrementare `NUMERO_NODI` **in `master.ino`**
(gli slave non hanno bisogno di conoscere `NUMERO_NODI`). Nessuna modifica
al master oltre a quella costante, né agli altri slave. Se la catena supera
8 nodi, aumentare anche `MAX_NODI_SUPPORTATI` in `master.ino` (array di
diagnostica per-nodo).
