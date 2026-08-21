"""
train_classifier.py
--------------------
Allena e confronta due classificatori (Random Forest e SVM RBF) sugli eventi
raccolti con registratore_stalta.py, e salva il migliore dei due (per F1
macro sul test set).

Uso:
    python train_classifier.py --dataset dataset_sensori_24kHz --out modello_classificatore.pkl

Perché confrontare due modelli invece di sceglierne uno a priori: con un
dataset piccolo/medio non è ovvio in anticipo quale dei due generalizzi
meglio (dipende da quanto le classi sono separabili linearmente nello spazio
delle feature, quanto rumore c'è, quanto sono bilanciate). Costa poco
allenarli entrambi e tenere il migliore secondo una metrica misurata, invece
di doverlo indovinare prima di avere dati.

Perché F1 macro come metrica di selezione: fa la media dell'F1 di ogni
classe con lo stesso peso, indipendentemente da quanti esempi ha. Con classi
sbilanciate (es. molto più RUMORE_AMBIENTALE che SCAVO) l'accuratezza
semplice premierebbe un modello che ignora le classi rare; F1 macro no.
"""

import argparse
import glob
import os
import sys

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.svm import SVC
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.preprocessing import LabelEncoder, StandardScaler

from audio_features import estrai_feature_da_wav, NOMI_FEATURE

MIN_CAMPIONI_PER_CLASSE = 8


def carica_dataset(cartella_dataset):
    """Struttura attesa: cartella_dataset/<id_fibra>/<classe>/*.wav"""
    pattern = os.path.join(cartella_dataset, "*", "*", "*.wav")
    file_wav = sorted(glob.glob(pattern))

    if not file_wav:
        print(f"Nessun file WAV trovato con pattern: {pattern}")
        sys.exit(1)

    X, y = [], []
    for path in file_wav:
        classe = os.path.basename(os.path.dirname(path))
        try:
            feat = estrai_feature_da_wav(path)
        except Exception as e:
            print(f"  ! Skip {path}: errore estrazione feature ({e})")
            continue
        X.append(feat)
        y.append(classe)

    return np.array(X), np.array(y)


def filtra_classi_troppo_piccole(X, y):
    classi, conteggi = np.unique(y, return_counts=True)
    classi_escluse = classi[conteggi < MIN_CAMPIONI_PER_CLASSE]
    if len(classi_escluse) > 0:
        print(f"\nClassi escluse dal training (meno di {MIN_CAMPIONI_PER_CLASSE} esempi):")
        for c in classi_escluse:
            n = conteggi[list(classi).index(c)]
            print(f"  {c}: {n} campioni — servono almeno {MIN_CAMPIONI_PER_CLASSE}")
        maschera = ~np.isin(y, classi_escluse)
        X, y = X[maschera], y[maschera]
    return X, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="dataset_sensori_24kHz")
    ap.add_argument("--out", default="modello_classificatore.pkl")
    ap.add_argument("--test-size", type=float, default=0.25)
    args = ap.parse_args()

    print(f"Carico dataset da: {args.dataset}")
    X, y = carica_dataset(args.dataset)

    print("\nDistribuzione classi (prima del filtro):")
    for c, n in zip(*np.unique(y, return_counts=True)):
        print(f"  {c}: {n} campioni")

    X, y = filtra_classi_troppo_piccole(X, y)
    classi_rimaste, conteggi = np.unique(y, return_counts=True)

    if len(classi_rimaste) < 2:
        print("\nServono almeno 2 classi con abbastanza esempi per allenare un classificatore.")
        sys.exit(1)

    encoder = LabelEncoder()
    y_enc = encoder.fit_transform(y)

    min_per_classe = conteggi.min()
    stratify = y_enc if min_per_classe >= 4 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X, y_enc, test_size=args.test_size, random_state=42, stratify=stratify
    )

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    risultati = {}

    # --- Random Forest ---
    print("\n--- Allenamento Random Forest ---")
    rf = RandomForestClassifier(class_weight="balanced", random_state=42, n_jobs=-1)
    grid_rf = GridSearchCV(
        rf,
        param_grid={"n_estimators": [200, 400], "max_depth": [None, 10, 20], "min_samples_leaf": [1, 2, 4]},
        scoring="f1_macro",
        cv=min(3, min_per_classe) if min_per_classe >= 2 else 2,
        n_jobs=-1,
    )
    grid_rf.fit(X_train_s, y_train)
    pred_rf = grid_rf.predict(X_test_s)
    f1_rf = f1_score(y_test, pred_rf, average="macro", zero_division=0)
    print(f"Random Forest — migliori parametri: {grid_rf.best_params_}")
    print(f"Random Forest — F1 macro sul test set: {f1_rf:.3f}")
    risultati["random_forest"] = (grid_rf.best_estimator_, f1_rf, pred_rf)

    # --- SVM RBF ---
    print("\n--- Allenamento SVM (kernel RBF) ---")
    svm = SVC(kernel="rbf", probability=True, class_weight="balanced", random_state=42)
    grid_svm = GridSearchCV(
        svm,
        param_grid={"C": [0.5, 1, 5, 10], "gamma": ["scale", "auto", 0.01, 0.1]},
        scoring="f1_macro",
        cv=min(3, min_per_classe) if min_per_classe >= 2 else 2,
        n_jobs=-1,
    )
    grid_svm.fit(X_train_s, y_train)
    pred_svm = grid_svm.predict(X_test_s)
    f1_svm = f1_score(y_test, pred_svm, average="macro", zero_division=0)
    print(f"SVM RBF — migliori parametri: {grid_svm.best_params_}")
    print(f"SVM RBF — F1 macro sul test set: {f1_svm:.3f}")
    risultati["svm_rbf"] = (grid_svm.best_estimator_, f1_svm, pred_svm)

    # --- Scelta del migliore ---
    nome_vincitore = max(risultati, key=lambda k: risultati[k][1])
    modello_vincitore, f1_vincitore, pred_vincitore = risultati[nome_vincitore]
    print(f"\n=== Modello scelto: {nome_vincitore} (F1 macro = {f1_vincitore:.3f}) ===")

    print("\n--- Report di classificazione (modello scelto, sul test set) ---")
    print(classification_report(y_test, pred_vincitore, target_names=encoder.classes_, zero_division=0))
    print("Matrice di confusione (righe=vero, colonne=predetto):")
    print(list(encoder.classes_))
    print(confusion_matrix(y_test, pred_vincitore))

    if nome_vincitore == "random_forest":
        print("\n--- Importanza delle feature (top 10) ---")
        importanze = modello_vincitore.feature_importances_
        ordine = np.argsort(importanze)[::-1][:10]
        for i in ordine:
            print(f"  {NOMI_FEATURE[i]}: {importanze[i]:.3f}")

    # Riallena il modello vincitore (con gli stessi iperparametri) su TUTTO
    # il dataset (train+test) per la versione finale da usare in produzione.
    X_s = scaler.fit_transform(X)
    if nome_vincitore == "random_forest":
        modello_finale = RandomForestClassifier(**grid_rf.best_params_, class_weight="balanced",
                                                 random_state=42, n_jobs=-1)
    else:
        modello_finale = SVC(kernel="rbf", **grid_svm.best_params_, probability=True,
                              class_weight="balanced", random_state=42)
    modello_finale.fit(X_s, y_enc)

    joblib.dump({
        "modello": modello_finale,
        "tipo_modello": nome_vincitore,
        "scaler": scaler,
        "encoder": encoder,
        "nomi_feature": NOMI_FEATURE,
        "f1_macro_test": f1_vincitore,
    }, args.out)
    print(f"\nModello salvato in: {args.out}")


if __name__ == "__main__":
    main()