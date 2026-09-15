"""
aggiorna_config_daisy.py
--------------------------
Come ../aggiorna_config_ino.py, ma per i due sketch della catena daisy
chain in questa cartella (master/master.ino, slave2/slave2.ino, e i futuri
slaveN/slaveN.ino): l'IDE Arduino non segue #include relativi fuori dalla
cartella dello sketch, quindi anche qui la sincronizzazione dei valori
F1/F2/T0/T1/GAP e' fatta riscrivendo le costanti letterali direttamente nei
.ino, invece che con un header condiviso.

NON e' stata creata una seconda config: la fonte di verita' resta UNA SOLA,
../config_condivisa.py - lo stesso file gia' usato da ../main.py e dal
vecchio ../trasmettitore/trasmettitore.ino (nodo singolo, non toccato da
questo script). master.ino e slave2.ino DEVONO avere le stesse
frequenze/timing del resto del sistema, altrimenti main.py smette di
riconoscerli - e' esattamente il tipo di disallineamento gia' capitato una
volta con T0_MS a 180 invece di 200 (vedi intestazione di
config_condivisa.py). Questo script importa ../config_condivisa.py invece
di duplicarne i valori.

Uso: cambia i valori in ../config_condivisa.py, poi lancia
`python aggiorna_config_daisy.py` da questa cartella (DAISY CHAIN/), poi
riflasha master.ino sul MASTER e slave2.ino sullo SLAVE2.

slave2.ino (e i futuri slaveN.ino) non hanno una costante T0_MS: solo il
master emette il marcatore di zero (SPECS.MD §2) - da qui la lista di
costanti diversa tra master e slave sotto.
"""

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

CARTELLA = Path(__file__).resolve().parent
CARTELLA_PADRE = CARTELLA.parent  # FREQUENCY DETECTION/, dove vive config_condivisa.py
sys.path.insert(0, str(CARTELLA_PADRE))
import config_condivisa

FILE_MASTER = CARTELLA / "master" / "master.ino"
FILE_SLAVE2 = CARTELLA / "slave2" / "slave2.ino"


def sostituisci_costante(testo, tipo_cpp, nome_costante, nuovo_valore, nome_file):
    pattern = re.compile(rf"(const {tipo_cpp}\s+{nome_costante}\s*=\s*)\d+(\s*;)")
    nuovo_testo, n_sostituzioni = pattern.subn(rf"\g<1>{int(nuovo_valore)}\g<2>", testo)
    if n_sostituzioni == 0:
        print(f"ATTENZIONE: non ho trovato 'const {tipo_cpp} {nome_costante} = ...;' in {nome_file}")
    return nuovo_testo, n_sostituzioni


def aggiorna_file(percorso, da_scrivere):
    if not percorso.exists():
        print(f"Non trovo {percorso}")
        sys.exit(1)

    testo = percorso.read_text(encoding="utf-8")
    n_totale = 0
    for tipo_cpp, nome, valore in da_scrivere:
        testo, n = sostituisci_costante(testo, tipo_cpp, nome, valore, percorso.name)
        n_totale += n

    if n_totale < len(da_scrivere):
        print(f"Nessuna modifica scritta su {percorso.name} (almeno un pattern non trovato, controlla a mano).")
        sys.exit(1)

    percorso.write_text(testo, encoding="utf-8")
    print(f"Aggiornato {percorso}:")
    for _tipo_cpp, nome, valore in da_scrivere:
        print(f"  {nome} = {int(valore)}")


def main():
    comuni = [
        ("uint32_t", "FREQ1", config_condivisa.F1_HZ),
        ("uint32_t", "FREQ2", config_condivisa.F2_HZ),
        ("unsigned long", "T1_MS", config_condivisa.T1_MS),
        ("unsigned long", "GAP_MS", config_condivisa.GAP_MS),
    ]
    # NUMERO_NODI: solo il master lo usa (per sapere quanti impulsi emettere
    # per ciclo) - gli slave agiscono solo sul proprio NODE_ID, non hanno
    # bisogno di conoscere il totale (vedi ../restart.md).
    aggiorna_file(FILE_MASTER, comuni + [
        ("unsigned long", "T0_MS", config_condivisa.T0_MS),
        ("uint8_t", "NUMERO_NODI", config_condivisa.NUMERO_NODI),
    ])
    aggiorna_file(FILE_SLAVE2, comuni)
    print("Ricorda di riflashare master.ino sul MASTER e slave2.ino sullo SLAVE2.")


if __name__ == "__main__":
    main()
