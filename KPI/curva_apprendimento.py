"""
curva_apprendimento.py
------------------------
KPI 4.4 — AI baseline learning duration.
Allena il modello su frazioni crescenti del dataset, misura F1 macro ad
ogni step, individua il punto di plateau, e riporta il tempo totale di
training.

Va lanciato da dentro la cartella KPI/ (o con qualunque cwd: i percorsi di default sono
calcolati relativamente alla posizione di questo file, non alla cwd).

Uso:
    python curva_apprendimento.py
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler

KPI_DIR = Path(__file__).resolve().parent
MLTODL_DIR = KPI_DIR.parent / "MLtoDL"
sys.path.insert(0, str(MLTODL_DIR))

from train_classifier import carica_dataset, filtra_classi_troppo_piccole


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(MLTODL_DIR / "dataset_sensori_24kHz"))
    ap.add_argument("--soglia-plateau", type=float, default=0.01,
                     help="miglioramento minimo di F1 macro tra due step consecutivi, sotto il quale si considera raggiunto il plateau")
    ap.add_argument("--out-grafico", default=str(KPI_DIR / "curva_apprendimento.png"))
    args = ap.parse_args()

    t0 = time.monotonic()

    print(f"Carico dataset da: {args.dataset}")
    X, y = carica_dataset(args.dataset)
    X, y = filtra_classi_troppo_piccole(X, y)

    if len(np.unique(y)) < 2:
        print("Servono almeno 2 classi con abbastanza esempi.")
        return

    encoder = LabelEncoder()
    y_enc = encoder.fit_transform(y)

    X_train_full, X_test, y_train_full, y_test = train_test_split(
        X, y_enc, test_size=0.25, random_state=42, stratify=y_enc if min(np.bincount(y_enc)) >= 4 else None
    )

    frazioni = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    n_totale = len(X_train_full)
    risultati = []

    print("\nAllenamento su frazioni crescenti del dataset:")
    for frazione in frazioni:
        n_campioni = max(4, int(n_totale * frazione))
        X_sub = X_train_full[:n_campioni]
        y_sub = y_train_full[:n_campioni]

        if len(np.unique(y_sub)) < 2:
            print(f"  {frazione:.0%} ({n_campioni} campioni): skip, meno di 2 classi presenti")
            continue

        scaler = StandardScaler()
        X_sub_s = scaler.fit_transform(X_sub)
        X_test_s = scaler.transform(X_test)

        clf = RandomForestClassifier(n_estimators=200, class_weight="balanced", random_state=42, n_jobs=-1)
        clf.fit(X_sub_s, y_sub)
        pred = clf.predict(X_test_s)
        f1 = f1_score(y_test, pred, average="macro", zero_division=0)

        risultati.append((n_campioni, f1))
        print(f"  {frazione:.0%} ({n_campioni} campioni): F1 macro = {f1:.3f}")

    # Individuazione del plateau: primo punto oltre il quale il
    # miglioramento rispetto al passo precedente scende sotto la soglia.
    punto_plateau = None
    for i in range(1, len(risultati)):
        delta = risultati[i][1] - risultati[i - 1][1]
        if delta < args.soglia_plateau:
            punto_plateau = risultati[i - 1]
            break
    if punto_plateau is None and risultati:
        punto_plateau = risultati[-1]

    durata_totale = time.monotonic() - t0

    print(f"\nPlateau raggiunto a: {punto_plateau[0]} campioni (F1 macro = {punto_plateau[1]:.3f})")
    print(f"Tempo totale di training (tutte le frazioni): {durata_totale:.1f}s ({durata_totale / 3600:.3f} ore)")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        xs = [r[0] for r in risultati]
        ys = [r[1] for r in risultati]
        plt.figure(figsize=(7, 4))
        plt.plot(xs, ys, marker="o")
        plt.axvline(punto_plateau[0], color="red", linestyle="--", label="Plateau")
        plt.xlabel("Numero di campioni di training")
        plt.ylabel("F1 macro (test set)")
        plt.title("Curva di apprendimento")
        plt.legend()
        plt.tight_layout()
        plt.savefig(args.out_grafico, dpi=120)
        print(f"Grafico salvato in: {args.out_grafico}")
    except ImportError:
        print("matplotlib non disponibile: grafico non generato (i numeri sopra restano validi).")


if __name__ == "__main__":
    main()