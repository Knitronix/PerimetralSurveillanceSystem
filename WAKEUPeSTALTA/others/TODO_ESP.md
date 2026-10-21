# TODO dopo l'acquisto del modulo ESP — fattibilità

Scritto il 19/10/2026. Obiettivo generale: **il minimo numero di modifiche
hardware possibile**. Per ogni punto: si può fare? cosa serve? rischi.

Hardware da comprare: **Wemos D1 mini (ESP8266)** x2 (uno di scorta) + 2
resistenze (partitore 1k/2k sulla linea TX Mega → RX ESP) + cavetti.
L'ESP va alimentato dai 5V, NON dal pin 3,3V del Mega.

---

## 1. Caricare il codice del master via WiFi (senza cavo USB)

**SI' — nessuna modifica al codice.**
Firmware **esp-link** sull'ESP, collegato a seriale 0 (pin 0/1) e RESET del
Mega: fa da "cavo USB senza fili". Upload da questo PC con un comando
`avrdude` pronto, oppure con una porta COM virtuale vista dall'Arduino IDE.
- Cambiare codice = cambiare anche frequenze e tempi (oggi sono costanti
  nel codice).
- Rischio basso: se un upload fallisce il bootloader resta intatto, si
  riprova.
- Mentre l'ESP è collegato non usare anche il cavo USB (stessa seriale).

## 2. Serial Monitor del master via WiFi

**SI' — nessuna modifica al codice** (viene con esp-link).
Si leggono da remoto i messaggi del master ("Nodo 2: NON RISPONDE",
"INT: chiuso"...), oggi invisibili in campo.

## 3. Togliere la Shelly: Arduino sempre accesi ma SILENZIOSI, main.py li fa parlare

**SI' — ma è la modifica più grossa: firmware di master e slave + main.py.**
Hardware: solo alimentazione fissa agli Arduino. Consiglio a costo zero:
**lasciare la Shelly installata e sempre accesa** invece di toglierla
fisicamente, come "interruttore di emergenza" per un riavvio completo da
remoto se un Arduino si blocca.

Come funziona: main.py apre una connessione TCP verso l'ESP e manda righe
di testo al master (es. `PARLA`, `ZITTO`). Il master ha già oggi i comandi
seriali `ON`/`OFF`, ma non bastano così come sono (vedi sotto).

Cosa si guadagna:
- **sparisce la cecità al risveglio** (REVISIONE.md punto A1): niente boot
  da 1,4 s, il master parla entro pochi ms dal comando, e le chiusure sono
  registrate anche mentre è zitto
- sparisce il rischio del "risveglio cieco in modalità sweep" (B1): la
  modalità si sceglie con un comando, non più con i cicli della Shelly

Da progettare (firmware):
- **Mentre è zitto il ciclo deve fermarsi**, non solo il suono: oggi `OFF`
  silenzia ma il ciclo continua e "consuma" le chiusure registrate → si
  perderebbero proprio quelle da segnalare.
- **Memoria delle chiusure con scadenza**: una chiusura di ore prima non va
  riportata al risveglio come allarme. Proposta: si riportano solo le
  chiusure degli ultimi N secondi (es. 5 s) o ancora in corso.
- **Scelta della modalità via comando** (`MODO 0/1/2`) al posto dei cicli
  di accensione — gli script `trova_frequenze.py` e `analizza_tempi_minimi.py`
  vanno adattati.
- **Watchdog**: main.py manda un `PING` periodico e mostra un avviso se
  l'ESP non risponde (oggi la stessa cosa la fa per la Shelly). Il Mega
  attiva il proprio watchdog hardware per riavviarsi da solo se si blocca.
- **Cosa fare se il WiFi cade mentre il master parla**: proposta, torna
  zitto da solo dopo X secondi senza comandi.

Rischi:
- il sistema di allarme dipende dal WiFi: oggi abbiamo già visto la Shelly
  non rispondere per qualche secondo. main.py deve gestirlo (riprova +
  avviso). Alternativa più robusta da valutare: ESP con **Ethernet via
  cavo** (es. scheda WT32-ETH01) — ma esp-link gira solo su ESP8266,
  andrebbe scelto un altro firmware per il punto 1.
- da verificare sul campo che l'uscita "zitta" non disturbi la fibra (in
  teoria no: è lo stesso stato delle pause di oggi, già misurate pulite).

## 4. Volume del tono controllabile via software

**SI' — solo firmware del master, zero hardware.**
Nel generatore (ISR del master) si scala l'onda prima di mandarla al PWM:
`uscita = 128 + (onda − 128) × volume / 256`. Costo trascurabile per il
processore. Il volume si imposta con un comando via ESP (es. `VOL 60`) e si
salva in EEPROM.

Limite: il PWM è a 8 bit (256 livelli). A volume basso (sotto ~15-20%)
l'onda ha pochi livelli e aumentano le armoniche. Alzare i bit del PWM
abbasserebbe la portante (oggi 62,5 kHz) dentro la banda ascoltata: da non
fare.

Conseguenza importante: **cambiare volume cambia la potenza vista da
Goertzel** (∝ volume²) → con soglie fisse in main.py ogni cambio di volume
richiederebbe di ricalibrarle. Da fare insieme: **soglie relative al
marcatore** (vedi sezione "soglie" in fondo).

## 5. Rilevare il filo tagliato (resistenza di fine linea)

**SI' — hardware minimo: 2 resistenze per tappeto + firmware + main.py.**
Tecnica degli impianti d'allarme: resistenze sull'interruttore, A0 letto
come ingresso analogico invece che digitale. Dalla tensione si distinguono
4 stati: aperto, chiuso, **filo tagliato**, **cortocircuito**.
Va aggiunto un terzo stato "guasto" al protocollo f1/f2 (es. slot di durata
diversa) e in main.py (es. tappeto giallo).
Indipendente dall'ESP: si può fare anche prima o dopo.

## 6. Nodo remoto che non risponde visibile in main.py

**SI' — solo firmware del master + main.py, zero hardware.**
Stesso terzo stato "guasto" del punto 5: se lo slave non risponde, il master
lo segnala sulla fibra invece di trasmettere "aperto". Oggi un tappeto
guasto sembra un tappeto tranquillo (REVISIONE.md B2).

## 7. Frequenze e tempi cambiabili con un comando (senza ricaricare il codice)

**SI' — solo firmware del master.** Comando `FREQ 4400 3200`, salvato in
EEPROM. Comodo, ma con il punto 1 già fatto è meno urgente.

---

## Ordine consigliato

1 → 2 (nessuna modifica al codice, verifica che il canale WiFi è
affidabile) → 3 insieme a 4 (stesso firmware, stesso giro di prove) → 6 →
5 (unico con hardware in più) → 7.
