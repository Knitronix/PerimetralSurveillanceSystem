"""
config_condivisa.py
--------------------
Unica fonte di verità per i parametri del protocollo f1/f2 che main.py e
trasmettitore.ino devono avere IDENTICI per funzionare insieme (frequenze e
timing) - condivisa anche dagli script Python di test in TEST TEMPI/. Cambia
qui, cambia ovunque - non serve più aggiornare a mano main.py e gli script
di test uno per uno, ed è proprio quello che ha causato in passato
disallineamenti reali passati inosservati (es. T0_MS a 180 su Arduino
contro 200 atteso da main.py: con tolleranza stretta, il marcatore di zero
non veniva più riconosciuto affatto).

Il lato Arduino (trasmettitore/trasmettitore.ino) NON legge questo file
direttamente - il toolchain Arduino non segue percorsi relativi fuori dalla
cartella dello sketch, un header condiviso non ha funzionato (vedi
aggiorna_config_ino.py). Le costanti corrispondenti lì restano valori
letterali, ma allineate automaticamente: dopo aver cambiato un valore qui,
lancia `python aggiorna_config_ino.py` (da questa cartella) per riscriverle
dentro trasmettitore.ino, poi riflasha lo sketch.

GAP_MS non serve a main.py (il rilevatore non lo misura mai, vedi SPECS.MD
§2) ma vive comunque qui perché va comunque tenuto allineato con
sweep_tempi.ino ogni volta che lo cambi dopo una caratterizzazione in
TEST TEMPI/.

Le soglie (SOGLIA_ON/OFF) restano locali a ciascun file: a differenza di
frequenze e timing non si "ereditano" da sole quando cambi f1/f2, vanno
sempre ritarate da capo con la procedura di SPECS.MD §5.1 - non avrebbe
senso farle cascare in automatico da un valore che comunque richiede una
nuova misura ogni volta.
"""

F1_HZ = 4600.0
F2_HZ = 3200.0

# Timing del protocollo (SPECS.MD §2): marcatore di zero, durata di uno
# slot, silenzio tra un simbolo e il successivo. Validati con lo sweep di
# TEST TEMPI/ prima di essere fissati qui.
T0_MS = 200.0
T1_MS = 20.0
GAP_MS = 50.0

# Tolleranza di classificazione delle durate in main.py: quanto vicino a
# T0_MS/T1_MS deve cadere una durata misurata per contare come marcatore/
# slot valido invece che rumore. Con T1_MS=20ms (un solo blocco Goertzel da
# 20ms, il minimo rappresentabile) va tenuta stretta - vedi il commento
# dettagliato accanto a TOLLERANZA_MS_DEFAULT in main.py.
TOLLERANZA_MS = 10.0
