"""Browser approval followed by a resumable user-scoped host registration."""

import json
import sys
from typing import Any

from durumari_mcp.account_setup import CLIENTS, configure, preflight
from durumari_mcp.credentials import AdapterError, Profile


def add_commands(subcommands: Any) -> None:
    parser = subcommands.add_parser(
        "account-setup", help="Connect and register independent account chats"
    )
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--server", default="https://app.durumari.cloud")
    parser.add_argument("--profile")
    parser.add_argument("--no-browser", action="store_true")
    migration = subcommands.add_parser(
        "account-migrate", help="Plan, inspect, apply or recover an explicit folder transition"
    )
    migration.add_argument("action", choices=("plan", "status", "apply", "rollback"))
    migration.add_argument("--client", choices=sorted(CLIENTS), required=True)
    migration.add_argument("--profile", required=True)
    migration.add_argument("--workspace")
    migration.add_argument("--migration-id")


def setup(args: Any) -> dict[str, Any]:
    # Parsing the CLI must not import the account MCP client.
    from durumari_mcp import account_cli

    try:
        try:
            origin = Profile.trusted_origin(args.server)
        except ValueError:
            raise AdapterError("invalid_gateway_origin") from None
        profile: str = account_cli.connection_profile(args.client, origin, args.profile)
        preflight(args.client, profile, origin)
        connected: dict[str, Any] = account_cli.connect(args)
        if connected.get("state") != "active":
            return connected
        result = configure(args.client, profile, origin)
        policy = result.get("runtime", {}).get("policy", {})
        if policy.get("capability") == "legacy_advisory":
            print(
                json.dumps(
                    {
                        "warning": "native_required_policy_unsupported",
                        "update_url": policy.get("update_url"),
                    }
                ),
                file=sys.stderr,
            )
        return result
    except AdapterError as error:
        return {
            "state": "action_required",
            "error": {"code": str(error)},
            "next_action": "resolve_configuration_or_update_then_repeat_account_setup",
        }
    except OSError:
        return {
            "state": "action_required",
            "error": {"code": "account_setup_filesystem_recovery_required"},
            "next_action": "check_permissions_and_repeat_account_setup",
        }


def execute(args: Any) -> None:
    if args.command == "account-migrate":
        from pathlib import Path

        from durumari_mcp import account_migration

        if args.action == "plan":
            if not args.workspace or args.migration_id:
                raise AdapterError("account_migration_explicit_workspace_required")
            result = account_migration.plan(Path(args.workspace), args.client, args.profile)
        else:
            if not args.migration_id or args.workspace:
                raise AdapterError("account_migration_id_required")
            result = getattr(account_migration, args.action)(
                args.migration_id, args.client, args.profile
            )
        print(json.dumps(result, ensure_ascii=False))
        return
    print(json.dumps(setup(args), ensure_ascii=False))
