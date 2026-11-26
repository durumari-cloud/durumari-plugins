import pytest
from test_context_v2_adapter import context_profile

from durumari_mcp.config_json import load, render
from durumari_mcp.context_binding import install_binding, remove_binding


def test_comments_urls_trailing_commas_and_other_tools_survive_install_remove(tmp_path):
    scope = context_profile(tmp_path)
    original = (
        '{\n // Existing user preferences\n "model": "own-model",\n "mcp": {\n '
        '/* Keep this tool */ "other": {"url":"https://api.invalid/a//b",},\n },\n}\n'
    )
    path = tmp_path / "opencode.jsonc"
    path.write_text(original)
    install_binding(scope, "shared", ["opencode"])
    installed = path.read_text()
    assert '/* Keep this tool */ "other": {"url":"https://api.invalid/a//b",}' in installed
    assert "// Existing user preferences" in installed
    assert load(installed)["mcp"]["other"]["url"] == "https://api.invalid/a//b"
    assert not (tmp_path / "opencode.json").exists()
    remove_binding(scope, "shared")
    assert load(path.read_text()) == load(original)
    assert '/* Keep this tool */ "other": {"url":"https://api.invalid/a//b",}' in path.read_text()


def test_owned_nested_replacement_preserves_other_comments_and_fields():
    original = (
        '{"tools": {"owned":{"args": ["old"]}, /* Unrelated */ "other":42}, '
        '"note":"한국어 \\"문장\\""}'
    )
    updated = load(original)
    updated["tools"]["owned"]["args"] = ["new"]
    result = render(original, updated)
    assert '/* Unrelated */ "other":42' in result
    assert load(result) == updated


@pytest.mark.parametrize("value", ['{"a":1,"a":2}', '{"x":{/* unterminated}', '{"x": NaN}'])
def test_ambiguous_or_invalid_settings_are_not_rewritten(value):
    with pytest.raises(ValueError):
        load(value)


@pytest.mark.parametrize(
    "original", ['{"a": 1}', '{"a": 1,}', '{"k": 0, "a": 1}', '{"k": 0, "a": 1,}']
)
def test_replacing_the_last_member_of_an_object_stays_valid(original):
    desired = load(original)
    desired.pop("a")
    desired["b"] = 2
    assert load(render(original, desired)) == desired
