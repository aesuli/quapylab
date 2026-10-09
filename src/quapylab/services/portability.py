"""Portable, versioned project/workflow interchange and Python export."""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from quapylab.domain.actor import ActorContext
from quapylab.persistence.models import Dataset, Project, Workflow
from quapylab.services.artifacts import ArtifactService
from quapylab.services.datasets import DatasetService
from quapylab.services.projects import ProjectService
from quapylab.services.quapy_export import generate_quapy_script
from quapylab.workflow.schemas import WorkflowSpec

FORMAT = "quapylab-portable"
FORMAT_VERSION = 1


class PortabilityService:
    def __init__(self, artifacts: ArtifactService) -> None:
        self.artifacts = artifacts
        self.projects = ProjectService()
        self.datasets = DatasetService(artifacts)

    def export_workflow(
        self, session: Session, *, actor: ActorContext, workflow_id: str
    ) -> dict[str, Any]:
        workflow = self.projects.get_workflow(session, actor=actor, workflow_id=workflow_id)
        if workflow.draft_revision_id is None:
            raise ValueError("The workflow has no draft revision.")
        revision = self.projects.get_revision(
            session, actor=actor, revision_id=workflow.draft_revision_id
        )
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "kind": "workflow",
            "workflow": {"name": workflow.name, "graph": revision.graph},
            "datasets": self._dataset_descriptors(
                session, actor, revision.graph, include_content=False
            ),
        }

    def export_project(
        self, session: Session, *, actor: ActorContext, project_id: str
    ) -> dict[str, Any]:
        project = self.projects.get_project(session, actor=actor, project_id=project_id)
        workflows = []
        for workflow in self.projects.list_workflows(session, actor=actor, project_id=project.id):
            if workflow.draft_revision_id is None:
                continue
            revision = self.projects.get_revision(
                session, actor=actor, revision_id=workflow.draft_revision_id
            )
            workflows.append({"name": workflow.name, "graph": revision.graph})
        datasets = []
        for dataset in self.datasets.list_datasets(session, actor=actor, project_id=project.id):
            if dataset.current_version_id:
                datasets.append(
                    self._dataset_descriptor(session, actor, dataset.current_version_id, True)
                )
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "kind": "project",
            "project": {
                "name": project.name,
                "description": project.description,
                "settings": project.settings,
            },
            "workflows": workflows,
            "datasets": datasets,
        }

    def import_workflow(
        self, session: Session, *, actor: ActorContext, project_id: str, document: dict[str, Any]
    ) -> Workflow:
        self._check(document, "workflow")
        item = document.get("workflow")
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("graph"), dict)
        ):
            raise ValueError("The workflow export is incomplete.")
        graph = self._remap_graph(
            session, actor, project_id, item["graph"], document.get("datasets", [])
        )
        WorkflowSpec.model_validate(graph)
        return self.projects.create_workflow(
            session, actor=actor, project_id=project_id, name=item["name"], graph=graph
        )

    def import_project(
        self, session: Session, *, actor: ActorContext, group_id: str, document: dict[str, Any]
    ) -> Project:
        self._check(document, "project")
        item = document.get("project")
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("The project export is incomplete.")
        project = self.projects.create_project(
            session,
            actor=actor,
            group_id=group_id,
            name=item["name"],
            description=str(item.get("description", "")),
        )
        session.flush()
        if isinstance(item.get("settings"), dict):
            project.settings = item["settings"]
        id_map: dict[str, str] = {}
        for descriptor in document.get("datasets", []):
            old_id, new_id = self._import_dataset(session, actor, project, descriptor)
            id_map[old_id] = new_id
        for exported in document.get("workflows", []):
            graph = deepcopy(exported.get("graph"))
            if not isinstance(graph, dict) or not isinstance(exported.get("name"), str):
                raise ValueError("The project contains an invalid workflow.")
            self._replace_dataset_ids(graph, id_map)
            WorkflowSpec.model_validate(graph)
            self.projects.create_workflow(
                session, actor=actor, project_id=project.id, name=exported["name"], graph=graph
            )
        return project

    def python_export(
        self, session: Session, *, actor: ActorContext, workflow_id: str, include_datasets: bool
    ) -> tuple[bytes, str, str]:
        manifest = self.export_workflow(session, actor=actor, workflow_id=workflow_id)
        workflow = manifest["workflow"]
        descriptors = self._dataset_descriptors(
            session, actor, workflow["graph"], include_content=include_datasets
        )
        script = _python_script(workflow["name"], workflow["graph"], descriptors)
        stem = _safe_name(workflow["name"])
        if not include_datasets:
            return script.encode(), f"{stem}.py", "text/x-python"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{stem}.py", script)
            archive.writestr("workflow.json", json.dumps(manifest, indent=2))
            for descriptor in descriptors:
                content = descriptor.get("content_base64")
                if content:
                    archive.writestr(descriptor["expected_path"], base64.b64decode(content))
        return output.getvalue(), f"{stem}.zip", "application/zip"

    def quapy_python_export(
        self,
        session: Session,
        *,
        actor: ActorContext,
        workflow_id: str,
        include_datasets: bool,
    ) -> tuple[bytes, str, str]:
        manifest = self.export_workflow(session, actor=actor, workflow_id=workflow_id)
        workflow = manifest["workflow"]
        descriptors = self._dataset_descriptors(
            session, actor, workflow["graph"], include_content=include_datasets
        )
        script = generate_quapy_script(workflow["name"], workflow["graph"], descriptors)
        stem = f"{_safe_name(workflow['name'])}-quapy"
        if not include_datasets:
            return script.encode(), f"{stem}.py", "text/x-python"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{stem}.py", script)
            for descriptor in descriptors:
                content = descriptor.get("content_base64")
                if content:
                    archive.writestr(descriptor["expected_path"], base64.b64decode(content))
        return output.getvalue(), f"{stem}.zip", "application/zip"

    def _dataset_descriptors(
        self, session: Session, actor: ActorContext, graph: dict[str, Any], include_content: bool
    ) -> list[dict[str, Any]]:
        ids = {
            node.get("config", {}).get("dataset_version_id")
            for node in graph.get("nodes", [])
            if node.get("type") == "dataset"
        }
        return [
            self._dataset_descriptor(session, actor, item, include_content)
            for item in sorted(ids)
            if isinstance(item, str)
        ]

    def _dataset_descriptor(
        self, session: Session, actor: ActorContext, version_id: str, include_content: bool
    ) -> dict[str, Any]:
        version = self.datasets.get_version(session, actor=actor, version_id=version_id)
        dataset = session.get(Dataset, version.dataset_id)
        if dataset is None:
            raise ValueError("The dataset referenced by the workflow is unavailable.")
        source = deepcopy(version.source)
        fmt = str(source.get("format", "bin"))
        filename = str(source.get("filename") or f"{_safe_name(dataset.name)}.{fmt}")
        result = {
            "version_id": version.id,
            "name": dataset.name,
            "is_labelled": dataset.is_labelled,
            "schema": version.schema,
            "row_count": version.row_count,
            "classes": version.classes,
            "source": source,
            "label_column": version.ingestion_report.get("label_column"),
            "expected_path": f"datasets/{Path(filename).name}",
        }
        artifact_id = source.get("artifact_id")
        if include_content and isinstance(artifact_id, str):
            _, content = self.artifacts.read(session, actor=actor, artifact_id=artifact_id)
            result["content_base64"] = base64.b64encode(content).decode("ascii")
        return result

    def _import_dataset(
        self, session: Session, actor: ActorContext, project: Project, item: Any
    ) -> tuple[str, str]:
        if not isinstance(item, dict) or not isinstance(item.get("version_id"), str):
            raise ValueError("Project dataset content is missing.")
        source = item.get("source", {})
        if isinstance(source, dict) and source.get("provider") == "quapy":
            version = self.datasets.ingest_quapy(
                session,
                actor=actor,
                project_id=project.id,
                source_id=str(source.get("loader", "")),
                dataset_name=str(source.get("dataset", "")),
                options=source.get("options", {}),
            )
            return item["version_id"], version.id
        if not isinstance(item.get("content_base64"), str):
            raise ValueError("Project dataset content is missing.")
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
        except ValueError as error:
            raise ValueError("Project dataset content is not valid base64.") from error
        filename = str(
            source.get("filename") or Path(str(item.get("expected_path", "dataset.csv"))).name
        )
        if source.get("format") not in {"csv", "tsv"}:
            raise ValueError("Only portable CSV/TSV datasets can currently be imported.")
        version = self.datasets.ingest(
            session,
            actor=actor,
            project_id=project.id,
            name=str(item.get("name", "Dataset")),
            filename=filename,
            content=content,
            label_column=item.get("label_column"),
        )
        return item["version_id"], version.id

    def _remap_graph(
        self,
        session: Session,
        actor: ActorContext,
        project_id: str,
        graph: dict[str, Any],
        descriptors: Any,
    ) -> dict[str, Any]:
        result = deepcopy(graph)
        available = self.datasets.list_datasets(session, actor=actor, project_id=project_id)
        by_name = {item.name: item.current_version_id for item in available}
        exported = (
            {
                item["version_id"]: item["name"]
                for item in descriptors
                if isinstance(item, dict)
                and isinstance(item.get("version_id"), str)
                and isinstance(item.get("name"), str)
            }
            if isinstance(descriptors, list)
            else {}
        )
        ids = {old: by_name.get(name) for old, name in exported.items()}
        self._replace_dataset_ids(result, {key: value for key, value in ids.items() if value})
        return result

    @staticmethod
    def _replace_dataset_ids(graph: dict[str, Any], mapping: dict[str, str]) -> None:
        for node in graph.get("nodes", []):
            config = node.get("config", {})
            old = config.get("dataset_version_id")
            if old in mapping:
                config["dataset_version_id"] = mapping[old]

    @staticmethod
    def _check(document: dict[str, Any], kind: str) -> None:
        if (
            document.get("format") != FORMAT
            or document.get("format_version") != FORMAT_VERSION
            or document.get("kind") != kind
        ):
            raise ValueError(f"This is not a supported QuaPy Lab {kind} export.")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.") or "workflow"


def _python_script(name: str, graph: dict[str, Any], datasets: list[dict[str, Any]]) -> str:
    requirements = [
        {
            key: item.get(key)
            for key in ("version_id", "name", "expected_path", "source", "schema", "label_column")
        }
        for item in datasets
    ]
    return f'''#!/usr/bin/env python3
"""Reproducible Python representation of the QuaPy Lab workflow {name!r}.

Dataset files are resolved relative to this script. Install the versions of
quapy, scikit-learn and quapylab used by your project environment.
"""
from pathlib import Path
import json

from quapylab.domain.actor import desktop_actor
from quapylab.execution.local_worker import LocalWorker
from quapylab.execution.runtime_adapters import RuntimeNodeExecutor
from quapylab.nodes.registry import default_registry
from quapylab.persistence.database import Database
from quapylab.persistence.models import Run
from quapylab.services.artifacts import ArtifactService, LocalArtifactStore
from quapylab.services.datasets import DatasetService
from quapylab.services.projects import ProjectService
from quapylab.services.runs import RunService
from quapylab.services.workspace import bootstrap_desktop_workspace

WORKFLOW = json.loads({json.dumps(json.dumps(graph))})
DATASETS = json.loads({json.dumps(json.dumps(requirements))})

def dataset_paths(base_dir: Path | None = None) -> dict[str, Path]:
    base = base_dir or Path(__file__).resolve().parent
    paths = {{
        item["version_id"]: base / item["expected_path"]
        for item in DATASETS
        if item.get("source", {{}}).get("provider") != "quapy"
    }}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing workflow dataset file(s): " + ", ".join(missing))
    return paths

def main() -> None:
    paths = dataset_paths()
    state = Path(__file__).resolve().parent / ".quapylab-export-run"
    database = Database(f"sqlite+pysqlite:///{{state / 'run.sqlite3'}}")
    database.create_schema()
    bootstrap_desktop_workspace(database)
    artifacts = ArtifactService(LocalArtifactStore(state / "artifacts"))
    datasets = DatasetService(artifacts)
    projects = ProjectService()
    actor = desktop_actor()
    graph = json.loads(json.dumps(WORKFLOW))
    with database.transaction() as session:
        project = projects.create_project(
            session, actor=actor, group_id=str(actor.group_id), name={name!r}
        )
        session.flush()
        replacements = {{}}
        for requirement in DATASETS:
            source = requirement.get("source", {{}})
            if source.get("provider") == "quapy":
                version = datasets.ingest_quapy(
                    session,
                    actor=actor,
                    project_id=project.id,
                    source_id=source["loader"],
                    dataset_name=source["dataset"],
                    options=source.get("options", {{}}),
                )
            else:
                path = paths[requirement["version_id"]]
                version = datasets.ingest(
                    session,
                    actor=actor,
                    project_id=project.id,
                    name=requirement["name"],
                    filename=path.name,
                    content=path.read_bytes(),
                    label_column=requirement.get("label_column"),
                )
            replacements[requirement["version_id"]] = version.id
        for node in graph["nodes"]:
            old = node.get("config", {{}}).get("dataset_version_id")
            if old in replacements:
                node["config"]["dataset_version_id"] = replacements[old]
        workflow = projects.create_workflow(
            session, actor=actor, project_id=project.id, name={name!r}, graph=graph
        )
        run_service = RunService(default_registry())
        run = run_service.enqueue(
            session,
            actor=actor,
            project_id=project.id,
            workflow_revision_id=workflow.draft_revision_id,
            seed=0,
        )
        run_id = run.id
    worker = LocalWorker(database, run_service, RuntimeNodeExecutor(artifacts))
    worker.run(run_id)
    with database.transaction() as session:
        run = session.get(Run, run_id)
        print(f"Workflow run {{run.id}} finished with status: {{run.status}}")
        if run.error_summary:
            raise RuntimeError(run.error_summary)

if __name__ == "__main__":
    main()
'''
