# Prontuario — ad ogni cambio di specifiche hardware

Due percorsi trasmettitore attivi, non mescolarli (SPECS.MD §4): **`DAISY CHAIN/`**
(N Arduino, attivo oggi) e **`trasmettitore.ino`** (nodo singolo, riferimento/fallback).
I passi 1-4 sotto valgono per entrambi; cambia solo `UPDATE ITER` al punto 5.

1. **TEST ARMONICHE** (sweep frequenze, `TEST ARMONICHE/`) → trova f1/f2 migliori → `UPDATE ITER`
2. **TEST TEMPI** (sweep durate, `TEST TEMPI/`) → trova T1/GAP minimi affidabili — attenzione,
   lo sweep varia un parametro alla volta: se stringi T1 e GAP insieme vicino al minimo, il test
   non lo copre davvero (SPECS.MD §6.1) → `UPDATE ITER`
3. Scegli la tolleranza (`TOLLERANZA_MS` in `config_condivisa.py`): deve coprire non solo il
   rumore ma anche l'arrotondamento a blocchi Goertzel del ricevitore, che campiona con un
   orologio indipendente da qualsiasi trasmettitore (SPECS.MD §3) — non scendere sotto un blocco
   Goertzel di margine per parte, o marcatori/slot validi verranno scartati come rumore
4. Calcola le soglie (picchi misurati nello sweep, o "Azzera picchi" in `main.py`) → aggiorna
   `SOGLIA_ON/OFF_F1/F2` in `main.py` — procedura completa in SPECS.MD §5.1, isolando i canali con
   `CANALE1_ABILITATO`/`CANALE2_ABILITATO` (presenti su tutti e tre gli sketch: `trasmettitore.ino`,
   `DAISY CHAIN/master.ino`, `DAISY CHAIN/slave2.ino`)

## UPDATE ITER — `DAISY CHAIN/` (percorso attivo)

1. Modifica `config_condivisa.py` (F1/F2/T0/T1/GAP/TOLLERANZA/NUMERO_NODI)
2. Lancia `python aggiorna_config_daisy.py` dalla cartella `DAISY CHAIN/`
3. Riflasha `DAISY CHAIN/master/master.ino` sul master e `DAISY CHAIN/slave2/slave2.ino` su ogni slave

Aggiungere un nodo: incrementa `NUMERO_NODI` in `config_condivisa.py`, ripeti i passi sopra,
assegna al nuovo slave il prossimo `NODE_ID` libero (costante locale nel suo sketch, non
propagata dallo script) e collega il suo `D2` in parallelo allo stesso bus di trigger
(`DAISY CHAIN/restart.md` per il wiring). Nessuna modifica al master né agli slave esistenti.

## UPDATE ITER — `trasmettitore.ino` (nodo singolo, fallback)

1. Modifica `config_condivisa.py`
2. Lancia `python aggiorna_config_ino.py` (da `FREQUENCY DETECTION/`)
3. Riflasha `trasmettitore/trasmettitore.ino`
