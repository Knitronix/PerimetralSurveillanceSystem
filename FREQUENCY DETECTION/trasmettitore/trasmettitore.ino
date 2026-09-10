// Genera f1 (600Hz) e f2 (1500Hz) sullo STESSO pin (12 - OC1B) tramite
// sintesi DDS + PWM ad alta frequenza, sommate in un unico segnale che va
// poi filtrato con un RC passa-basso.
//
// Il gating dei due canali NON e' piu' a durata fissa indipendente:
// implementa il vero protocollo f1/f2 (SPECS.MD §2-3):
//   [marcatore zero: f1 per T0_MS] [gap]
//   per ogni interruttore i=1..NUM_SWITCH:
//     [f1 per T1_MS, + f2 in overlap se l'interruttore i e' chiuso] [gap]
//   ripete da capo
//
// Controllo master via Serial (accendi/spegni) INVARIATO rispetto alla
// versione precedente: il suono parte SEMPRE spento al boot/reset, a
// prescindere da quale simbolo del protocollo si starebbe emettendo,
// finche' non si scrive "ON" sul Serial Monitor.

#include <avr/io.h>
#include <avr/interrupt.h>
#include <math.h>

uint8_t sineTable[256];
volatile uint32_t phase1 = 0, phase2 = 0;
uint32_t inc1, inc2;
// Hz - frequenza di aggiornamento del DDS (quante volte al secondo si
// sceglie un nuovo campione dalla tabella del seno). Portata da 20000 a
// 40000: a 20kHz, f1=2500Hz aveva solo 8 campioni per periodo (un'onda
// "a gradini" molto grezza, non un seno pulito), e f2=1000Hz solo 20 - lo
// spettrogramma su main.py mostrava diverse righe spurie persistenti oltre
// a f1 (non solo armoniche pulite a multipli di 2500Hz), quasi certamente
// dovute a questa scarsa risoluzione. A 40kHz i campioni per periodo
// raddoppiano (16 per f1, 40 per f2): onda più pulita, meno energia spuria
// sparsa nello spettro che possa "sporcare" il filtro Goertzel dell'altra
// frequenza in main.py.
const uint32_t SAMPLE_RATE = 40000; // Nyquist = 20kHz

// --- Frequenze canale (Hz) ---
// AUTOGENERATO da aggiorna_config_ino.py a partire da config_condivisa.py -
// non modificare questi due valori a mano: cambia F1_HZ/F2_HZ in
// config_condivisa.py e rilancia "python aggiorna_config_ino.py" (dalla
// cartella FREQUENCY DETECTION/), che li riscrive qui e in sweep_tempi.ino
// (Arduino non riesce a includere un header con percorso relativo fuori
// dalla cartella dello sketch, da qui l'esigenza di uno script che li
// scriva direttamente invece di un #include condiviso). Scelte con lo
// sweep a gradini di TEST ARMONICHE/ (vedi SPECS.MD §5.1/§6.2): le due con
// segnale più forte, meno armoniche proprie e senza cross-talk misurato
// tra loro, sotto il limite di banda del sistema (~5400Hz).
const uint32_t FREQ1 = 4600;   // f1: clock/counter
const uint32_t FREQ2 = 3200;  // f2: stato interruttore

// --- Protocollo f1/f2 (SPECS.MD §2/§3): valori da tarare sperimentalmente ---
const unsigned long T0_MS      = 200;  // durata marcatore di zero
const unsigned long T1_MS      = 20;  // durata di uno slot
const unsigned long GAP_MS     = 50;   // silenzio tra un simbolo e il successivo
const uint8_t        NUM_SWITCH = 50; // numero di interruttori nel ciclo

// Placeholder in RAM per test: sostituire con lettura hardware reale
// (es. catena di shift register per 100 ingressi)
bool switchState[NUM_SWITCH];

// Simulazione per test SENZA hardware reale collegato: se true, ad ogni
// boot vengono chiusi a caso circa PERCENTUALE_CHIUSI_SIMULATI% degli
// interruttori, cosi' si puo' verificare che il rilevatore distingua
// davvero APERTO/CHIUSO prima di cablare i 100 ingressi veri. Mettere a
// false quando si passa alla lettura hardware reale.
const bool SIMULA_INTERRUTTORI_CASUALI = true;
const uint8_t PERCENTUALE_CHIUSI_SIMULATI = 15; // 0-100

volatile bool canale1_on = true;
volatile bool canale2_on = true;

// Abilita/disabilita manualmente un canale intero (utile per la PoC:
// testare "solo f1", "solo f2", poi entrambe insieme). Mettere a false per
// silenziare completamente un canale a prescindere dal protocollo.
const bool CANALE1_ABILITATO = true;
const bool CANALE2_ABILITATO = true;

// --- Controllo master via seriale (INVARIATO) ---
volatile bool soundEnabled = false; // parte spento

// --- Macchina a stati del protocollo ---
enum StatoProtocollo : uint8_t { STATO_TONO, STATO_GAP };
StatoProtocollo stato_protocollo = STATO_TONO;
uint8_t slot_corrente = 0; // 0 = marcatore di zero, 1..NUM_SWITCH = slot interruttore
unsigned long inizio_stato_ms = 0;

bool readSwitch(uint8_t idx) {
  // Placeholder: sostituire con lettura hardware reale. idx va da 1 a NUM_SWITCH.
  return switchState[idx - 1];
}

void setup() {
  Serial.begin(9600);

  for (int i = 0; i < 256; i++) {
    sineTable[i] = (uint8_t)(127.5 + 127.5 * sin(2.0 * PI * i / 256.0));
  }
  inc1 = (uint32_t)(((uint64_t)FREQ1 << 32) / SAMPLE_RATE);
  inc2 = (uint32_t)(((uint64_t)FREQ2 << 32) / SAMPLE_RATE);

  pinMode(12, OUTPUT); // OC1B
  pinMode(11, OUTPUT);
  digitalWrite(11, LOW); // parte spento, coerente con soundEnabled = false
  // Timer1: Fast PWM 8 bit, nessun prescaler -> portante PWM a 62.5kHz
  TCCR1A = _BV(COM1B1) | _BV(WGM10);
  TCCR1B = _BV(WGM12) | _BV(CS10);
  OCR1B = 128; // silenzio iniziale (valore centrale)

  // Timer2: CTC, genera interrupt a SAMPLE_RATE per aggiornare il campione
  TCCR2A = _BV(WGM21);
  TCCR2B = _BV(CS21); // prescaler 8
  OCR2A = (16000000UL / 8 / SAMPLE_RATE) - 1;
  TIMSK2 = _BV(OCIE2A);

  // Placeholder: tutti aperti finche' non si collega la lettura reale,
  // a meno che SIMULA_INTERRUTTORI_CASUALI non sia attivo (vedi sopra).
  randomSeed(analogRead(A0)); // pin scollegato = rumore, seed diverso ad ogni boot
  for (uint8_t i = 0; i < NUM_SWITCH; i++) {
    if (SIMULA_INTERRUTTORI_CASUALI) {
      switchState[i] = (random(0, 100) < PERCENTUALE_CHIUSI_SIMULATI);
    } else {
      switchState[i] = false;
    }
  }

  inizio_stato_ms = millis();
  sei();

  // Stampa diagnostica: serve a verificare senza ambiguita' che lo sketch
  // davvero in esecuzione sulla board sia quello appena caricato (capita di
  // modificare il .ino e dimenticare/fallire l'upload, restando con un
  // binario vecchio) e con quali costanti gira.
  Serial.println(F("--- trasmettitore f1/f2: parametri attivi ---"));
  Serial.print(F("SAMPLE_RATE=")); Serial.print(SAMPLE_RATE); Serial.println(F("Hz"));
  Serial.print(F("FREQ1=")); Serial.print(FREQ1);
  Serial.print(F("Hz  FREQ2=")); Serial.print(FREQ2); Serial.println(F("Hz"));
  Serial.print(F("CANALE1_ABILITATO=")); Serial.println(CANALE1_ABILITATO ? "true" : "false");
  Serial.print(F("CANALE2_ABILITATO=")); Serial.println(CANALE2_ABILITATO ? "true" : "false");
  Serial.print(F("T0_MS=")); Serial.print(T0_MS);
  Serial.print(F("  T1_MS=")); Serial.print(T1_MS);
  Serial.print(F("  GAP_MS=")); Serial.println(GAP_MS);
  Serial.println(F("Pronto (silenzio). Scrivi 'ON' o 'OFF' e premi invio."));
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

  // Se il canale e' OFF (o disabilitato), il suo contributo e' il punto
  // medio (silenzio), non zero: mantiene centrata la somma.
  uint8_t contrib1 = canale1_on ? sineTable[idx1] : 128;
  uint8_t contrib2 = canale2_on ? sineTable[idx2] : 128;
  uint8_t sample = ((uint16_t)contrib1 + contrib2) / 2;
  OCR1B = sample;
}

// Macchina a stati del protocollo f1/f2. Sostituisce il vecchio
// aggiorna_gating() a durata fissa indipendente per canale: qui f1 scandisce
// marcatore/slot, e f2 e' condizionata allo stato dell'interruttore dello
// slot corrente. Non blocca mai (nessun delay()), stesso stile di prima.
void aggiorna_protocollo(unsigned long ora_ms) {
  unsigned long trascorso = ora_ms - inizio_stato_ms;

  if (stato_protocollo == STATO_TONO) {
    if (slot_corrente == 0) {
      // marcatore di zero: solo f1. CANALE*_ABILITATO va dentro la stessa
      // assegnazione (non come override subito dopo): canale1_on/canale2_on
      // sono lette in modo asincrono dall'ISR audio, che puo' interromperci
      // in qualunque punto. Se prima scriviamo "true" e solo due righe dopo
      // rimettiamo "false", esiste una finestra in cui l'ISR puo' leggere
      // "true" e far trapelare un campione di tono vero anche a canale
      // "disabilitato" - capitava esattamente durante le fasi TONO, mai nei
      // GAP, ed e' per questo che il leakage sembrava seguire il ritmo del
      // protocollo. Scrivendo subito il valore finale non esiste piu' uno
      // stato intermedio da poter leggere a meta'.
      canale1_on = CANALE1_ABILITATO;
      canale2_on = false;
      if (trascorso >= T0_MS) {
        stato_protocollo = STATO_GAP;
        inizio_stato_ms = ora_ms;
      }
    } else {
      // slot interruttore: f1 sempre, f2 solo se chiuso
      bool chiuso = readSwitch(slot_corrente);
      canale1_on = CANALE1_ABILITATO;
      canale2_on = CANALE2_ABILITATO && chiuso;
      if (trascorso >= T1_MS) {
        stato_protocollo = STATO_GAP;
        inizio_stato_ms = ora_ms;
      }
    }
  } else { // STATO_GAP: silenzio tra un simbolo e il successivo
    canale1_on = false;
    canale2_on = false;
    if (trascorso >= GAP_MS) {
      if (slot_corrente >= NUM_SWITCH) {
        slot_corrente = 0; // fine ciclo, torna al marcatore di zero
      } else {
        slot_corrente++;
      }
      stato_protocollo = STATO_TONO;
      inizio_stato_ms = ora_ms;
    }
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

void loop() {
  aggiorna_protocollo(millis());
  gestisci_seriale();
  // nessun delay(): protocollo, DDS e seriale restano tutti reattivi
}
