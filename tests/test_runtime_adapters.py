"""Focused execution coverage using a small deterministic QuaPy stand-in."""

import json
import math
import pickle
import sys
from io import BytesIO
from types import ModuleType

import pytest

from quapylab.app import create_app
from quapylab.domain.actor import desktop_actor
from quapylab.execution.local_worker import LocalWorker
from quapylab.execution.runtime_adapters import (
    RuntimeNodeExecutor,
    _classifier_from_spec,
    _classifier_spec,
    _comparison_result,
    _model_selection_config,
    _protocol_config,
    _quantifier_options,
    _tabular_transformer,
)
from quapylab.persistence.models import Artifact, Dataset, DatasetVersion, Project


class FixtureSplit:
    classes_ = ["negative", "positive"]
    instances = [0, 1]

    @staticmethod
    def prevalence() -> list[float]:
        return [0.5, 0.5]


class FixtureDataset:
    training = FixtureSplit()
    test = FixtureSplit()


class FixtureACC:
    def fit(self, _: FixtureSplit) -> "FixtureACC":
        return self

    def quantify(self, _: list[int]) -> list[float]:
        return [0.5, 0.5]


def test_tabular_preprocessing_fits_imputation_and_scaling_on_training_only() -> None:
    import numpy as np

    transformer, resolved = _tabular_transformer({"imputation": "mean", "scaling": "standard"})
    training = np.asarray([[0.0, np.nan], [2.0, 4.0]])
    evaluation = np.asarray([[100.0, np.nan]])

    transformed_training = transformer.fit_transform(training)
    transformed_evaluation = transformer.transform(evaluation)

    assert resolved == {"imputation": "mean", "scaling": "standard"}
    assert transformer.named_steps["imputer"].statistics_.tolist() == [1.0, 4.0]
    assert transformer.named_steps["scaler"].mean_.tolist() == [1.0, 4.0]
    assert transformed_training[:, 0].tolist() == [-1.0, 1.0]
    assert transformed_evaluation[0, 0] == 99.0
    assert transformed_evaluation[0, 1] == 0.0


def test_classifier_allowlist_resolves_deterministic_probabilistic_estimators() -> None:
    logistic_spec = _classifier_spec(
        {"kind": "logistic_regression", "max_iter": 321, "c": 0.25}, 17
    )
    logistic = _classifier_from_spec(logistic_spec)
    assert logistic.max_iter == 321
    assert logistic.C == 0.25
    assert logistic.random_state == 17

    forest_spec = _classifier_spec(
        {"kind": "random_forest", "n_estimators": 13, "max_depth": 4}, 23
    )
    forest = _classifier_from_spec(forest_spec)
    assert forest.n_estimators == 13
    assert forest.max_depth == 4
    assert forest.random_state == 23
    assert forest.n_jobs == 1
    assert callable(forest.predict_proba)


@pytest.mark.parametrize(
    "config",
    [
        {"kind": "arbitrary.import.Classifier"},
        {"kind": "logistic_regression", "c": 0},
        {"kind": "random_forest", "n_estimators": 0},
        {"kind": "random_forest", "max_depth": 0},
        {"kind": "logistic_regression", "unexpected": True},
    ],
)
def test_classifier_allowlist_rejects_unsupported_or_invalid_config(
    config: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _classifier_spec(config, 0)


def test_comparison_ranks_methods_and_preserves_paired_samples() -> None:
    def evaluation(quantifier: str, mae: float) -> dict[str, object]:
        return {
            "schema_version": 2,
            "protocol": "app",
            "quantifier": quantifier,
            "classes": ["negative", "positive"],
            "metrics": {"mae": mae, "mse": mae**2},
            "samples": [
                {
                    "sample_id": "sample-000000",
                    "true_prevalence": [0.25, 0.75],
                    "estimated_prevalence": [0.25 + mae, 0.75 - mae],
                    "metrics": {"ae": mae, "se": mae**2},
                }
            ],
        }

    comparison = _comparison_result(
        [("acc-evaluation", evaluation("acc", 0.2)), ("pacc-evaluation", evaluation("pacc", 0.1))]
    )

    assert comparison["paired_sample_ids"] == ["sample-000000"]
    assert comparison["rankings"]["mae"] == [
        {"rank": 1, "method_id": "pacc-evaluation", "value": 0.1},
        {"rank": 2, "method_id": "acc-evaluation", "value": 0.2},
    ]


def test_comparison_rejects_unpaired_ground_truth() -> None:
    left = {
        "schema_version": 2,
        "protocol": "app",
        "quantifier": "acc",
        "classes": ["negative", "positive"],
        "metrics": {"mae": 0.1},
        "samples": [
            {
                "sample_id": "sample-000000",
                "true_prevalence": [0.5, 0.5],
                "estimated_prevalence": [0.4, 0.6],
                "metrics": {"ae": 0.1},
            }
        ],
    }
    right = json.loads(json.dumps(left))
    right["samples"][0]["true_prevalence"] = [0.75, 0.25]

    with pytest.raises(ValueError, match="paired sample IDs"):
        _comparison_result([("left", left), ("right", right)])


def test_model_selection_grid_is_allowlisted_for_the_classifier() -> None:
    logistic = _model_selection_config(
        {"classifier_values": [0.1, 1], "error": "mae"},
        {"kind": "logistic_regression"},
    )
    assert logistic["parameter_name"] == "classifier__C"
    assert logistic["classifier_values"] == [0.1, 1.0]

    forest = _model_selection_config({"classifier_values": [10, 25]}, {"kind": "random_forest"})
    assert forest["parameter_name"] == "classifier__n_estimators"

    with pytest.raises(ValueError, match="positive integers"):
        _model_selection_config({"classifier_values": [10.5]}, {"kind": "random_forest"})
    with pytest.raises(ValueError, match="one and fifty"):
        _model_selection_config({"classifier_values": []}, {"kind": "logistic_regression"})


def test_quapy_node_options_resolve_native_defaults_and_constraints() -> None:
    assert _quantifier_options("acc", {}) == {
        "fit_classifier": True,
        "n_jobs": 1,
        "val_split": 5,
        "solver": "minimize",
        "method": "inversion",
        "norm": "clip",
    }
    assert _quantifier_options("emq", {"calib": "bcts", "on_calib_error": "backup"}) == {
        "fit_classifier": True,
        "n_jobs": 1,
        "val_split": None,
        "exact_train_prev": True,
        "calib": "bcts",
        "on_calib_error": "backup",
    }
    assert (
        _quantifier_options(
            "dmy",
            {"nbins": 16, "divergence": "topsoe", "cdf": True, "search": "linear_search"},
        )["nbins"]
        == 16
    )
    with pytest.raises(ValueError, match="val_split"):
        _quantifier_options("hdy", {"val_split": 1})

    assert _protocol_config("app", {}, 19) == {
        "sample_size": None,
        "repeats": 10,
        "seed": 19,
        "return_type": "sample_prev",
        "n_prevalences": 21,
        "smooth_limits_epsilon": 0.0,
        "sanity_check": 10000,
    }
    assert _protocol_config("npp", {}, 23) == {
        "sample_size": None,
        "repeats": 100,
        "seed": 23,
        "return_type": "sample_prev",
    }


def _install_quapy(monkeypatch: object) -> None:
    quapy = ModuleType("quapy")
    error = ModuleType("quapy.error")
    method = ModuleType("quapy.method")
    aggregative = ModuleType("quapy.method.aggregative")
    aggregative.ACC = FixtureACC  # type: ignore[attr-defined]
    for name in ("ae", "rae", "se", "kld", "nkld", "nae", "nrae"):
        setattr(error, name, lambda _actual, _estimated, **_kwargs: 0.0)
    monkeypatch.setitem(sys.modules, "quapy", quapy)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quapy.error", error)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quapy.method", method)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "quapy.method.aggregative", aggregative)  # type: ignore[attr-defined]


def _prepared_run(config: dict[str, object]) -> tuple[object, object, str]:
    application = create_app({"TESTING": True})
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project_id = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "Experiment"}
    ).get_json()["id"]
    database = application.extensions["quapylab.database"]
    artifacts = application.extensions["quapylab.artifacts"]
    with database.transaction() as session:
        project = session.get(Project, project_id)
        assert project is not None
        dataset = Dataset(project_id=project.id, name="fixture", is_labelled=True)
        session.add(dataset)
        session.flush()
        materialization = artifacts.create(
            session,
            group_id=project.group_id,
            kind="quapy-materialization",
            media_type="application/vnd.quapylab.quapy-materialization+pickle",
            content=pickle.dumps(FixtureDataset()),
            serialization={"format": "pickle", "trusted_producer": "quapy-loader"},
        )
        version = DatasetVersion(
            dataset_id=dataset.id,
            schema={},
            row_count=4,
            classes=["negative", "positive"],
            source={"artifact_id": materialization.id},
            artifact_hashes=[materialization.sha256],
            ingestion_report={},
        )
        session.add(version)
        session.flush()
        dataset.current_version_id = version.id
        version_id = version.id
    graph = {
        "schema_version": 1,
        "nodes": [
            {"id": "dataset", "type": "dataset", "version": 1, "config": config},
            {"id": "split", "type": "split", "version": 1, "config": {}},
            {"id": "classifier", "type": "logistic_regression", "version": 1, "config": {}},
            {"id": "acc", "type": "acc", "version": 1, "config": {}},
            {"id": "fit", "type": "fit_quantifier", "version": 1, "config": {}},
            {"id": "evaluation", "type": "evaluation", "version": 1, "config": {}},
            {"id": "report", "type": "report", "version": 1, "config": {}},
        ],
        "edges": [
            {
                "id": "dataset-split",
                "source": "dataset",
                "source_port": "dataset",
                "target": "split",
                "target_port": "dataset",
            },
            {
                "id": "split-acc",
                "source": "split",
                "source_port": "train",
                "target": "fit",
                "target_port": "train",
            },
            {
                "id": "classifier-acc",
                "source": "classifier",
                "source_port": "classifier",
                "target": "acc",
                "target_port": "classifier",
            },
            {
                "id": "acc-fit",
                "source": "acc",
                "source_port": "quantifier",
                "target": "fit",
                "target_port": "quantifier",
            },
            {
                "id": "two",
                "source": "fit",
                "source_port": "quantifier",
                "target": "evaluation",
                "target_port": "quantifier",
            },
            {
                "id": "split-evaluation",
                "source": "split",
                "source_port": "test",
                "target": "evaluation",
                "target_port": "test",
            },
            {
                "id": "evaluation-report",
                "source": "evaluation",
                "source_port": "report",
                "target": "report",
                "target_port": "evaluation",
            },
        ],
    }
    if config == {"dataset_version_id": "VERSION"}:
        graph["nodes"][0]["config"] = {"dataset_version_id": version_id}
    workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows", json={"name": "Fixture", "graph": graph}
    ).get_json()
    run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 1},
    ).get_json()
    return application, client, run["id"]


def test_runtime_executes_and_persists_a_provenanced_evaluation(monkeypatch: object) -> None:
    _install_quapy(monkeypatch)
    application, client, run_id = _prepared_run({"dataset_version_id": "VERSION"})
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(application.extensions["quapylab.artifacts"]),
    )

    assert worker.run_once()
    assert client.get(f"/api/v1/runs/{run_id}").get_json()["status"] == "succeeded"
    nodes = client.get(f"/api/v1/runs/{run_id}/nodes").get_json()["items"]
    evaluation = next(node for node in nodes if node["node_type"] == "evaluation")
    artifact = client.get(f"/api/v1/artifacts/{evaluation['output_artifact_ids'][0]}").get_json()
    assert artifact["producing_run_id"] == run_id
    assert artifact["producing_node_run_id"] == evaluation["id"]
    download = client.get(f"/api/v1/artifacts/{artifact['id']}/download")
    assert json.loads(download.data)["value"] == 0.0
    csv_export = client.get(f"/api/v1/artifacts/{artifact['id']}/evaluation.csv")
    assert csv_export.status_code == 200
    assert csv_export.data.startswith(
        b"sample_id,true_negative,true_positive,estimated_negative,estimated_positive,"
    )
    assert b",ae," in csv_export.data.splitlines()[0] + b","
    report = next(node for node in nodes if node["node_type"] == "report")
    rendered = client.get(f"/api/v1/artifacts/{report['output_artifact_ids'][0]}/download")
    assert rendered.content_type.startswith("text/html")
    assert b"Evaluation report" in rendered.data
    assert b"Node timings" in rendered.data
    assert b"Reproducibility" in rendered.data


def test_runtime_rejects_missing_dataset_configuration() -> None:
    application, client, run_id = _prepared_run({})
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(application.extensions["quapylab.artifacts"]),
    )

    assert worker.run_once()
    run = client.get(f"/api/v1/runs/{run_id}").get_json()
    assert run["status"] == "failed"
    assert "dataset_version_id" in run["error_summary"]


def test_runtime_verifies_materialization_integrity_before_unpickling() -> None:
    application, client, run_id = _prepared_run({"dataset_version_id": "VERSION"})
    database = application.extensions["quapylab.database"]
    artifacts = application.extensions["quapylab.artifacts"]
    with database.transaction() as session:
        version = session.query(DatasetVersion).one()
        artifact_id = version.source["artifact_id"]
    # The object-store corruption is deliberately outside the database transaction.
    # Look up the row through the public model instead of trusting a serialized input.
    with database.transaction() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None
        (artifacts._store.root / artifact.storage_key).write_bytes(b"corrupt")
    worker = LocalWorker(
        database,
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(artifacts),
    )

    assert worker.run_once()
    run = client.get(f"/api/v1/runs/{run_id}").get_json()
    assert run["status"] == "failed"
    assert "integrity" in run["error_summary"]


def test_runtime_materializes_a_labelled_csv_and_executes_a_split() -> None:
    application = create_app({"TESTING": True})
    client = application.test_client()
    group_id = str(desktop_actor().group_id)
    project_id = client.post(
        f"/api/v1/groups/{group_id}/projects", json={"name": "CSV experiment"}
    ).get_json()["id"]
    rows = "\n".join(
        [f"{index},negative" for index in range(12)]
        + [f"{index + 100},positive" for index in range(12)]
    )
    dataset = client.post(
        f"/api/v1/projects/{project_id}/datasets",
        data={
            "name": "Numeric training data",
            "file": (BytesIO(f"feature,label\n{rows}\n".encode()), "training.csv"),
        },
    ).get_json()
    graph = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "dataset",
                "type": "dataset",
                "version": 1,
                "config": {"dataset_version_id": dataset["id"], "label_column": "label"},
            },
            {
                "id": "split",
                "type": "split",
                "version": 1,
                "config": {"train_proportion": 0.7},
            },
            {"id": "classifier", "type": "logistic_regression", "version": 1, "config": {}},
            {"id": "acc", "type": "acc", "version": 1, "config": {}},
            {"id": "fit", "type": "fit_quantifier", "version": 1, "config": {}},
            {"id": "evaluation", "type": "evaluation", "version": 1, "config": {}},
        ],
        "edges": [
            {
                "id": "dataset-split",
                "source": "dataset",
                "source_port": "dataset",
                "target": "split",
                "target_port": "dataset",
            },
            {
                "id": "split-acc",
                "source": "split",
                "source_port": "train",
                "target": "fit",
                "target_port": "train",
            },
            {
                "id": "classifier-acc",
                "source": "classifier",
                "source_port": "classifier",
                "target": "acc",
                "target_port": "classifier",
            },
            {
                "id": "acc-fit",
                "source": "acc",
                "source_port": "quantifier",
                "target": "fit",
                "target_port": "quantifier",
            },
            {
                "id": "acc-evaluation",
                "source": "fit",
                "source_port": "quantifier",
                "target": "evaluation",
                "target_port": "quantifier",
            },
            {
                "id": "split-evaluation",
                "source": "split",
                "source_port": "test",
                "target": "evaluation",
                "target_port": "test",
            },
        ],
    }
    workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows", json={"name": "CSV ACC", "graph": graph}
    ).get_json()
    run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": workflow["draft_revision_id"], "seed": 7},
    ).get_json()
    worker = LocalWorker(
        application.extensions["quapylab.database"],
        application.extensions["quapylab.runs"],
        RuntimeNodeExecutor(application.extensions["quapylab.artifacts"]),
    )

    assert worker.run_once()
    result = client.get(f"/api/v1/runs/{run['id']}").get_json()
    assert result["status"] == "succeeded", result
    assert result["environment"]["dataset_version_bindings"] == {"dataset": dataset["id"]}
    assert result["environment"]["application"]["python"]
    assert result["environment"]["application"]["packages"]["quapy"]
    assert result["environment"]["execution"]["wall_time_seconds"] >= 0

    nodes = client.get(f"/api/v1/runs/{run['id']}/nodes").get_json()["items"]
    split = next(node for node in nodes if node["node_type"] == "split")
    assert split["wall_time_seconds"] >= 0
    manifests = [
        json.loads(client.get(f"/api/v1/artifacts/{artifact_id}/download").data)
        for artifact_id in split["output_artifact_ids"]
    ]
    assert {manifest["partition"] for manifest in manifests} == {"train", "test"}
    for manifest in manifests:
        assert manifest["schema_version"] == 1
        assert manifest["source_dataset_version_id"] == dataset["id"]
        assert manifest["seed"] == 7
        assert manifest["strategy"] == "stratified"
        assert set(manifest["train_indices"]).isdisjoint(manifest["test_indices"])
        assert len(manifest["train_indices"]) + len(manifest["test_indices"]) == 24
        assert manifest["class_distribution"]["train"] == {
            "negative": 8,
            "positive": 8,
        }

    # Golden check: execute the equivalent QuaPy pipeline directly with the exact
    # persisted indices and compare its MAE with the Lab artifact.
    import numpy as np
    from quapy.data.base import LabelledCollection
    from quapy.method.aggregative import ACC
    from sklearn.linear_model import LogisticRegression

    full = LabelledCollection(
        np.asarray(
            [[float(index)] for index in range(12)] + [[float(index + 100)] for index in range(12)]
        ),
        np.asarray(["negative"] * 12 + ["positive"] * 12),
        classes=["negative", "positive"],
    )
    split_manifest = manifests[0]
    direct_train = full.sampling_from_index(split_manifest["train_indices"])
    direct_test = full.sampling_from_index(split_manifest["test_indices"])
    direct = ACC(LogisticRegression(max_iter=1000, random_state=7))
    direct.fit(direct_train.instances, direct_train.labels)
    direct_estimate = direct.quantify(direct_test.instances)
    direct_mae = sum(
        abs(left - right)
        for left, right in zip(direct_estimate, direct_test.prevalence(), strict=True)
    ) / len(direct_estimate)
    evaluation_node = next(node for node in nodes if node["node_type"] == "evaluation")
    lab_evaluation = json.loads(
        client.get(f"/api/v1/artifacts/{evaluation_node['output_artifact_ids'][0]}/download").data
    )
    assert math.isclose(lab_evaluation["value"], direct_mae, rel_tol=1e-12, abs_tol=1e-12)

    graph["nodes"].append(
        {
            "id": "preprocessing",
            "type": "preprocessing",
            "version": 1,
            "config": {"imputation": "none", "scaling": "standard"},
        }
    )
    graph["edges"] = [
        edge for edge in graph["edges"] if edge["id"] not in {"split-acc", "split-evaluation"}
    ] + [
        {
            "id": "split-preprocessing-train",
            "source": "split",
            "source_port": "train",
            "target": "preprocessing",
            "target_port": "train",
        },
        {
            "id": "split-preprocessing-test",
            "source": "split",
            "source_port": "test",
            "target": "preprocessing",
            "target_port": "test",
        },
        {
            "id": "preprocessing-acc",
            "source": "preprocessing",
            "source_port": "train",
            "target": "fit",
            "target_port": "train",
        },
        {
            "id": "preprocessing-evaluation",
            "source": "preprocessing",
            "source_port": "test",
            "target": "evaluation",
            "target_port": "test",
        },
    ]
    classifier_node = next(node for node in graph["nodes"] if node["id"] == "classifier")
    classifier_node["type"] = "random_forest"
    classifier_node["config"] = {
        "n_estimators": 11,
        "max_depth": 3,
    }
    preprocessing_workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows",
        json={"name": "Train-only preprocessing", "graph": graph},
    ).get_json()
    preprocessing_run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": preprocessing_workflow["draft_revision_id"], "seed": 7},
    ).get_json()
    assert worker.run_once()
    preprocessing_result = client.get(f"/api/v1/runs/{preprocessing_run['id']}").get_json()
    assert preprocessing_result["status"] == "succeeded", preprocessing_result
    preprocessing_nodes = client.get(f"/api/v1/runs/{preprocessing_run['id']}/nodes").get_json()[
        "items"
    ]
    preprocessing = next(
        node for node in preprocessing_nodes if node["node_type"] == "preprocessing"
    )
    classifier_result = next(
        node for node in preprocessing_nodes if node["node_type"] == "random_forest"
    )
    classifier_spec = json.loads(
        client.get(f"/api/v1/artifacts/{classifier_result['output_artifact_ids'][0]}/download").data
    )
    assert classifier_spec == {
        "kind": "random_forest",
        "max_depth": 3,
        "n_estimators": 11,
        "seed": 7,
    }
    transformer_id = preprocessing["output_artifact_ids"][2]
    transformer_metadata = client.get(f"/api/v1/artifacts/{transformer_id}").get_json()
    assert transformer_metadata["serialization"]["fit_scope"] == "train_only"
    fitted_transformer = pickle.loads(
        client.get(f"/api/v1/artifacts/{transformer_id}/download").data
    )
    training_mean = float(full.instances[split_manifest["train_indices"]].mean(axis=0)[0])
    assert fitted_transformer.named_steps["scaler"].mean_[0] == training_mean
    full_mean = float(full.instances.mean(axis=0)[0])
    assert fitted_transformer.named_steps["scaler"].mean_[0] != full_mean

    graph["nodes"] = [node for node in graph["nodes"] if node["type"] != "evaluation"] + [
        {
            "id": "app",
            "type": "app",
            "version": 1,
            "config": {"sample_size": 4, "n_prevalences": 3, "repeats": 1},
        },
        {"id": "protocol-evaluation", "type": "protocol_evaluation", "version": 1, "config": {}},
    ]
    graph["edges"] = [edge for edge in graph["edges"] if edge["target"] != "evaluation"] + [
        {
            "id": "split-app",
            "source": "split",
            "source_port": "test",
            "target": "app",
            "target_port": "collection",
        },
        {
            "id": "app-evaluation",
            "source": "app",
            "source_port": "protocol",
            "target": "protocol-evaluation",
            "target_port": "protocol",
        },
        {
            "id": "acc-protocol-evaluation",
            "source": "fit",
            "source_port": "quantifier",
            "target": "protocol-evaluation",
            "target_port": "quantifier",
        },
    ]
    protocol_workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows",
        json={"name": "CSV ACC protocol", "graph": graph},
    ).get_json()
    protocol_run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": protocol_workflow["draft_revision_id"], "seed": 7},
    ).get_json()

    assert worker.run_once()
    protocol_result = client.get(f"/api/v1/runs/{protocol_run['id']}").get_json()
    assert protocol_result["status"] == "succeeded", protocol_result
    protocol_nodes = client.get(f"/api/v1/runs/{protocol_run['id']}/nodes").get_json()["items"]
    evaluation = next(node for node in protocol_nodes if node["node_type"] == "protocol_evaluation")
    normalized = json.loads(
        client.get(f"/api/v1/artifacts/{evaluation['output_artifact_ids'][0]}/download").data
    )
    assert normalized["schema_version"] == 2
    assert normalized["protocol"] == "app"
    assert len(normalized["samples"]) == 3
    assert normalized["samples"][0]["sample_id"] == "sample-000000"
    assert set(normalized["samples"][0]) == {
        "sample_id",
        "true_prevalence",
        "estimated_prevalence",
        "metrics",
    }

    quantifier_node = next(node for node in graph["nodes"] if node["id"] == "acc")
    for method in ("pcc", "pacc"):
        quantifier_node["type"] = method
        method_workflow = client.post(
            f"/api/v1/projects/{project_id}/workflows",
            json={"name": f"CSV {method.upper()} protocol", "graph": graph},
        ).get_json()
        method_run = client.post(
            f"/api/v1/projects/{project_id}/runs",
            json={"workflow_revision_id": method_workflow["draft_revision_id"], "seed": 7},
        ).get_json()
        assert worker.run_once()
        method_result = client.get(f"/api/v1/runs/{method_run['id']}").get_json()
        assert method_result["status"] == "succeeded", method_result

    quantifier_node["type"] = "acc"
    graph["nodes"].extend(
        [
            {"id": "pacc", "type": "pacc", "version": 1, "config": {}},
            {"id": "fit-pacc", "type": "fit_quantifier", "version": 1, "config": {}},
            {
                "id": "pacc-protocol-evaluation",
                "type": "protocol_evaluation",
                "version": 1,
                "config": {},
            },
            {"id": "comparison", "type": "comparison", "version": 1, "config": {}},
        ]
    )
    graph["edges"].extend(
        [
            {
                "id": "preprocessing-fit-pacc",
                "source": "preprocessing",
                "source_port": "train",
                "target": "fit-pacc",
                "target_port": "train",
            },
            {
                "id": "classifier-pacc",
                "source": "classifier",
                "source_port": "classifier",
                "target": "pacc",
                "target_port": "classifier",
            },
            {
                "id": "pacc-fit-pacc",
                "source": "pacc",
                "source_port": "quantifier",
                "target": "fit-pacc",
                "target_port": "quantifier",
            },
            {
                "id": "pacc-protocol",
                "source": "fit-pacc",
                "source_port": "quantifier",
                "target": "pacc-protocol-evaluation",
                "target_port": "quantifier",
            },
            {
                "id": "app-pacc-evaluation",
                "source": "app",
                "source_port": "protocol",
                "target": "pacc-protocol-evaluation",
                "target_port": "protocol",
            },
            {
                "id": "acc-comparison",
                "source": "protocol-evaluation",
                "source_port": "report",
                "target": "comparison",
                "target_port": "evaluation_1",
            },
            {
                "id": "pacc-comparison",
                "source": "pacc-protocol-evaluation",
                "source_port": "report",
                "target": "comparison",
                "target_port": "evaluation_2",
            },
        ]
    )
    comparison_workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows",
        json={"name": "Paired ACC and PACC comparison", "graph": graph},
    ).get_json()
    comparison_run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": comparison_workflow["draft_revision_id"], "seed": 7},
    ).get_json()
    assert worker.run_once()
    comparison_run_result = client.get(f"/api/v1/runs/{comparison_run['id']}").get_json()
    assert comparison_run_result["status"] == "succeeded", comparison_run_result
    comparison_nodes = client.get(f"/api/v1/runs/{comparison_run['id']}/nodes").get_json()["items"]
    comparison_node = next(node for node in comparison_nodes if node["node_type"] == "comparison")
    comparison_json_id, comparison_html_id = comparison_node["output_artifact_ids"]
    comparison = json.loads(client.get(f"/api/v1/artifacts/{comparison_json_id}/download").data)
    assert comparison["protocol"] == "app"
    assert comparison["paired_sample_ids"] == [
        "sample-000000",
        "sample-000001",
        "sample-000002",
    ]
    assert {method["quantifier"] for method in comparison["methods"]} == {"acc", "pacc"}
    assert [item["rank"] for item in comparison["rankings"]["mae"]] == [1, 2]
    comparison_csv = client.get(f"/api/v1/artifacts/{comparison_json_id}/evaluation.csv")
    assert comparison_csv.status_code == 200
    assert b"method_id,quantifier,sample_id" in comparison_csv.data
    rendered_comparison = client.get(f"/api/v1/artifacts/{comparison_html_id}/download")
    assert b"Method ranking" in rendered_comparison.data
    assert b"Binary diagnostics" in rendered_comparison.data
    assert b"<svg" in rendered_comparison.data

    selection_graph = {
        "schema_version": 1,
        "nodes": [
            graph["nodes"][0],
            {
                "id": "selection-split",
                "type": "split",
                "version": 1,
                "config": {"train_proportion": 0.6, "validation_proportion": 0.2},
            },
            {
                "id": "selection-classifier",
                "type": "logistic_regression",
                "version": 1,
                "config": {"max_iter": 1000},
            },
            {
                "id": "validation-app",
                "type": "app",
                "version": 1,
                "config": {"sample_size": 4, "n_prevalences": 3, "repeats": 1},
            },
            {
                "id": "acc-specification",
                "type": "acc",
                "version": 1,
                "config": {},
            },
            {
                "id": "selection",
                "type": "model_selection",
                "version": 1,
                "config": {
                    "classifier_values": [0.1, 1.0],
                    "error": "mae",
                    "refit": True,
                },
            },
            {"id": "direct-spec-fit", "type": "fit_quantifier", "version": 1, "config": {}},
            {"id": "held-out-evaluation", "type": "evaluation", "version": 1, "config": {}},
        ],
        "edges": [
            {
                "id": "dataset-selection-split",
                "source": "dataset",
                "source_port": "dataset",
                "target": "selection-split",
                "target_port": "dataset",
            },
            {
                "id": "validation-app-source",
                "source": "selection-split",
                "source_port": "validation",
                "target": "validation-app",
                "target_port": "collection",
            },
            {
                "id": "selection-training",
                "source": "selection-split",
                "source_port": "train",
                "target": "selection",
                "target_port": "train",
            },
            {
                "id": "selection-classifier-spec",
                "source": "selection-classifier",
                "source_port": "classifier",
                "target": "acc-specification",
                "target_port": "classifier",
            },
            {
                "id": "selection-quantifier-spec",
                "source": "acc-specification",
                "source_port": "quantifier",
                "target": "selection",
                "target_port": "quantifier",
            },
            {
                "id": "selection-validation-protocol",
                "source": "validation-app",
                "source_port": "protocol",
                "target": "selection",
                "target_port": "validation_protocol",
            },
            {
                "id": "direct-spec-fit-training",
                "source": "selection-split",
                "source_port": "train",
                "target": "direct-spec-fit",
                "target_port": "train",
            },
            {
                "id": "direct-spec-fit-quantifier",
                "source": "acc-specification",
                "source_port": "quantifier",
                "target": "direct-spec-fit",
                "target_port": "quantifier",
            },
            {
                "id": "selected-quantifier-evaluation",
                "source": "selection",
                "source_port": "quantifier",
                "target": "held-out-evaluation",
                "target_port": "quantifier",
            },
            {
                "id": "held-out-test",
                "source": "selection-split",
                "source_port": "test",
                "target": "held-out-evaluation",
                "target_port": "test",
            },
        ],
    }
    selection_workflow = client.post(
        f"/api/v1/projects/{project_id}/workflows",
        json={"name": "Leakage-safe ACC selection", "graph": selection_graph},
    ).get_json()
    selection_run = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"workflow_revision_id": selection_workflow["draft_revision_id"], "seed": 7},
    ).get_json()
    assert worker.run_once()
    selection_run_result = client.get(f"/api/v1/runs/{selection_run['id']}").get_json()
    assert selection_run_result["status"] == "succeeded", selection_run_result
    selection_nodes = client.get(f"/api/v1/runs/{selection_run['id']}/nodes").get_json()["items"]
    selection_node = next(
        node for node in selection_nodes if node["node_type"] == "model_selection"
    )
    selection_split = next(node for node in selection_nodes if node["node_id"] == "selection-split")
    selection_manifests = [
        json.loads(client.get(f"/api/v1/artifacts/{artifact_id}/download").data)
        for artifact_id in selection_split["output_artifact_ids"]
    ]
    assert [manifest["partition"] for manifest in selection_manifests] == [
        "train",
        "test",
        "validation",
    ]
    train_indices = set(selection_manifests[0]["train_indices"])
    test_indices = set(selection_manifests[0]["test_indices"])
    validation_indices = set(selection_manifests[0]["validation_indices"])
    assert train_indices.isdisjoint(test_indices)
    assert train_indices.isdisjoint(validation_indices)
    assert test_indices.isdisjoint(validation_indices)
    assert train_indices | validation_indices | test_indices == set(range(24))
    evidence = json.loads(
        client.get(f"/api/v1/artifacts/{selection_node['output_artifact_ids'][1]}/download").data
    )
    assert evidence["parameter_grid"] == {"classifier__C": [0.1, 1.0]}
    assert evidence["best_params"]["classifier__C"] in [0.1, 1.0]
    assert evidence["n_jobs"] == 1
    held_out = next(node for node in selection_nodes if node["node_id"] == "held-out-evaluation")
    assert held_out["state"] == "succeeded"
    fitted = next(node for node in selection_nodes if node["node_id"] == "direct-spec-fit")
    assert fitted["state"] == "succeeded"
