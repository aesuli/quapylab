"""Small, download-free QuaPy compatibility experiment.

This is deliberately independent of the web application. It proves that the
selected Python/QuaPy/scikit-learn versions can import a labelled CSV, split
it deterministically, optimize, evaluate, and report both ACC and PACC.
"""

# Imports below the cache initialization are intentional.
# ruff: noqa: E402, I001

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Matplotlib is imported transitively by QuaPy. Keep its cache inside the
# ignored project data directory so the spike is hermetic in restricted CI.
os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / ".quapylab" / "matplotlib")
)

import numpy as np
import quapy as qp
from quapy.method.aggregative import ACC, PACC
from quapy.protocol import APP
from sklearn.datasets import make_classification
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split


SEED = 42
ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / ".quapylab" / "phase-0-reference-input.csv"


def labelled_collection(X: np.ndarray, y: np.ndarray) -> Any:
    return qp.data.LabelledCollection(X, y, classes=[0, 1])


def create_and_load_csv() -> tuple[np.ndarray, np.ndarray]:
    """Create a deterministic labelled fixture, then exercise CSV ingestion."""
    X, y = make_classification(
        n_samples=1_200,
        n_features=12,
        n_informative=8,
        n_redundant=2,
        weights=[0.7, 0.3],
        random_state=SEED,
    )
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = ",".join([f"feature_{index}" for index in range(X.shape[1])] + ["label"])
    np.savetxt(CSV_PATH, np.column_stack((X, y)), delimiter=",", header=header, comments="")
    loaded = np.loadtxt(CSV_PATH, delimiter=",", skiprows=1)
    return loaded[:, :-1], loaded[:, -1].astype(int)


def run() -> dict[str, Any]:
    X, y = create_and_load_csv()
    X_development, X_test, y_development, y_test = train_test_split(
        X, y, test_size=0.25, stratify=y, random_state=SEED
    )
    X_train, X_validation, y_train, y_validation = train_test_split(
        X_development,
        y_development,
        test_size=0.25,
        stratify=y_development,
        random_state=SEED,
    )

    validation = labelled_collection(X_validation, y_validation)
    test = labelled_collection(X_test, y_test)
    qp.environ["SAMPLE_SIZE"] = 100
    qp.environ["N_JOBS"] = 1

    results: dict[str, dict[str, Any]] = {}
    for name, quantifier in {
        "acc": ACC(LogisticRegression(max_iter=2_000, random_state=SEED)),
        "pacc": PACC(LogisticRegression(max_iter=2_000, random_state=SEED)),
    }.items():
        search = qp.model_selection.GridSearchQ(
            model=quantifier,
            param_grid={"classifier__C": [0.1, 1.0, 10.0]},
            protocol=APP(validation, n_prevalences=5, repeats=1, random_state=SEED),
            error="mae",
            refit=True,
            n_jobs=1,
        ).fit(X_train, y_train)
        report = qp.evaluation.evaluation_report(
            search.best_model_,
            protocol=APP(test, n_prevalences=5, repeats=2, random_state=SEED),
            error_metrics=["mae", "mrae"],
        )
        results[name] = {
            "best_parameters": search.best_params_,
            "mean_mae": float(report["mae"].mean()),
            "mean_mrae": float(report["mrae"].mean()),
            "samples": int(len(report)),
        }
    return {
        "csv": {
            "path": str(CSV_PATH.relative_to(ROOT)),
            "rows": int(len(y)),
            "features": int(X.shape[1]),
        },
        "methods": results,
        "seed": SEED,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))

