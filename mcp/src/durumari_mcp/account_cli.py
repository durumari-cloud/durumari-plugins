"""Browser-approved native account setup without any folder configuration changes."""

import hashlib
import json
import secrets
import sys
import time
import webbrowser
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import SecretStr

from durumari_mcp.account_client import AccountContextClient, reference
from durumari_mcp.account_credentials import (
    CLIENT_FAMILIES,
    AccountProfile,
    PendingEnrollment,
    delete_pending,
    load_account_profile,
    load_pending,
    profile_from_response,
    save_account_profile,
    save_pending,
)
from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.credentials import AdapterError, Profile, profile_name
from durumari_mcp.feature_tools import HiddenFeatures, hide_unavailable
from durumari_mcp.project_gateway import ContextFailure, ProjectTransport
from durumari_mcp.project_outbox import process_lock, state_root
from durumari_mcp.server_aliases import identity, request_origin, same_server
from durumari_mcp.stdio_serve import serve_stdio


def add_commands(subcommands: Any) -> None:
    connect = subcommands.add_parser(
        "account-connect", help="Connect your account for independent chats"
    )
    connect.add_argument("--client", choices=sorted(CLIENT_FAMILIES), required=True)
    connect.add_argument("--server", default="https://app.durumari.cloud")
    connect.add_argument("--profile")
    connect.add_argument("--no-browser", action="store_true")
    serve = subcommands.add_parser(
        "account-serve", help="Run account MCP with per-conversation targets"
    )
    serve.add_argument("--client", choices=sorted(CLIENT_FAMILIES), required=True)
    serve.add_argument("--profile", required=True)


def _existing(name: str, origin: str, client: str) -> AccountProfile | None:
    try:
        profile = load_account_profile(name)
    except AdapterError as error:
        if str(error) == "account_credential_missing":
            return None
        raise
    profile.check_client(client)
    if not same_server(profile.gateway_origin, origin):
        raise AdapterError("account_profile_scope_changed")
    return profile


def _approval(value: dict[str, Any], origin: str) -> tuple[str, datetime]:
    try:
        request_id = reference(value["request_id"])
        url = urljoin(request_origin(origin) + "/", value["approval_url"])
        parsed = urlsplit(url)
        if (
            not same_server(f"{parsed.scheme}://{parsed.netloc}", origin)
            or parsed.path != "/connect/account"
            or parsed.fragment
            or parse_qs(parsed.query) != {"request": [request_id]}
            or value["interval_seconds"] != 2
            or type(value["interval_seconds"]) is not int
        ):
            raise ValueError
        expiry = datetime.fromisoformat(value["expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError
        return request_origin(origin) + "/connect/account?request=" + request_id, expiry
    except Exception:
        raise AdapterError("invalid_account_enrollment") from None


def _step(name: str, origin: str, client: str) -> dict[str, Any]:
    existing = _existing(name, origin, client)
    if existing is not None:
        try:
            result = AccountContextClient(name, client).status()
            delete_pending(name)
            return {**result, "profile": name, "connection_id": existing.connection_id}
        except ContextFailure as error:
            if error.status not in {401, 403, 410}:
                raise
        except AdapterError as error:
            if str(error) != "account_reconnect_required":
                raise
    pending = load_pending(name)
    if pending is None:
        pending = PendingEnrollment(
            gateway_origin=origin,
            client=client,
            device_code=SecretStr("mcd_" + secrets.token_urlsafe(32)),
            request_key="account-enroll-" + str(uuid4()),
        )
        # This durable write precedes the first network side effect. Unknown starts
        # replay the same proof/key and cannot enroll another device accidentally.
        save_pending(name, pending)
    if not same_server(pending.gateway_origin, origin) or pending.client != client:
        raise AdapterError("account_profile_scope_changed")
    transport = ProjectTransport(origin)
    try:
        if pending.request_id is None:
            value = transport.request(
                "/api/v2/agent/account-enrollments",
                body={
                    "client": client,
                    "device_code": pending.device_code.get_secret_value(),
                    "request_key": pending.request_key,
                },
            )
            approval_url, expiry = _approval(value, origin)
            pending = pending.model_copy(
                update={
                    "request_id": value["request_id"],
                    "expires_at": expiry,
                }
            )
            save_pending(name, pending)
        else:
            assert pending.expires_at is not None
            approval_url = request_origin(origin) + "/connect/account?request=" + pending.request_id
            expiry = pending.expires_at
        result = transport.request(
            "/api/v2/agent/account-enrollments/poll",
            body={
                "device_code": pending.device_code.get_secret_value(),
            },
        )
    except ContextFailure as error:
        if error.status == 410:
            delete_pending(name)
            raise AdapterError("account_enrollment_expired_retry") from None
        raise
    if result.get("status") == "approved":
        key = (
            existing.outbox_key.get_secret_value()
            if existing is not None
            else Fernet.generate_key().decode()
        )
        profile = profile_from_response(result, origin, client, key)
        save_account_profile(name, profile)
        # Only after verified OS-store persistence may the recoverable proof disappear.
        delete_pending(name)
        return {
            "state": "active",
            "profile": name,
            "connection_id": profile.connection_id,
            "scope_kind": "account",
            "client": client,
            "server": origin,
        }
    if result.get("status") == "denied":
        delete_pending(name)
        return {"state": "denied", "profile": name, "next_action": "run_account_connect_again"}
    if result.get("status") != "pending":
        raise AdapterError("invalid_account_response")
    return {
        "state": "pending",
        "profile": name,
        "approval_url": approval_url,
        "expires_at": expiry.isoformat(),
        "next_action": "approve_then_run_same_command",
    }


def connection_profile(client: str, origin: str, requested: str | None = None) -> str:
    """Keep the same client/origin default across connect and host setup."""
    server = identity(origin).encode()
    return profile_name(
        requested or ("account-" + client + "-" + hashlib.sha256(server).hexdigest()[:12])
    )


def connect(args: Any) -> dict[str, Any]:
    try:
        origin = Profile.trusted_origin(args.server)
    except ValueError:
        raise AdapterError("invalid_gateway_origin") from None
    if args.client not in CLIENT_FAMILIES:
        raise AdapterError("account_client_mismatch")
    name = connection_profile(args.client, origin, args.profile)
    opened = False
    deadline = time.monotonic() + 600
    expired_retries = 0
    while True:
        try:
            with process_lock(state_root() / "accounts" / (name + ".enrollment")):
                result = _step(name, origin, args.client)
        except AdapterError as error:
            if str(error) == "account_enrollment_expired_retry" and expired_retries == 0:
                expired_retries += 1
                opened = False
                continue
            raise
        if result["state"] != "pending" or args.no_browser:
            return result
        if not opened:
            print(
                "브라우저에서 계정 연결을 승인해 주세요: " + result["approval_url"], file=sys.stderr
            )
            try:
                opened = webbrowser.open(result["approval_url"])
            except Exception:
                opened = False
            if not opened:
                return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(2)


def execute(args: Any) -> None:
    if args.command == "account-connect":
        print(json.dumps(connect(args), ensure_ascii=False))
        return
    if args.command == "account-serve":
        from durumari_mcp.account_migration import register

        client = AccountContextClient(args.profile, args.client)
        server = create_context_server(client)
        register(server, client)
        hide_unavailable(server, HiddenFeatures(client.release))
        serve_stdio(server)
        return
    raise AdapterError("invalid_command_arguments")
