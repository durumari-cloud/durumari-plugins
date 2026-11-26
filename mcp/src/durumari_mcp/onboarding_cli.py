"""Argument shapes of retired onboarding commands, kept so old calls get guidance.

project-init, -doctor, -preflight, -import and -export are retired in cli; their
implementations were removed (connection contract section 12).
"""

from typing import Any


def add_commands(commands: Any) -> None:
    init = commands.add_parser(
        "project-init", help="Preview or apply repository binding and host settings"
    )
    init.add_argument("--workspace", required=True)
    init.add_argument("--gateway-origin")
    init.add_argument("--project-id")
    init.add_argument("--source-id")
    init.add_argument(
        "--hosts", nargs="+", choices=["codex", "claude"], default=["codex", "claude"]
    )
    init.add_argument("--apply", action="store_true")
    init.add_argument("--for-migration", action="store_true")
    init.add_argument("--login", action="store_true")
    init.add_argument("--allow-external-context", action="store_true")
    init.add_argument("--create-project")
    init.add_argument("--workspace-id")
    for name in ("project-import", "project-export", "project-doctor", "project-preflight"):
        command = commands.add_parser(
            name, help="Reviewed project migration or connection diagnosis"
        )
        command.add_argument("--workspace", required=True)
        command.add_argument("--profile")
        command.add_argument("--client", choices=["codex", "claude"], default="codex")
        if name == "project-preflight":
            command.add_argument("--change", choices=["git-init", "move", "detach"], required=True)
            command.add_argument("--target-workspace")
        if name == "project-import":
            command.add_argument("--paths", nargs="+", default=["docs"])
            command.add_argument("--include-resources", action="store_true")
            command.add_argument("--reference-source-code", action="store_true")
            command.add_argument("--apply", action="store_true")
            command.add_argument("--allow-incomplete", action="store_true")
            command.add_argument("--bundle")
            command.add_argument("--output")
        if name == "project-export":
            command.add_argument("--output", required=True)
            command.add_argument("--local", action="store_true")
            command.add_argument("--paths", nargs="+", default=["docs"])
            command.add_argument("--include-resources", action="store_true")
