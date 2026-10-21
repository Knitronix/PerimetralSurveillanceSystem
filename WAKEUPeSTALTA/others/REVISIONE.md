# Revisione completa WAKEUPeSTALTA — 19/10/2026

Revisione di tutta la catena (Python + Arduino + strumenti di taratura) fatta
rileggendo il codice da zero, non la documentazione. Scopo: capire cosa fa
davvero ogni pezzo, cosa è solido, cosa si è perso o complicato per strada.

**Nessuna modifica al codice è stata fatta in questa revisione**: i
problemi sotto sono da decidere insieme (vedi "Domande aperte" e "Piano").

---

## 1. Come funziona oggi, in 6 passi

1. **Ascolto (AMBIENTE)** — `3main.py` riceve la fibra via UDP (24 kHz) e
   calcola lo STA/LTA a banda larga. La daisy chain è **spenta** (Shelly OFF).
2. **Risveglio** — STA/LTA supera la soglia ON → `3main.py` accende la Shelly
   (riprova ogni 2 s se non risponde) e aspetta `ATTESA_BOOT_DAISY_CHAIN_S`
   (2 s). **In questi 2 s `3main.py` non decodifica nulla.**
3. **Boot del master** — all'accensione il master sceglie la modalità
   leggendo l'EEPROM (0 = produzione se la sessione precedente è durata più
   di 2,5 s). Legge l'interruttore **solo da quel momento**: una pressione
   finita prima del boot non esiste per lui.
4. **Ciclo del protocollo** (~1,05 s, si ripete) — tutto generato dal solo
   master su un'unica uscita audio:
   ```
   f1 200ms (marcatore) · 70 · [impulso bus] f1 50ms (+f2 se tappeto 1 chiuso) · 70 ·
   [impulso bus + risposta slave2, ≤45ms] f1 50ms (+f2 se tappeto 2 chiuso) · 70 · pausa 500
   ```
   Le due onde sono **sommate e divise per 2** (una sola uscita PWM 0-255):
   ogni tono suona sempre a metà ampiezza, anche da solo.
5. **Decodifica (ARMATO)** — `3main.py` misura la potenza a f1 e f2 in blocchi
   da 20 ms (Goertzel), accende/spegne con due soglie fisse (isteresi),
   classifica la durata di f1: 9-11 blocchi = marcatore (azzera il
   contatore), 2-4 blocchi = slot (contatore +1). Se f2 era ON durante lo
   slot → "tappeto N chiuso" → ALLARME, e si azzera il timer di inattività.
6. **Ritorno ad AMBIENTE** — dopo N minuti senza chiusure (o il tetto
   massimo, o RESET): Shelly OFF, STA/LTA ripartito da zero.

---

## 2. Costanti che devono coincidere tra file diversi

Nessuna è sincronizzata in automatico: se se ne cambia una, vanno cambiate a
mano **tutte** le copie.

| Cosa | Dove vive | Stato oggi |
|---|---|---|
| f1 / f2 | `master.ino` FREQ1/FREQ2 · `3main.py` F1_HZ/F2_HZ | ✅ 4400 / 3200 ovunque |
| f usata dal test tempi | `master.ino` (= FREQ2, automatico) · `2analizzatempiminimi.py` FREQ_TEST_HZ (**scritto a mano: 3200**) | ⚠ se cambia f2 va aggiornato a mano |
| T0 / T1 | `master.ino` · `3main.py` | ✅ 200 / 50 ms |
| Soglie Goertzel f1/f2 | `3main.py` · `2analizzatempiminimi.py` (copia propria) | ⚠ tarate il 13/10 su fibra e mezzo che oggi non ci sono più |
| Rampa sweep (200-6000, passo 200, 4 s) | `master.ino` · `0trovafrequenze.py` · `analizza_armoniche_sweep.py` | ✅ |
| Tempi interrogazione (15/6/18 ms) | `master.ino` · `slave2.ino` | ✅ |
| Numero di nodi | solo `master.ino` (NUMERO_NODI=2) | ⚠ `3main.py` non lo conosce: non può scartare un "tappeto 3" impossibile |
| Soglia sessione 2,5 s | `master.ino` · `others/shelly_mode_selector.py` | ✅ |
| IP Shelly | `3main.py` Config · `0trovafrequenze.py` · `2analizzatempiminimi.py` · `analizza_armoniche_sweep.py` | ⚠ 4 copie, il DHCP lo cambia |

---

## 3. Problemi trovati

### A — Critici: possono far perdere un evento o indicare il tappeto sbagliato

**A1. Cecità al risveglio: una pressione breve sul tappeto si perde.**
La daisy chain è spenta mentre si ascolta. Tra il passo e il momento in cui
il master inizia a leggere l'interruttore passano: ritardo STA/LTA +
chiamata Shelly + relè + boot dell'Arduino (da misurare, ordine 1-2 s).
Se il piede si è già alzato, la pressione non viene mai vista (il latch
parte solo dopo il boot). In più `3main.py` scarta tutto quello che arriva
nei primi 2 s dopo l'accensione (`ATTESA_BOOT_DAISY_CHAIN_S`), ma il latch
del master si **consuma** al primo ciclo trasmesso: se il master è già
partito e ha trasmesso "chiuso" in quei 2 s, quella chiusura è persa.
Un passo singolo dura ~0,5 s: con l'architettura attuale un passaggio
rapido su un tappeto è probabilmente **non identificabile**, anche se lo
STA/LTA lo ha sentito. → vedi Domanda 1.

**A2. Slot contati prima del primo marcatore → tappeto sbagliato.**
All'ingresso in ARMATO il contatore parte da 0 e conta qualunque slot,
anche se il marcatore di quel ciclo non è stato visto. Se ARMATO inizia tra
lo slot 1 e lo slot 2, lo slot 2 viene numerato "1": la chiusura del tappeto
2 viene riportata come **tappeto 1**. Stessa cosa se un marcatore si perde
(f1 debole): il contatore continua a salire (3, 4, 5…) finché non ne arriva
uno buono. Correzione semplice: contare gli slot solo dopo un marcatore
visto, e scartare contatori > numero di nodi.

**A3. Soglie f1/f2 fisse e ormai vecchie.** — *RISOLTO il 19/10/2026 (causa
trovata): le soglie del 13/10 erano calcolate su un tono a piena ampiezza,
senza il fattore 1/4 della produzione - erano 4x troppo alte da sempre; con
la fibra nuova f2 finiva sotto soglia (nessun tappeto sarebbe mai risultato
chiuso). Ricalcolate con `0trovafrequenze.py` in `3main.py` e in
`2analizzatempiminimi.py` (lì x4, piena ampiezza). Frequenze 4400/3200
confermate: margine 23-24 dB, OTTIMA. Da verificare dal vivo premendo un
tappeto.* Testo originale:
Le soglie in `3main.py` sono valori assoluti tarati il 13/10. Oggi sono
cambiati fibra e mezzo, e le misure di sweep mostrano che la risposta
della fibra a una stessa frequenza può cambiare di 10 volte tra un giorno e
l'altro. Vanno ricalcolate con `0trovafrequenze.py` (che le propone già).
A medio termine: soglie **relative** al marcatore (che è sempre f1 per
200 ms, quindi una misura di riferimento gratuita ad ogni ciclo), così si
adattano da sole a fibra, mezzo e temperatura.

**A4. Le frequenze attuali (4400/3200) sono state scelte su dati probabilmente
sfasati.** `analizza_armoniche_sweep.py` etichetta i gradini con un errore
di un gradino (+200 Hz): il master inizia la rampa al boot, lo script fa
partire il suo orologio ~3 s dopo. Lo sweep di oggi lo dimostra. 4400 è
risultata debole nella misura corretta. → rifare la scelta con
`0trovafrequenze.py`.

### B — Importanti

**B1. Selettore di modalità: rischio di partire in SWEEP durante un evento vero.**
Se una sessione di produzione dura meno di 2,5 s (RESET premuto subito dopo
un risveglio, app chiusa durante il RISVEGLIO, blackout, rimbalzo del relè),
il master la interpreta come "ciclo di selezione" e **al risveglio successivo
parte in modalità SWEEP**: per tutto quel risveglio nessun tappeto viene
sentito (resta solo l'avviso "TAPPETI NON RILEVATI"). Si ripara da solo al
giro dopo, ma quel giro è cieco. Mitigazione semplice lato `3main.py`: non
spegnere mai la Shelly prima di 3 s dall'accensione.

**B2. Un nodo remoto morto è indistinguibile da "tappeto aperto".**
Se slave2 non risponde, il master genera comunque lo slot (senza f2): sulla
fibra si legge "tappeto 2 aperto", per sempre. La diagnosi esiste solo sul
Serial Monitor del master, che in campo non è raggiungibile. Un tappeto
guasto non darà mai allarme e nessuno lo saprà. Proposta: codificare "nodo
muto" con una durata di slot diversa (es. 100 ms) e mostrarlo in `3main.py`.

**B3. Margine del reset dello slave: 14 ms.**
Lo slave si azzera se non vede impulsi per 300 ms. Tra l'impulso del
marcatore e il successivo passano 8 + 200 + 70 = 278 ms (più la latenza di
`loop()`). Se un giorno si allunga T0 o GAP anche di poco, lo slave si
azzera a metà ciclo e risponde allo slot sbagliato o mai. Valore più sicuro:
~400 ms (resta sotto la pausa di fine ciclo, 570 ms di silenzio).

**B4. RESET / timeout dimenticano le finestre STA/LTA tarate.**
`_torna_ad_ambiente` ricrea il rilevatore con `STA_FINESTRA_SEC_DEFAULT` e
`LTA_FINESTRA_SEC_DEFAULT`, non con i valori degli spinbox. Dopo il primo
ritorno ad AMBIENTE le finestre tarate nella tab 1 sono perse (le soglie
ON/OFF invece restano). Correzione di una riga.

### C — Minori / pulizia

- **C1.** Un blip di rumore di 20 ms su f1 viene scartato solo per un
  arrotondamento in virgola mobile (|0,02−0,05| = 0,030000000000000002 >
  0,03). Funziona, ma per caso: meglio una tolleranza esplicita (25 ms) o
  contare i blocchi interi.
- **C2.** `2analizzatempiminimi.py` ha f e soglie scritte a mano (3200 Hz,
  soglie vecchie): se cambia f2 va riallineato.
- **C3.** README e commenti in `master.ino` indicano ancora
  `analizza_armoniche_sweep.py` come strumento per scegliere le frequenze.
- **C4.** Timeout (boot 2 s, inattività 2 min, tetto 5 min) ancora marcati
  TODO, mai validati con un test lungo.
- **C5.** IP Shelly in 4 copie (vedi tabella sopra).

---

## 4. Cosa è solido (da non toccare)

- Generazione centralizzata sul master: elimina lo sfasamento delle identità
  se un nodo tace (scelta giusta, ben documentata in `restart.md`).
- Latch con debounce sugli interruttori: una chiusura breve **tra due cicli**
  non si perde (il problema A1 è prima del boot, non durante).
- Interrogazione dello slave misurata con `micros()` invece di `pulseIn()`.
- Gestione della Shelly che non risponde (resta in RISVEGLIO, riprova).
- Attesa di stabilizzazione dello STA/LTA dopo ogni ripartenza.
- La decodifica in `3main.py` (Goertzel + isteresi + durata) è semplice e
  corretta nella sostanza: i problemi A2/C1 sono ai bordi, non nel cuore.

---

## 5. Domande aperte (da decidere insieme)

1. **A1 — DECISO (19/10/2026): gli Arduino restano SPENTI mentre si ascolta.**
   Motivo: i toni saturano la fibra e lo STA/LTA non sentirebbe altri passi;
   senza modulo WiFi l'unico comando disponibile è accendere/spegnere la
   Shelly, quindi non c'è modo di tenerli accesi ma muti. Conseguenza: la
   pressione persa PRIMA del boot (perdita hardware) oggi non si elimina, si
   misura (log del ritardo) e si elimina la sola perdita software (decodifica
   subito dopo l'accensione). Da riconsiderare con il modulo WiFi: Arduino
   sempre alimentati e muti, che registrano le chiusure in continuo e parlano
   solo su comando (risolverebbe anche B1).
2. **Modulo WiFi (ESP) sul master**: se si fa, conviene fargli fare tutto
   insieme — cambio frequenze, comando "parla/zitto", scelta della modalità
   — al posto dei cicli della Shelly.
3. **B2** — vuoi vedere sul PC quando un tappeto/nodo è guasto? (serve
   toccare master + `3main.py`).
4. **Timeout reali** (C4): quanto deve restare armato il sistema dopo
   l'ultima chiusura, in campo?

## 6. Piano proposto, in ordine

1. Misura `0trovafrequenze.py` sulla configurazione attuale → nuove f1/f2 e
   soglie (A3, A4).
2. Correzioni piccole e sicure in `3main.py`: slot solo dopo marcatore e
   tetto al contatore (A2), finestre STA/LTA conservate (B4), Shelly mai
   spenta prima di 3 s (B1), tolleranza esplicita (C1), decodifica attiva
   subito dopo l'accensione invece che dopo 2 s (metà di A1).
3. `slave2.ino`: reset a 400 ms (B3) — richiede un flash via USB.
4. Decisione sulla Domanda 1 (e 2): è l'unico modo per chiudere A1 del tutto.
5. Pulizia: `analizza_armoniche_sweep.py` in pensione, README aggiornato,
   IP Shelly in un posto solo (C2, C3, C5).
