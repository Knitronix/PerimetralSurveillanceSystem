"""
valuta_modello.py
-------------------
KPI 7.1 — Personnel classification accuracy (accuracy/precision/recall per classe)
KPI 7.6 — Classification confidence (curva di calibrazione)

Va lanciato da dentro la cartella KPI/ (o con qualunque cwd: i percorsi di default sono
calcolati relativamente alla posizione di questo file, non alla cwd).

Uso:
    python valuta_modello.py
"""

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split

KPI_DIR = Path(__file__).resolve().parent
MLTODL_DIR = KPI_DIR.parent / "MLtoDL"
sys.path.insert(0, str(MLTODL_DIR))

from train_classifier import carica_dataset


def calcola_curva_calibrazione(y_true, probabilita_predette, etichette_predette, n_bin=10):
    """
    Per ogni bin di confidenza (es. 0.70-0.75, 0.75-0.80, ...), calcola la
    confidenza media dichiarata e l'accuratezza reale in quel bin. Un
    modello ben calibrato ha i due valori vicini per ogni bin.
    """
    confidenze = np.max(probabilita_predette, axis=1)
    corretto = (etichette_predette == y_true).astype(int)

    bordi = np.linspace(confidenze.min() if len(confidenze) else 0.0, 1.0, n_bin + 1)
    curva = []
    for i in range(n_bin):
        mask = (confidenze >= bordi[i]) & (confidenze < bordi[i + 1] if i < n_bin - 1 else confidenze <= bordi[i + 1])
        if mask.sum() == 0:
            continue
        curva.append({
            "bin_confidenza_min": round(float(bordi[i]), 3),
            "bin_confidenza_max": round(float(bordi[i + 1]), 3),
            "confidenza_media": round(float(confidenze[mask].mean()), 3),
            "accuratezza_reale": round(float(corretto[mask].mean()), 3),
            "n_campioni": int(mask.sum()),
        })
    return curva


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modello", default=str(MLTODL_DIR / "modello_classificatore.pkl"))
    ap.add_argument("--dataset", default=str(MLTODL_DIR / "dataset_sensori_24kHz"))
    ap.add_argument("--test-size", type=float, default=0.25)
    ap.add_argument("--out-71", default=str(KPI_DIR / "report_kpi_7_1.json"))
    ap.add_argument("--out-76", default=str(KPI_DIR / "report_kpi_7_6.json"))
    ap.add_argument("--out-grafico", default=str(KPI_DIR / "calibration_curve.png"))
    args = ap.parse_args()

    pacchetto = joblib.load(args.modello)
    modello, scaler, encoder = pacchetto["modello"], pacchetto["scaler"], pacchetto["encoder"]

    print(f"Carico dataset da: {args.dataset}")
    X, y = carica_dataset(args.dataset)
    y_enc = encoder.transform(y)  # usa l'encoder del modello, non uno nuovo

    min_per_classe = np.bincount(y_enc).min() if len(y_enc) else 0
    stratify = y_enc if min_per_classe >= 4 else None
    _, X_test, _, y_test = train_test_split(X, y_enc, test_size=args.test_size, random_state=42, stratify=stratify)

    X_test_s = scaler.transform(X_test)
    pred = modello.predict(X_test_s)
    probs = modello.predict_proba(X_test_s)

    # --- KPI 7.1 ---
    report = classification_report(y_test, pred, target_names=encoder.classes_, output_dict=True, zero_division=0)
    matrice = confusion_matrix(y_test, pred).tolist()

    with open(args.out_71, "w", encoding="utf-8") as f:
        json.dump({
            "classification_report": report,
            "matrice_confusione": matrice,
            "classi": list(encoder.classes_),
            "n_campioni_test": int(len(y_test)),
        }, f, ensure_ascii=False, indent=2)
    print(f"\nKPI 7.1 salvato in: {args.out_71}")
    print(classification_report(y_test, pred, target_names=encoder.classes_, zero_division=0))

    # --- KPI 7.6 ---
    curva = calcola_curva_calibrazione(y_test, probs, pred)
    with open(args.out_76, "w", encoding="utf-8") as f:
        json.dump({"curva_calibrazione": curva}, f, ensure_ascii=False, indent=2)
    print(f"KPI 7.6 salvato in: {args.out_76}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        conf = [c["confidenza_media"] for c in curva]
        acc = [c["accuratezza_reale"] for c in curva]
        plt.figure(figsize=(5, 5))
        plt.plot([0, 1], [0, 1], "--", color="gray", label="Calibrazione perfetta")
        plt.plot(conf, acc, marker="o", color="C0", label="Modello")
        plt.xlabel("Confidenza dichiarata")
        plt.ylabel("Accuratezza reale")
        plt.title("Curva di calibrazione")
        plt.legend()
        plt.tight_layout()
        plt.savefig(args.out_grafico, dpi=120)
        print(f"Grafico salvato in: {args.out_grafico}")
    except ImportError:
        print("matplotlib non disponibile: grafico non generato (i dati sopra restano validi).")


if __name__ == "__main__":
    main()