"""Project-scoped settings with safe ownership checks and recoverable writes."""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import yaml

from durumari_mcp import instruction_files, opencode_config
from durumari_mcp.config_json import load as load_json
from durumari_mcp.config_json import masked as mask_json_comments
from durumari_mcp.config_json import render as render_json
from durumari_mcp.credentials import AdapterError, profile_name
from durumari_mcp.legacy import manifest_path
from durumari_mcp.local_upgrade import USER_KEYS, save_backup
from durumari_mcp.onboarding import END, START, inside, merge_hooks
from durumari_mcp.project_credentials import ProjectProfile
from durumari_mcp.server_aliases import identity as server_identity
from durumari_mcp.server_aliases import same_server

AGENTS = ["codex", "claude", "cursor", "gemini", "antigravity", "opencode", "copilot"]
# Experimental hosts are configured only when explicitly selected.
DEFAULT_AGENTS = [agent for agent in AGENTS if agent != "copilot"]
JSON_FILES = {
    "claude": (".mcp.json", "mcpServers"),
    "cursor": (".cursor/mcp.json", "mcpServers"),
    "gemini": (".gemini/settings.json", "mcpServers"),
    "antigravity": (".agents/mcp_config.json", "mcpServers"),
    "opencode": ("opencode.json", "mcp"),
    # GitHub Copilot CLI reads the same project .mcp.json as Claude Code:
    # https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
    "copilot": (".mcp.json", "mcpServers"),
}
SERVER = "durumari_project"
IGNORES = [
    "/.codex/config.toml",
    "/.codex/hooks.json",
    "/.mcp.json",
    "/.claude/settings.local.json",
    "/.cursor/mcp.json",
    "/.gemini/settings.json",
    "/.agents/mcp_config.json",
    "/opencode.json",
    "/opencode.jsonc",
    "/" + opencode_config.SKILL_PATH,
    "/.durumari/",
    "*.durumari-backup-*",
]


def workspace_identity(root: Path) -> str:
    return hashlib.sha256(
        ("workspace:" + os.path.normcase(str(root.resolve()))).encode()
    ).hexdigest()


def shared_profile(server: str, project: str, root: Path) -> str:
    key = hashlib.sha256(
        (server_identity(server) + "\0" + project + "\0" + str(root.resolve())).encode()
    ).hexdigest()
    return "durumari-" + key[:16] + "-mcp"


def json_file(root: Path, name: str) -> dict[str, Any]:
    path = inside(root, root / name)
    try:
        value = load_json(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise AdapterError("invalid_agent_configuration") from None


def json_output(root: Path, name: str, value: dict[str, Any]) -> str:
    path = inside(root, root / name)
    try:
        return render_json(path.read_text(encoding="utf-8") if path.exists() else "", value)
    except (ValueError, UnicodeError):
        raise AdapterError("invalid_agent_configuration") from None


IGNORE_HEADER = "# Durumari local Agent settings"


def created_directories(record: dict[str, Any]) -> set[str]:
    value = record.get("created_directories", [])
    return {name for name in value if isinstance(name, str)} if isinstance(value, list) else set()


def ignore_lines(record: dict[str, Any]) -> list[str]:
    value = record.get("gitignore_added", [])
    # Only lines this runtime writes can be taken out again.
    return [line for line in value if line in IGNORES] if isinstance(value, list) else []


def created_files(record: dict[str, Any]) -> set[str]:
    value = record.get("created_files", [])
    # Old or damaged metadata never authorizes deleting a user's file.
    return {name for name in value if isinstance(name, str)} if isinstance(value, list) else set()


def settings_path(root: Path, agent: str) -> tuple[str, str]:
    name, key = JSON_FILES[agent]
    if agent == "opencode" and (root / "opencode.jsonc").exists():
        if (root / name).exists():
            raise AdapterError("ambiguous_agent_configuration")
        name = "opencode.jsonc"
    return name, key


def owned(entry: Any, profile: str | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    arguments = entry.get("args", entry.get("command", []))
    return (
        isinstance(arguments, list)
        and (
            profile is None
            or ["--profile", profile] in [arguments[i : i + 2] for i in range(len(arguments))]
        )
        and "durumari_mcp" in arguments
        and any(command in arguments for command in ("project-serve", "context-serve"))
    )


def write_files(root: Path, files: dict[str, str | None]) -> None:
    # Resolve and validate every destination before changing any settings.
    for name in files:
        inside(root, root / name)
    changes: list[tuple[Path, bytes | None]] = []
    for name, content in files.items():
        path = root / name
        previous = path.read_bytes() if path.exists() else None
        desired = content.encode() if content is not None else None
        if previous == desired:
            continue
        if previous is not None:
            # The previous content goes to the user data folder, one copy per file, so
            # the repository never collects backups. Every backup is
            # taken before any write, and a backup the folder refuses (an Agent sandbox,
            # a read-only home) is skipped with a warning: it never fails the binding
            # or leaves it half applied.
            try:
                save_backup(root, name, previous)
            except OSError as error:
                print(
                    f"durumari: backup skipped for {name} ({type(error).__name__})",
                    file=sys.stderr,
                )
        changes.append((path, desired))
    for path, desired in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        if desired is None:
            path.unlink(missing_ok=True)
            continue
        descriptor, temporary_name = tempfile.mkstemp(prefix=".durumari-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(desired)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def toml_comments(text: str) -> str:
    """Keep comments from an owned table, never hashes inside its string values."""
    comments = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote.startswith('"') and char == "\\":
                index += 2
            elif text.startswith(quote, index):
                index += len(quote)
                if len(quote) == 3:
                    # TOML permits one or two quotes just before a multiline close.
                    while index < len(text) and text[index] == quote[0]:
                        index += 1
                quote = ""
            else:
                index += 1
        elif char in "\"'":
            quote = char * 3 if text.startswith(char * 3, index) else char
            index += len(quote)
        elif char == "#":
            end = text.find("\n", index)
            if end == -1:
                end = len(text)
            comments.append(text[index:end].rstrip("\r") + "\n")
            index = end
        else:
            index += 1
    return "".join(comments)


def remove_codex_block(text: str, *, profile: str | None = None) -> str:
    parsed = tomllib.loads(text)
    entry = parsed.get("mcp_servers", {}).get(SERVER)
    if entry is None:
        return text
    if not owned(entry, profile):
        raise AdapterError("existing_mcp_binding_conflict")
    pattern = r"(?ms)^\[mcp_servers\.durumari_project\][^\n]*\n.*?(?=^\[|\Z)"
    # Reconnect moves this table after [features]. Comments added at EOF then sit
    # inside our table syntactically, but remain user content on reconnect/remove.
    def keep_comments(match: re.Match[str]) -> str:
        return toml_comments(match.group())

    updated, count = re.subn(pattern, keep_comments, text)
    if count != 1:
        raise AdapterError("existing_mcp_binding_conflict")
    return updated


def hook_settings(root: Path, agent: str, name: str, *, remove: bool = False) -> tuple[str, str]:
    path = ".codex/hooks.json" if agent == "codex" else ".claude/settings.local.json"
    current = json_file(root, path)
    for event, groups in current.get("hooks", {}).items():
        retained = []
        for group in groups:
            hooks = []
            for hook in group.get("hooks", []):
                try:
                    args = (
                        hook.get("args", [])
                        if agent == "claude"
                        else shlex.split(hook.get("command", ""))
                    )
                except ValueError:
                    args = []
                matching = not remove or ["--profile", name] in [
                    args[i : i + 2] for i in range(len(args))
                ]
                if matching and "durumari_mcp" in args and "project-hook" in args:
                    continue
                if (remove and agent == "codex" and matching
                        and args[1:3] == ["-m", "durumari_mcp.context_guard_runner"]):
                    # Reconnect/upgrade preserves the exact reviewed guard. An
                    # explicit disconnect removes only this profile's runner,
                    # not another profile or a user command mentioning it.
                    continue
                hooks.append(hook)
            if hooks:
                retained.append({**group, "hooks": hooks})
        current["hooks"][event] = retained
    if not remove:
        command = [
            sys.executable,
            "-m",
            "durumari_mcp",
            "project-hook",
            "--profile",
            name,
            "--client",
            agent,
        ]
        template = json.loads(
            (Path(__file__).parent / "examples" / f"project-{agent}-hooks.json").read_text(
                encoding="utf-8"
            )
        )
        for groups in template["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    if agent == "claude":
                        hook["command"], hook["args"] = command[0], command[1:]
                    else:
                        hook["command"] = shlex.join(command)
                        hook["commandWindows"] = subprocess.list2cmdline(command)
        current = merge_hooks(current, template)
    return path, json_output(root, path, current)


def bound_profile(root: Path, manifest: dict[str, Any]) -> str | None:
    """This user's profile: the ignored binding record, else a legacy manifest key."""
    try:
        record = json_file(root, ".durumari/context-binding.json")
    except AdapterError:
        record = {}
    name = record.get("profile") or manifest.get("profile")
    return profile_name(name) if isinstance(name, str) and name else None


def bound_agents(root: Path, name: str) -> list[str]:
    """Agents recorded for this profile plus any whose settings it still owns."""
    try:
        record = json_file(root, ".durumari/context-binding.json")
    except AdapterError:
        record = {}  # A damaged record must not hide or block this profile's settings.
    recorded = record.get("agents") if record.get("profile") == name else None
    agents = [agent for agent in recorded if agent in AGENTS] if isinstance(recorded, list) else []
    for agent in AGENTS:
        if agent in agents:
            continue
        try:
            if agent == "codex":
                path = inside(root, root / ".codex/config.toml")
                text = path.read_text(encoding="utf-8") if path.exists() else ""
                servers = tomllib.loads(text).get("mcp_servers", {})
            else:
                path_name, key = settings_path(root, agent)
                if any(JSON_FILES.get(other, (None,))[0] == path_name for other in agents):
                    continue  # Claude Code and Copilot share .mcp.json; one owner suffices.
                servers = json_file(root, path_name).get(key, {})
        except (AdapterError, ValueError, UnicodeError):
            continue  # Unreadable settings are not ours to sweep; recorded agents still fail.
        if isinstance(servers, dict) and owned(servers.get(SERVER), name):
            agents.append(agent)
    return agents


def install_binding(
    profile: ProjectProfile,
    name: str,
    agents: list[str],
    *,
    apply: bool = True,
    approved_manifest: dict[str, Any] | None = None,
    opencode_format: str = "auto",
) -> dict[str, Any]:
    name = profile_name(name)
    root = Path(profile.workspace)
    if approved_manifest is None:
        profile.check_workspace(str(root))
    else:
        from durumari_mcp.onboarding import read_manifest, repository_identity

        if read_manifest(root) != approved_manifest:
            raise AdapterError("project_binding_changed_retry_connect")
        if profile.folder_source_id:
            from durumari_mcp.workspace_binding import folder_identity

            if profile.workspace_identity != workspace_identity(
                root
            ) or profile.repository_id != folder_identity(profile.folder_source_id):
                raise AdapterError("workspace_out_of_scope")
        else:
            try:
                top = subprocess.check_output(
                    ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    timeout=2,
                ).strip()
                if (
                    Path(top).resolve() != root
                    or profile.workspace_identity != workspace_identity(root)
                    or profile.repository_id != repository_identity(root)
                ):
                    raise ValueError
            except (ValueError, OSError, subprocess.SubprocessError):
                raise AdapterError("workspace_out_of_scope") from None
    if not profile.context_only or not agents or set(agents) - set(AGENTS):
        raise AdapterError("invalid_context_binding")
    files: dict[str, str | None] = {}
    extra: dict[str, Any] = {}
    hooks_feature_added = False
    arguments = ["-m", "durumari_mcp", "context-serve", "--profile", name]
    for agent in agents:
        args = [*arguments, "--client", agent]
        if agent == "codex":
            path = inside(root, root / ".codex/config.toml")
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            current = remove_codex_block(current).rstrip() + "\n"
            current += "\n[mcp_servers.durumari_project]\ncommand = " + json.dumps(sys.executable)
            current += "\nargs = " + json.dumps(args) + "\nstartup_timeout_sec = 10\n"
            # Browser enrollment approves scoped read/write. History and reversible
            # version operations share one tool; trust only that bound Context tool.
            current += 'tools = { durumari_context_version = { approval_mode = "approve" } }\n'
            # Respect an explicitly disabled hook feature. Hooks are advisory.
            features = tomllib.loads(current).get("features", {})
            if "hooks" not in features:
                hooks_feature_added = True
                if "[features]" in current:
                    current = current.replace("[features]", "[features]\nhooks = true", 1)
                else:
                    current += "\n[features]\nhooks = true\n"
            files[".codex/config.toml"] = current
        elif agent == "opencode":
            path_name, _ = settings_path(root, agent)
            current_json = json_file(root, path_name)
            fmt, basis = opencode_config.choose_format(current_json, opencode_format, SERVER, owned)
            previous = opencode_config.take_entry(current_json, SERVER, owned)
            opencode_config.place_entry(
                current_json, SERVER, [sys.executable, *args], fmt, previous
            )
            files[path_name] = json_output(root, path_name, current_json)
            skill = inside(root, root / opencode_config.SKILL_PATH)
            text = skill.read_text(encoding="utf-8") if skill.exists() else None
            if text is None or opencode_config.SKILL_MARKER in text:
                files[opencode_config.SKILL_PATH] = opencode_config.packaged_skill()
                skill_state = "managed"
            else:
                skill_state = "kept_existing"  # Never overwrite a user's own skill.
            extra["opencode"] = {"config": fmt, "basis": basis, "skill": skill_state}
        else:
            path_name, key = settings_path(root, agent)
            current_json = json_file(root, path_name)
            servers = current_json.setdefault(key, {})
            if not isinstance(servers, dict):
                raise AdapterError("invalid_agent_configuration")
            if SERVER in servers and not owned(servers[SERVER]):
                raise AdapterError("existing_mcp_binding_conflict")
            if path_name == ".mcp.json" and {"claude", "copilot"} <= set(agents):
                # One shared entry serves both hosts; the label is only a local check.
                args = [*arguments, "--client", "mcp"]
            servers[SERVER] = {"command": sys.executable, "args": args}
            if path_name == ".mcp.json" and "copilot" in agents:
                # Claude Code and Copilot CLI (>= 0.0.370) both accept "stdio".
                servers[SERVER] = {"type": "stdio", **servers[SERVER]}
            files[path_name] = json_output(root, path_name, current_json)
        if agent in {"codex", "claude"}:
            path_name, contents = hook_settings(root, agent, name)
            files[path_name] = contents
    manifest = {
        "schema_version": 1,
        "server": profile.gateway_origin,
        "project_id": profile.project_id,
        "source_id": profile.source_id,
        "context_source": "server",
        "repository_id": profile.repository_id,
    }
    if profile.folder_source_id:
        manifest["context_mode"] = "cloud-context"
        manifest["folder_source_id"] = profile.folder_source_id
    # Keep the shared file's source_id once written: it names whichever connection
    # wrote it first, and rewriting it per teammate made commits flip it.
    # Older runtimes still require the key, so it stays in the file.
    try:
        from durumari_mcp.onboarding import read_manifest

        shared: dict[str, Any] = read_manifest(root) if manifest_path(root).exists() else {}
    except AdapterError:
        shared = {}
    if (
        isinstance(shared.get("server"), str)
        and same_server(shared["server"], profile.gateway_origin)
        and (shared.get("project_id"), shared.get("folder_source_id"))
        == (manifest["project_id"], manifest.get("folder_source_id"))
    ):
        # A teammate connecting through the moved app host must not flip the
        # committed server either.
        manifest["server"] = shared["server"]
        manifest["source_id"] = shared["source_id"]
    files[manifest_path(root).name] = yaml.safe_dump(manifest, sort_keys=False)
    # AGENTS.md is the one source; CLAUDE.md/GEMINI.md import it.
    current_instructions = {
        instruction: (
            path.read_text(encoding="utf-8")
            if (path := inside(root, root / instruction)).exists()
            else None
        )
        for instruction in instruction_files.INSTRUCTION_FILES
    }
    try:
        files.update(instruction_files.plan(current_instructions, agents, profile.project_id))
    except instruction_files.ManagedTextError:
        raise AdapterError("invalid_managed_instructions") from None
    ignores = inside(root, root / ".gitignore")
    current_ignore = ignores.read_text(encoding="utf-8") if ignores.exists() else ""
    missing = [pattern for pattern in IGNORES if pattern not in current_ignore.splitlines()]
    if missing:
        current_ignore = (
            current_ignore.rstrip() + "\n\n" + IGNORE_HEADER + "\n" + "\n".join(missing) + "\n"
        )
    files[".gitignore"] = current_ignore
    # Keep earlier agents of this profile so a split connection is removed as a whole.
    recorded = list(dict.fromkeys([*bound_agents(root, name), *agents]))
    try:
        previous_record = json_file(root, ".durumari/context-binding.json")
    except AdapterError:
        previous_record = {}  # Settings can be recovered; unknown creation ownership stays empty.
    same = previous_record.get("profile") == name
    created = created_files(previous_record) if same else set()
    created.update(path_name for path_name in files if not (root / path_name).exists())
    files[".durumari/context-binding.json"] = None  # Placeholder: its folder is ours too.
    directories = created_directories(previous_record) if same else set()
    for path_name in files:
        for parent in Path(path_name).parents:
            if parent != Path(".") and not (root / parent).exists():
                directories.add(parent.as_posix())
    added_ignores = set(ignore_lines(previous_record) if same else []) | set(missing)
    # Per-user values stay in this ignored record; durumari.yaml is shared by the team.
    # Removal undoes exactly what is recorded here: files, folders, ignore lines and the
    # Codex hooks feature this connection added (a Windows regression).
    files[".durumari/context-binding.json"] = (
        json.dumps(
            {
                "profile": name,
                "agents": recorded,
                "workspace_identity": profile.workspace_identity,
                "created_files": sorted(created),
                "created_directories": sorted(directories),
                "gitignore_added": [line for line in IGNORES if line in added_ignores],
                "codex_hooks_feature": hooks_feature_added
                or (same and previous_record.get("codex_hooks_feature") is True),
            }
        )
        + "\n"
    )
    for path_name in files:
        inside(root, root / path_name)
    if apply:
        write_files(root, files)
    return {
        "project_id": profile.project_id,
        "configured_agents": agents,
        "profile": name,
        **extra,
    }


def remove_binding(profile: ProjectProfile, name: str) -> dict[str, list[str]]:
    """Remove this grant's entries; report deleted and restored files for the user."""
    root = Path(profile.workspace)
    record = json_file(root, ".durumari/context-binding.json")
    if record.get("profile") != name:
        raise AdapterError("project_connection_scope_changed")
    created = created_files(record)
    files: dict[str, str | None] = {}
    for agent in bound_agents(root, name):
        if agent == "codex":
            path = inside(root, root / ".codex/config.toml")
            if path.exists():
                original = path.read_text(encoding="utf-8")
                text = remove_codex_block(original, profile=name)
                if text != original:
                    # Connect wrote the block after the stripped original and one blank line.
                    text = text.rstrip() + "\n" if text.strip() else ""
                # An older record has no flag: a file we created held no user feature.
                _, remaining_hooks = hook_settings(root, "codex", name, remove=True)
                needs_hooks = any(
                    group.get("hooks")
                    for groups in load_json(remaining_hooks).get("hooks", {}).values()
                    for group in groups
                )
                if not needs_hooks and (record.get("codex_hooks_feature") is True or (
                    "codex_hooks_feature" not in record and ".codex/config.toml" in created
                )):
                    text = remove_hooks_feature(text)
                files[".codex/config.toml"] = text
        elif agent == "opencode":
            path_name, _ = settings_path(root, agent)
            current = json_file(root, path_name)
            if opencode_config.take_entry(current, SERVER, lambda e: owned(e, name)):
                files[path_name] = json_output(root, path_name, current)
            skill = opencode_config.skill_removal(root)
            if skill:
                files[skill] = None
        elif agent in JSON_FILES:
            path_name, key = settings_path(root, agent)
            current = json_file(root, path_name)
            entry = current.get(key, {}).get(SERVER)
            if entry is not None and not owned(entry, name):
                raise AdapterError("existing_mcp_binding_conflict")
            current.get(key, {}).pop(SERVER, None)
            files[path_name] = json_output(root, path_name, current)
        if agent in {"codex", "claude"}:
            path_name, contents = hook_settings(root, agent, name, remove=True)
            files[path_name] = contents
    for instruction in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        path = inside(root, root / instruction)
        if path.exists():
            files[instruction] = re.sub(
                re.escape(START) + r".*?" + re.escape(END),
                "",
                path.read_text(encoding="utf-8"),
                flags=re.S,
            )
    # The shared manifest stays for the team; only a legacy copy of this grant's
    # per-user keys is taken out of it.
    path = inside(root, manifest_path(root))
    if path.exists():
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("profile") == name:
            for key in USER_KEYS:
                manifest.pop(key, None)
            files[path.name] = yaml.safe_dump(manifest, sort_keys=False)
    ignores = inside(root, root / ".gitignore")
    added = ignore_lines(record)
    if "gitignore_added" not in record and ".gitignore" in created:
        added = IGNORES  # An older record: the file we created held only our block.
    if added and ignores.exists() and not committed_ignore_block(root):
        text = ignores.read_text(encoding="utf-8")
        updated = remove_ignore_block(text, added)
        if updated != text:
            files[".gitignore"] = updated
    files[".durumari/context-binding.json"] = None
    for path_name, content in files.items():
        if path_name == manifest_path(root).name:
            continue  # Shared project metadata is never deleted by one user's removal.
        if path_name in created and content is not None and left_empty(path_name, content):
            files[path_name] = None  # Only our entries were there; user content keeps the file.
    write_files(root, files)
    directories = created_directories(record)
    if "created_directories" not in record:
        # An older record: only folders of files we created and just deleted.
        directories = {
            parent.as_posix()
            for path_name, content in files.items()
            if content is None and path_name in created | {".durumari/context-binding.json"}
            for parent in Path(path_name).parents
            if parent != Path(".")
        }
    emptied = []
    # Deepest first; a folder that holds anything else (a user file, shared Skills) stays.
    for directory in sorted(directories, key=lambda item: item.count("/"), reverse=True):
        path = inside(root, root / directory)
        try:
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
                emptied.append(directory + "/")
        except OSError:
            continue  # A leftover empty folder never fails the removal.
    return {
        "removed": sorted([path for path, content in files.items() if content is None] + emptied),
        "restored": sorted(path for path, content in files.items() if content is not None),
    }


def remove_hooks_feature(text: str) -> str:
    """Take out the `[features] hooks = true` line a connection added, and an emptied table."""
    lines = text.splitlines()
    try:
        header = next(i for i, line in enumerate(lines) if line.strip() == "[features]")
    except StopIteration:
        return text
    end = next(
        (i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    flag = next(
        (i for i in range(header + 1, end) if re.fullmatch(r"\s*hooks\s*=\s*true\s*", lines[i])),
        None,
    )
    if flag is None:
        return text
    del lines[flag]
    end -= 1
    if not any(line.strip() for line in lines[header + 1 : end]):
        del lines[header:end]
    updated = "\n".join(lines).strip()
    updated = updated + "\n" if updated else ""
    try:
        if "hooks" in tomllib.loads(updated).get("features", {}):
            return text
    except tomllib.TOMLDecodeError:
        return text
    return updated


def remove_ignore_block(text: str, added: list[str]) -> str:
    """Remove recorded lines from Durumari's .gitignore block; other lines stay."""
    lines = text.splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != IGNORE_HEADER:
            result.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        kept = [line for line in lines[index + 1 : end] if line.strip() not in added]
        if kept:
            result += [lines[index], *kept]
        else:
            while result and not result[-1].strip():
                result.pop()  # The blank line the connection put before its block.
        index = end
    if result == lines:
        return text
    return "\n".join(result).strip("\n") + "\n" if any(line.strip() for line in result) else ""


def committed_ignore_block(root: Path) -> bool:
    """A Durumari block committed to the repository is shared team metadata, not ours."""
    try:
        committed = subprocess.run(
            ["git", "-C", str(root), "show", "HEAD:.gitignore"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return committed.returncode == 0 and IGNORE_HEADER.encode() in committed.stdout


def left_empty(path_name: str, content: str) -> bool:
    if not content.strip():
        return True
    if not path_name.endswith((".json", ".jsonc")):
        return False
    try:
        if mask_json_comments(content) != content:
            return False  # Comments are user content, even when the parsed object is empty.
        value = load_json(content)
    except (ValueError, UnicodeError):
        return False

    # Only empty containers that our settings create are disposable. Explicit user
    # values such as permissions.allow=[] keep their file, even if we created it.
    return all(
        (key == "mcpServers" and item == {})
        or (key == "mcp" and item in ({}, {"servers": {}}))
        or (
            key == "hooks"
            and isinstance(item, dict)
            and all(
                event in {"SessionStart", "UserPromptSubmit", "Stop", "PreToolUse",
                          "PostCompact"} and groups == []
                for event, groups in item.items()
            )
        )
        for key, item in value.items()
    )


def upgrade_instructions(root: Path, name: str, project_id: str) -> list[str]:
    """Convert managed instruction files written by an older runtime; return changed names.

    Update contract, device layer: the runtime converts what an
    older version wrote when it runs. Only managed blocks change; a created AGENTS.md
    is recorded so that removing this connection also removes it.
    """
    current = {
        instruction: (
            path.read_text(encoding="utf-8")
            if (path := inside(root, root / instruction)).is_file()
            else None
        )
        for instruction in instruction_files.INSTRUCTION_FILES
    }
    try:
        changes: dict[str, str | None] = dict(instruction_files.upgrade(current, project_id))
    except instruction_files.ManagedTextError:
        return []  # Damaged markers stay for the user; connect reports them.
    if not changes:
        return []
    if current[instruction_files.SOURCE] is None:
        try:
            record = json_file(root, ".durumari/context-binding.json")
        except AdapterError:
            record = {}
        if record.get("profile") == name:
            record["created_files"] = sorted(created_files(record) | {instruction_files.SOURCE})
            changes[".durumari/context-binding.json"] = json.dumps(record) + "\n"
    write_files(root, changes)
    return sorted(change for change in changes if change in instruction_files.INSTRUCTION_FILES)
