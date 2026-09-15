// Nodo MASTER della catena daisy chain - bus condiviso, open-loop a tempo
// fisso (vedi DAISY CHAIN/restart.md, versione corrente: NON e' piu' una
// staffetta punto-punto).
//
// Il master e' l'iniziatore del bus (nessun D2 collegato) ED E' ANCHE il
// nodo NODE_ID=1 del protocollo f1/f2 ("MASTER=SLAVE1" in restart.md):
// scandisce da solo il tempo, senza aspettare conferma da nessuno slave, ed
// emette sul bus (D8, in parallelo a tutti gli slave) una sequenza di
// impulsi il cui indice coincide ESATTAMENTE con l'evento generato a
// livello di frequenze:
//   impulso 0 -> f1 per T0_MS (marcatore di zero)
//   impulso 1 -> f1 per T1_MS (+f2 se il proprio interruttore e' chiuso) - e' il master stesso (NODE_ID=1)
//   impulso 2 -> nessun tono locale, e' il turno di SLAVE2 (NODE_ID=2): il
//                master aspetta INTERVALLO_MS "alla cieca" (open-loop) che
//                lo slave finisca il proprio slot, poi prosegue
//   impulso k (2 < k <= NUMERO_NODI) -> stesso schema di impulso 2, per i
//                futuri slave3..slaveN
// Dopo l'ultimo impulso, pausa fine ciclo e si riparte dall'impulso 0.
//
// Il master conosce sempre lo stato di avanzamento (e' lui a scandirlo):
// non serve nessun filo di ritorno da SLAVE N verso il master.
//
// Sintesi f1/f2 (DDS + PWM su OC1B + filtro RC esterno) duplicata da
// ../../trasmettitore/trasmettitore.ino: l'IDE Arduino non segue #include
// relativi fuori dalla cartella dello sketch.
//
// F1/F2/T0_MS/T1_MS/GAP_MS sotto sono AUTOGENERATI da
// ../aggiorna_config_daisy.py a partire da ../../config_condivisa.py (unica
// fonte di verita' per tutto il progetto, main.py compreso) - non
// modificarli a mano qui.

#include <avr/io.h>
#include <avr/interrupt.h>
#include <math.h>

// --- Pin daisy chain (SPECS.MD DAISY CHAIN/restart.md - non cambiare senza
// aggiornare anche il cablaggio) ---
const int PIN_TRIGGER_OUT = 8; // bus condiviso, verso D2 di TUTTI gli slave in parallelo

uint8_t sineTable[256];
volatile uint32_t phase1 = 0, phase2 = 0;
uint32_t inc1, inc2;
// Hz, frequenza di aggiornamento del DDS - stesso valore/stessa
// motivazione di trasmettitore.ino (16 campioni/periodo per f1, 40 per f2).
const uint32_t SAMPLE_RATE = 40000;

// --- Frequenze canale (Hz) - AUTOGENERATO, vedi intestazione sopra ---
const uint32_t FREQ1 = 4600;  // f1: clock/counter
const uint32_t FREQ2 = 3200;  // f2: stato interruttore

// --- Timing protocollo (SPECS.MD §2) - AUTOGENERATO, vedi intestazione
// sopra. Solo il master ha T0_MS: e' l'unico nodo che emette il marcatore
// di zero. ---
const unsigned long T0_MS  = 200; // durata marcatore di zero
const unsigned long T1_MS  = 50;  // durata del proprio slot
const unsigned long GAP_MS = 70;  // silenzio dopo ogni tono, prima del prossimo impulso

// --- Identita' di questo nodo sul bus e topologia corrente ---
const uint8_t NODE_ID_PROPRIO = 1; // il master e' sempre NODE_ID 1
// Numero totale di nodi nel ciclo (oggi: master + slave2 = 2) - AUTOGENERATO
// da ../aggiorna_config_daisy.py a partire da ../../config_condivisa.py: e'
// lo stesso valore che main.py usa per il warning "interruttori attesi/
// ciclo", quindi non va cambiato solo qui (si perderebbe comunque al
// prossimo giro dello script, e andrebbe fuori sincrono con main.py).
// Aggiungere uno slave in futuro richiede SOLO di incrementare
// NUMERO_NODI in config_condivisa.py (nessuna modifica architetturale,
// restart.md "Nota per estensione futura").
const uint8_t NUMERO_NODI = 2;

// Intervallo "alla cieca" (open-loop) tra un impulso di slot e il
// successivo, quando il turno e' di un nodo remoto: deve coprire per intero
// T1_MS (il tono) + GAP_MS (il silenzio dopo) di quel nodo, cosi' il master
// non emette l'impulso successivo prima che lo slot remoto sia finito.
// Derivato da T1_MS/GAP_MS (stessa fonte di verita', non un valore a parte
// da tenere allineato a mano) - restart.md: "INTERVALLO_MS = T1_MS + margine_sicurezza".
const unsigned long INTERVALLO_MS = T1_MS + GAP_MS;

// Durata dell'impulso di trigger in uscita (5-10ms, restart.md).
const unsigned long DURATA_IMPULSO_TRIGGER_MS = 8;

// Pausa dopo l'ultimo impulso del ciclo prima di ripartire dall'impulso 0.
// Deve restare ABBONDANTEMENTE piu' lunga di SOGLIA_TIMEOUT_RESET_MS (vedi
// slave2.ino) cosi' ogni slave si "auto-resetta" per tempo e riconosce il
// prossimo impulso 0 come tale (self-healing, restart.md).
const unsigned long PAUSA_FINE_CICLO_MS = 500;

// Placeholder per test SENZA hardware reale collegato, stesso schema di
// trasmettitore.ino ma per UN SOLO interruttore (quello di questo nodo):
// sostituire con la lettura reale (digitalRead su un pin dedicato, o dal
// multiplexer) quando il proprio interruttore e' cablato - vedi
// aggiorna_interruttore() sotto.
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
// (vedi aggiorna_master()) - nessun altro punto del codice deve scriverci.
bool interruttoreChiuso = false;

// Isolamento canali per la procedura di taratura soglie (SPECS.MD §5.1),
// identico a trasmettitore.ino.
const bool CANALE1_ABILITATO = true;
const bool CANALE2_ABILITATO = true;

// Il suono parte attivo dall'accensione (niente piu' controllo seriale
// ON/OFF, vedi setup()/loop()): canale1_on/canale2_on partono comunque a
// false, quindi resta silenzioso finche' la macchina a stati non accende
// davvero un tono.
volatile bool canale1_on = false;
volatile bool canale2_on = false;

// --- Macchina a stati del ciclo (non blocca mai in loop() tranne
// l'impulso di trigger, esplicitamente autorizzato bloccante da
// restart.md). contatore_impulso e' il "k" descritto sopra: EMETTI_IMPULSO
// lo trasmette sul bus, poi lo stato successivo dipende da chi e' il
// destinatario di quel k. ---
enum StatoMaster : uint8_t {
  EMETTI_IMPULSO,
  TONO_T0,
  GAP_DOPO_T0,
  TONO_SLOT_PROPRIO,
  GAP_DOPO_SLOT_PROPRIO,
  ATTESA_SLOT_REMOTO,
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
  digitalWrite(11, HIGH); // abilita audio permanentemente, niente piu' controllo seriale ON/OFF
  pinMode(PIN_TRIGGER_OUT, OUTPUT);
  digitalWrite(PIN_TRIGGER_OUT, LOW);

  // Timer1: Fast PWM 8 bit, nessun prescaler -> portante PWM a 62.5kHz
  TCCR1A = _BV(COM1B1) | _BV(WGM10);
  TCCR1B = _BV(WGM12) | _BV(CS10);
  OCR1B = 128; // silenzio iniziale (valore centrale)

  // Timer2: CTC, genera interrupt a SAMPLE_RATE per aggiornare il campione
  TCCR2A = _BV(WGM21);
  TCCR2B = _BV(CS21); // prescaler 8
  OCR2A = (16000000UL / 8 / SAMPLE_RATE) - 1;
  TIMSK2 = _BV(OCIE2A);

  randomSeed(analogRead(A0)); // pin scollegato = rumore, seed diverso ad ogni boot
  // Stato iniziale del latch: coerente con "mai sovrascrivere a false", solo
  // eventualmente a true (vedi dichiarazione di interruttoreChiuso sopra).
  if (SIMULA_INTERRUTTORE_CASUALE && random(0, 100) < PERCENTUALE_CHIUSO_SIMULATO) {
    interruttoreChiuso = true;
  }

  inizio_stato_ms = millis();
  sei();

  Serial.println(F("--- daisy chain (bus condiviso): MASTER (NODE_ID=1) ---"));
  Serial.print(F("SAMPLE_RATE=")); Serial.print(SAMPLE_RATE); Serial.println(F("Hz"));
  Serial.print(F("FREQ1=")); Serial.print(FREQ1);
  Serial.print(F("Hz  FREQ2=")); Serial.print(FREQ2); Serial.println(F("Hz"));
  Serial.print(F("T0_MS=")); Serial.print(T0_MS);
  Serial.print(F("  T1_MS=")); Serial.print(T1_MS);
  Serial.print(F("  GAP_MS=")); Serial.println(GAP_MS);
  Serial.print(F("NUMERO_NODI=")); Serial.print(NUMERO_NODI);
  Serial.print(F("  INTERVALLO_MS=")); Serial.print(INTERVALLO_MS);
  Serial.print(F("  PAUSA_FINE_CICLO_MS=")); Serial.println(PAUSA_FINE_CICLO_MS);
  Serial.print(F("interruttore proprio: ")); Serial.println(interruttoreChiuso ? "CHIUSO" : "aperto");
  Serial.println(F("Sincronizzazione avviata automaticamente all'accensione."));
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

void aggiorna_master(unsigned long ora_ms) {
  unsigned long trascorso = ora_ms - inizio_stato_ms;

  switch (stato) {
    case EMETTI_IMPULSO:
      // Impulso bloccante (autorizzato da restart.md): l'audio e' gia'
      // silenzioso (arriviamo qui solo da uno stato di silenzio o dal boot).
      // Il DDS continua a girare in background (Timer2 non disabilitato da
      // delay()).
      digitalWrite(PIN_TRIGGER_OUT, HIGH);
      delay(DURATA_IMPULSO_TRIGGER_MS);
      digitalWrite(PIN_TRIGGER_OUT, LOW);

      if (contatore_impulso == 0) {
        stato = TONO_T0;
      } else if (contatore_impulso == NODE_ID_PROPRIO) {
        stato = TONO_SLOT_PROPRIO;
      } else {
        stato = ATTESA_SLOT_REMOTO;
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
      // canale1_on/canale2_on assegnati insieme (non separatamente): stessa
      // guardia contro la finestra di lettura a meta' dell'ISR asincrona
      // gia' documentata in trasmettitore.ino.
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

    case ATTESA_SLOT_REMOTO:
      // Il turno e' di un altro nodo: il master tace e aspetta alla cieca
      // (open-loop, restart.md) che quello slot remoto finisca.
      canale1_on = false;
      canale2_on = false;
      if (trascorso >= INTERVALLO_MS) {
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
  aggiorna_master(millis());
  // nessun delay() qui: solo lo stato EMETTI_IMPULSO blocca brevemente, per
  // l'impulso, come autorizzato da restart.md
}
