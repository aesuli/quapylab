import pytest

from quapylab.app import create_app


def workflow() -> dict[str, object]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": "dataset",
                "type": "dataset",
                "version": 1,
                "config": {"dataset_version_id": "00000000-0000-0000-0000-000000000001"},
            },
            {"id": "split", "type": "split", "version": 1},
            {"id": "classifier", "type": "logistic_regression", "version": 1},
            {"id": "acc", "type": "acc", "version": 1},
            {"id": "fit", "type": "fit_quantifier", "version": 1},
            {"id": "evaluation", "type": "evaluation", "version": 1},
        ],
        "edges": [
            {
                "id": "classifier-acc",
                "source": "classifier",
                "source_port": "classifier",
                "target": "acc",
                "target_port": "classifier",
            },
            {
                "id": "dataset-split",
                "source": "dataset",
                "source_port": "dataset",
                "target": "split",
                "target_port": "dataset",
            },
            {
                "id": "split-fit",
                "source": "split",
                "source_port": "train",
                "target": "fit",
                "target_port": "train",
            },
            {
                "id": "acc-fit",
                "source": "acc",
                "source_port": "quantifier",
                "target": "fit",
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
                "id": "acc-evaluation",
                "source": "fit",
                "source_port": "quantifier",
                "target": "evaluation",
                "target_port": "quantifier",
            },
        ],
    }


def test_valid_workflow_returns_deterministic_compilation_order() -> None:
    client = create_app({"TESTING": True}).test_client()

    response = client.post("/api/v1/workflows/validate", json=workflow())

    assert response.status_code == 200
    assert response.get_json() == {
        "topological_order": ["classifier", "acc", "dataset", "split", "fit", "evaluation"],
        "valid": True,
    }


def test_incompatible_ports_are_rejected() -> None:
    payload = workflow()
    edges = payload["edges"]
    assert isinstance(edges, list)
    edges[0]["source_port"] = "not-a-port"
    client = create_app({"TESTING": True}).test_client()

    response = client.post("/api/v1/workflows/validate", json=payload)

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "unknown_port"


def test_cycles_are_rejected_before_compilation() -> None:
    payload = workflow()
    nodes = payload["nodes"]
    edges = payload["edges"]
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
    edges.append(
        {
            "id": "evaluation-split",
            "source": "evaluation",
            "source_port": "report",
            "target": "split",
            "target_port": "dataset",
        }
    )
    # The deliberately bad edge must be rejected on type before it can be a cycle.
    client = create_app({"TESTING": True}).test_client()
    response = client.post("/api/v1/workflows/validate", json=payload)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "incompatible_ports"


@pytest.mark.parametrize(
    ("node_id", "config", "field", "reason"),
    [
        ("classifier", {"c": 0}, "c", "below_exclusive_minimum"),
        ("split", {"train_proportion": 1.0}, "train_proportion", "above_maximum"),
        ("classifier", {"unknown": True}, "unknown", "unsupported"),
    ],
)
def test_node_configuration_is_rejected_before_execution(
    node_id: str, config: dict[str, object], field: str, reason: str
) -> None:
    payload = workflow()
    nodes = payload["nodes"]
    assert isinstance(nodes, list)
    next(node for node in nodes if node["id"] == node_id)["config"] = config

    response = (
        create_app({"TESTING": True}).test_client().post("/api/v1/workflows/validate", json=payload)
    )

    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "invalid_node_config"
    assert {"node_id": node_id, "field": field, "reason": reason} in error["details"]
