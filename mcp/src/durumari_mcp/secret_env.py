"""`secret run` and `secret env`: values reach a child environment or a private file only.

`run` injects the referenced secrets as environment variables of one child process
(no shell), streams its stdout and stderr through a real-time redactor that masks the
injected secret values (and their base64/URL/JSON encodings), prints nothing itself and
exits with the child's code. `env` renders a `.env.durumari` template, where a value
`durumari://NAME` names a secret, into a `.env` file that git ignores, created with
owner-only access (0600, or an owner-only ACL on Windows) and replaced atomically,
refusing recovery/runtime or policy-forbidden secrets.
"""

import json
import os
import re
import secrets as random
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from durumari_mcp import plugin_env
from durumari_mcp.credentials import AdapterError
from durumari_mcp.private_files import protect_file
from durumari_mcp.secret_redact import StreamRedactor, pump_stream
from durumari_mcp.secret_use import LABEL, SecretUseError, Target

TEMPLATE = ".env.durumari"
OUTPUT = ".env"
REFERENCE = "durumari://"
MAX_TEMPLATE_BYTES = 256_000
ENV_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,127}"
LINE = re.compile(
    r"(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_.]{0,127})\s*=\s*"
    r"(?P<quote>[\"']?)durumari://(?P<name>" + LABEL + r")(?P=quote)\s*(?:#.*)?"
)
PLAIN = re.compile(r"\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.]{0,127})\s*=(?P<value>.*)")
SAFE_VALUE = re.compile(r"[A-Za-z0-9_./:@+,=-]*")


@dataclass(frozen=True)
class Line:
    text: str
    key: str | None = None
    name: str | None = None
    prefix: str = ""


def reserved(name: str) -> bool:
    # Values never change how a child is loaded or how this tool finds its state. Only the
    # plugin's own DURUMARI_MCP_* names (and their earlier spellings) are reserved; backend
    # DURUMARI_* settings are ordinary names.
    upper = name.upper()
    return (
        upper in {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT"}
        or upper.startswith(("LD_", "DYLD_"))
        or plugin_env.reserved(name)
    )


def env_name(name: str) -> str:
    if not re.fullmatch(ENV_NAME, name) or reserved(name):
        raise SecretUseError("invalid_env_name", [name])
    return name


def parse_template(path: Path) -> list[Line]:
    try:
        if path.is_symlink() or not path.is_file():
            raise AdapterError("secret_template_missing")
        data = path.read_bytes()
    except OSError:
        raise AdapterError("secret_template_missing") from None
    if len(data) > MAX_TEMPLATE_BYTES:
        raise AdapterError("secret_template_too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("secret_template_not_utf8") from None
    lines = []
    for number, raw in enumerate(text.splitlines(), start=1):
        found = LINE.fullmatch(raw)
        if found:
            lines.append(Line(raw, found["key"], found["name"], found["prefix"]))
        elif REFERENCE in raw:
            # A reference must be the whole value; partial use would hide where it went.
            raise SecretUseError("invalid_template_line", [f"line {number}"])
        else:
            lines.append(Line(raw))
    return lines


def plain_values(lines: list[Line]) -> dict[str, str]:
    """Non-reference `KEY=value` lines, for `run`; quotes are removed, nothing expanded."""
    values = {}
    for line in lines:
        if line.name is not None:
            continue
        stripped = line.text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found = PLAIN.fullmatch(line.text)
        if not found:
            continue
        value = found["value"].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        key = found["key"]
        if reserved(key):
            raise SecretUseError("invalid_env_name", [key])
        # Dotted keys suit some .env readers but are not process environment names.
        if re.fullmatch(ENV_NAME, key):
            values[key] = value
    return values


def mapping(pairs: list[str]) -> dict[str, str]:
    """`--env VAR` (variable named like the secret) or `--env VAR=NAME`."""
    result: dict[str, str] = {}
    for pair in pairs:
        variable, _, name = pair.partition("=")
        name = name or variable
        env_name(variable)
        if not re.fullmatch(LABEL, name):
            raise SecretUseError("invalid_secret_name", [name])
        if variable in result:
            raise SecretUseError("duplicate_env_name", [variable])
        result[variable] = name
    return result


def quote(value: str) -> str:
    """dotenv quoting: bare when safe, single quotes when literal, else escaped doubles."""
    if SAFE_VALUE.fullmatch(value):
        return value
    if "'" not in value and "\n" not in value and "\r" not in value:
        return "'" + value + "'"
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    )
    return '"' + escaped + '"'


def render(lines: list[Line], values: dict[str, str]) -> str:
    out = []
    for line in lines:
        if line.name is None:
            out.append(line.text)
        else:
            out.append(f"{line.prefix}{line.key}={quote(values[line.name])}")
    return "\n".join(out) + "\n"


# git ------------------------------------------------------------------------------------


def git(directory: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    executable = shutil.which("git")
    if executable is None:
        return None
    try:
        return subprocess.run(
            [executable, "-C", str(directory), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def inside_repository(directory: Path) -> bool:
    return any((parent / ".git").exists() for parent in (directory, *directory.parents))


def check_ignored(output: Path) -> str:
    """`ignored` or `no_repository`; a file git would commit is refused."""
    directory = output.parent
    probe = git(directory, "rev-parse", "--is-inside-work-tree")
    if probe is None:
        if inside_repository(directory):
            raise AdapterError("gitignore_check_unavailable")
        return "no_repository"
    if probe.returncode != 0 or probe.stdout.strip() != b"true":
        if inside_repository(directory):
            raise AdapterError("gitignore_check_unavailable")
        return "no_repository"
    tracked = git(directory, "ls-files", "--error-unmatch", "--", output.name)
    if tracked is None:
        raise AdapterError("gitignore_check_unavailable")
    if tracked.returncode == 0:
        raise AdapterError("env_tracked_by_git")
    ignored = git(directory, "check-ignore", "-q", "--", output.name)
    if ignored is None or ignored.returncode not in {0, 1}:
        raise AdapterError("gitignore_check_unavailable")
    if ignored.returncode != 0:
        raise AdapterError("env_not_gitignored")
    return "ignored"


# .env output ------------------------------------------------------------------------


def confirm_overwrite(path: Path) -> bool:
    stdin, stderr = sys.stdin, sys.stderr
    if stdin is None or not stdin.isatty() or stderr is None or not stderr.isatty():
        return False
    print(f"두루마리: {path.name} 파일이 이미 있습니다. 덮어쓸까요? [y/N] ", end="", file=stderr)
    stderr.flush()
    try:
        answer = stdin.readline()
    except (OSError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes", "예", "네"}


def write_private(path: Path, text: str) -> None:
    temporary = path.with_name("." + path.name + ".durumari-" + random.token_hex(6) + ".tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
    except OSError:
        raise AdapterError("env_write_failed") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            # Owner-only access applies while the file is still empty.
            protect_file(temporary)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        if sys.platform != "win32":
            os.chmod(path, 0o600)
    except AdapterError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError:
        temporary.unlink(missing_ok=True)
        raise AdapterError("env_write_failed") from None


def prepare_output(path: Path, *, force: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise AdapterError("env_output_unsafe") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AdapterError("env_output_unsafe")
    if not force and not confirm_overwrite(path):
        raise AdapterError("env_exists_confirm_required")


# Commands ---------------------------------------------------------------------------


def run_child(
    command: list[str],
    environment: dict[str, str],
    *,
    redact_values: Collection[str] | None = None,
) -> int:
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise AdapterError("run_command_required")
    executable = shutil.which(command[0], path=environment.get("PATH"))
    if executable is None:
        raise AdapterError("run_command_not_found")
    stdout_redactor = StreamRedactor(redact_values) if redact_values else None
    redacting = stdout_redactor is not None and stdout_redactor.active
    stderr_redactor = StreamRedactor(redact_values) if redacting and redact_values else None
    try:
        if redacting:
            process = subprocess.Popen(
                [executable, *command[1:]],
                env=environment,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            process = subprocess.Popen([executable, *command[1:]], env=environment, shell=False)
    except OSError:
        raise AdapterError("run_command_failed") from None
    threads: list[threading.Thread] = []
    if redacting and stdout_redactor is not None and stderr_redactor is not None:
        assert process.stdout is not None and process.stderr is not None
        for source, sink, redactor in (
            (process.stdout, sys.stdout, stdout_redactor),
            (process.stderr, sys.stderr, stderr_redactor),
        ):
            worker = threading.Thread(
                target=pump_stream, args=(source, sink, redactor), daemon=True
            )
            worker.start()
            threads.append(worker)
    while True:
        try:
            code = process.wait()
            break
        except KeyboardInterrupt:
            # The terminal delivers the interrupt to the child too; report its own exit.
            continue
    for worker in threads:
        worker.join()
    # A child ended by a signal exits like a shell would report it: 128 + signal.
    return 128 - code if code < 0 else code


def run_target(workspace: Path, command: list[str]) -> Target:
    args = command[1:] if command[:1] == ["--"] else command
    if not args:
        raise AdapterError("run_command_required")
    name = Path(args[0]).name
    if sys.platform == "win32":
        name = re.sub(r"\.(exe|cmd|bat|com)$", "", name.lower())
    return Target.of("run", str(workspace), name)


def env_target(workspace: Path, output: Path) -> Target:
    try:
        relative = output.relative_to(workspace).as_posix()
    except ValueError:
        relative = output.as_posix()
    return Target.of("env", str(workspace), relative)


def references(args: Any, workspace: Path) -> tuple[dict[str, str], list[Line]]:
    """Variables to secret names for `run`, from --env pairs and/or the template."""
    pairs = mapping(args.env or [])
    lines: list[Line] = []
    template = Path(args.template) if args.template else None
    if template is None and not pairs and (workspace / TEMPLATE).is_file():
        template = workspace / TEMPLATE
    if template is not None:
        lines = parse_template(template if template.is_absolute() else Path.cwd() / template)
        for line in lines:
            if line.name is not None and line.key is not None:
                variable = env_name(line.key)
                pairs.setdefault(variable, line.name)
    if not pairs:
        raise AdapterError("secret_refs_required")
    return pairs, lines


def summary(names: list[str], *, cached: list[str], offline: bool) -> str:
    source = "오프라인 캐시" if offline else f"캐시 {len(cached)}개"
    return f"비밀값 {len(names)}개({', '.join(sorted(names))}), {source}"


def execute_run(args: Any, gateway: Any, cache: Any, workspace: Path) -> None:
    from durumari_mcp.secret_use import resolve

    pairs, lines = references(args, workspace)
    target = run_target(workspace, args.child_command)
    resolved = resolve(gateway, cache, list(pairs.values()), target, offline=args.offline)
    secret_values = list(resolved.values.values())
    environment = dict(os.environ)
    environment.update(plain_values(lines))
    for variable, name in pairs.items():
        environment[variable] = resolved.values[name]
    resolved.values.clear()
    if args.verbose:
        print(
            "두루마리: "
            + summary(list(set(pairs.values())), cached=resolved.cached, offline=resolved.offline),
            file=sys.stderr,
        )
    try:
        code = run_child(args.child_command, environment, redact_values=secret_values)
    finally:
        secret_values.clear()
        environment.clear()
    raise SystemExit(code)


def execute_env(args: Any, gateway: Any, cache: Any, workspace: Path) -> None:
    from durumari_mcp.secret_use import resolve

    template = Path(args.template or workspace / TEMPLATE)
    template = template if template.is_absolute() else Path.cwd() / template
    output = Path(args.output or workspace / OUTPUT)
    output = output if output.is_absolute() else Path.cwd() / output
    if output.resolve(strict=False) == template.resolve(strict=False):
        raise AdapterError("env_output_is_template")
    if not output.parent.is_dir():
        raise AdapterError("env_output_unsafe")
    lines = parse_template(template)
    for line in lines:
        if line.key is not None and reserved(line.key):
            raise SecretUseError("invalid_env_name", [line.key])
    names = sorted({line.name for line in lines if line.name is not None})
    ignored = check_ignored(output)
    prepare_output(output, force=args.force)
    values: dict[str, str] = {}
    cached: list[str] = []
    offline = False
    if names:
        resolved = resolve(
            gateway, cache, names, env_target(workspace, output), offline=args.offline
        )
        values, cached, offline = resolved.values, resolved.cached, resolved.offline
    text = render(lines, values)
    values.clear()
    try:
        write_private(output, text)
    finally:
        del text
    receipt = {
        "status": "written",
        "path": str(output),
        "references": len(names),
        "names": names,
        "gitignore": ignored,
        "cached": sorted(cached),
        "offline": offline,
    }
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        mode = "소유자 전용" if sys.platform == "win32" else "600"
        print(f"생성됨: {output.name} (두루마리 참조 {len(names)}개, 권한 {mode})")
        if ignored == "no_repository":
            print("git 저장소 밖이라 gitignore 검사를 건너뛰었습니다.", file=sys.stderr)
