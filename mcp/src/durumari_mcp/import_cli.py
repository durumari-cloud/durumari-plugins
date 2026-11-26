"""`durumari-mcp import issues`: bring a Jira export (JSON lines or CSV) into the bound space.

Every step is one call of the space's `/issue-imports` API (the same one the web screen, the
REST API and the MCP tool use), so permissions and receipts are the server's. A file is sent in
chunks that can be resumed with `--job`; a repeated chunk changes nothing.
"""

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from durumari_mcp.context_binding import bound_profile
from durumari_mcp.context_client import ContextClient
from durumari_mcp.credentials import AdapterError
from durumari_mcp.json_output import emit_json
from durumari_mcp.legacy import manifest_path
from durumari_mcp.onboarding import read_manifest
from durumari_mcp.project_credentials import load_project_profile
from durumari_mcp.project_gateway import ProjectGateway
from durumari_mcp.workspace_binding import binding_root

ACTIONS = (
    "start",
    "status",
    "preview",
    "mapping",
    "approve",
    "run",
    "retry",
    "cancel",
    "list",
)
CHUNK_BYTES = 2 * 1024 * 1024
JOB = re.compile(r"[A-Za-z0-9-]{1,64}")


def add_commands(subcommands: Any) -> None:
    parser = subcommands.add_parser("import", help="Import issues from another tracker")
    parser.add_argument("source", choices=["issues"], help="What to import")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--profile")
    parser.add_argument("--client", default="cli")
    parser.add_argument("--file", help="Jira export: JSON lines (.jsonl) or CSV (.csv)")
    parser.add_argument("--format", choices=["jsonl", "csv"], help="Default: from the file name")
    parser.add_argument("--provider", default="jira", choices=["jira"])
    parser.add_argument("--site", default="", help="The source site, e.g. acme.atlassian.net")
    parser.add_argument("--job", help="An existing job id (resume an upload, read, approve, run)")
    parser.add_argument("--mapping", help="A JSON file with the confirmed mapping (action mapping)")
    parser.add_argument("--fingerprint", help="The preview's fingerprint (action approve)")
    parser.add_argument("--yes", action="store_true", help="With start: approve and run at once")
    parser.add_argument("--chunk-bytes", type=int, default=CHUNK_BYTES)


def _client(args: Any) -> ContextClient:
    from durumari_mcp.context_cli import selected_workspace

    cwd = selected_workspace(args.workspace)
    root = binding_root(cwd)
    name = args.profile or (
        bound_profile(root, read_manifest(root)) if manifest_path(root).exists() else None
    )
    if not name:
        raise AdapterError("project_credential_missing")
    profile = load_project_profile(name)
    profile.check_workspace(str(cwd))
    os.chdir(cwd)
    gateway = ProjectGateway(name, args.client)
    transport = getattr(gateway, "transport", None)
    if transport is not None:
        transport.via = "cli"
    return ContextClient(gateway, os.environ.get("DURUMARI_SESSION_ID") or f"process-{os.getpid()}")


class Api:
    def __init__(self, client: ContextClient) -> None:
        self.client = client
        self.base = "/api/v2/projects/" + client.gateway.profile.project_id + "/issue-imports"

    def call(
        self,
        path: str = "",
        body: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        key: str | None = None,
    ) -> dict[str, Any]:
        return self.client.gateway.context_request(
            self.base + path, body, method=method, idempotency_key=key
        )


def _job(args: Any) -> str:
    if not args.job or not JOB.fullmatch(args.job):
        raise AdapterError("import_job_required")
    return str(args.job)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def upload(api: Api, job: dict[str, Any], path: Path) -> dict[str, Any]:
    """Send the chunks the job is still missing, then verify the whole file."""
    size = int(job["chunk_size"])
    missing = set(job["missing_chunks"]) if job["status"] == "uploading" else set()
    with path.open("rb") as handle:
        index = 0
        while True:
            data = handle.read(size)
            if not data:
                break
            if index in missing:
                api.call(
                    f"/{job['id']}/chunks/{index}",
                    {
                        "data": base64.b64encode(data).decode(),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    },
                    method="PUT",
                )
            index += 1
    return api.call(f"/{job['id']}/complete", {}, method="POST")


def run_to_end(api: Api, job_id: str) -> dict[str, Any]:
    while True:
        view = api.call(f"/{job_id}/run", {}, method="POST")
        print(
            json.dumps({"status": view["status"], "progress": view.get("progress")}),
            file=sys.stderr,
        )
        if view["status"] not in ("approved", "running"):
            return view


def summary(view: dict[str, Any]) -> dict[str, Any]:
    """The job without the people and sprint lists, which can be long."""
    analysis = view.get("analysis") or {}
    return {
        **{k: v for k, v in view.items() if k not in ("analysis", "mapping", "limits")},
        "analysis": {
            k: analysis.get(k)
            for k in (
                "issues",
                "comments",
                "history_items",
                "links",
                "types",
                "statuses",
                "priorities",
            )
        }
        if analysis
        else None,
        "people": len(analysis.get("people", [])) if analysis else None,
        "sprints": len(analysis.get("sprints", [])) if analysis else None,
    }


def execute(args: Any) -> None:
    api = Api(_client(args))
    action = args.action
    if action == "list":
        emit_json(api.call())
        return
    if action == "start":
        path = Path(args.file or "")
        if args.job:
            job = api.call(f"/{_job(args)}")
        else:
            if not path.is_file():
                raise AdapterError("import_file_required")
            fmt = args.format or ("csv" if path.suffix.lower() == ".csv" else "jsonl")
            digest = _hash(path)
            job = api.call(
                "",
                {
                    "provider": args.provider,
                    "format": fmt,
                    "file_name": path.name[:200],
                    "size_bytes": path.stat().st_size,
                    "chunk_bytes": max(1024, min(args.chunk_bytes, 4 * 1024 * 1024)),
                    "sha256": digest,
                    "site": args.site,
                },
                method="POST",
                key="import-" + digest[:48],
            )
        if job["status"] == "uploading":
            job = upload(api, job, path)
        if job["status"] == "uploaded":
            job = api.call(f"/{job['id']}/analyze", {}, method="POST")
        emit_json(summary(job) if not args.yes else job)
        if args.yes and job["status"] == "preview":
            api.call(f"/{job['id']}/approve", {"fingerprint": job["fingerprint"]}, method="POST")
            emit_json(run_to_end(api, job["id"]))
        return
    job_id = _job(args)
    if action in ("status", "preview"):
        view = api.call(f"/{job_id}")
        emit_json(view if action == "preview" else summary(view))
    elif action == "mapping":
        if not args.mapping:
            raise AdapterError("import_mapping_required")
        value = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
        emit_json(api.call(f"/{job_id}/mapping", {"mapping": value}, method="PUT"))
    elif action == "approve":
        if not args.fingerprint:
            raise AdapterError("import_fingerprint_required")
        api.call(f"/{job_id}/approve", {"fingerprint": args.fingerprint}, method="POST")
        emit_json(run_to_end(api, job_id))
    elif action == "run":
        emit_json(run_to_end(api, job_id))
    elif action in ("retry", "cancel"):
        emit_json(api.call(f"/{job_id}/{action}", {}, method="POST"))
