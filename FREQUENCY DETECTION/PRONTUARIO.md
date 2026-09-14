# Prontuario — ad ogni cambio di specifiche hardware

1. **TEST ARMONICHE** (sweep frequenze, `TEST ARMONICHE/`) → trova f1/f2 migliori → `UPDATE ITER`
2. Calcola le soglie (picchi misurati nello sweep, o "Azzera picchi" in main.py) → aggiorna `SOGLIA_ON/OFF_F1/F2` in `main.py`
3. **TEST TEMPI** (sweep durate, `TEST TEMPI/`) → trova T1/GAP minimi affidabili → `UPDATE ITER`
4. Scegli la tolleranza (mai più larga del blocco Goertzel se T1 è piccolo) e ricontrolla `BLOCCO_GOERTZEL_MS` in `main.py`

## UPDATE ITER

1. Modifica `config_condivisa.py`
2. Lancia `python aggiorna_config_ino.py`
3. Riflasha `trasmettitore.ino`