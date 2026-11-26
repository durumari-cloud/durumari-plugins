"""The import tool only opens a bound browser task; source text never crosses it."""

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from durumari_mcp.credentials import AdapterError
from durumari_mcp.import_tools import Reference, register_import_tools

REF = "11111111-1111-1111-1111-111111111111"


def registered(result=None, failure=None):
    calls, tools = [], {}

    def resources(action, body):
        calls.append((action, body))
        if failure:
            raise failure
        return result or {"task_ref": REF, "state": "pending", "browser_url": "https://x"}

    def register(fn, **options):
        tools[fn.__name__] = (fn, options)

    register_import_tools(resources, register)
    return calls, tools


def test_tool_binds_import_manage_to_the_conversation_project():
    calls, tools = registered()
    fn, options = tools["durumari_context_import"]
    assert options == {
        "title": "Migrate selected source material",
        "read": False,
        "required_scopes": ["context:read", "context:write"],
    }
    result = fn(REF, "same-retry")
    assert not result.is_error
    assert calls == [("begin", {
        "project_ref": REF, "request_key": "same-retry", "kind": "import", "action": "manage",
    })]


def test_tool_takes_no_source_body_or_selection():
    _, tools = registered()
    fn, _ = tools["durumari_context_import"]
    assert set(fn.__annotations__) - {"return"} == {"project_ref", "request_key"}


@pytest.mark.parametrize(
    "value", ["", "not-a-uuid", "AAAAAAAA-1111-1111-1111-111111111111", REF + "0"]
)
def test_project_reference_is_an_exact_lowercase_uuid(value):
    with pytest.raises(ValidationError):
        TypeAdapter(Reference).validate_python(value)


def test_server_refusal_is_a_value_free_tool_error():
    calls, tools = registered(failure=AdapterError("resource_scope_denied"))
    result = tools["durumari_context_import"][0](REF, "k")
    assert result.is_error
    assert json.loads(result.content[0].text) == {"error": {"code": "resource_scope_denied"}}
    assert len(calls) == 1
