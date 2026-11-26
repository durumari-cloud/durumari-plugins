"""Render every host copy of the durumari-context skill from one source.

The canonical skill (``canonical`` in variants.json) is the full Agent skill. Each
variant is either an exact copy of canonical files, or a SKILL.md made of a generated
frontmatter (the shared ``name`` and a host ``description``) and a host body kept in
this folder. Run without arguments to write the copies; ``--check`` exits 1 and names
the files that differ, without writing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def normalized(text: str) -> str:
    return text.replace("\r\n", "\n")


def frontmatter(name: str, description: str) -> str:
    if "\n" in description or not description.strip():
        raise ValueError(f"invalid description for {name}")
    return f"---\nname: {name}\ndescription: {description}\n---\n"


def render(config_path: Path = HERE / "variants.json") -> dict[Path, str]:
    """Every generated file and its expected text (LF line endings)."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = (config_path.parent / config["root"]).resolve()
    skill = config["skill"]
    canonical = root / config["canonical"]
    source = normalized((canonical / "SKILL.md").read_text(encoding="utf-8"))
    match = FRONTMATTER.match(source)
    if not match or f"name: {skill}" not in match.group(1).splitlines():
        raise ValueError("canonical SKILL.md needs frontmatter naming the skill")
    files: dict[Path, str] = {}
    for variant in config["variants"]:
        target = root / variant["target"]
        if "copy" in variant:
            for name in variant["copy"]:
                text = normalized((canonical / name).read_text(encoding="utf-8"))
                files[target / name] = text
            continue
        body = normalized((config_path.parent / variant["body"]).read_text(encoding="utf-8"))
        if body.startswith("---"):
            raise ValueError(f"{variant['body']} must not carry its own frontmatter")
        files[target / "SKILL.md"] = frontmatter(skill, variant["description"]) + "\n" + body
    return files


def stale(files: dict[Path, str]) -> list[Path]:
    return [
        path
        for path, text in files.items()
        if not path.is_file() or normalized(path.read_text(encoding="utf-8")) != text
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report stale copies only")
    parser.add_argument("--config", type=Path, default=HERE / "variants.json")
    args = parser.parse_args()
    files = render(args.config)
    changed = stale(files)
    if args.check:
        for path in changed:
            print(f"stale: {path}", file=sys.stderr)
        return 1 if changed else 0
    for path in changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[path].encode("utf-8"))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
