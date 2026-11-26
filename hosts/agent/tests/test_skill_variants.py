"""Every host copy of durumari-context is rendered from the one source, not edited by hand."""

import importlib.util

import pytest
from agent_layout import SKILL_VARIANTS


def load_renderer():
    spec = importlib.util.spec_from_file_location("skill_variants_render", SKILL_VARIANTS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_skill_copies_match_the_source():
    render = load_renderer()
    files = render.render()
    assert len(files) >= 6
    stale = render.stale(files)
    assert not stale, (
        f"run `python {SKILL_VARIANTS}` to regenerate: {[path.as_posix() for path in stale]}"
    )


def test_every_copy_names_the_skill_once_and_keeps_a_one_line_description():
    render = load_renderer()
    for path, text in render.render().items():
        if path.name != "SKILL.md":
            continue
        front = text.split("---\n")[1].splitlines()
        assert front[0] == "name: durumari-context", path
        assert front[1].startswith("description: ") and len(front) == 2, path


def test_a_body_with_its_own_frontmatter_is_refused(tmp_path):
    render = load_renderer()
    (tmp_path / "skills/durumari-context").mkdir(parents=True)
    (tmp_path / "skills/durumari-context/SKILL.md").write_text(
        "---\nname: durumari-context\ndescription: x\n---\n\n# X\n", encoding="utf-8"
    )
    (tmp_path / "body.md").write_text("---\nname: other\n---\n", encoding="utf-8")
    config = tmp_path / "variants.json"
    config.write_text(
        '{"skill": "durumari-context", "root": ".", "canonical": "skills/durumari-context",'
        ' "variants": [{"name": "x", "target": "out", "description": "d", "body": "body.md"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        render.render(config)
