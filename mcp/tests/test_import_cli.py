"""`durumari-mcp import issues` and the MCP import actions (design F7); generic example data."""

import base64
import hashlib
import json
from argparse import Namespace

import pytest
from mcp import Client

from durumari_mcp import import_cli
from durumari_mcp.cloud_protocol import create_context_server

JOB = "44444444-4444-4444-8444-444444444444"


class Recorder:
    """Stands in for the project API: records each call and answers like the server."""

    def __init__(self, view):
        self.view = view
        self.calls = []

    def call(self, path="", body=None, *, method="GET", key=None):
        self.calls.append((method, path, body, key))
        if path.endswith("/complete"):
            return {**self.view, "status": "uploaded"}
        return {"status": "ok"}


def test_upload_sends_only_the_missing_chunks_and_verifies(tmp_path):
    data = b"x" * 2500
    path = tmp_path / "export.jsonl"
    path.write_bytes(data)
    api = Recorder({"id": JOB})
    job = {
        "id": JOB,
        "status": "uploading",
        "chunk_size": 1024,
        "missing_chunks": [1],
        "received_chunks": 2,
    }
    result = import_cli.upload(api, job, path)
    assert result["status"] == "uploaded"
    sent = [c for c in api.calls if c[0] == "PUT"]
    assert [c[1] for c in sent] == [f"/{JOB}/chunks/1"]
    chunk = data[1024:2048]
    assert sent[0][2] == {
        "data": base64.b64encode(chunk).decode(),
        "sha256": hashlib.sha256(chunk).hexdigest(),
    }
    assert api.calls[-1][:2] == ("POST", f"/{JOB}/complete")


def test_run_repeats_until_the_job_leaves_running():
    answers = iter(
        [
            {"status": "running", "progress": {"done": 10}},
            {"status": "running", "progress": {"done": 20}},
            {"status": "done", "progress": {"done": 20}, "result": {"created": 20}},
        ]
    )

    class Api:
        calls = 0

        def call(self, path="", body=None, *, method="GET", key=None):
            self.calls += 1
            return next(answers)

    api = Api()
    assert import_cli.run_to_end(api, JOB)["result"] == {"created": 20}
    assert api.calls == 3


def test_summary_leaves_out_people_names_and_the_mapping():
    view = {
        "id": JOB,
        "status": "preview",
        "analysis": {
            "issues": 3,
            "people": [{"name": "Alex Kim"}],
            "sprints": [{"id": 1}],
            "types": {"Bug": 3},
        },
        "mapping": {"people": {"a": "b"}},
        "limits": {},
    }
    text = json.dumps(import_cli.summary(view))
    assert "Alex Kim" not in text and "mapping" not in text and '"people": 1' in text


def test_job_ids_are_checked_and_the_command_parses():
    with pytest.raises(import_cli.AdapterError):
        import_cli._job(Namespace(job="../x"))
    assert import_cli._job(Namespace(job=JOB)) == JOB

    class Sub:
        def add_parser(self, name, help=None):
            from argparse import ArgumentParser

            self.parser = ArgumentParser(prog=name)
            return self.parser

    sub = Sub()
    import_cli.add_commands(sub)
    parsed = sub.parser.parse_args(
        ["issues", "start", "--file", "a.jsonl", "--site", "acme.atlassian.net"]
    )
    assert (parsed.source, parsed.action, parsed.provider) == ("issues", "start", "jira")


class Plain:
    def read(self, suffix="", **query):
        return {}

    def mutate(self, method, suffix, body, key):
        return {}

    def operation(self, key):
        return {}

    def policies(self):
        return {}

    def status(self):
        return {}

    def __init__(self):
        self.calls = []

    def web_documents(self, tool, action, body):
        self.calls.append((tool, action, body))
        return {"tool": tool, "action": action}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_tool_passes_job_and_body_for_import_actions():
    operations = Plain()
    async with Client(create_context_server(operations)) as client:
        await client.call_tool(
            "durumari_document_type_submit",
            {
                "action": "import_chunk",
                "job_id": JOB,
                "body": {"index": 0, "data": "eA=="},
            },
        )
        await client.call_tool(
            "durumari_document_type_read", {"action": "import_job", "job_id": JOB}
        )
        refused = await client.call_tool(
            "durumari_document_type_submit", {"action": "import_unknown", "job_id": JOB}
        )
    assert operations.calls == [
        (
            "durumari_document_type_submit",
            "import_chunk",
            {"job_id": JOB, "body": {"index": 0, "data": "eA=="}},
        ),
        ("durumari_document_type_read", "import_job", {"job_id": JOB}),
    ]
    assert refused.is_error
