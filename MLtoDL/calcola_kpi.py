"""
calcola_kpi.py
----------------
Cruscotto per i KPI operativi:
- calibration-time  (TC-4.1)
- mission-ready      (TC-4.5)
- pd                 (TC-5.1 / 5.2 / 5.3 — Probability of Detection)
- far                (TC-5.5 — False Alarm Rate)

Uso:
    python calcola_kpi.py calibration-time
    python calcola_kpi.py mission-ready --tempo-posa-minuti 45
    python calcola_kpi.py pd --pianificati eventi_pianificati.csv --log dataset_sensori_24kHz/eventi_log.jsonl
    python calcola_kpi.py far --log dataset_sensori_24kHz/eventi_log.jsonl --inizio ... --fine ...
"""

import argparse
import csv
import json
import statistics
from datetime import datetime

FILE_LOG_CALIBRAZIONE = "kpi_calibration_log.jsonl"
FILE_LOG_SELFTEST = "kpi_selftest_log.jsonl"

# Tolleranza temporale per associare un evento pianificato a un evento
# rilevato nel log: se il trigger cade entro questa finestra rispetto
# all'intervallo pianificato, lo consideriamo lo stesso evento fisico.
TOLLERANZA_MATCH_SEC = 3.0


def _leggi_jsonl(path):
    righe = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for riga in f:
                riga = riga.strip()
                if riga:
                    righe.append(json.loads(riga))
    except FileNotFoundError:
        pass
    return righe


def cmd_calibration_time(args):
    righe = _leggi_jsonl(FILE_LOG_CALIBRAZIONE)
    if not righe:
        print(f"Nessun dato in {FILE_LOG_CALIBRAZIONE}. Usa il pulsante \"✅ Sistema Pronto\" nel registratore per registrarli.")
        return
    tempi = [r["secondi_da_avvio"] for r in righe]
    print(f"Misurazioni disponibili: {len(tempi)}")
    for i, t in enumerate(tempi, 1):
        print(f"  {i}. {t:.1f}s")
    if len(tempi) < 2:
        print("\nServe almeno 2 misurazioni per media/deviazione standard (consigliate: N=10).")
        return
    media = statistics.mean(tempi)
    dev_std = statistics.stdev(tempi)
    print(f"\nCalibration time: {media:.1f}s ± {dev_std:.1f}s  (N={len(tempi)})")
    if len(tempi) < 10:
        print("Nota: la tua tabella KPI richiede N=10 ripetizioni per un valore consolidato.")


def cmd_mission_ready(args):
    calib = _leggi_jsonl(FILE_LOG_CALIBRAZIONE)
    selftest = _leggi_jsonl(FILE_LOG_SELFTEST)

    if not calib:
        print(f"Manca il calibration time (vedi {FILE_LOG_CALIBRAZIONE}). Esegui prima quel KPI.")
        return
    tempo_calibrazione = statistics.mean([r["secondi_da_avvio"] for r in calib])

    tempo_selftest = 0.0
    if selftest:
        tempo_selftest = statistics.mean([r["durata_verifica_sec"] for r in selftest])
    else:
        print("Nota: nessun log di self-test trovato — assumo 0s per quella voce (esegui sensor_integrity_verification.py e salvane il log se serve includerla).")

    tempo_posa_sec = args.tempo_posa_minuti * 60.0
    totale_sec = tempo_calibrazione + tempo_selftest + tempo_posa_sec

    print("Mission Ready time:")
    print(f"  Calibrazione (4.1):      {tempo_calibrazione:6.1f}s")
    print(f"  Self-test (4.3):         {tempo_selftest:6.1f}s")
    print(f"  Posa fibra (manuale):    {tempo_posa_sec:6.1f}s ({args.tempo_posa_minuti} min)")
    print(f"  TOTALE:                  {totale_sec:6.1f}s  ({totale_sec / 60:.1f} min)")


def cmd_pd(args):
    with open(args.pianificati, "r", encoding="utf-8") as f:
        pianificati = list(csv.DictReader(f))

    log = _leggi_jsonl(args.log)
    intervalli_rilevati = []
    for r in log:
        try:
            inizio = datetime.fromisoformat(r["timestamp"])
            fine_stimata = inizio  # il timestamp nel log è quello di SALVATAGGIO (fine evento)
            intervalli_rilevati.append((inizio, fine_stimata, r.get("durata_s", 0.0)))
        except (KeyError, ValueError):
            continue

    per_categoria = {}
    mancati = []
    for ev in pianificati:
        categoria = ev["categoria"]
        t_inizio = datetime.fromisoformat(ev["timestamp_inizio"])
        t_fine = datetime.fromisoformat(ev["timestamp_fine"])

        rilevato = False
        for (t_salvataggio, _, durata_s) in intervalli_rilevati:
            t_inizio_evento_rilevato = t_salvataggio  # approssimazione: momento di salvataggio
            delta_inizio = abs((t_inizio_evento_rilevato - t_inizio).total_seconds())
            delta_fine = abs((t_inizio_evento_rilevato - t_fine).total_seconds())
            if delta_inizio <= TOLLERANZA_MATCH_SEC or delta_fine <= TOLLERANZA_MATCH_SEC:
                rilevato = True
                break

        per_categoria.setdefault(categoria, {"totali": 0, "rilevati": 0})
        per_categoria[categoria]["totali"] += 1
        if rilevato:
            per_categoria[categoria]["rilevati"] += 1
        else:
            mancati.append(ev)

    print("Probability of Detection per categoria:")
    for categoria, dati in per_categoria.items():
        pd_val = dati["rilevati"] / dati["totali"] if dati["totali"] else 0.0
        print(f"  {categoria:<12} PD = {dati['rilevati']}/{dati['totali']} = {pd_val:.1%}")

    if mancati:
        print(f"\nEventi non rilevati ({len(mancati)}):")
        for ev in mancati:
            print(f"  #{ev['event_id']} — {ev['categoria']} — {ev['timestamp_inizio']}")


def cmd_far(args):
    log = _leggi_jsonl(args.log)
    inizio = datetime.fromisoformat(args.inizio)
    fine = datetime.fromisoformat(args.fine)
    durata_ore = (fine - inizio).total_seconds() / 3600.0

    conteggio = 0
    for r in log:
        try:
            t = datetime.fromisoformat(r["timestamp"])
        except (KeyError, ValueError):
            continue
        if inizio <= t <= fine:
            conteggio += 1

    far_orario = conteggio / durata_ore if durata_ore > 0 else 0.0
    print(f"Periodo analizzato: {args.inizio} → {args.fine} ({durata_ore:.2f} ore)")
    print(f"Eventi registrati nel periodo (= falsi allarmi, per definizione): {conteggio}")
    print(f"False Alarm Rate: {far_orario:.2f} falsi allarmi/ora")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="comando", required=True)

    sub.add_parser("calibration-time")

    p_mr = sub.add_parser("mission-ready")
    p_mr.add_argument("--tempo-posa-minuti", type=float, required=True)

    p_pd = sub.add_parser("pd")
    p_pd.add_argument("--pianificati", required=True)
    p_pd.add_argument("--log", required=True)

    p_far = sub.add_parser("far")
    p_far.add_argument("--log", required=True)
    p_far.add_argument("--inizio", required=True, help="ISO 8601, es. 2026-09-10T14:00:00")
    p_far.add_argument("--fine", required=True)

    args = ap.parse_args()
    {
        "calibration-time": cmd_calibration_time,
        "mission-ready": cmd_mission_ready,
        "pd": cmd_pd,
        "far": cmd_far,
    }[args.comando](args)


if __name__ == "__main__":
    main()