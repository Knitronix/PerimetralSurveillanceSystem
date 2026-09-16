# Restart — Sincronizzazione daisy chain (open-loop, bus condiviso)

> **Stato: implementato e verificato dal vivo** (2 nodi reali, marcatore e slot
> riconosciuti stabilmente ad ogni ciclo dopo il fix di `MARGINE_SICUREZZA_MS`
> sotto e della tolleranza in `config_condivisa.py`, vedi SPECS.MD §3/§7).
> Questo documento resta la spec di riferimento per l'architettura e per
> estendere a nuovi nodi - non solo un piano da realizzare.

## Obiettivo
Sincronizzare N Arduino Mega (oggi 2: MASTER=SLAVE1 e SLAVE2, in futuro SLAVE3...N)
in modo che ognuno generi le proprie frequenze f1/f2 solo nel proprio slot temporale,
seguendo il protocollo già esistente in `trasmettitore.ino` (stesso schema
T0/T1/GAP, stesse costanti di frequenza — vedi `config_condivisa.py`).

## Architettura: bus condiviso open-loop a tempo fisso
**Non** è una staffetta punto-punto (dove ogni nodo aspetta il trigger dal
precedente e lo rilancia al successivo). È un **bus broadcast**: il master ha
un'unica uscita di trigger collegata in parallelo a tutti gli slave, e scandisce
il tempo da solo, senza aspettare conferma da nessuno. Nessuno slave rilancia
mai il segnale: lo ricevono tutti direttamente dal master, sullo stesso filo.

```
                    ┌──────────────► D2 (SLAVE2, NODE_ID=2)
MASTER D8 ──[R]────┼──────────────► D2 (SLAVE3, NODE_ID=3)   ← futuro
                    └──────────────► D2 (SLAVE N, NODE_ID=N)  ← futuro

GND master ──────────────────────── GND comune a tutti i nodi
```

- Il master genera N impulsi consecutivi sul bus, uno ogni intervallo fisso
- Ogni slave conta gli impulsi ricevuti e agisce solo quando il conteggio
  coincide col proprio `NODE_ID`
- Il master **sa sempre quando il ciclo è finito** (è lui che lo scandisce),
  quindi non serve nessun filo di ritorno da SLAVE N verso il master

## Perché questo invece della staffetta
- Un nodo guasto/scollegato non blocca gli altri (nella staffetta sì: se un
  nodo intermedio non riceve mai il trigger, tutti i successivi restano fermi)
- Il master conosce sempre lo stato di avanzamento del ciclo
- Wiring più semplice a N grande: un solo bus condiviso, non N-1 collegamenti
  punto-punto in cascata
- Aggiungere un nuovo nodo non richiede toccare nessun altro nodo esistente:
  basta collegarlo in parallelo allo stesso bus

## Cosa genera ogni nodo nel proprio slot (protocollo f1/f2 esistente)
Riusa le funzioni di generazione toni (DDS/PWM) già presenti in
`trasmettitore.ino` — non riscriverle.

1. **f1 per T0_MS** — SOLO il master, come marcatore di zero/clock a inizio
   ciclo (equivalente all'impulso "conteggio=0" del bus)
2. **f1 per T1_MS** — ogni nodo, quando il conteggio coincide col proprio
   NODE_ID, marca il proprio slot come attivo
3. **f2 sovrapposta a f1 per T1_MS** — SOLO SE l'interruttore locale di quel
   nodo risulta chiuso nell'ultimo ciclo letto (stessa logica di lettura
   switch già presente in `trasmettitore.ino`)

## Timing open-loop
```
INTERVALLO_MS = T1_MS + GAP_MS + MARGINE_SICUREZZA_MS
```
Il master emette un impulso ogni `INTERVALLO_MS`, per N volte, senza aspettare
risposta. Ogni slave esegue il proprio slot (T1_MS) interamente dentro quella
finestra, quindi non c'è mai sovrapposizione tra uno slot e il successivo.

**`GAP_MS` e `MARGINE_SICUREZZA_MS` non sono la stessa cosa, anche se sembrano
entrambi "tempo extra"**: `GAP_MS` serve al RICEVITORE (main.py) per vedere
silenzio tra due toni e classificarli come eventi separati - esiste già ed è
condiviso con tutto il resto del protocollo (`config_condivisa.py`).
`MARGINE_SICUREZZA_MS` è un concetto diverso, solo del master: è il cuscinetto
che tiene conto della latenza reale del nodo remoto (interrupt, debounce, giri
di `loop()`), che supera leggermente il tempo nominale `T1_MS+GAP_MS`. Prima
che questa distinzione fosse esplicita nel codice, `INTERVALLO_MS` era
`T1_MS + GAP_MS` senza nessun margine dedicato - sintomo osservato dal vivo:
marcatori misurati 220-240ms invece di 200ms, slot misurati 80ms invece di
60ms (sempre "troppo lunghi", mai "troppo corti"), perché il tono/gap dello
slave remoto sconfinava nell'evento successivo prima che il master procedesse.
Valore attuale: `MARGINE_SICUREZZA_MS = 40`ms, in `master.ino`.

## Wiring — IMPORTANTE: asimmetria master/slave
A differenza della vecchia staffetta, master e slave NON hanno più lo stesso
schema pin. Il segnale è a senso unico dal master verso tutti gli slave:

- **Master**: solo `D8` in uscita, collegato tramite resistore serie 220-330Ω
  in parallelo su tutti i `D2` degli slave (bus condiviso). Nessun `D2` in
  ingresso sul master.
- **Ogni slave** (2, 3, ..., N): solo `D2` in ingresso, con pull-down 10kΩ
  verso GND. **Nessun `D8` dedicato al trigger** — a differenza della vecchia
  staffetta, lo slave non deve mai rilanciare il segnale a nessun altro nodo,
  quindi D8 non è cablato per questo scopo sugli slave (può restare libero
  sulla scheda per usi futuri, ma non fa parte dello schema di sincronizzazione).
- `GND` comune a tutti i nodi (obbligatorio)

```cpp
const int PIN_TRIGGER_IN  = 2;   // SOLO sugli slave, interrupt-capable
const int PIN_TRIGGER_OUT = 8;   // SOLO sul master
```

## Requisiti software
- Rilevamento impulsi via `attachInterrupt` (RISING) su `PIN_TRIGGER_IN`, mai
  polling
- ISR minimale: solo flag `volatile` + contatore impulsi + debounce (finestra
  2ms via `micros()`), nessuna logica pesante dentro l'interrupt
- Tutta la logica reale (confronto NODE_ID, generazione f1/f2) va nel `loop()`
- Nessun `delay()` per l'attesa del trigger sugli slave (event-driven)
- **Reset del contatore ad ogni nuovo ciclo**: se non arrivano impulsi per più
  di una soglia di timeout (es. 300ms), lo slave resetta `contaImpulsi = 0` in
  autonomia (self-healing, senza bisogno di un comando di reset esplicito)

## Cosa NON fare
- Non implementare la staffetta punto-punto (superata da questa versione)
- Non collegare D8 sugli slave per scopi di trigger/rilancio
- Non introdurre librerie esterne per il trigger, solo `attachInterrupt` nativo
- Non duplicare le funzioni di generazione toni: riusarle da `trasmettitore.ino`
- Non toccare `trasmettitore.ino` esistente (resta il riferimento a nodo singolo)
- `main.py`/`config_condivisa.py`: eccezione puntuale già applicata -
  `NUMERO_NODI` vive in `config_condivisa.py` (deve restare allineato con
  `master.ino`, stesso motivo per cui ci vivono F1/F2/T0/T1/GAP) e `main.py`
  lo legge da lì per il warning "interruttori attesi/ciclo" (lo spinbox
  manuale è stato rimosso, non più modificabile a runtime). A parte questo,
  non toccare altro in quei due file senza un motivo equivalente.

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
   interruttore tenuto chiuso per più cicli deve continuare a generare f2 ad
   ogni ciclo, non solo al primo). Torna a `false` in UN SOLO punto: dentro
   `TONO_SLOT_PROPRIO`, subito dopo essere stato consumato per `canale2_on`.
   Nessun altro punto del codice deve scriverci.

Il controllo seriale (`ON`/`OFF`, invariato rispetto a `trasmettitore.ino`) parte
già attivo al boot in entrambi gli sketch - resta comunque disattivabile a mano.

## Nota per estensione futura a N nodi
Aggiungere un nodo richiede solo: collegare il suo `D2` in parallelo allo
stesso bus condiviso (con pull-down 10kΩ locale), assegnargli il NODE_ID
incrementale successivo. Nessuna modifica al master né agli altri slave.