// Sketch standalone per Arduino Mega - nessuna dipendenza da file esterni
// a parte le librerie AVR/EEPROM incluse sotto. restart.md (nella cartella
// arduino/ che contiene anche questa) spiega l'architettura del bus
// daisy chain e il perche' delle scelte di timing.
//
// UN SOLO sketch, TRE modalita' (per non dover riflashare via USB una volta
// installato - vedi "Selettore modalita'" sotto):
//   0 = DAISY_CHAIN     produzione: catena daisy chain vera (vedi sotto)
//   1 = SWEEP_FREQUENZE caratterizzazione risposta in frequenza (ex
//                       TEST ARMONICHE/sweep_gradini.ino)
//   2 = SWEEP_TEMPI     caratterizzazione delta t minimo (ex
//                       TEST TEMPI/sweep_tempi.ino)
//
// --- Selettore modalita' (nessun filo/pin in piu': solo accensione e
// spegnimento, via la stessa Shelly che alimenta la scheda) ---
// Ad ogni boot, in EEPROM si tiene un contatore e un flag "sessione
// confermata" (scritto UNA SOLA volta per boot, appena si supera
// SOGLIA_SESSIONE_LUNGA_MS - non periodicamente, per non usurare l'EEPROM).
// Se la sessione PRECEDENTE si era confermata (era durata piu' di quella
// soglia - sempre il caso in uso reale: un evento vero accende la Shelly
// per minuti, non secondi), questo boot riparte dal contatore 0 = DAISY
// CHAIN, SENZA NESSUN ritardo aggiunto - identico al comportamento di
// sempre. Se invece la sessione precedente era stata interrotta PRIMA di
// quella soglia, vuol dire che si stava ciclando di proposito: il
// contatore avanza di uno (mod 3) e la modalita' di QUESTO boot e' quella
// nuova - decisa e avviata subito, zero ritardo anche qui.
// Per scegliere la modalita' K: accendere/spegnere velocemente (< soglia)
// la Shelly K volte, poi lasciarla accesa l'ultima volta (si confermera'
// da sola dopo la soglia). Per tornare in DAISY CHAIN basta un ciclo
// qualsiasi lasciato acceso: il boot successivo riparte sempre da 0.
// Questo e' SICURO per l'uso reale: la Shelly non si accende/spegne mai
// cosi' velocemente da sola (ATTESA_BOOT_DAISY_CHAIN_S e i timeout di
// WAKEUPeSTALTA sono tutti in secondi/minuti, non in questo ordine di
// grandezza), quindi un'intrusione vera non puo' mai essere scambiata per
// una richiesta di cambio modalita'.
// Pilotato in automatico (nessuna azione manuale) dagli script Python
// analizza_armoniche_sweep.py/2analizzatempiminimi.py tramite
// FREQUENCY DETECTION/shelly_mode_selector.py, PRIMA di aprire la seriale.
//
// --- Modalita' 0, DAISY CHAIN (come documentato da sempre) ---
// Bus condiviso, open-loop a tempo fisso (vedi restart.md). Il master e'
// l'UNICO generatore di f1/f2 di tutta la catena: gli slave non generano
// piu' nessun tono, hanno solo un interruttore locale da riferire.
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
// la cui DURATA codifica "vivo + stato interruttore".
//
// --- Modalita' 1/2 ---
// Riusano lo stesso DDS/PWM (Timer1+Timer2, pin 12/OC1B) della modalita' 0,
// ma a canale singolo e piena ampiezza (vedi ISR sotto) - stessi numeri
// delle vecchie sketch separate sweep_gradini.ino/sweep_tempi.ino, cosi'
// le misure fatte finora restano comparabili.

#include <avr/io.h>
#include <avr/interrupt.h>
#include <math.h>
#include <EEPROM.h>

// ============================================================
// Selettore modalita' (vedi intestazione sopra)
// ============================================================
enum Modalita : uint8_t { DAISY_CHAIN = 0, SWEEP_FREQUENZE = 1, SWEEP_TEMPI = 2, NUM_MODALITA = 3 };

const int EEPROM_ADDR_CONTATORE = 0;
const int EEPROM_ADDR_CONFERMATO = 1;
// Il primo flash di questo sketch trova in EEPROM qualunque valore ci
// fosse prima (mai scritta da questo sketch finora) - senza una firma che
// segnali "questo layout l'ho già inizializzato io", il primissimo boot
// partirebbe in una modalità indeterminata invece che, in modo prevedibile,
// in DAISY_CHAIN. FIRMA_INIZIALIZZATA e' un valore arbitrario, improbabile
// per puro caso (0x00/0xFF, i due valori "vuoti" tipici, sono esclusi).
const int EEPROM_ADDR_FIRMA = 2;
const uint8_t FIRMA_INIZIALIZZATA = 0xA5;
// Sopra questa soglia di uptime, la sessione e' considerata "vera" (non un
// ciclo rapido di selezione) - deve restare ABBONDANTEMENTE piu' breve di
// qualunque tempo reale di accensione (ATTESA_BOOT_DAISY_CHAIN_S e i
// timeout di WAKEUPeSTALTA sono in secondi/minuti), e scritta in EEPROM
// una sola volta per boot (non periodicamente, per non usurarla).
const unsigned long SOGLIA_SESSIONE_LUNGA_MS = 2500;

volatile Modalita modalita_attiva;  // letta anche nella ISR, decisa una volta a setup()
bool sessione_confermata = false;
unsigned long inizio_boot_ms = 0;

// ============================================================
// DDS/PWM condiviso da tutte le modalita' (Timer1 Fast PWM su OC1B come
// portante, Timer2 CTC come clock del campionamento)
// ============================================================
uint8_t sineTable[256];
volatile uint32_t phase1 = 0, phase2 = 0;
uint32_t inc1, inc2;
const uint32_t SAMPLE_RATE = 40000; // Hz

volatile bool soundEnabled = false;
volatile bool canale1_on = false;
volatile bool canale2_on = false;

// ============================================================
// Modalita' 0: DAISY CHAIN (produzione)
// ============================================================
// --- Pin daisy chain (restart.md - non cambiare senza aggiornare anche il
// cablaggio). Bidirezionale: OUTPUT per gli impulsi di indirizzamento,
// INPUT quando si ascolta la risposta di un nodo remoto. ---
const int PIN_BUS = 8; // bus condiviso, verso D2 di TUTTI gli slave in parallelo

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
const uint8_t NUMERO_NODI = 2;     // master + slave2 - deve restare uguale in tutti gli sketch della catena

const unsigned long DURATA_IMPULSO_TRIGGER_MS = 8;

// --- Interrogazione dei nodi remoti ---
const unsigned long MARGINE_SICUREZZA_RISPOSTA_MS = 15;
const unsigned long DURATA_RISPOSTA_APERTO_MS = 6;
const unsigned long DURATA_RISPOSTA_CHIUSO_MS = 18;
const unsigned long SOGLIA_CLASSIFICAZIONE_RISPOSTA_MS =
    (DURATA_RISPOSTA_APERTO_MS + DURATA_RISPOSTA_CHIUSO_MS) / 2;
const unsigned long TIMEOUT_RISPOSTA_MS = 45;

// 21/10/2026: da 500 a 250 -> ciclo da ~1.05s a ~0.80s (reazione piu' veloce).
// LEGATA a SOGLIA_TIMEOUT_RESET_MS in slave2.ino (340): lo slave capisce che
// e' iniziato un ciclo nuovo dal silenzio sul bus. Silenzio fra due cicli =
// ~145ms + questa pausa (~395ms) deve restare SOPRA 340; il silenzio piu'
// lungo dentro un ciclo (dopo il marcatore, 8+T0_MS+GAP_MS = 278ms) SOTTO 340.
const unsigned long PAUSA_FINE_CICLO_MS = 250;

const int PIN_INTERRUTTORE = A0;
const unsigned long DEBOUNCE_INTERRUTTORE_MS = 30;
bool letturaGrezzaCorrente = false;
unsigned long ultimo_cambio_lettura_ms = 0;
bool interruttoreChiuso = false;
bool valoreGrezzoInterruttore = false;

const bool CANALE1_ABILITATO = true;
const bool CANALE2_ABILITATO = true;

bool interruttoreChiuso_stampato = false;
bool nodoRemotoVivo = false;
bool nodoRemotoChiuso = false;

const uint8_t MAX_NODI_SUPPORTATI = 8;
bool vivoPrecedente[MAX_NODI_SUPPORTATI] = {false};
bool vivoNotoPrecedente[MAX_NODI_SUPPORTATI] = {false};

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

void setup_daisy_chain() {
  pinMode(PIN_BUS, OUTPUT);
  digitalWrite(PIN_BUS, LOW);
  pinMode(PIN_INTERRUTTORE, INPUT); // pull-down 10k esterno gia' cablato

  inc1 = (uint32_t)(((uint64_t)FREQ1 << 32) / SAMPLE_RATE);
  inc2 = (uint32_t)(((uint64_t)FREQ2 << 32) / SAMPLE_RATE);

  soundEnabled = true; // parte GIA' ACCESO - in campo nessuno scrive "ON" a mano
  digitalWrite(11, HIGH);

  letturaGrezzaCorrente = (digitalRead(PIN_INTERRUTTORE) == HIGH);
  valoreGrezzoInterruttore = letturaGrezzaCorrente;
  if (valoreGrezzoInterruttore) interruttoreChiuso = true;
  interruttoreChiuso_stampato = interruttoreChiuso;

  inizio_stato_ms = millis();

  Serial.println(F("--- MODALITA 0: daisy chain (produzione), MASTER NODE_ID=1 ---"));
  Serial.print(F("FREQ1=")); Serial.print(FREQ1);
  Serial.print(F("Hz  FREQ2=")); Serial.print(FREQ2); Serial.println(F("Hz"));
  Serial.print(F("T0_MS=")); Serial.print(T0_MS);
  Serial.print(F("  T1_MS=")); Serial.print(T1_MS);
  Serial.print(F("  GAP_MS=")); Serial.println(GAP_MS);
  Serial.print(F("NUMERO_NODI=")); Serial.println(NUMERO_NODI);
  Serial.print(F("interruttore proprio: ")); Serial.println(interruttoreChiuso ? "CHIUSO" : "aperto");
  Serial.println(F("Pronto, suono GIA' ACCESO. Scrivi 'OFF' o 'ON' e premi invio per disattivare/riattivare."));
}

// Misura la durata di un impulso HIGH su PIN_BUS, con timeout - stessa cosa
// che fa pulseIn() di Arduino, ma SENZA usarlo: pulseIn() stima i
// microsecondi contando cicli di CPU, e l'interrupt di Timer2 (il DDS) che
// spara ogni 25us continuamente corrompe quel conteggio. micros() usa
// Timer0, indipendente da Timer2: resta accurato comunque.
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

  uint8_t idx = contatore_impulso;
  if (idx < MAX_NODI_SUPPORTATI &&
      (!vivoNotoPrecedente[idx] || nodoRemotoVivo != vivoPrecedente[idx])) {
    Serial.print(F("Nodo "));
    Serial.print(idx);
    Serial.println(nodoRemotoVivo ? F(": risponde") : F(": NON RISPONDE"));
    vivoPrecedente[idx] = nodoRemotoVivo;
    vivoNotoPrecedente[idx] = true;
  }

  pinMode(PIN_BUS, OUTPUT);
  digitalWrite(PIN_BUS, LOW);
}

void aggiorna_master(unsigned long ora_ms) {
  unsigned long trascorso = ora_ms - inizio_stato_ms;

  switch (stato) {
    case EMETTI_IMPULSO:
      digitalWrite(PIN_BUS, HIGH);
      delay(DURATA_IMPULSO_TRIGGER_MS);
      digitalWrite(PIN_BUS, LOW);

      if (contatore_impulso == 0) {
        stato = TONO_T0;
      } else if (contatore_impulso == NODE_ID_PROPRIO) {
        stato = TONO_SLOT_PROPRIO;
      } else {
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
      canale1_on = CANALE1_ABILITATO;
      canale2_on = CANALE2_ABILITATO && interruttoreChiuso;
      if (trascorso >= T1_MS) {
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
      canale1_on = false;
      canale2_on = false;
      interroga_nodo_remoto();
      stato = TONO_SLOT_REMOTO;
      inizio_stato_ms = millis();
      break;

    case TONO_SLOT_REMOTO:
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

void aggiorna_interruttore() {
  bool letturaOra = (digitalRead(PIN_INTERRUTTORE) == HIGH);
  if (letturaOra != letturaGrezzaCorrente) {
    letturaGrezzaCorrente = letturaOra;
    ultimo_cambio_lettura_ms = millis();
    return;
  }
  if (millis() - ultimo_cambio_lettura_ms < DEBOUNCE_INTERRUTTORE_MS) {
    return;
  }
  valoreGrezzoInterruttore = letturaGrezzaCorrente;
  if (valoreGrezzoInterruttore) {
    interruttoreChiuso = true;
  }
}

void stampa_stato_interruttore() {
  if (interruttoreChiuso != interruttoreChiuso_stampato) {
    interruttoreChiuso_stampato = interruttoreChiuso;
    Serial.println(interruttoreChiuso ? "INT: chiuso" : "INT: aperto");
  }
}

// ============================================================
// Modalita' 1: SWEEP FREQUENZE (ex TEST ARMONICHE/sweep_gradini.ino)
// ============================================================
const uint32_t SWEEPF_FREQ_MIN = 200;
const uint32_t SWEEPF_FREQ_MAX = 6000;
const uint32_t SWEEPF_FREQ_STEP = 200;
const unsigned long SWEEPF_STEP_DURATION_MS = 4000;

uint32_t sweepf_freq_corrente;
unsigned long sweepf_inizio_gradino_ms;

void sweepf_imposta(uint32_t freq_hz) {
  inc1 = (uint32_t)(((uint64_t)freq_hz << 32) / SAMPLE_RATE);
}

void sweepf_stampa(uint32_t freq_hz) {
  Serial.print(F("STEP;"));
  Serial.print(millis());
  Serial.print(';');
  Serial.println(freq_hz);
}

void setup_sweep_frequenze() {
  // Parte GIA' ACCESO (non piu' "spento, aspetta ON" come lo sketch
  // originale): in campo non c'e' nessuna seriale da cui arriverebbe
  // quel comando - stesso motivo per cui soundEnabled parte true anche in
  // DAISY_CHAIN. Gli script Python riconoscono l'inizio del tono dal
  // flusso UDP stesso, non serve piu' nessun handshake.
  soundEnabled = true;
  digitalWrite(11, HIGH);
  canale2_on = false;
  sweepf_freq_corrente = SWEEPF_FREQ_MIN;
  sweepf_imposta(sweepf_freq_corrente);
  sweepf_inizio_gradino_ms = millis();

  Serial.println(F("--- MODALITA 1: sweep frequenze (TEST ARMONICHE) ---"));
  Serial.print(F("FREQ_MIN=")); Serial.print(SWEEPF_FREQ_MIN);
  Serial.print(F("  FREQ_MAX=")); Serial.print(SWEEPF_FREQ_MAX);
  Serial.print(F("  FREQ_STEP=")); Serial.println(SWEEPF_FREQ_STEP);
  Serial.print(F("STEP_DURATION_MS=")); Serial.println(SWEEPF_STEP_DURATION_MS);
  Serial.println(F("Pronto (silenzio). Scrivi 'ON' o 'OFF' e premi invio."));
  sweepf_stampa(sweepf_freq_corrente);
}

void aggiorna_sweep_frequenze(unsigned long ora_ms) {
  canale1_on = soundEnabled;
  if (ora_ms - sweepf_inizio_gradino_ms < SWEEPF_STEP_DURATION_MS) return;

  sweepf_freq_corrente += SWEEPF_FREQ_STEP;
  if (sweepf_freq_corrente > SWEEPF_FREQ_MAX) {
    sweepf_freq_corrente = SWEEPF_FREQ_MIN; // ripete in loop, come l'originale
  }
  sweepf_imposta(sweepf_freq_corrente);
  sweepf_inizio_gradino_ms = ora_ms;
  sweepf_stampa(sweepf_freq_corrente);
}

// ============================================================
// Modalita' 2: SWEEP TEMPI (ex TEST TEMPI/sweep_tempi.ino)
// ============================================================
// Frequenza di test per questa modalita': di default F2 (3200Hz) - per
// testare F1 invece, cambiare questa costante e riflashare (stessa scelta
// manuale per sessione dello sketch originale, non c'e' automatismo per
// "quale frequenza oggi" dato che e' una decisione diversa ogni volta).
const uint32_t TEMPI_FREQ_TEST_HZ = FREQ2;

const uint16_t TEMPI_DURATE_ON_MS[] = {150, 120, 100, 80, 60, 50, 40, 30, 25, 20, 15, 10};
const uint8_t TEMPI_N_DURATE_ON = sizeof(TEMPI_DURATE_ON_MS) / sizeof(TEMPI_DURATE_ON_MS[0]);
const uint16_t TEMPI_GAP_FISSO_FASE_A_MS = 300;

const uint16_t TEMPI_DURATE_GAP_MS[] = {200, 150, 120, 100, 80, 60, 50, 40, 30, 25, 20, 15, 10};
const uint8_t TEMPI_N_DURATE_GAP = sizeof(TEMPI_DURATE_GAP_MS) / sizeof(TEMPI_DURATE_GAP_MS[0]);
const uint16_t TEMPI_ON_FISSO_FASE_B_MS = 80;

const uint8_t TEMPI_RIPETIZIONI_PER_GRADINO = 20;

enum FaseTempi : uint8_t { TEMPI_FASE_A, TEMPI_FASE_B, TEMPI_SWEEP_FINITO };
FaseTempi tempi_fase_corrente = TEMPI_FASE_A;
uint8_t tempi_indice_gradino = 0;
uint8_t tempi_ripetizione_corrente = 0;
uint16_t tempi_on_ms_corrente = 0;
uint16_t tempi_gap_ms_corrente = 0;
bool tempi_nello_stato_on = false;
unsigned long tempi_inizio_stato_ms = 0;

void tempi_stampa_gradino() {
  Serial.print(F("STEP;"));
  Serial.print(millis());
  Serial.print(';');
  Serial.print(tempi_fase_corrente == TEMPI_FASE_A ? 'A' : 'B');
  Serial.print(';');
  Serial.print(tempi_on_ms_corrente);
  Serial.print(';');
  Serial.println(tempi_gap_ms_corrente);
}

void tempi_avvia_gradino() {
  if (tempi_fase_corrente == TEMPI_FASE_A) {
    tempi_on_ms_corrente = TEMPI_DURATE_ON_MS[tempi_indice_gradino];
    tempi_gap_ms_corrente = TEMPI_GAP_FISSO_FASE_A_MS;
  } else if (tempi_fase_corrente == TEMPI_FASE_B) {
    tempi_on_ms_corrente = TEMPI_ON_FISSO_FASE_B_MS;
    tempi_gap_ms_corrente = TEMPI_DURATE_GAP_MS[tempi_indice_gradino];
  }
  tempi_ripetizione_corrente = 0;
  tempi_nello_stato_on = true;
  canale1_on = true;
  tempi_inizio_stato_ms = millis();
  tempi_stampa_gradino();
}

void tempi_avanza_gradino() {
  tempi_indice_gradino++;
  if (tempi_fase_corrente == TEMPI_FASE_A && tempi_indice_gradino >= TEMPI_N_DURATE_ON) {
    tempi_fase_corrente = TEMPI_FASE_B;
    tempi_indice_gradino = 0;
  } else if (tempi_fase_corrente == TEMPI_FASE_B && tempi_indice_gradino >= TEMPI_N_DURATE_GAP) {
    tempi_fase_corrente = TEMPI_SWEEP_FINITO;
  }
  if (tempi_fase_corrente != TEMPI_SWEEP_FINITO) {
    tempi_avvia_gradino();
  } else {
    canale1_on = false;
    Serial.println(F("SWEEP_FINITO"));
  }
}

void setup_sweep_tempi() {
  // Vedi commento identico in setup_sweep_frequenze(): parte GIA' ACCESO,
  // nessun handshake seriale assunto disponibile.
  soundEnabled = true;
  digitalWrite(11, HIGH);
  canale2_on = false;
  inc1 = (uint32_t)(((uint64_t)TEMPI_FREQ_TEST_HZ << 32) / SAMPLE_RATE);

  Serial.println(F("--- MODALITA 2: sweep tempi (TEST TEMPI) ---"));
  Serial.print(F("FREQ_TEST_HZ=")); Serial.println(TEMPI_FREQ_TEST_HZ);
  Serial.print(F("RIPETIZIONI_PER_GRADINO=")); Serial.println(TEMPI_RIPETIZIONI_PER_GRADINO);
  Serial.println(F("Pronto (silenzio). Scrivi 'ON' o 'OFF' e premi invio."));

  tempi_avvia_gradino();
}

void aggiorna_sweep_tempi(unsigned long ora_ms) {
  if (tempi_fase_corrente == TEMPI_SWEEP_FINITO) return;

  unsigned long trascorso = ora_ms - tempi_inizio_stato_ms;
  uint16_t durata_stato = tempi_nello_stato_on ? tempi_on_ms_corrente : tempi_gap_ms_corrente;
  if (trascorso < durata_stato) return;

  tempi_inizio_stato_ms = ora_ms;
  if (tempi_nello_stato_on) {
    tempi_nello_stato_on = false;
    canale1_on = false;
  } else {
    tempi_nello_stato_on = true;
    canale1_on = true;
    tempi_ripetizione_corrente++;
    if (tempi_ripetizione_corrente >= TEMPI_RIPETIZIONI_PER_GRADINO) {
      tempi_avanza_gradino();
    }
  }
}

// ============================================================
// Comune a tutte le modalita'
// ============================================================
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

void setup() {
  Serial.begin(9600);

  for (int i = 0; i < 256; i++) {
    sineTable[i] = (uint8_t)(127.5 + 127.5 * sin(2.0 * PI * i / 256.0));
  }
  pinMode(12, OUTPUT); // OC1B, uscita audio (verso il trasduttore/fibra)
  pinMode(11, OUTPUT);

  // Timer1: Fast PWM 8 bit, nessun prescaler -> portante PWM a 62.5kHz
  TCCR1A = _BV(COM1B1) | _BV(WGM10);
  TCCR1B = _BV(WGM12) | _BV(CS10);
  OCR1B = 128; // silenzio iniziale

  // Timer2: CTC, genera interrupt a SAMPLE_RATE per aggiornare il campione
  TCCR2A = _BV(WGM21);
  TCCR2B = _BV(CS21);
  OCR2A = (16000000UL / 8 / SAMPLE_RATE) - 1;
  TIMSK2 = _BV(OCIE2A);

  // --- Selettore modalita' (vedi intestazione del file) ---
  if (EEPROM.read(EEPROM_ADDR_FIRMA) != FIRMA_INIZIALIZZATA) {
    // Primo boot dopo il flash di questo sketch: forza un punto di
    // partenza noto (modalita' 0, sessione "confermata") invece di fidarsi
    // di qualunque valore residuo ci fosse in EEPROM prima.
    EEPROM.write(EEPROM_ADDR_FIRMA, FIRMA_INIZIALIZZATA);
    EEPROM.write(EEPROM_ADDR_CONTATORE, 0);
    EEPROM.write(EEPROM_ADDR_CONFERMATO, 1);
  }
  uint8_t era_confermato = EEPROM.read(EEPROM_ADDR_CONFERMATO);
  uint8_t contatore = EEPROM.read(EEPROM_ADDR_CONTATORE);
  if (era_confermato) {
    contatore = 0;
  } else {
    contatore = (contatore + 1) % NUM_MODALITA;
  }
  EEPROM.write(EEPROM_ADDR_CONTATORE, contatore);
  EEPROM.write(EEPROM_ADDR_CONFERMATO, 0);
  modalita_attiva = (Modalita)contatore;
  inizio_boot_ms = millis();

  sei();

  switch (modalita_attiva) {
    case DAISY_CHAIN:     setup_daisy_chain(); break;
    case SWEEP_FREQUENZE: setup_sweep_frequenze(); break;
    case SWEEP_TEMPI:     setup_sweep_tempi(); break;
    default: break;
  }
}

ISR(TIMER2_COMPA_vect) {
  phase1 += inc1;
  uint8_t idx1 = phase1 >> 24;

  if (!soundEnabled) {
    OCR1B = 128;
    return;
  }

  if (modalita_attiva == DAISY_CHAIN) {
    phase2 += inc2;
    uint8_t idx2 = phase2 >> 24;
    uint8_t contrib1 = canale1_on ? sineTable[idx1] : 128;
    uint8_t contrib2 = canale2_on ? sineTable[idx2] : 128;
    OCR1B = ((uint16_t)contrib1 + contrib2) / 2;
  } else {
    // Modalita' sweep: un solo tono, piena ampiezza (non mediato con un
    // secondo canale fittizio) - stessa uscita delle vecchie sketch
    // separate, per numeri comparabili con le misure gia' fatte.
    OCR1B = canale1_on ? sineTable[idx1] : 128;
  }
}

void loop() {
  if (!sessione_confermata && millis() - inizio_boot_ms >= SOGLIA_SESSIONE_LUNGA_MS) {
    sessione_confermata = true;
    EEPROM.write(EEPROM_ADDR_CONFERMATO, 1); // scrittura UNICA per boot
  }

  switch (modalita_attiva) {
    case DAISY_CHAIN:
      aggiorna_interruttore();
      stampa_stato_interruttore();
      aggiorna_master(millis());
      break;
    case SWEEP_FREQUENZE:
      aggiorna_sweep_frequenze(millis());
      break;
    case SWEEP_TEMPI:
      aggiorna_sweep_tempi(millis());
      break;
    default:
      break;
  }
  gestisci_seriale();
}
