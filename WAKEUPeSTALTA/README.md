# WAKEUPeSTALTA

Sistema di sorveglianza perimetrale su fibra ottica (interferometro Sagnac):
ascolta la fibra in attesa di un evento vibrazionale forte, accende una
presa Shelly che alimenta una catena di Arduino ("daisy chain") posizionati
sotto i tappeti/zerbini, e identifica quale tappeto è stato premuto
leggendo un tono in frequenza (f1/f2) trasmesso dagli Arduino sulla stessa
fibra.


## Cosa fa

1. **AMBIENTE** — ascolto continuo del segnale della fibra, calcolo in
   tempo reale del rapporto STA/LTA (energia a breve termine vs a lungo
   termine). Quando il rapporto supera una soglia, è successo qualcosa di
   forte.
2. **RISVEGLIO** — accende la presa Shelly che alimenta la daisy chain di
   Arduino e aspetta che si sincronizzi (qualche secondo).
3. **ARMATO** — ascolta il protocollo f1/f2 trasmesso dagli Arduino per
   capire quale tappeto è premuto, e lo mostra a schermo. Resta armato
   finché c'è attività; torna da solo ad AMBIENTE dopo un periodo di
   inattività (o comunque dopo un tetto massimo di tempo, anche con
   attività continua — entrambi i timeout sono regolabili dall'interfaccia).

Se la presa Shelly non risponde (rete, IP cambiato, presa scollegata), il
sistema **non** prosegue: resta in
RISVEGLIO, mostra un avviso e riprova a intervalli regolari finché non si
accende davvero.

## Struttura del progetto

```
WAKEUPeSTALTA/
├── main.py                      interrogatore + GUI + macchina a stati (avvio: python main.py)
├── analizza_armoniche_sweep.py  taratura: trova le frequenze migliori per questa fibra
├── analizza_tempi_minimi.py     taratura: trova i tempi T1/GAP minimi affidabili
├── requirements.txt             dipendenze Python, versioni testate
├── others/                      moduli di supporto, non punti di ingresso (vedi sotto)
│   ├── __init__.py
│   ├── tema.py                  design system condiviso (palette, QSS, componenti riusati)
│   ├── widget_timer.py          widget dei due timer di riarmo (inattività / tetto massimo)
│   ├── vista_impianto.py        widget con il disegno dell'impianto (tab 3, vista cliente)
│   ├── shelly_control.py        controllo diretto della presa Shelly via RPC HTTP (anche da riga di comando)
│   └── shelly_mode_selector.py  sceglie la modalità del master ciclando la Shelly (vedi sotto)
└── arduino/
    ├── master/master.ino    sketch del nodo MASTER (NODE_ID=1) - UNO sketch, 3 modalità (vedi sotto)
    ├── slave2/slave2.ino    sketch del nodo SLAVE2 (NODE_ID=2)
    └── restart.md           architettura del bus, wiring, timing — leggerlo prima di toccare gli sketch
```

`main.py` è l'unico punto di ingresso per l'uso normale: GUI e logica di
rilevamento vivono nello stesso processo, nessun altro script da avviare a
parte gli Arduino (che girano per conto loro una volta flashati e
alimentati). Gli altri due script Python (`analizza_*`) servono solo per
la taratura occasionale, vedi sotto. I moduli in `others/` non si lanciano
da soli nell'uso normale: sono importati da `main.py` e/o dagli script
`analizza_*` (package Python vero e proprio, grazie a `others/__init__.py` —
import con `from others.xxx import ...`).

## Le 3 modalità del master (nessun USB necessario dopo l'installazione)

`arduino/master/master.ino` non è solo il programma della daisy chain: è
un unico sketch con **tre modalità** (0 = daisy chain produzione, 1 = sweep
frequenze, 2 = sweep tempi), perché una volta installato il master non è
quasi mai raggiungibile via USB per riflasharlo. La modalità si sceglie
ciclando **solo l'accensione/spegnimento della Shelly** che lo alimenta —
nessun filo o comando in più:

- Un ciclo di accensione **breve** (meno di ~2.5s) fa avanzare un
  contatore di modalità salvato in EEPROM.
- Un'accensione **lasciata attiva** più a lungo la conferma: il master
  parte in quella modalità e ci resta finché non viene spenta.
- Al ciclo successivo (qualsiasi, anche normale) si torna da solo alla
  modalità 0 — nessuna azione per "uscire" da una modalità di test.

Questo è sicuro per l'uso reale: un'intrusione vera accende la Shelly per
minuti, non secondi, quindi non può mai essere scambiata per una richiesta
di cambio modalità. **Gli script `analizza_armoniche_sweep.py` e
`analizza_tempi_minimi.py` pilotano la Shelly da soli** (tramite
`others/shelly_mode_selector.py`) per scegliere la modalità giusta prima di
partire — non serve farlo a mano, basta lanciare lo script.

## Requisiti e installazione

Serve Python 3.10 o superiore (testato con 3.14.6 su Windows).

```
pip install -r requirements.txt
```

Installa: `PySide6` (GUI), `pyqtgraph` (grafici in tempo reale), `numpy`
(elaborazione numerica del segnale), `requests` (controllo della presa
Shelly via RPC HTTP).

## Avvio

```
python main.py
```

Si apre un'unica finestra con tre tab.

### Tab 1 · Taratura STA/LTA

Mostra la forma d'onda e lo storico del rapporto STA/LTA dal vivo, con le
soglie ON/OFF e le finestre STA/LTA regolabili a schermo. **In questa tab
il sistema è in modalità "a secco" (dry-run)**: un evento sopra soglia viene
solo loggato ("Avrei attivato RISVEGLIO...") — non accende la presa Shelly
né cambia stato davvero. Serve per tarare le soglie guardando la forma
d'onda reale, senza far scattare l'impianto ad ogni prova.

Il pulsante **"OK STA/LTA TARATO"** (in alto a destra) conferma la taratura:
da quel momento in poi un evento sopra soglia è **reale** (accende la
Shelly, sveglia la daisy chain) e si sbloccano le tab 2 e 3. La conferma non
è reversibile dall'interfaccia: i parametri restano poi in sola lettura,
apposta per evitare di toccare uno spinbox per sbaglio e far scattare un
risveglio vero.

### Tab 2 · Pannello di controllo

Vista tecnica per chi installa/manutiene il sistema: forma d'onda,
contatore della daisy chain, timer di riarmo regolabili (vedi sotto), campo
per l'IP della presa Shelly.

### Tab 3 · Pannello di visualizzazione

Vista pensata per il cliente finale: niente numeri grezzi o soglie, solo lo
stato dell'impianto (anello colorato: verde = ascolto, rosso pulsante =
risveglio, grigio con tappeti colorati = armato) e i due timer in versione
compatta. Un pulsante RESET (disegnato sull'impianto) spegne la presa e
torna subito in ascolto, senza aspettare i timeout.

### I due timer di riarmo (presenti in tab 2 e 3, sincronizzati)

- **INATTIVITÀ** — prioritario. Si azzera ad ogni chiusura di un tappeto:
  scade quando i tappeti restano fermi per N minuti. Pensato per il caso
  normale (evento breve, poi silenzio): si torna in ascolto appena finisce
  l'attività, non dopo un tempo fisso.
- **TETTO MASSIMO** — rete di sicurezza. Scorre sempre da quando si entra in
  ARMATO e **non si azzera mai**, nemmeno con attività continua — copre il
  caso patologico in cui l'inattività non scatterebbe mai da sola (sensore
  bloccato, rumore continuo). Può essere disattivato con l'interruttore
  "Mai" se non si vuole nessun limite di tempo.

## Configurazione

- **IP della presa Shelly**: modificabile a runtime dal campo "SMART PLUG"
  nella tab 2 (il DHCP può riassegnarlo di tanto in tanto) — non serve
  toccare il codice. Il valore di partenza è in `Config.SMART_PLUG_HOST`
  dentro `main.py`.
- **Soglie e finestre STA/LTA**: regolabili a schermo nella tab 1 (vedi
  sopra); i valori di partenza sono le costanti `STA_LTA_SOGLIA_*` e
  `*_FINESTRA_SEC_DEFAULT` in testa a `main.py`.
- **Timeout di riarmo** (inattività, tetto massimo, attesa di sincronizzazione
  della daisy chain): regolabili a schermo nelle tab 2/3; i valori di
  partenza sono in `Config` dentro `main.py` e sono marcati `TODO` nel
  codice — non ancora validati con un test lungo sul campo.
- **Frequenze e timing del protocollo f1/f2** (`F1_HZ`, `F2_HZ`, `T0_MS`,
  `T1_MS` in testa a `main.py`): **devono restare identiche** alle costanti
  in `arduino/master/master.ino` — è l'unico sketch che genera audio (vedi
  sotto), `slave2.ino` non le usa più. Se si cambiano da una parte, vanno
  aggiornate anche dall'altra — non c'è nessuna sincronizzazione
  automatica.

## La daisy chain Arduino

Gli sketch in `arduino/` sono una **copia di deployment**, pronta per essere
flashata così com'è (costanti già impostate, nessuna dipendenza esterna a
parte le librerie AVR standard incluse in ogni file). Leggere
`arduino/restart.md` prima di modificare qualcosa: spiega la topologia del
bus condiviso, il wiring (master → tutti gli slave in parallelo), il timing
e perché i margini di sicurezza sono quei valori.

In breve:
- Il **master** (NODE_ID=1) è l'**unico** generatore di f1/f2 di tutta la
  catena: scandisce il tempo, genera gli impulsi di indirizzamento sul bus
  condiviso, legge il proprio interruttore per il proprio slot e
  **interroga** ogni slave remoto (sulla stessa linea, a turno) per sapere
  se è vivo e se il suo interruttore è chiuso — poi genera lui il tono
  anche per quello slot.
- Ogni **slave** (oggi solo `slave2`, NODE_ID=2) non genera più nessun
  tono: legge solo il proprio interruttore locale e, quando il master lo
  interroga, risponde con un impulso sulla stessa linea del bus (nessun filo
  in più) la cui durata codifica "vivo + stato interruttore".
- f1 fa da "clock/contatore" (marcatore di inizio ciclo + numero di slot),
  f2 si sovrappone a f1 solo se l'interruttore di quel nodo (proprio o
  remoto) è chiuso. Un nodo remoto che non risponde non lascia un buco nel
  conteggio: il master genera comunque f1 per quello slot — la diagnosi
  "nodo non risponde" si legge sul Serial Monitor del master, vedi
  `arduino/restart.md` § "Interrogazione remota".

Per aggiungere un terzo nodo: vedi la nota "estensione futura" in
`arduino/restart.md` — serve un nuovo sketch `slave3` (copia di `slave2.ino`
con `NODE_ID_PROPRIO=3`) e incrementare `NUMERO_NODI` solo in `master.ino`
(gli slave non hanno bisogno di conoscerlo).

## Note operative

- **Chiudere l'app dalla X della finestra**, non con Ctrl+C o task manager:
  `closeEvent` spegne la presa Shelly e aspetta che il thread di rete finisca
  pulito prima di uscire — saltandolo si perde entrambe le cose.
- All'avvio (o ad ogni ritorno in AMBIENTE) il rapporto STA/LTA ha un
  transitorio di circa 1 secondo in cui i trigger vengono ignorati
  (si vede un avviso "Stabilizzazione in corso..."): è normale, serve a non
  scattare su un falso allarme dovuto solo al riavvio del calcolo.
- Se la presa Shelly non risponde, il pannello mostra un avviso ambra
  "SMART PLUG NON RISPONDE" e il sistema riprova da solo ogni 2 secondi,
  restando in RISVEGLIO (non passa mai ad ARMATO senza una daisy chain
  davvero alimentata).
