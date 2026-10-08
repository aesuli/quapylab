"""Authoritative graph validation and deterministic topological compilation."""

from dataclasses import dataclass
from typing import Any

from quapylab.nodes.registry import NodeDefinition, NodeRegistry, PortDefinition
from quapylab.workflow.schemas import WorkflowSpec


@dataclass(frozen=True, slots=True)
class GraphCompilation:
    topological_order: list[str]


class GraphValidationError(ValueError):
    def __init__(self, code: str, message: str, details: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _port(ports: tuple[PortDefinition, ...], name: str) -> PortDefinition | None:
    return next((port for port in ports if port.name == name), None)


def _definition(
    registry: NodeRegistry, node_type: str, version: int, node_id: str
) -> NodeDefinition:
    definition = registry.get(node_type, version)
    if definition is None:
        raise GraphValidationError(
            "unknown_node_type",
            "Workflow contains an unknown node type.",
            [{"node_id": node_id, "type": node_type}],
        )
    return definition


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "array": lambda: isinstance(value, list),
        "object": lambda: isinstance(value, dict),
        "null": lambda: value is None,
    }.get(expected, lambda: True)()


def _validate_node_config(node_id: str, config: dict[str, Any], definition: NodeDefinition) -> None:
    schema = definition.config_schema or {"type": "object", "additionalProperties": False}
    properties = schema.get("properties", {})
    details: list[dict[str, str]] = []
    if schema.get("additionalProperties") is False:
        for name in sorted(set(config) - set(properties)):
            details.append({"node_id": node_id, "field": name, "reason": "unsupported"})
    for name, value in config.items():
        property_schema = properties.get(name)
        if not isinstance(property_schema, dict):
            continue
        expected = property_schema.get("type")
        expected_types = expected if isinstance(expected, list) else [expected]
        if expected and not any(
            isinstance(item, str) and _matches_type(value, item) for item in expected_types
        ):
            details.append({"node_id": node_id, "field": name, "reason": "wrong_type"})
            continue
        if "enum" in property_schema and value not in property_schema["enum"]:
            details.append({"node_id": node_id, "field": name, "reason": "not_allowlisted"})
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in property_schema and value < property_schema["minimum"]:
                details.append({"node_id": node_id, "field": name, "reason": "below_minimum"})
            if "maximum" in property_schema and value > property_schema["maximum"]:
                details.append({"node_id": node_id, "field": name, "reason": "above_maximum"})
            if (
                "exclusiveMinimum" in property_schema
                and value <= property_schema["exclusiveMinimum"]
            ):
                details.append(
                    {"node_id": node_id, "field": name, "reason": "below_exclusive_minimum"}
                )
    if details:
        raise GraphValidationError(
            "invalid_node_config", "Workflow contains invalid node configuration.", details
        )


def validate_workflow(workflow: WorkflowSpec, registry: NodeRegistry) -> GraphCompilation:
    node_map = {node.id: node for node in workflow.nodes}
    if len(node_map) != len(workflow.nodes):
        raise GraphValidationError("duplicate_node_id", "Workflow node IDs must be unique.", [])
    definitions = {
        node.id: _definition(registry, node.type, node.version, node.id) for node in workflow.nodes
    }
    for node in workflow.nodes:
        _validate_node_config(node.id, node.config, definitions[node.id])
    edge_ids = {edge.id for edge in workflow.edges}
    if len(edge_ids) != len(workflow.edges):
        raise GraphValidationError("duplicate_edge_id", "Workflow edge IDs must be unique.", [])

    incoming: dict[str, set[str]] = {node.id: set() for node in workflow.nodes}
    outgoing: dict[str, set[str]] = {node.id: set() for node in workflow.nodes}
    connected_inputs: set[tuple[str, str]] = set()
    for edge in workflow.edges:
        source, target = node_map.get(edge.source), node_map.get(edge.target)
        if source is None or target is None:
            raise GraphValidationError(
                "unknown_edge_endpoint",
                "An edge references an unknown node.",
                [{"edge_id": edge.id}],
            )
        source_port = _port(definitions[source.id].outputs, edge.source_port)
        target_port = _port(definitions[target.id].inputs, edge.target_port)
        if source_port is None or target_port is None:
            raise GraphValidationError(
                "unknown_port", "An edge references an unknown port.", [{"edge_id": edge.id}]
            )
        if source_port.data_type != target_port.data_type:
            raise GraphValidationError(
                "incompatible_ports",
                "Connected ports have incompatible types.",
                [
                    {
                        "edge_id": edge.id,
                        "source_type": source_port.data_type,
                        "target_type": target_port.data_type,
                    }
                ],
            )
        input_key = (target.id, target_port.name)
        if input_key in connected_inputs:
            raise GraphValidationError(
                "multiple_input_edges",
                "An input port can have only one connection.",
                [{"node_id": target.id, "port": target_port.name}],
            )
        connected_inputs.add(input_key)
        incoming[target.id].add(source.id)
        outgoing[source.id].add(target.id)

    missing = [
        {"node_id": node_id, "port": port.name}
        for node_id, definition in definitions.items()
        for port in definition.inputs
        if port.required and (node_id, port.name) not in connected_inputs
    ]
    if missing:
        raise GraphValidationError(
            "missing_required_input", "Workflow has unconnected required inputs.", missing
        )

    ready = sorted(node_id for node_id, dependencies in incoming.items() if not dependencies)
    order: list[str] = []
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for child in sorted(outgoing[node_id]):
            incoming[child].remove(node_id)
            if not incoming[child]:
                ready.append(child)
        ready.sort()
    if len(order) != len(workflow.nodes):
        cyclic = sorted(node_id for node_id, dependencies in incoming.items() if dependencies)
        raise GraphValidationError(
            "cycle_detected",
            "Workflow graph must be acyclic.",
            [{"node_id": node_id} for node_id in cyclic],
        )
    return GraphCompilation(topological_order=order)
