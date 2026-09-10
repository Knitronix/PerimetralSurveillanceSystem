// Sweep a gradini per caratterizzare armoniche/spurie del canale
// generatore->accoppiamento->interrogatore, INDIPENDENTE dal protocollo
// f1/f2 (niente marcatore/slot/gap, niente interruttori simulati): qui
// interessa solo un tono pulito, un valore alla volta, tenuto abbastanza a
// lungo da poterlo analizzare con calma (a occhio sullo spettrogramma di
// main.py, o meglio con analizza_armoniche_sweep.py che lo fa in automatico).
//
// Non tocca trasmettitore.ino (che resta lo sketch del protocollo vero):
// questo e' un file a se', da caricare solo per le sessioni di
// caratterizzazione, poi si torna a trasmettitore.ino per i test normali.
//
// Stessa tecnica DDS+PWM di trasmettitore.ino (Timer1 Fast PWM 8 bit su
// OC1B come portante, Timer2 CTC come clock del DDS), ma un solo canale:
// qui non serve sommare due toni, solo emetterne uno pulito alla volta.
//
// Protocollo seriale: scrivi "ON"/"OFF" per accendere/spegnere il suono
// (identico a trasmettitore.ino). Ad ogni cambio di gradino stampa una riga
// "STEP;<millis>;<freq_hz>" - e' il formato che analizza_armoniche_sweep.py
// legge per sapere quando inizia ogni gradino e a quale frequenza.

#include <avr/io.h>
#include <avr/interrupt.h>
#include <math.h>

uint8_t sineTable[256];
volatile uint32_t phase = 0;
volatile uint32_t inc = 0;

const uint32_t SAMPLE_RATE = 40000; // Hz, stesso valore di trasmettitore.ino

// --- Gradini dello sweep: da FREQ_MIN a FREQ_MAX a passi di FREQ_STEP,
// ciascuno tenuto per STEP_DURATION_MS. Valori di partenza pensati per
// coprire la banda che main.py visualizza (fino a 6kHz), con gradini
// abbastanza larghi (200Hz) da distinguerli bene ma non troppi da rendere
// lo sweep interminabile (29 gradini * 4s = poco più di 2 minuti).
const uint32_t FREQ_MIN = 200;
const uint32_t FREQ_MAX = 6000;
const uint32_t FREQ_STEP = 200;
const unsigned long STEP_DURATION_MS = 4000;

// Se true, ricomincia da FREQ_MIN alla fine invece di fermarsi - comodo per
// lasciare la caratterizzazione in loop mentre si osserva o si registra.
const bool RIPETI_IN_LOOP = true;

uint32_t freq_corrente = FREQ_MIN;
unsigned long inizio_gradino_ms = 0;

volatile bool soundEnabled = false; // parte spento, come trasmettitore.ino

void imposta_incremento(uint32_t freq_hz) {
  inc = (uint32_t)(((uint64_t)freq_hz << 32) / SAMPLE_RATE);
}

void stampa_gradino(uint32_t freq_hz) {
  Serial.print(F("STEP;"));
  Serial.print(millis());
  Serial.print(';');
  Serial.println(freq_hz);
}

void setup() {
  Serial.begin(9600);

  for (int i = 0; i < 256; i++) {
    sineTable[i] = (uint8_t)(127.5 + 127.5 * sin(2.0 * PI * i / 256.0));
  }

  pinMode(12, OUTPUT); // OC1B, stesso pin di trasmettitore.ino
  pinMode(11, OUTPUT);
  digitalWrite(11, LOW);
  TCCR1A = _BV(COM1B1) | _BV(WGM10);
  TCCR1B = _BV(WGM12) | _BV(CS10);
  OCR1B = 128;

  TCCR2A = _BV(WGM21);
  TCCR2B = _BV(CS21);
  OCR2A = (16000000UL / 8 / SAMPLE_RATE) - 1;
  TIMSK2 = _BV(OCIE2A);

  imposta_incremento(freq_corrente);
  inizio_gradino_ms = millis();
  sei();

  Serial.println(F("--- sweep_gradini: caratterizzazione armoniche ---"));
  Serial.print(F("SAMPLE_RATE=")); Serial.print(SAMPLE_RATE); Serial.println(F("Hz"));
  Serial.print(F("FREQ_MIN=")); Serial.print(FREQ_MIN);
  Serial.print(F("  FREQ_MAX=")); Serial.print(FREQ_MAX);
  Serial.print(F("  FREQ_STEP=")); Serial.println(FREQ_STEP);
  Serial.print(F("STEP_DURATION_MS=")); Serial.println(STEP_DURATION_MS);
  Serial.println(F("Pronto (silenzio). Scrivi 'ON' o 'OFF' e premi invio."));
  stampa_gradino(freq_corrente);
}

ISR(TIMER2_COMPA_vect) {
  phase += inc;
  uint8_t idx = phase >> 24;

  if (!soundEnabled) {
    OCR1B = 128;
    return;
  }
  OCR1B = sineTable[idx];
}

void aggiorna_sweep(unsigned long ora_ms) {
  if (ora_ms - inizio_gradino_ms < STEP_DURATION_MS) return;

  freq_corrente += FREQ_STEP;
  if (freq_corrente > FREQ_MAX) {
    if (!RIPETI_IN_LOOP) {
      freq_corrente -= FREQ_STEP; // resta fermo sull'ultimo gradino
      return;
    }
    freq_corrente = FREQ_MIN;
  }
  imposta_incremento(freq_corrente);
  inizio_gradino_ms = ora_ms;
  stampa_gradino(freq_corrente);
}

void gestisci_seriale() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.equalsIgnoreCase("ON")) {
      digitalWrite(11, HIGH);
      soundEnabled = true;
      Serial.println(F("Suono ON"));
    } else if (cmd.equalsIgnoreCase("OFF")) {
      digitalWrite(11, LOW);
      soundEnabled = false;
      Serial.println(F("Suono OFF"));
    }
  }
}

void loop() {
  aggiorna_sweep(millis());
  gestisci_seriale();
}
