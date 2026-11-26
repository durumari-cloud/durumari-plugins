"""Build a plugin release from this repository alone.

The installable catalog keeps the layout every installed updater expects
(``plugins/durumari-agent`` with ``skills/`` and ``runtime/``), so this script stages
``hosts/agent`` and ``skills/`` into that layout, builds the runtime wheel from ``mcp/``,
binds everything to one service origin and verifies the archive with the shipping
installer before writing anything.

Output (``--output``, a fresh directory)::

    releases/<version>/durumari-plugins.zip   the catalog archive updaters download
    releases/<version>/SHA256SUMS
    latest.json                               release metadata the installer reads
    durumari_mcp-<version>-py3-none-any.whl   the runtime wheel
    install.py                                standalone first-install script
    install.sh, install.ps1                   one-line installs naming the origin
    plugin-artifact.json                      hashes for the server's release assembly

``--catalog-output`` also writes the extracted catalog tree (the Git catalog's content).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mcp"
HOST = ROOT / "hosts/agent"
SKILLS = ROOT / "skills"
sys.path.insert(0, str(PACKAGE / "src"))

from durumari_mcp.releases import (  # noqa: E402
    BUNDLE,
    CATALOG,
    CATALOG_README,
    allowed_member,
    extract_catalog,
    parse_release,
    parse_service,
    service_catalog,
)

AGENT = "plugins/durumari-agent"
MANIFESTS = (
    "plugin.json",
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
    "gemini-extension.json",
)
MARKETPLACES = (
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".cursor-plugin/marketplace.json",
)
BOOTSTRAPS = ("install.sh", "install.ps1")
ORIGIN_MARK = "__DURUMARI_ORIGIN__"
# Host files that never ship: tests, evals and build helpers stay in this repository.
STAGE_EXCLUDE = {"tests", "evals", "runtime", "__pycache__", ".gitignore"}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def package_version() -> str:
    version = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    return version["project"]["version"]


def check_versions(version: str) -> None:
    for name in MANIFESTS:
        if json.loads((HOST / name).read_text(encoding="utf-8"))["version"] != version:
            raise SystemExit(f"{name} does not carry the MCP version {version}")


def stage(target: Path) -> Path:
    """hosts/agent + skills/ laid out as the catalog's plugins/durumari-agent."""
    plugin = target / AGENT
    for source in sorted(HOST.rglob("*")):
        relative = source.relative_to(HOST)
        if STAGE_EXCLUDE & set(relative.parts) or not source.is_file():
            continue
        if source.is_symlink():
            raise SystemExit(f"refusing a linked source: {relative}")
        destination = plugin / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    if (plugin / "skills").exists():
        raise SystemExit("hosts/agent must not carry its own skills/")
    for source in sorted(SKILLS.rglob("*")):
        if source.is_file():
            if source.is_symlink():
                raise SystemExit(f"refusing a linked skill file: {source}")
            destination = plugin / "skills" / source.relative_to(SKILLS)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    return plugin


def build_runtime(plugin: Path, origin: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(HOST / "scripts/build_bundle.py"),
            "--service-origin",
            origin,
            "--plugin",
            str(plugin),
            "--package",
            str(PACKAGE),
        ],
        check=True,
    )


def catalog_files(staged: Path, version: str) -> dict[str, bytes]:
    files = {"README.md": CATALOG_README.encode()}
    for name in MARKETPLACES:
        metadata = json.loads((ROOT / name).read_text(encoding="utf-8"))
        metadata["name"] = "durumari"
        files[name] = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode()
    plugin = staged / AGENT
    wheel = f"durumari_mcp-{version}-py3-none-any.whl"
    for path in sorted(plugin.rglob("*")):
        relative = path.relative_to(staged).as_posix()
        if path.suffix == ".whl" and path.name != wheel:
            continue
        if path.is_file() and allowed_member(relative):
            files[relative] = path.read_bytes()
    if BUNDLE not in files:
        raise SystemExit("runtime bundle missing from the staged plugin")
    return files


def catalog_archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, data in sorted(files.items()):
            entry = zipfile.ZipInfo("durumari-catalog/" + relative, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return stream.getvalue()


def release_metadata(version: str, payload: bytes, origin: str) -> dict:
    release = {
        "schema_version": 1,
        "version": version,
        "catalog": {"name": "durumari", "repository": CATALOG},
        "archive": {
            "path": f"/downloads/releases/{version}/durumari-plugins.zip",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        },
    }
    # The shipping installer is the authority for paths, versions and bundle integrity.
    parse_release(json.dumps(release).encode())
    with tempfile.TemporaryDirectory() as directory:
        root = extract_catalog(payload, Path(directory), version)
        bundle = json.loads((root / BUNDLE).read_text(encoding="utf-8"))
        if parse_service(bundle["service"]) != origin:
            raise SystemExit("the archive names another service origin")
    return release


def write_bootstraps(destination: Path, origin: str) -> None:
    for name in BOOTSTRAPS:
        text = (PACKAGE / "bootstrap" / name).read_text(encoding="utf-8")
        if ORIGIN_MARK not in text:
            raise SystemExit(f"{name} names no server")
        (destination / name).write_bytes(text.replace(ORIGIN_MARK, origin).encode())


def write_release(destination: Path, version: str, files: dict[str, bytes], origin: str) -> dict:
    payload = catalog_archive(files)
    release = release_metadata(version, payload, origin)
    archive = destination / "releases" / version / "durumari-plugins.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(payload)
    (archive.parent / "SHA256SUMS").write_text(
        f"{release['archive']['sha256']}  durumari-plugins.zip\n", encoding="utf-8"
    )
    write_json(destination / "latest.json", release)
    write_bootstraps(destination, origin)
    return release


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--service-origin", required=True, help="e.g. https://app.durumari.cloud")
    parser.add_argument("--output", type=Path, required=True, help="fresh output directory")
    parser.add_argument("--catalog-output", type=Path, help="also write the extracted catalog")
    parser.add_argument(
        "--preview-origin", help="also write a preview/ variant bound to this origin"
    )
    args = parser.parse_args()
    origin = parse_service({"schema_version": 1, "gateway_origin": args.service_origin})
    output = args.output.resolve()
    for path in (output, args.catalog_output):
        if path is not None and path.exists() and any(path.iterdir()):
            raise SystemExit(f"{path} is not empty; use a fresh directory")
    version = package_version()
    check_versions(version)
    with tempfile.TemporaryDirectory(prefix="durumari-release-") as scratch:
        staged = Path(scratch) / "catalog"
        plugin = stage(staged)
        build_runtime(plugin, origin)
        source = catalog_files(staged, version)
        runtime_bundle = source[BUNDLE]
        files = service_catalog(source, origin)
        output.mkdir(parents=True, exist_ok=True)
        release = write_release(output, version, files, origin)
        wheel = plugin / "runtime" / f"durumari_mcp-{version}-py3-none-any.whl"
        shutil.copyfile(wheel, output / wheel.name)
        # The first-install command downloads this exact standalone installer; it must run
        # with the standard library alone.
        installer = output / "install.py"
        shutil.copyfile(PACKAGE / "src/durumari_mcp/releases.py", installer)
        subprocess.run(
            [sys.executable, "-I", "-S", str(installer), "--help"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        write_json(
            output / "plugin-artifact.json",
            {
                **release,
                "service_origin": origin,
                "wheel": {
                    "path": "/downloads/" + wheel.name,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                },
                "installer": {
                    "path": "/downloads/install.py",
                    "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
                },
                "source_files": {
                    name: hashlib.sha256(value).hexdigest() for name, value in files.items()
                },
                "verified_by_shipping_installer": True,
            },
        )
        if args.preview_origin:
            preview = parse_service({"schema_version": 1, "gateway_origin": args.preview_origin})
            retargeted = service_catalog({**files, BUNDLE: runtime_bundle}, preview)
            release_preview = write_release(output / "preview", version, retargeted, preview)
            write_json(
                output / "preview/plugin-artifact.json",
                {
                    **release_preview,
                    "service_origin": preview,
                    "verified_by_shipping_installer": True,
                },
            )
        if args.catalog_output:
            with tempfile.TemporaryDirectory() as directory:
                root = extract_catalog(catalog_archive(files), Path(directory), version)
                shutil.copytree(root, args.catalog_output, dirs_exist_ok=True)
    print(
        json.dumps(
            {
                "version": version,
                "service_origin": origin,
                "sha256": release["archive"]["sha256"],
                "files": len(files),
                "output": str(output),
            }
        )
    )


if __name__ == "__main__":
    main()
