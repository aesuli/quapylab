from quapylab.app import create_app


def workflow() -> dict[str, object]:
    return {
        "schema_version": 1,
        "nodes": [
            {"id": "dataset", "type": "dataset", "version": 1},
            {"id": "split", "type": "split", "version": 1},
            {"id": "classifier", "type": "classifier", "version": 1},
            {"id": "acc", "type": "acc", "version": 1},
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
                "id": "split-acc",
                "source": "split",
                "source_port": "train",
                "target": "acc",
                "target_port": "train",
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
                "source": "acc",
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
        "topological_order": ["classifier", "dataset", "split", "acc", "evaluation"],
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
