"""Build the portable MCP wheel and its locked dependencies into this native plugin."""

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path


def default_package(plugin: Path) -> Path:
    """The MCP package beside this plugin: plugins/durumari-mcp here, mcp/ in the plugin
    repository (whose hosts/agent is this folder)."""
    sibling = plugin.parent / "durumari-mcp"
    return sibling if sibling.is_dir() else plugin.parents[1] / "mcp"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-origin", required=True)
    parser.add_argument(
        "--plugin", type=Path, help="plugin folder that receives runtime/ (default: this one)"
    )
    parser.add_argument("--package", type=Path, help="durumari-mcp project to build")
    args = parser.parse_args()
    plugin = (args.plugin or Path(__file__).resolve().parents[1]).resolve()
    package = (args.package or default_package(Path(__file__).resolve().parents[1])).resolve()
    sys.path.insert(0, str(package / "src"))
    from durumari_mcp.releases import parse_service

    service = {"schema_version": 1, "gateway_origin": args.service_origin}
    service["gateway_origin"] = parse_service(service)
    destination = plugin / "runtime"
    destination.mkdir(exist_ok=True)
    version = tomllib.loads((package / "pyproject.toml").read_text())["project"][
        "version"
    ]
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(destination),
            str(package),
        ],
        check=True,
    )
    lock = subprocess.run(
        [
            "uv",
            "export",
            "--project",
            str(package),
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--format",
            "requirements-txt",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (destination / "requirements.txt").write_text(lock.stdout, encoding="utf-8")
    names = [f"durumari_mcp-{version}-py3-none-any.whl", "requirements.txt"]
    metadata = {
        "schema_version": 1,
        "package": "durumari-mcp",
        "version": version,
        "service": service,
        "files": {
            name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
            for name in names
        },
    }
    (destination / "bundle.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps({"runtime_bundle": str(destination), "version": version}))


if __name__ == "__main__":
    main()
