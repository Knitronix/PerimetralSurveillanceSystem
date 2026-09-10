"""
aggiorna_config_ino.py
------------------------
Arduino non riesce a includere un header con percorso relativo fuori dalla
cartella dello sketch (provato con config_condivisa.h in trasmettitore.ino:
"fatal error: ../config_condivisa.h: No such file or directory" - i tool di
build Arduino cercano gli #include solo dentro la cartella dello sketch e
nelle librerie, non seguono ".." verso l'alto). Invece di un header
condiviso, questo script fa la stessa cosa "a mano": legge F1_HZ/F2_HZ da
config_condivisa.py e riscrive le righe corrispondenti direttamente dentro
trasmettitore/trasmettitore.ino.

main.py non ha questo problema (è Python, `import config_condivisa` funziona
davvero) - questo script serve solo per il lato Arduino.

Sincronizza sia le frequenze (FREQ1/FREQ2, dichiarate `uint32_t`) sia il
timing del protocollo (T0_MS/T1_MS/GAP_MS, dichiarate `unsigned long` - tipo
diverso, da qui i due pattern separati sotto).

Uso: cambia i valori in config_condivisa.py, poi lancia
`python aggiorna_config_ino.py` da questa cartella (FREQUENCY DETECTION/),
poi riflasha trasmettitore.ino sull'Arduino come al solito.

Non tocca TEST TEMPI/sweep_tempi/sweep_tempi.ino: lì FREQ_TEST_HZ è una
scelta manuale di quale frequenza caratterizzare in quella sessione (può
essere F1, F2, o anche un valore diverso per testare candidati nuovi), non
un valore che deve sempre combaciare con F1_HZ/F2_HZ correnti.
"""

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import config_condivisa

CARTELLA = Path(__file__).resolve().parent
FILE_TRASMETTITORE = CARTELLA / "trasmettitore" / "trasmettitore.ino"


def sostituisci_costante(testo, tipo_cpp, nome_costante, nuovo_valore):
    pattern = re.compile(rf"(const {tipo_cpp}\s+{nome_costante}\s*=\s*)\d+(\s*;)")
    nuovo_testo, n_sostituzioni = pattern.subn(rf"\g<1>{int(nuovo_valore)}\g<2>", testo)
    if n_sostituzioni == 0:
        print(f"ATTENZIONE: non ho trovato 'const {tipo_cpp} {nome_costante} = ...;' in {FILE_TRASMETTITORE.name}")
    return nuovo_testo, n_sostituzioni


def main():
    if not FILE_TRASMETTITORE.exists():
        print(f"Non trovo {FILE_TRASMETTITORE}")
        sys.exit(1)

    testo = FILE_TRASMETTITORE.read_text(encoding="utf-8")
    da_scrivere = [
        ("uint32_t", "FREQ1", config_condivisa.F1_HZ),
        ("uint32_t", "FREQ2", config_condivisa.F2_HZ),
        ("unsigned long", "T0_MS", config_condivisa.T0_MS),
        ("unsigned long", "T1_MS", config_condivisa.T1_MS),
        ("unsigned long", "GAP_MS", config_condivisa.GAP_MS),
    ]

    n_totale = 0
    for tipo_cpp, nome, valore in da_scrivere:
        testo, n = sostituisci_costante(testo, tipo_cpp, nome, valore)
        n_totale += n

    if n_totale < len(da_scrivere):
        print("Nessuna modifica scritta (almeno un pattern non trovato, controlla il file a mano).")
        sys.exit(1)

    FILE_TRASMETTITORE.write_text(testo, encoding="utf-8")
    print(f"Aggiornato {FILE_TRASMETTITORE}:")
    for _tipo_cpp, nome, valore in da_scrivere:
        print(f"  {nome} = {int(valore)}")
    print("Ricorda di riflashare lo sketch sull'Arduino perché il cambiamento abbia effetto.")


if __name__ == "__main__":
    main()
