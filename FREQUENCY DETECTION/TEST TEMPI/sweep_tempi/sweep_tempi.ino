// Sweep sulle DURATE (non sulle frequenze) per trovare il delta t minimo
// affidabile del canale generatore->accoppiamento->interrogatore, alla
// frequenza e con le soglie ATTUALMENTE in uso nel protocollo vero.
// FREQ_TEST_HZ sotto e' un valore letterale (Arduino non riesce a includere
// un header con percorso relativo fuori dalla cartella dello sketch, quindi
// niente config_condivisa.h qui): impostalo a mano sulla frequenza che
// vuoi caratterizzare in questa sessione (di solito una tra F1_HZ/F2_HZ di
// config_condivisa.py) E aggiorna FREQ_TEST_HZ/SOGLIA_ON/SOGLIA_OFF anche
// in analizza_tempi_minimi.py di conseguenza - i due vanno sempre cambiati
// insieme, non c'e' automatismo per "quale frequenza testare oggi" dato
// che e' una scelta diversa ad ogni sessione, non un valore fisso come
// F1_HZ/F2_HZ in trasmettitore.ino. Il delta t minimo trovato vale solo
// per QUESTA frequenza con le soglie attualmente impostate.
//
// Non genera il protocollo f1/f2 completo (niente marcatore/slot/interrutt-
// tori): un solo tono, ON/OFF a durate decrescenti, in due fasi separate:
//   FASE A: quanto puo' essere corto l'impulso ON? (gap tenuto largo,
//           di sicuro sufficiente, cosi' l'unica variabile e' la durata ON)
//   FASE B: quanto puo' essere corto il gap OFF? (ON tenuto a un valore
//           sicuro, cosi' l'unica variabile e' la durata del gap)
// analizza_tempi_minimi.py ricostruisce l'andamento della potenza blocco
// per blocco (20ms, come in main.py) e dice a quale durata il canale non
// fa piu' in tempo a salire sopra SOGLIA_ON o scendere sotto SOGLIA_OFF.
//
// Stessa tecnica DDS+PWM di trasmettitore.ino e sweep_gradini.ino (Timer1
// Fast PWM 8 bit su OC1B, Timer2 CTC come clock del DDS), un solo canale.
// Protocollo seriale identico agli altri due sketch: "ON"/"OFF" per il
// suono, stampa "STEP;<millis>;<fase>;<on_ms>;<gap_ms>" ad ogni cambio.

#include <avr/io.h>
#include <avr/interrupt.h>
#include <math.h>

uint8_t sineTable[256];
volatile uint32_t phase = 0;
uint32_t inc = 0;

const uint32_t SAMPLE_RATE = 40000; // Hz, stesso valore di trasmettitore.ino

// Frequenza di test: vedi commento in cima al file - valore letterale da
// tenere allineato a mano con FREQ_TEST_HZ in analizza_tempi_minimi.py.
const uint32_t FREQ_TEST_HZ = 3200;

// --- Fase A: durata ON decrescente, gap fisso e largo ---
const uint16_t DURATE_ON_MS[] = {150, 120, 100, 80, 60, 50, 40, 30, 25, 20, 15, 10};
const uint8_t N_DURATE_ON = sizeof(DURATE_ON_MS) / sizeof(DURATE_ON_MS[0]);
const uint16_t GAP_FISSO_FASE_A_MS = 300; // di sicuro largo abbastanza

// --- Fase B: durata gap decrescente, ON fisso e sicuro ---
const uint16_t DURATE_GAP_MS[] = {200, 150, 120, 100, 80, 60, 50, 40, 30, 25, 20, 15, 10};
const uint8_t N_DURATE_GAP = sizeof(DURATE_GAP_MS) / sizeof(DURATE_GAP_MS[0]);
const uint16_t ON_FISSO_FASE_B_MS = 80; // valore atteso sicuro, non il minimo esatto

// Quanti cicli ON+GAP per ogni gradino, per avere abbastanza ripetizioni
// da fare statistica (allineamento diverso rispetto ai blocchi da 20ms
// ogni volta, utile per non dipendere da un solo colpo di fortuna).
const uint8_t RIPETIZIONI_PER_GRADINO = 20;

volatile bool soundEnabled = false; // parte spento, come gli altri sketch
volatile bool tono_on = false;

enum Fase : uint8_t { FASE_A, FASE_B, SWEEP_FINITO };
Fase fase_corrente = FASE_A;
uint8_t indice_gradino = 0;
uint8_t ripetizione_corrente = 0;
uint16_t on_ms_corrente = 0;
uint16_t gap_ms_corrente = 0;
bool nello_stato_on = false;
unsigned long inizio_stato_ms = 0;

void imposta_incremento(uint32_t freq_hz) {
  inc = (uint32_t)(((uint64_t)freq_hz << 32) / SAMPLE_RATE);
}

void stampa_gradino() {
  Serial.print(F("STEP;"));
  Serial.print(millis());
  Serial.print(';');
  Serial.print(fase_corrente == FASE_A ? 'A' : 'B');
  Serial.print(';');
  Serial.print(on_ms_corrente);
  Serial.print(';');
  Serial.println(gap_ms_corrente);
}

void avvia_gradino() {
  if (fase_corrente == FASE_A) {
    on_ms_corrente = DURATE_ON_MS[indice_gradino];
    gap_ms_corrente = GAP_FISSO_FASE_A_MS;
  } else if (fase_corrente == FASE_B) {
    on_ms_corrente = ON_FISSO_FASE_B_MS;
    gap_ms_corrente = DURATE_GAP_MS[indice_gradino];
  }
  ripetizione_corrente = 0;
  nello_stato_on = true;
  tono_on = true;
  inizio_stato_ms = millis();
  stampa_gradino();
}

void avanza_gradino() {
  indice_gradino++;
  if (fase_corrente == FASE_A && indice_gradino >= N_DURATE_ON) {
    fase_corrente = FASE_B;
    indice_gradino = 0;
  } else if (fase_corrente == FASE_B && indice_gradino >= N_DURATE_GAP) {
    fase_corrente = SWEEP_FINITO;
  }
  if (fase_corrente != SWEEP_FINITO) {
    avvia_gradino();
  } else {
    tono_on = false;
    Serial.println(F("SWEEP_FINITO"));
  }
}

void setup() {
  Serial.begin(9600);

  for (int i = 0; i < 256; i++) {
    sineTable[i] = (uint8_t)(127.5 + 127.5 * sin(2.0 * PI * i / 256.0));
  }
  imposta_incremento(FREQ_TEST_HZ);

  pinMode(12, OUTPUT); // OC1B, stesso pin degli altri sketch
  pinMode(11, OUTPUT);
  digitalWrite(11, LOW);
  TCCR1A = _BV(COM1B1) | _BV(WGM10);
  TCCR1B = _BV(WGM12) | _BV(CS10);
  OCR1B = 128;

  TCCR2A = _BV(WGM21);
  TCCR2B = _BV(CS21);
  OCR2A = (16000000UL / 8 / SAMPLE_RATE) - 1;
  TIMSK2 = _BV(OCIE2A);

  sei();

  Serial.println(F("--- sweep_tempi: caratterizzazione delta t minimo ---"));
  Serial.print(F("FREQ_TEST_HZ=")); Serial.println(FREQ_TEST_HZ);
  Serial.print(F("RIPETIZIONI_PER_GRADINO=")); Serial.println(RIPETIZIONI_PER_GRADINO);
  Serial.println(F("Pronto (silenzio). Scrivi 'ON' o 'OFF' e premi invio."));

  avvia_gradino();
}

ISR(TIMER2_COMPA_vect) {
  phase += inc;
  uint8_t idx = phase >> 24;

  if (!soundEnabled || !tono_on) {
    OCR1B = 128;
    return;
  }
  OCR1B = sineTable[idx];
}

void aggiorna_sweep(unsigned long ora_ms) {
  if (fase_corrente == SWEEP_FINITO) return;

  unsigned long trascorso = ora_ms - inizio_stato_ms;
  uint16_t durata_stato = nello_stato_on ? on_ms_corrente : gap_ms_corrente;

  if (trascorso < durata_stato) return;

  inizio_stato_ms = ora_ms;
  if (nello_stato_on) {
    nello_stato_on = false;
    tono_on = false;
  } else {
    nello_stato_on = true;
    tono_on = true;
    ripetizione_corrente++;
    if (ripetizione_corrente >= RIPETIZIONI_PER_GRADINO) {
      avanza_gradino();
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
