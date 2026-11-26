import copy
import json
from pathlib import Path

import pytest

from durumari_mcp.onboarding import merge_hooks


def test_repository_identity_normalizes_remote_protocol_and_omits_credentials(
    tmp_path, monkeypatch
):
    from durumari_mcp.onboarding import repository_identity

    monkeypatch.setattr(
        "durumari_mcp.onboarding.subprocess.check_output",
        lambda *a, **kw: "https://user:password@github.com/org/repo.git\n",
    )
    first = repository_identity(tmp_path)
    monkeypatch.setattr(
        "durumari_mcp.onboarding.subprocess.check_output",
        lambda *a, **kw: "git@github.com:org/repo.git\n",
    )
    assert repository_identity(tmp_path) == first


@pytest.mark.parametrize("label", [None, "", " "])
def test_hook_label_upgrade_preserves_identity_flags_and_inputs(label):
    current = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "durumari-hook",
                            "timeout": 60,
                            "enabled": False,
                        },
                    ],
                    "disabled": True,
                },
                {"hooks": [{"type": "command", "command": "user-hook"}]},
            ]
        }
    }
    template = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "durumari-hook",
                            "timeout": 60,
                            "statusMessage": "두루마리 · 응답 기록 저장",
                        },
                    ]
                }
            ]
        }
    }
    before_current, before_template = copy.deepcopy(current), copy.deepcopy(template)
    current["hooks"]["Stop"][0]["hooks"][0]["statusMessage"] = label
    before_current = copy.deepcopy(current)
    result = merge_hooks(current, template)
    assert len(result["hooks"]["Stop"]) == 2
    original_hook = result["hooks"]["Stop"][0]["hooks"][0]
    assert original_hook["statusMessage"] == "두루마리 · 응답 기록 저장"
    assert original_hook["enabled"] is False
    assert result["hooks"]["Stop"][0]["disabled"] is True
    assert result["hooks"]["Stop"][1] == current["hooks"]["Stop"][1]
    assert merge_hooks(result, template) == result
    assert current == before_current and template == before_template


def test_hook_label_upgrade_preserves_user_label_and_execution_distinction():
    current = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "durumari-hook",
                            "timeout": 30,
                            "statusMessage": "내가 정한 이름",
                        },
                    ]
                }
            ]
        }
    }
    template = copy.deepcopy(current)
    template["hooks"]["Stop"][0]["hooks"][0]["statusMessage"] = "두루마리 · 응답 기록 저장"
    assert merge_hooks(current, template) == current
    template["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 60
    result = merge_hooks(current, template)
    assert len(result["hooks"]["Stop"]) == 2
    result["hooks"]["Stop"][1]["hooks"][0]["command"] = "edited"
    assert template["hooks"]["Stop"][0]["hooks"][0]["command"] == "durumari-hook"


def test_codex_hook_templates_identify_each_role():
    root = Path(__file__).parents[1]
    external = json.loads((root / "examples/project-codex-hooks.json").read_text())
    packaged = json.loads((root / "src/durumari_mcp/examples/project-codex-hooks.json").read_text())
    assert external == packaged
    labels = [
        hook.get("statusMessage", "")
        for groups in packaged["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert len(labels) == len(set(labels)) == 3
    assert all(label.startswith("두루마리 · ") for label in labels)


def test_legacy_codex_interrupt_clamp_upgrade_is_idempotent():
    hook = {
        "type": "command",
        "timeout": 60,
        "command": "python -m durumari_mcp project-hook --profile p --client codex",
        "commandWindows": "python -m durumari_mcp project-hook --profile p --client codex",
        "enabled": False,
    }
    current = {"hooks": {"Interrupt": [{"hooks": [hook]}]}}
    before = copy.deepcopy(current)
    template = copy.deepcopy(current)
    incoming = template["hooks"]["Interrupt"][0]["hooks"][0]
    incoming.pop("enabled")
    incoming["timeout"] = 3
    result = merge_hooks(current, template)
    assert len(result["hooks"]["Interrupt"]) == 1
    assert result["hooks"]["Interrupt"][0]["hooks"][0]["timeout"] == 3
    assert result["hooks"]["Interrupt"][0]["hooks"][0]["enabled"] is False
    assert merge_hooks(result, template) == result
    assert current == before
































