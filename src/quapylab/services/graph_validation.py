"""Authoritative graph validation and deterministic topological compilation."""

from dataclasses import dataclass

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


def validate_workflow(workflow: WorkflowSpec, registry: NodeRegistry) -> GraphCompilation:
    node_map = {node.id: node for node in workflow.nodes}
    if len(node_map) != len(workflow.nodes):
        raise GraphValidationError("duplicate_node_id", "Workflow node IDs must be unique.", [])
    definitions = {
        node.id: _definition(registry, node.type, node.version, node.id) for node in workflow.nodes
    }
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
