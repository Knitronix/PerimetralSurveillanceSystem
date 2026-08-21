"""
sensor_integrity_verification.py
-----------------------------------
KPI 4.3 — Sensor integrity verification.
Ascolta per una durata configurabile e verifica: continuità dei pacchetti,
livello di rumore plausibile, assenza di clipping. Pass/fail esplicito per
ciascun controllo, più il tempo di verifica.

Il risultato viene anche loggato in kpi_selftest_log.jsonl, letto da
calcola_kpi.py mission-ready per sommare questo tempo a quello di
calibrazione (KPI 4.5).

Uso:
    python sensor_integrity_verification.py --durata 30
"""

import argparse
import json
import socket
import time
import numpy as np

UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254
SAMPLE_RATE = 24000
BYTES_ATTESI = CAMPIONI_PER_PACCHETTO * 2
FILE_LOG_SELFTEST = "kpi_selftest_log.jsonl"

# Soglie di plausibilità: da tarare sulla base dell'esperienza reale del
# sistema. Questi default sono un punto di partenza, non valori assoluti.
RMS_MINIMO_PLAUSIBILE = 5.0      # sotto questo: sospetto fibra scollegata / segnale digitale piatto
RMS_MASSIMO_PLAUSIBILE = 20000.0  # sopra questo: sospetto saturazione permanente
FRAZIONE_MINIMA_PACCHETTI = 0.85  # rispetto al teorico, sotto: continuità compromessa
SOGLIA_CLIPPING = 32000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--durata", type=float, default=30.0, help="secondi di ascolto")
    args = ap.parse_args()

    t0 = time.monotonic()
    print(f"Self-test sensore — ascolto per {args.durata:.0f}s...")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(1.0)

    pacchetti_ricevuti = 0
    rms_totali = []
    picco_massimo_assoluto = 0

    t_fine = time.monotonic() + args.durata
    while time.monotonic() < t_fine:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        if len(data) != BYTES_ATTESI:
            continue
        campioni = np.frombuffer(data, dtype=np.int16)
        rms_totali.append(float(np.sqrt(np.mean(campioni.astype(np.float64) ** 2))))
        picco_massimo_assoluto = max(picco_massimo_assoluto, int(np.max(np.abs(campioni))))
        pacchetti_ricevuti += 1

    sock.close()
    durata_verifica = time.monotonic() - t0

    pacchetti_attesi = int(args.durata * SAMPLE_RATE / CAMPIONI_PER_PACCHETTO)
    frazione_ricevuta = pacchetti_ricevuti / pacchetti_attesi if pacchetti_attesi else 0.0
    rms_medio = float(np.mean(rms_totali)) if rms_totali else 0.0

    test_continuita = frazione_ricevuta >= FRAZIONE_MINIMA_PACCHETTI
    test_rumore = RMS_MINIMO_PLAUSIBILE <= rms_medio <= RMS_MASSIMO_PLAUSIBILE
    test_clipping = picco_massimo_assoluto < SOGLIA_CLIPPING

    print()
    print(f"{'Continuità pacchetti':<28} {'PASS' if test_continuita else 'FAIL':<6} "
          f"({pacchetti_ricevuti}/{pacchetti_attesi} attesi, {frazione_ricevuta:.0%})")
    print(f"{'Livello di rumore':<28} {'PASS' if test_rumore else 'FAIL':<6} (RMS medio: {rms_medio:.1f})")
    print(f"{'Assenza di clipping':<28} {'PASS' if test_clipping else 'FAIL':<6} (picco massimo: {picco_massimo_assoluto})")
    print()
    esito_globale = test_continuita and test_rumore and test_clipping
    print(f"ESITO GLOBALE: {'PASS' if esito_globale else 'FAIL'}")
    print(f"Tempo di verifica: {durata_verifica:.1f}s")

    try:
        with open(FILE_LOG_SELFTEST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "esito_globale": "PASS" if esito_globale else "FAIL",
                "durata_verifica_sec": round(durata_verifica, 2),
                "test_continuita": test_continuita,
                "test_rumore": test_rumore,
                "test_clipping": test_clipping,
                "rms_medio": round(rms_medio, 1),
                "picco_massimo": picco_massimo_assoluto,
            }, ensure_ascii=False) + "\n")
        print(f"\nRisultato registrato in: {FILE_LOG_SELFTEST}")
    except Exception as e:
        print(f"⚠ Impossibile scrivere {FILE_LOG_SELFTEST}: {e}")


if __name__ == "__main__":
    main()