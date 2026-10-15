// Sketch standalone per Arduino Mega - nessuna dipendenza da file esterni
// a parte le librerie AVR incluse sotto. restart.md (nella cartella
// arduino/ che contiene anche questa) spiega l'architettura completa del
// bus. Le costanti sotto devono restare uguali a quelle in
// master/master.ino: se vanno cambiate, si modifica a mano in entrambi.
//
// Nodo SLAVE (NODE_ID=2, slot 2 del protocollo) della catena daisy chain -
// bus condiviso, open-loop a tempo fisso (vedi restart.md).
//
// A differenza della versione precedente, questo slave NON genera piu'
// nessun tono f1/f2: il master e' l'UNICO generatore di tutta la catena
// (vedi intestazione di master.ino per il perche'). Il compito di questo
// sketch si riduce a due cose:
//   1. leggere il proprio interruttore locale (A0), con debounce e latch
//      (identico a prima - questa parte non cambia)
//   2. quando il master lo indirizza (conteggio impulsi sul bus = proprio
//      NODE_ID), rispondere con UN impulso sulla STESSA linea del bus,
//      la cui durata codifica "vivo + stato interruttore" - vedi
//      invia_risposta() sotto
//
// L'uscita D8 del master e' collegata in parallelo su D2/PIN_BUS di TUTTI
// gli slave (bus broadcast, non punto-punto): questo slave conta gli
// impulsi che vede passare sul bus e agisce SOLO quando il conteggio
// coincide con il proprio NODE_ID. Il primo impulso ricevuto dopo un
// silenzio prolungato (fine ciclo precedente) vale "0" (il marcatore di
// zero, mai generato qui, solo il master lo emette): da li' si riparte a
// contare 1, 2, 3...
//
// PIN_BUS e' bidirezionale a turno: INPUT (con interrupt) mentre ascolta
// gli impulsi di indirizzamento del master, OUTPUT solo per la breve
// finestra in cui invia la propria risposta - mai contemporaneamente al
// master, per costruzione temporale (restart.md "Interrogazione remota").

#include <avr/io.h>
#include <avr/interrupt.h>
#include <util/atomic.h>

// --- Pin daisy chain (restart.md - non cambiare senza aggiornare anche il
// cablaggio) ---
const int PIN_BUS = 2; // dal bus condiviso D8 del master (interrupt-capable, pull-down 10k gia' cablato)

// --- Identita' di questo nodo sul bus ---
const uint8_t NODE_ID_PROPRIO = 2;

// --- Risposta all'interrogazione del master (restart.md "Interrogazione
// remota") - DEVONO restare identiche a master.ino, che e' chi classifica
// la durata che riceve. ---
// Tempo che questo nodo aspetta dopo aver visto il proprio impulso di
// indirizzamento, prima di rispondere: deve restare ABBONDANTEMENTE piu'
// lungo della durata dell'impulso di indirizzamento del master (8ms),
// altrimenti risponderebbe mentre il master sta ancora guidando lui stesso
// il bus (contesa). Valore di partenza, da verificare sull'hardware reale.
const unsigned long MARGINE_SICUREZZA_RISPOSTA_MS = 15;
const unsigned long DURATA_RISPOSTA_APERTO_MS = 6;  // impulso corto = aperto
const unsigned long DURATA_RISPOSTA_CHIUSO_MS = 18; // impulso lungo = chiuso

// Soglia di timeout per il self-healing (restart.md, valore suggerito
// 300ms): se non arriva nessun impulso per piu' di questo tempo, il ciclo
// del master e' sicuramente finito - lo slave si resetta da solo, pronto a
// riconoscere il prossimo impulso come "0". Fa da rete di sicurezza anche
// se un impulso si perde per rumore elettrico.
const unsigned long SOGLIA_TIMEOUT_RESET_MS = 300;

const unsigned long DEBOUNCE_US = 2000; // 2ms, rimbalzi elettrici sul fronte (restart.md)

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
// della propria risposta risulta di nuovo aperto - altrimenti un click
// breve tra un ciclo e l'altro andrebbe perso. Va a true SOLO dentro
// aggiorna_interruttore() (mai a false li' dentro), e torna a false SOLO in
// invia_risposta() subito dopo essere stato consumato per la durata
// dell'impulso - nessun altro punto del codice deve scriverci.
bool interruttoreChiuso = false;

// Valore GREZZO (debounced) del pin, sale E scende liberamente - usato
// internamente da aggiorna_interruttore() per decidere quando riarmare il
// latch sotto. Non stampato (debug via pin HIGH/LOW rimosso, non serve
// piu': il cablaggio era il problema, non la lettura).
bool valoreGrezzoInterruttore = false;

// Per stampare lo stato dell'interruttore solo quando cambia (debug, vedi
// stampa_stato_interruttore()), non ad ogni giro di loop().
bool interruttoreChiuso_stampato = false;

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

  pinMode(PIN_BUS, INPUT); // pull-down 10k esterno gia' cablato, non serve INPUT_PULLUP
  pinMode(PIN_INTERRUTTORE, INPUT); // pull-down 10k esterno gia' cablato

  // Interrupt hardware sul fronte di salita (riposo LOW via pull-down,
  // impulso in ingresso HIGH) - restart.md.
  attachInterrupt(digitalPinToInterrupt(PIN_BUS), onImpulsoRicevuto, RISING);

  // Stato iniziale: leggi subito il pin reale, cosi' non si parte sempre da
  // "aperto" per un giro intero se l'interruttore e' gia' chiuso al boot.
  letturaGrezzaCorrente = (digitalRead(PIN_INTERRUTTORE) == HIGH);
  valoreGrezzoInterruttore = letturaGrezzaCorrente;
  if (valoreGrezzoInterruttore) {
    interruttoreChiuso = true;
  }

  sei();

  Serial.println(F("--- daisy chain (bus condiviso): SLAVE2 (NODE_ID=2), nessun tono generato qui ---"));
  Serial.print(F("MARGINE_SICUREZZA_RISPOSTA_MS=")); Serial.println(MARGINE_SICUREZZA_RISPOSTA_MS);
  Serial.print(F("risposta aperto=")); Serial.print(DURATA_RISPOSTA_APERTO_MS);
  Serial.print(F("ms  chiuso=")); Serial.print(DURATA_RISPOSTA_CHIUSO_MS); Serial.println(F("ms"));
  Serial.print(F("SOGLIA_TIMEOUT_RESET_MS=")); Serial.println(SOGLIA_TIMEOUT_RESET_MS);
  Serial.print(F("interruttore proprio: ")); Serial.println(interruttoreChiuso ? "CHIUSO" : "aperto");
  interruttoreChiuso_stampato = interruttoreChiuso;
  Serial.println(F("Pronto, in ascolto sul bus (D2)."));
}

// Risponde all'interrogazione del master sulla STESSA linea del bus
// (PIN_BUS, bidirezionale a turno - restart.md "Interrogazione remota").
// Bloccante: a differenza del master, qui non c'e' nessun DDS da tenere
// vivo durante l'attesa (questo nodo non genera piu' audio), quindi non
// c'e' nessun controindicazione a bloccare brevemente loop().
void invia_risposta() {
  // Aspetta che il master abbia finito il proprio impulso di
  // indirizzamento e rilasciato il bus (lo fa sempre entro poche decine di
  // ms) - senza questo margine si rischia la contesa sul bus (due nodi che
  // lo guidano insieme).
  delay(MARGINE_SICUREZZA_RISPOSTA_MS);

  // Disattiva l'interrupt PRIMA di guidare il pin: altrimenti il nostro
  // stesso fronte di uscita si autotriggererebbe come un falso impulso del
  // master.
  detachInterrupt(digitalPinToInterrupt(PIN_BUS));
  pinMode(PIN_BUS, OUTPUT);
  digitalWrite(PIN_BUS, HIGH);
  delay(interruttoreChiuso ? DURATA_RISPOSTA_CHIUSO_MS : DURATA_RISPOSTA_APERTO_MS);
  digitalWrite(PIN_BUS, LOW);
  pinMode(PIN_BUS, INPUT); // torna in ascolto, il pull-down esterno riprende il controllo
  attachInterrupt(digitalPinToInterrupt(PIN_BUS), onImpulsoRicevuto, RISING);

  // Consumato per questa risposta: unico punto del codice che rimette il
  // latch a false, pronto a catturare una nuova chiusura nel prossimo ciclo
  // (vedi dichiarazione di interruttoreChiuso).
  interruttoreChiuso = false;
}

void aggiorna_slave(unsigned long ora_ms) {
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

  if (impulso_locale && conta_locale == NODE_ID_PROPRIO) {
    invia_risposta();
  }
}

// Monitoraggio dell'interruttore proprio, continuo e indipendente dal ciclo
// del protocollo (chiamato ad ogni giro di loop()): interruttoreChiuso
// riflette sempre lo stato piu' recente, cosi' se cambia mentre il nodo non
// sta rispondendo la prossima invia_risposta() lo trova gia' aggiornato -
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
  // rilevato subito dopo il primo consumo (invia_risposta() lo resetta a
  // false, e senza questo il valore grezzo non cambierebbe piu' finche' il
  // pin resta fermo, quindi il latch non si riarmerebbe mai). Resta vero
  // che non va mai messo a false qui dentro (solo invia_risposta() lo
  // consuma, vedi dichiarazione di interruttoreChiuso).
  if (valoreGrezzoInterruttore) {
    interruttoreChiuso = true;
  }
}

// Debug: stampa lo stato del LATCH (vedi interruttoreChiuso) SOLO quando
// cambia. Per costruzione va sempre a coppie chiuso/aperto (si accende alla
// chiusura, si spegne quando la risposta lo consuma).
void stampa_stato_interruttore() {
  if (interruttoreChiuso != interruttoreChiuso_stampato) {
    interruttoreChiuso_stampato = interruttoreChiuso;
    Serial.println(interruttoreChiuso ? "INT: chiuso" : "INT: aperto");
  }
}

void loop() {
  aggiorna_interruttore();
  stampa_stato_interruttore();
  aggiorna_slave(millis());
  // nessun delay() qui fuori: l'attesa dell'impulso di indirizzamento e'
  // event-driven via interrupt (restart.md); invia_risposta() blocca solo
  // per la propria, breve, finestra di risposta.
}
