// Sketch standalone per Arduino Mega - nessuna dipendenza da file esterni
// a parte le librerie AVR incluse sotto. restart.md (nella cartella
// arduino/ che contiene anche questa) spiega l'architettura completa del
// bus e il perche' delle scelte di timing: tienilo vicino a questo file se
// serve ritoccare qualcosa. Le costanti F1/F2/T0/T1/GAP qui sotto sono
// valori fissi (non generate da uno script): se vanno cambiate, si modifica
// direttamente qui E nello sketch gemello (slave2/slave2.ino), a mano.
//
// Nodo MASTER della catena daisy chain - bus condiviso, open-loop a tempo
// fisso (vedi restart.md). A differenza della versione precedente, il
// master e' l'UNICO generatore di f1/f2 di tutta la catena: gli slave non
// generano piu' nessun tono, hanno solo un interruttore locale da riferire.
// Motivo (vedi restart.md "Interrogazione remota" per i dettagli): con N
// generatori indipendenti un nodo muto in mezzo alla sequenza sfasava
// silenziosamente l'identita' di tutti i nodi successivi (il contatore del
// ricevitore e' ordinale, non legato al tempo assoluto) - con un solo
// generatore che decide lui stesso cosa suonare per ciascuno slot, quel
// bug non puo' piu' esistere.
//
// Il master e' anche il nodo NODE_ID=1 del protocollo f1/f2
// ("MASTER=SLAVE1" in restart.md): scandisce da solo il tempo, senza
// aspettare conferma da nessuno slave per la TEMPORIZZAZIONE, ed emette sul
// bus (D8/PIN_BUS, in parallelo a tutti gli slave) una sequenza di impulsi
// di indirizzamento:
//   impulso 0 -> f1 per T0_MS (marcatore di zero)
//   impulso 1 -> f1 per T1_MS (+f2 se il PROPRIO interruttore e' chiuso) -
//                e' il master stesso (NODE_ID=1), nessuna interrogazione
//   impulso k (1 < k <= NUMERO_NODI) -> e' il turno dello slave NODE_ID=k:
//                il master rilascia brevemente il bus e ascolta un impulso
//                di risposta da quel nodo (vivo? interruttore chiuso?),
//                poi genera LUI il tono f1 [+f2] per conto di quello slot
// Dopo l'ultimo impulso, pausa fine ciclo e si riparte dall'impulso 0.
//
// Interrogazione remota: stessa linea del trigger (PIN_BUS), usata a turno
// nei due versi - MAI un filo in piu'. Il master guida la linea per
// l'impulso di indirizzamento, poi la rilascia (INPUT) e ascolta; lo slave
// indirizzato, dopo un margine di sicurezza, la guida lui per un impulso
// la cui DURATA codifica "vivo + stato interruttore" (vedi costanti sotto).
// Mai due nodi guidano il bus insieme, per costruzione temporale (stessa
// disciplina gia' usata per i toni).
//
// Sintesi f1/f2 (DDS + PWM su OC1B + filtro RC esterno).

#include <avr/io.h>
#include <avr/interrupt.h>
#include <math.h>

// --- Pin daisy chain (restart.md - non cambiare senza aggiornare anche il
// cablaggio). Bidirezionale: OUTPUT per gli impulsi di indirizzamento,
// INPUT quando si ascolta la risposta di un nodo remoto. ---
const int PIN_BUS = 8; // bus condiviso, verso D2 di TUTTI gli slave in parallelo

uint8_t sineTable[256];
volatile uint32_t phase1 = 0, phase2 = 0;
uint32_t inc1, inc2;
// Hz, frequenza di aggiornamento del DDS (16 campioni/periodo per f1, 40
// campioni/periodo per f2).
const uint32_t SAMPLE_RATE = 40000;

// --- Frequenze canale (Hz) ---
const uint32_t FREQ1 = 4400;  // f1: clock/counter (cambiata da 4600 il 13/10/2026, vedi TEST ARMONICHE su questa fibra)
const uint32_t FREQ2 = 3200;  // f2: stato interruttore

// --- Timing protocollo. Solo il master ha T0_MS: e' l'unico nodo che
// emette il marcatore di zero. ---
const unsigned long T0_MS  = 200; // durata marcatore di zero
const unsigned long T1_MS  = 50;  // durata del proprio slot (e di ogni slot remoto)
const unsigned long GAP_MS = 70;  // silenzio dopo ogni tono, prima del prossimo impulso

// --- Identita' di questo nodo sul bus e topologia corrente ---
const uint8_t NODE_ID_PROPRIO = 1; // il master e' sempre NODE_ID 1
// Numero totale di nodi nel ciclo (oggi: master + slave2 = 2). Deve
// restare uguale in TUTTI gli sketch della catena (master e ogni slave):
// aggiungere un nodo in futuro richiede di incrementare questo valore qui
// E nel sketch del nuovo slave, nessun'altra modifica architetturale
// (restart.md "Nota per estensione futura").
const uint8_t NUMERO_NODI = 2;

// Durata dell'impulso di trigger/indirizzamento in uscita (5-10ms,
// restart.md).
const unsigned long DURATA_IMPULSO_TRIGGER_MS = 8;

// --- Interrogazione dei nodi remoti (vedi intestazione sopra). Il nodo
// remoto aspetta MARGINE_SICUREZZA_RISPOSTA_MS dal proprio impulso di
// indirizzamento prima di rispondere: deve restare ABBONDANTEMENTE piu'
// lungo di DURATA_IMPULSO_TRIGGER_MS, altrimenti risponderebbe mentre il
// master sta ancora guidando lui stesso il bus (contesa). La durata
// dell'impulso di risposta codifica lo stato: corto = aperto, lungo =
// chiuso, NESSUN impulso entro TIMEOUT_RISPOSTA_MS = nodo non funzionante
// (per questo ciclo - si riprova al ciclo successivo, non e' un guasto
// permanente dal punto di vista del protocollo). Valori di partenza, da
// verificare sull'hardware reale. ---
const unsigned long MARGINE_SICUREZZA_RISPOSTA_MS = 15;
const unsigned long DURATA_RISPOSTA_APERTO_MS = 6;
const unsigned long DURATA_RISPOSTA_CHIUSO_MS = 18;
// A meta' tra le due durate sopra, stessa logica delle soglie f1 zero/slot.
const unsigned long SOGLIA_CLASSIFICAZIONE_RISPOSTA_MS =
    (DURATA_RISPOSTA_APERTO_MS + DURATA_RISPOSTA_CHIUSO_MS) / 2;
// Deve coprire margine_sicurezza_risposta + la risposta piu' lunga
// possibile (chiuso) + un margine extra per il jitter reale di
// interrupt/loop() sullo slave.
const unsigned long TIMEOUT_RISPOSTA_MS = 45;

// Pausa dopo l'ultimo impulso del ciclo prima di ripartire dall'impulso 0.
// Deve restare ABBONDANTEMENTE piu' lunga di SOGLIA_TIMEOUT_RESET_MS (vedi
// slave2.ino) cosi' ogni slave si "auto-resetta" per tempo e riconosce il
// prossimo impulso 0 come tale (self-healing, restart.md).
const unsigned long PAUSA_FINE_CICLO_MS = 500;

// Interruttore locale reale su A0: chiuso = HIGH (pull-down esterno 10k
// verso GND, switch verso 5V/VCC - stesso schema gia' usato per il bus di
// trigger D8/D2, vedi restart.md).
const int PIN_INTERRUTTORE = A0;

// Debounce software sulla lettura del pin (rimbalzo meccanico tipico
// 5-30ms): una transizione e' accettata solo se la lettura resta stabile
// per almeno questo tempo, altrimenti un singolo click rischierebbe di
// generare piu' fronti spuri e far scattare il latch per rumore elettrico
// invece che per una pressione vera.
const unsigned long DEBOUNCE_INTERRUTTORE_MS = 30;
bool letturaGrezzaCorrente = false; // ultima lettura del pin, non ancora stabilizzata
unsigned long ultimo_cambio_lettura_ms = 0;

// Latch, non una lettura istantanea: true se l'interruttore e' stato chiuso
// in QUALSIASI momento dall'ultimo reset, anche se esattamente nel momento
// del proprio slot risulta di nuovo aperto - altrimenti un click breve tra
// un ciclo e l'altro andrebbe perso. Va a true SOLO dentro
// aggiorna_interruttore() (mai a false li' dentro), e torna a false SOLO in
// TONO_SLOT_PROPRIO subito dopo essere stato consumato per canale2_on
// (vedi aggiorna_master()) - nessun altro punto del codice deve scriverci.
bool interruttoreChiuso = false;

// Valore GREZZO (debounced) del pin, sale E scende liberamente - usato
// internamente da aggiorna_interruttore() per decidere quando riarmare il
// latch sotto. Non stampato (debug via pin HIGH/LOW rimosso, non serve
// piu': il cablaggio era il problema, non la lettura).
bool valoreGrezzoInterruttore = false;

// Isolamento canali per la procedura di taratura soglie.
const bool CANALE1_ABILITATO = true;
const bool CANALE2_ABILITATO = true;

// --- Controllo master via seriale (vedi gestisci_seriale()): il suono
// parte GIA' ACCESO al boot/reset (comodo per non dover scrivere "ON" ad
// ogni accensione), ma resta comunque disattivabile/riattivabile a mano
// scrivendo "OFF"/"ON" sul Serial Monitor. ---
volatile bool soundEnabled = true;
volatile bool canale1_on = false;
volatile bool canale2_on = false;

// Per stampare lo stato dell'interruttore solo quando cambia (debug, vedi
// stampa_stato_interruttore()), non ad ogni giro di loop().
bool interruttoreChiuso_stampato = false;

// Stato "vivo" dell'ultima interrogazione di un nodo remoto (vedi
// aggiorna_master(), stato ATTESA_RISPOSTA_REMOTA) - usato sia per decidere
// se suonare f2 sia per il log diagnostico sotto.
bool nodoRemotoVivo = false;
bool nodoRemotoChiuso = false;

// Diagnostica "nodo X non risponde" (vedi intestazione sopra): stampata
// solo sui cambi di stato per nodo, non ad ogni ciclo (altrimenti un nodo
// davvero giu' per minuti intaserebbe il Serial Monitor di righe identiche)
// - dimensionato oltre NUMERO_NODI per tollerare future espansioni della
// catena senza doverci ripensare.
const uint8_t MAX_NODI_SUPPORTATI = 8;
bool vivoPrecedente[MAX_NODI_SUPPORTATI] = {false};
bool vivoNotoPrecedente[MAX_NODI_SUPPORTATI] = {false}; // garantisce un primo log anche se gia' giu' al boot

// --- Macchina a stati del ciclo (non blocca mai in loop() tranne
// l'impulso di trigger e l'interrogazione remota, esplicitamente
// autorizzati bloccanti da restart.md - il DDS continua a girare in
// background via Timer2, indipendente da loop()). contatore_impulso e' il
// "k" descritto sopra. ---
enum StatoMaster : uint8_t {
  EMETTI_IMPULSO,
  TONO_T0,
  GAP_DOPO_T0,
  TONO_SLOT_PROPRIO,
  GAP_DOPO_SLOT_PROPRIO,
  ATTESA_RISPOSTA_REMOTA,
  TONO_SLOT_REMOTO,
  GAP_DOPO_SLOT_REMOTO,
  PAUSA_FINE_CICLO
};
StatoMaster stato = EMETTI_IMPULSO;
uint8_t contatore_impulso = 0;
unsigned long inizio_stato_ms = 0;

void setup() {
  Serial.begin(9600);

  for (int i = 0; i < 256; i++) {
    sineTable[i] = (uint8_t)(127.5 + 127.5 * sin(2.0 * PI * i / 256.0));
  }
  inc1 = (uint32_t)(((uint64_t)FREQ1 << 32) / SAMPLE_RATE);
  inc2 = (uint32_t)(((uint64_t)FREQ2 << 32) / SAMPLE_RATE);

  pinMode(12, OUTPUT); // OC1B, uscita audio f1/f2 (verso il trasduttore/fibra)
  pinMode(11, OUTPUT);
  digitalWrite(11, HIGH); // coerente con soundEnabled = true
  pinMode(PIN_BUS, OUTPUT);
  digitalWrite(PIN_BUS, LOW);
  pinMode(PIN_INTERRUTTORE, INPUT); // pull-down 10k esterno gia' cablato

  // Timer1: Fast PWM 8 bit, nessun prescaler -> portante PWM a 62.5kHz
  TCCR1A = _BV(COM1B1) | _BV(WGM10);
  TCCR1B = _BV(WGM12) | _BV(CS10);
  OCR1B = 128; // silenzio iniziale (valore centrale)

  // Timer2: CTC, genera interrupt a SAMPLE_RATE per aggiornare il campione
  TCCR2A = _BV(WGM21);
  TCCR2B = _BV(CS21); // prescaler 8
  OCR2A = (16000000UL / 8 / SAMPLE_RATE) - 1;
  TIMSK2 = _BV(OCIE2A);

  // Stato iniziale: leggi subito il pin reale, cosi' non si parte sempre da
  // "aperto" per un giro intero se l'interruttore e' gia' chiuso al boot.
  letturaGrezzaCorrente = (digitalRead(PIN_INTERRUTTORE) == HIGH);
  valoreGrezzoInterruttore = letturaGrezzaCorrente;
  if (valoreGrezzoInterruttore) {
    interruttoreChiuso = true;
  }

  inizio_stato_ms = millis();
  sei();

  Serial.println(F("--- daisy chain (bus condiviso): MASTER (NODE_ID=1), unico generatore f1/f2 ---"));
  Serial.print(F("SAMPLE_RATE=")); Serial.print(SAMPLE_RATE); Serial.println(F("Hz"));
  Serial.print(F("FREQ1=")); Serial.print(FREQ1);
  Serial.print(F("Hz  FREQ2=")); Serial.print(FREQ2); Serial.println(F("Hz"));
  Serial.print(F("T0_MS=")); Serial.print(T0_MS);
  Serial.print(F("  T1_MS=")); Serial.print(T1_MS);
  Serial.print(F("  GAP_MS=")); Serial.println(GAP_MS);
  Serial.print(F("NUMERO_NODI=")); Serial.println(NUMERO_NODI);
  Serial.print(F("Interrogazione remota: margine=")); Serial.print(MARGINE_SICUREZZA_RISPOSTA_MS);
  Serial.print(F("ms  risposta aperto=")); Serial.print(DURATA_RISPOSTA_APERTO_MS);
  Serial.print(F("ms  chiuso=")); Serial.print(DURATA_RISPOSTA_CHIUSO_MS);
  Serial.print(F("ms  timeout=")); Serial.print(TIMEOUT_RISPOSTA_MS); Serial.println(F("ms"));
  Serial.print(F("PAUSA_FINE_CICLO_MS=")); Serial.println(PAUSA_FINE_CICLO_MS);
  Serial.print(F("interruttore proprio: ")); Serial.println(interruttoreChiuso ? "CHIUSO" : "aperto");
  interruttoreChiuso_stampato = interruttoreChiuso;
  Serial.println(F("Pronto, suono GIA' ACCESO. Scrivi 'OFF' o 'ON' e premi invio per disattivare/riattivare."));
}

ISR(TIMER2_COMPA_vect) {
  phase1 += inc1;
  phase2 += inc2;
  uint8_t idx1 = phase1 >> 24;
  uint8_t idx2 = phase2 >> 24;

  if (!soundEnabled) {
    OCR1B = 128; // silenzio totale, ma le fasi continuano ad avanzare sopra
    return;
  }

  uint8_t contrib1 = canale1_on ? sineTable[idx1] : 128;
  uint8_t contrib2 = canale2_on ? sineTable[idx2] : 128;
  OCR1B = ((uint16_t)contrib1 + contrib2) / 2;
}

// Misura la durata di un impulso HIGH su PIN_BUS, con timeout - stessa cosa
// che fa pulseIn() di Arduino, ma SENZA usarlo: pulseIn() stima i
// microsecondi contando cicli di CPU, assumendo di avere la CPU
// praticamente per se' durante la misura. Qui NON e' vero - l'interrupt di
// Timer2 (il DDS) spara ogni 25us, continuamente, anche durante questa
// attesa, e quel traffico corrompe il conteggio di pulseIn() al punto da
// non riconoscere mai l'impulso (sintomo osservato dal vivo: il tappeto del
// master, letto con digitalRead diretto, funzionava; quello dello slave,
// che passava da pulseIn(), risultava sempre "non risponde"). micros() usa
// Timer0, non toccato dal nostro Timer2: legge il tempo reale trascorso ad
// ogni iterazione invece di contare cicli "a occhio", quindi resta
// accurato anche con Timer2 che interrompe di continuo.
unsigned long attendi_impulso_risposta(unsigned long timeout_us) {
  unsigned long inizio = micros();
  while (digitalRead(PIN_BUS) == LOW) {
    if ((unsigned long)(micros() - inizio) > timeout_us) return 0;
  }
  unsigned long inizio_impulso = micros();
  while (digitalRead(PIN_BUS) == HIGH) {
    if ((unsigned long)(micros() - inizio) > timeout_us) return 0;
  }
  return (unsigned long)(micros() - inizio_impulso);
}

// Interroga il nodo remoto appena indirizzato e aggiorna nodoRemotoVivo/
// nodoRemotoChiuso. Bloccante (attendi_impulso_risposta sopra): l'audio e'
// gia' silenzioso in questo momento (arriviamo qui solo da EMETTI_IMPULSO),
// il DDS continua a girare in background via Timer2, nessun suono da
// perdere nell'attesa.
void interroga_nodo_remoto() {
  unsigned long durata_risposta_us = attendi_impulso_risposta(TIMEOUT_RISPOSTA_MS * 1000UL);
  if (durata_risposta_us == 0) {
    nodoRemotoVivo = false;
    nodoRemotoChiuso = false;
  } else {
    unsigned long durata_risposta_ms = durata_risposta_us / 1000UL;
    nodoRemotoVivo = true;
    nodoRemotoChiuso = durata_risposta_ms > SOGLIA_CLASSIFICAZIONE_RISPOSTA_MS;
  }

  // Diagnostica: solo sui cambi di stato (o al primo log in assoluto per
  // questo indice), non ad ogni ciclo - vedi dichiarazione degli array sopra.
  uint8_t idx = contatore_impulso;
  if (idx < MAX_NODI_SUPPORTATI &&
      (!vivoNotoPrecedente[idx] || nodoRemotoVivo != vivoPrecedente[idx])) {
    Serial.print(F("Nodo "));
    Serial.print(idx);
    Serial.println(nodoRemotoVivo ? F(": risponde") : F(": NON RISPONDE"));
    vivoPrecedente[idx] = nodoRemotoVivo;
    vivoNotoPrecedente[idx] = true;
  }

  // Richiude il bus in uscita, pronto per il prossimo impulso di
  // indirizzamento (EMETTI_IMPULSO del prossimo giro).
  pinMode(PIN_BUS, OUTPUT);
  digitalWrite(PIN_BUS, LOW);
}

void aggiorna_master(unsigned long ora_ms) {
  unsigned long trascorso = ora_ms - inizio_stato_ms;

  switch (stato) {
    case EMETTI_IMPULSO:
      // Impulso bloccante (autorizzato da restart.md): l'audio e' gia'
      // silenzioso (arriviamo qui solo da uno stato di silenzio o dal boot).
      // Il DDS continua a girare in background (Timer2 non disabilitato da
      // delay()).
      digitalWrite(PIN_BUS, HIGH);
      delay(DURATA_IMPULSO_TRIGGER_MS);
      digitalWrite(PIN_BUS, LOW);

      if (contatore_impulso == 0) {
        stato = TONO_T0;
      } else if (contatore_impulso == NODE_ID_PROPRIO) {
        stato = TONO_SLOT_PROPRIO;
      } else {
        // Rilascia subito il bus: lo slave indirizzato aspetta un margine
        // di sicurezza prima di rispondere, ma prima possibile il master
        // si mette in ascolto meglio e' (vedi interroga_nodo_remoto()).
        pinMode(PIN_BUS, INPUT);
        stato = ATTESA_RISPOSTA_REMOTA;
      }
      inizio_stato_ms = millis();
      break;

    case TONO_T0:
      canale1_on = CANALE1_ABILITATO;
      canale2_on = false;
      if (trascorso >= T0_MS) {
        stato = GAP_DOPO_T0;
        inizio_stato_ms = ora_ms;
      }
      break;

    case GAP_DOPO_T0:
      canale1_on = false;
      canale2_on = false;
      if (trascorso >= GAP_MS) {
        contatore_impulso++;
        stato = EMETTI_IMPULSO;
        inizio_stato_ms = ora_ms;
      }
      break;

    case TONO_SLOT_PROPRIO:
      // canale1_on/canale2_on assegnati insieme (non separatamente): evita
      // che l'ISR del DDS (asincrona rispetto a qui) li legga a meta',
      // uno aggiornato e l'altro ancora no.
      canale1_on = CANALE1_ABILITATO;
      canale2_on = CANALE2_ABILITATO && interruttoreChiuso;
      if (trascorso >= T1_MS) {
        // Latch consumato per questo slot: unico punto del codice che lo
        // rimette a false, pronto a catturare una nuova chiusura nel
        // prossimo ciclo (vedi dichiarazione di interruttoreChiuso).
        interruttoreChiuso = false;
        stato = GAP_DOPO_SLOT_PROPRIO;
        inizio_stato_ms = ora_ms;
      }
      break;

    case GAP_DOPO_SLOT_PROPRIO:
      canale1_on = false;
      canale2_on = false;
      if (trascorso >= GAP_MS) {
        contatore_impulso++;
        stato = (contatore_impulso > NUMERO_NODI) ? PAUSA_FINE_CICLO : EMETTI_IMPULSO;
        inizio_stato_ms = ora_ms;
      }
      break;

    case ATTESA_RISPOSTA_REMOTA:
      // Silenzio esplicito (gia' cosi' per costruzione, arrivando da uno
      // stato di GAP - ridondante ma stesso stile defensive degli altri
      // stati). Bloccante (attendi_impulso_risposta dentro
      // interroga_nodo_remoto): vedi la funzione per il motivo per cui va
      // bene qui.
      canale1_on = false;
      canale2_on = false;
      interroga_nodo_remoto();
      stato = TONO_SLOT_REMOTO;
      inizio_stato_ms = millis();
      break;

    case TONO_SLOT_REMOTO:
      // Il master genera LUI il tono per conto del nodo remoto appena
      // interrogato - f1 sempre (anche se il nodo non risponde: cosi' il
      // contatore ordinale del ricevitore non si sfasa per i nodi
      // successivi, vedi intestazione del file), f2 solo se vivo E chiuso.
      canale1_on = CANALE1_ABILITATO;
      canale2_on = CANALE2_ABILITATO && nodoRemotoVivo && nodoRemotoChiuso;
      if (trascorso >= T1_MS) {
        stato = GAP_DOPO_SLOT_REMOTO;
        inizio_stato_ms = ora_ms;
      }
      break;

    case GAP_DOPO_SLOT_REMOTO:
      canale1_on = false;
      canale2_on = false;
      if (trascorso >= GAP_MS) {
        contatore_impulso++;
        stato = (contatore_impulso > NUMERO_NODI) ? PAUSA_FINE_CICLO : EMETTI_IMPULSO;
        inizio_stato_ms = ora_ms;
      }
      break;

    case PAUSA_FINE_CICLO:
      if (trascorso >= PAUSA_FINE_CICLO_MS) {
        contatore_impulso = 0;
        stato = EMETTI_IMPULSO;
        inizio_stato_ms = ora_ms;
      }
      break;
  }
}

// Monitoraggio dell'interruttore proprio, continuo e indipendente dal ciclo
// del protocollo (chiamato ad ogni giro di loop()): interruttoreChiuso
// riflette sempre lo stato piu' recente, cosi' se cambia mentre il nodo non
// sta trasmettendo il prossimo TONO_SLOT_PROPRIO lo trova gia' aggiornato -
// non serve piu' rileggerlo "a mano" in un punto fisso del ciclo.
void aggiorna_interruttore() {
  bool letturaOra = (digitalRead(PIN_INTERRUTTORE) == HIGH);
  if (letturaOra != letturaGrezzaCorrente) {
    // Il pin ha appena cambiato stato: potrebbe essere un rimbalzo, riparti
    // a cronometrare la stabilita' da qui invece di accettarlo subito.
    letturaGrezzaCorrente = letturaOra;
    ultimo_cambio_lettura_ms = millis();
    return;
  }
  if (millis() - ultimo_cambio_lettura_ms < DEBOUNCE_INTERRUTTORE_MS) {
    return; // non ancora stabile per abbastanza tempo, aspetta
  }
  // Stabile da almeno DEBOUNCE_INTERRUTTORE_MS: accetta la lettura.
  // Valore grezzo (debounced): sale E scende liberamente, riflette il pin
  // cosi' com'e' (a differenza del latch sotto) - usato solo internamente
  // per decidere quando riarmare il latch.
  valoreGrezzoInterruttore = letturaGrezzaCorrente;
  // Latch: va a true ad OGNI passaggio in cui il pin risulta stabilmente
  // chiuso, non solo alla prima transizione false->true - altrimenti un
  // interruttore tenuto chiuso per piu' cicli smetterebbe di essere
  // rilevato subito dopo il primo consumo (TONO_SLOT_PROPRIO lo resetta a
  // false, e senza questo il valore grezzo non cambierebbe piu' finche' il
  // pin resta fermo, quindi il latch non si riarmerebbe mai). Resta vero
  // che non va mai messo a false qui dentro (solo TONO_SLOT_PROPRIO lo
  // consuma, vedi dichiarazione di interruttoreChiuso).
  if (valoreGrezzoInterruttore) {
    interruttoreChiuso = true;
  }
}

void gestisci_seriale() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.equalsIgnoreCase("ON")) {
      digitalWrite(11, HIGH);
      soundEnabled = true;
      Serial.println("Suono ON");
    } else if (cmd.equalsIgnoreCase("OFF")) {
      digitalWrite(11, LOW);
      soundEnabled = false;
      Serial.println("Suono OFF");
    }
  }
}

// Debug: stampa lo stato del LATCH (vedi interruttoreChiuso) SOLO quando
// cambia. Per costruzione va sempre a coppie chiuso/aperto (si accende alla
// chiusura, si spegne quando il proprio slot lo consuma).
void stampa_stato_interruttore() {
  if (interruttoreChiuso != interruttoreChiuso_stampato) {
    interruttoreChiuso_stampato = interruttoreChiuso;
    Serial.println(interruttoreChiuso ? "INT: chiuso" : "INT: aperto");
  }
}

void loop() {
  aggiorna_interruttore();
  stampa_stato_interruttore();
  aggiorna_master(millis());
  gestisci_seriale();
  // nessun delay() qui fuori: solo EMETTI_IMPULSO e ATTESA_RISPOSTA_REMOTA
  // bloccano brevemente, come autorizzato da restart.md.
}
