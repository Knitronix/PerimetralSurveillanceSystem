// Nodo SLAVE (NODE_ID=2, slot 2 del protocollo) della catena daisy chain -
// bus condiviso, open-loop a tempo fisso (vedi DAISY CHAIN/restart.md,
// versione corrente: NON e' piu' una staffetta punto-punto - questo slave
// non aspetta piu' un trigger dedicato da un nodo precedente ne' rilancia
// nulla a un nodo successivo, vedi master/master.ino per chi pilota il bus).
//
// L'uscita D8 del master e' collegata in parallelo su D2 di TUTTI gli
// slave (bus broadcast, non punto-punto): questo slave conta gli impulsi
// che vede passare sul bus e agisce SOLO quando il conteggio coincide con
// il proprio NODE_ID. Il primo impulso ricevuto dopo un silenzio prolungato
// (fine ciclo precedente) vale "0" (il marcatore di zero del master, f1 per
// T0_MS - mai generato qui, solo il master lo emette): da li' si riparte a
// contare 1, 2, 3... Questo nodo agisce quando il conteggio = 2.
//
// Questo slave NON ha un'uscita di trigger: nella topologia a bus solo il
// master (ed eventuali "ripetitori" futuri per bus molto lunghi, non questo
// nodo) guida D8 - restart.md, sezione Pin.
//
// Sintesi f1/f2 (DDS + PWM su OC1B + filtro RC esterno) duplicata da
// master.ino/../../trasmettitore/trasmettitore.ino: l'IDE Arduino non
// segue #include relativi fuori dalla cartella dello sketch.
//
// F1/F2/T1_MS/GAP_MS sotto sono AUTOGENERATI da ../aggiorna_config_daisy.py
// a partire da ../../config_condivisa.py (unica fonte di verita', la stessa
// gia' usata da main.py) - non modificarli a mano qui. Niente T0_MS: solo
// il master emette il marcatore di zero (SPECS.MD §2).

#include <avr/io.h>
#include <avr/interrupt.h>
#include <util/atomic.h>
#include <math.h>

// --- Pin daisy chain (SPECS.MD DAISY CHAIN/restart.md - non cambiare senza
// aggiornare anche il cablaggio) ---
const int PIN_TRIGGER_IN = 2; // dal bus condiviso D8 del master (interrupt-capable, pull-down 10k gia' cablato)

uint8_t sineTable[256];
volatile uint32_t phase1 = 0, phase2 = 0;
uint32_t inc1, inc2;
const uint32_t SAMPLE_RATE = 40000; // Hz, stesso valore di master.ino/trasmettitore.ino

// --- Frequenze canale (Hz) - AUTOGENERATO, vedi intestazione sopra ---
const uint32_t FREQ1 = 4600;  // f1: clock/counter
const uint32_t FREQ2 = 3200;  // f2: stato interruttore

// --- Timing protocollo (SPECS.MD §2) - AUTOGENERATO, vedi intestazione
// sopra. Niente T0_MS qui: gli slave non emettono mai il marcatore di zero. ---
const unsigned long T1_MS  = 50; // durata del proprio slot
const unsigned long GAP_MS = 70; // silenzio dopo il proprio slot

// --- Identita' di questo nodo sul bus ---
const uint8_t NODE_ID_PROPRIO = 2;

// Soglia di timeout per il self-healing (restart.md, valore suggerito
// 300ms): se non arriva nessun impulso per piu' di questo tempo, il ciclo
// del master e' sicuramente finito (PAUSA_FINE_CICLO_MS in master.ino e'
// molto piu' lungo, 500ms) - lo slave si resetta da solo, pronto a
// riconoscere il prossimo impulso come "0". Fa da rete di sicurezza anche
// se un impulso si perde per rumore elettrico.
const unsigned long SOGLIA_TIMEOUT_RESET_MS = 300;

const unsigned long DEBOUNCE_US = 2000; // 2ms, rimbalzi elettrici sul fronte (restart.md)

// Placeholder per test SENZA hardware reale collegato, stesso schema di
// master.ino/trasmettitore.ino ma per UN SOLO interruttore (quello di
// questo nodo): sostituire con la lettura reale (digitalRead su un pin
// dedicato, o dal multiplexer) quando cablato - vedi aggiorna_interruttore()
// sotto.
const bool SIMULA_INTERRUTTORE_CASUALE = true;
const uint8_t PERCENTUALE_CHIUSO_SIMULATO = 15; // 0-100
// Solo per la simulazione: ogni quanto ri-estrarre un valore casuale, per
// esercitare anche da banco il caso "l'interruttore cambia mentre il nodo
// non sta trasmettendo" (monitoraggio continuo, non piu' legato al ciclo).
const unsigned long INTERVALLO_SIMULAZIONE_MS = 3000;
unsigned long ultimo_redraw_simulazione_ms = 0;
// Latch, non una lettura istantanea: true se l'interruttore e' stato chiuso
// in QUALSIASI momento dall'ultimo reset, anche se esattamente nel momento
// del proprio slot risulta di nuovo aperto - altrimenti un click breve tra
// un ciclo e l'altro andrebbe perso. Va a true SOLO dentro
// aggiorna_interruttore() (mai a false li' dentro), e torna a false SOLO in
// TONO_SLOT_PROPRIO subito dopo essere stato consumato per canale2_on
// (vedi aggiorna_slave()) - nessun altro punto del codice deve scriverci.
bool interruttoreChiuso = false;

// Isolamento canali per la procedura di taratura soglie (SPECS.MD §5.1),
// identico a master.ino/trasmettitore.ino.
const bool CANALE1_ABILITATO = true;
const bool CANALE2_ABILITATO = true;

// Il suono parte attivo dall'accensione (niente piu' controllo seriale
// ON/OFF, vedi setup()/loop()): canale1_on/canale2_on partono comunque a
// false, quindi resta silenzioso finche' la macchina a stati non accende
// davvero un tono.
volatile bool canale1_on = false;
volatile bool canale2_on = false;

// --- Macchina a stati minimale: IDLE -> impulso col conteggio giusto ->
// esegui slot -> torna IDLE. Nessun invio di trigger (bus a stella, non
// staffetta). ---
enum StatoSlave : uint8_t { IDLE, TONO_SLOT_PROPRIO, GAP_DOPO_SLOT };
StatoSlave stato = IDLE;
unsigned long inizio_stato_ms = 0;

// --- ISR: SOLO il minimo indispensabile (debounce + contatore + flag),
// nessuna logica pesante qui dentro (restart.md). contaImpulsi parte da -1:
// il primo impulso ricevuto dopo un reset lo porta a 0 (il marcatore),
// ogni impulso successivo incrementa di 1 - cosi' il valore locale
// coincide sempre con l'indice trasmesso dal master (0, 1, 2, ...), senza
// bisogno che gli impulsi sul bus portino nessuna informazione oltre al
// fronte in se'. ---
volatile int16_t contaImpulsi = -1;
volatile unsigned long ultimo_impulso_ms = 0;
volatile unsigned long ultimo_impulso_us_debounce = 0;
volatile bool nuovo_impulso = false;

void onImpulsoRicevuto() {
  unsigned long ora_us = micros();
  if (ora_us - ultimo_impulso_us_debounce < DEBOUNCE_US) return; // rimbalzo, ignora
  ultimo_impulso_us_debounce = ora_us;
  contaImpulsi = (contaImpulsi < 0) ? 0 : contaImpulsi + 1;
  ultimo_impulso_ms = millis();
  nuovo_impulso = true;
}

void setup() {
  Serial.begin(9600);

  for (int i = 0; i < 256; i++) {
    sineTable[i] = (uint8_t)(127.5 + 127.5 * sin(2.0 * PI * i / 256.0));
  }
  inc1 = (uint32_t)(((uint64_t)FREQ1 << 32) / SAMPLE_RATE);
  inc2 = (uint32_t)(((uint64_t)FREQ2 << 32) / SAMPLE_RATE);

  pinMode(12, OUTPUT); // OC1B, uscita audio f1/f2 (verso il trasduttore/fibra)
  pinMode(11, OUTPUT);
  digitalWrite(11, HIGH); // abilita audio permanentemente, niente piu' controllo seriale ON/OFF
  pinMode(PIN_TRIGGER_IN, INPUT); // pull-down 10k esterno gia' cablato, non serve INPUT_PULLUP

  // Timer1: Fast PWM 8 bit, nessun prescaler -> portante PWM a 62.5kHz
  TCCR1A = _BV(COM1B1) | _BV(WGM10);
  TCCR1B = _BV(WGM12) | _BV(CS10);
  OCR1B = 128; // silenzio iniziale (valore centrale)

  // Timer2: CTC, genera interrupt a SAMPLE_RATE per aggiornare il campione
  TCCR2A = _BV(WGM21);
  TCCR2B = _BV(CS21); // prescaler 8
  OCR2A = (16000000UL / 8 / SAMPLE_RATE) - 1;
  TIMSK2 = _BV(OCIE2A);

  // Interrupt hardware sul fronte di salita (riposo LOW via pull-down,
  // impulso in ingresso HIGH) - restart.md.
  attachInterrupt(digitalPinToInterrupt(PIN_TRIGGER_IN), onImpulsoRicevuto, RISING);

  randomSeed(analogRead(A0)); // pin scollegato = rumore, seed diverso ad ogni boot
  // Stato iniziale del latch: coerente con "mai sovrascrivere a false", solo
  // eventualmente a true (vedi dichiarazione di interruttoreChiuso sopra).
  if (SIMULA_INTERRUTTORE_CASUALE && random(0, 100) < PERCENTUALE_CHIUSO_SIMULATO) {
    interruttoreChiuso = true;
  }

  sei();

  Serial.println(F("--- daisy chain (bus condiviso): SLAVE2 (NODE_ID=2) ---"));
  Serial.print(F("SAMPLE_RATE=")); Serial.print(SAMPLE_RATE); Serial.println(F("Hz"));
  Serial.print(F("FREQ1=")); Serial.print(FREQ1);
  Serial.print(F("Hz  FREQ2=")); Serial.print(FREQ2); Serial.println(F("Hz"));
  Serial.print(F("T1_MS=")); Serial.print(T1_MS);
  Serial.print(F("  GAP_MS=")); Serial.println(GAP_MS);
  Serial.print(F("SOGLIA_TIMEOUT_RESET_MS=")); Serial.println(SOGLIA_TIMEOUT_RESET_MS);
  Serial.print(F("interruttore proprio: ")); Serial.println(interruttoreChiuso ? "CHIUSO" : "aperto");
  Serial.println(F("In ascolto sul bus (D2), sincronizzazione automatica dall'accensione."));
}

ISR(TIMER2_COMPA_vect) {
  phase1 += inc1;
  phase2 += inc2;
  uint8_t idx1 = phase1 >> 24;
  uint8_t idx2 = phase2 >> 24;

  uint8_t contrib1 = canale1_on ? sineTable[idx1] : 128;
  uint8_t contrib2 = canale2_on ? sineTable[idx2] : 128;
  OCR1B = ((uint16_t)contrib1 + contrib2) / 2;
}

void aggiorna_slave(unsigned long ora_ms) {
  unsigned long trascorso = ora_ms - inizio_stato_ms;

  // Lettura atomica delle variabili condivise con la ISR: su AVR (8 bit)
  // una lettura di int16_t/unsigned long non e' atomica, un interrupt a
  // meta' lettura darebbe un valore "spezzato" - stesso genere di bug gia'
  // sofferto altrove nel progetto per assegnazioni non atomiche.
  int16_t conta_locale;
  unsigned long ultimo_impulso_locale;
  bool impulso_locale;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    conta_locale = contaImpulsi;
    ultimo_impulso_locale = ultimo_impulso_ms;
    impulso_locale = nuovo_impulso;
    nuovo_impulso = false;
  }

  // Watchdog self-healing (restart.md): il ciclo del master e' finito da
  // tempo (o si e' perso un impulso) - torna pronto a riconoscere il
  // prossimo impulso come "0".
  if (conta_locale >= 0 && (ora_ms - ultimo_impulso_locale) > SOGLIA_TIMEOUT_RESET_MS) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { contaImpulsi = -1; }
    conta_locale = -1;
  }

  switch (stato) {
    case IDLE:
      canale1_on = false;
      canale2_on = false;
      if (impulso_locale && conta_locale == NODE_ID_PROPRIO) {
        stato = TONO_SLOT_PROPRIO;
        inizio_stato_ms = ora_ms;
      }
      break;

    case TONO_SLOT_PROPRIO:
      canale1_on = CANALE1_ABILITATO;
      canale2_on = CANALE2_ABILITATO && interruttoreChiuso;
      if (trascorso >= T1_MS) {
        // Latch consumato per questo slot: unico punto del codice che lo
        // rimette a false, pronto a catturare una nuova chiusura nel
        // prossimo ciclo (vedi dichiarazione di interruttoreChiuso).
        interruttoreChiuso = false;
        stato = GAP_DOPO_SLOT;
        inizio_stato_ms = ora_ms;
      }
      break;

    case GAP_DOPO_SLOT:
      canale1_on = false;
      canale2_on = false;
      if (trascorso >= GAP_MS) {
        stato = IDLE;
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
  if (!SIMULA_INTERRUTTORE_CASUALE) {
    // TODO: quando si passa al pin reale, aggiungere debounce (hardware o
    // software) - un rimbalzo meccanico di pochi ms rischia di far scattare
    // il latch per un click involontario.
    // if (digitalRead(PIN_INTERRUTTORE) == HIGH) interruttoreChiuso = true;
    return;
  }
  unsigned long ora = millis();
  if (ora - ultimo_redraw_simulazione_ms >= INTERVALLO_SIMULAZIONE_MS) {
    ultimo_redraw_simulazione_ms = ora;
    // Solo true, mai false: il redraw simula "e' stato premuto", non
    // sostituisce lo stato attuale (vedi dichiarazione di interruttoreChiuso).
    if (random(0, 100) < PERCENTUALE_CHIUSO_SIMULATO) {
      interruttoreChiuso = true;
    }
  }
}

void loop() {
  aggiorna_interruttore();
  aggiorna_slave(millis());
  // nessun delay() qui: l'attesa dell'impulso e' event-driven via
  // interrupt (restart.md), nessuno stato qui blocca mai.
}
