"""Account Skill copies: the bound folder when there is one, else a per-user folder.

An account is not a device: installing never enrolls one or grants credential access.
Without a folder binding the copy lives under the user's data folder, one directory per
server and project, so Git, durumari.yaml and AGENTS.md stay optional.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from durumari_mcp import local_upgrade
from durumari_mcp.context_removal import local_profile
from durumari_mcp.credentials import AdapterError
from durumari_mcp.onboarding import read_manifest
from durumari_mcp.project_credentials import load_project_profile
from durumari_mcp.server_aliases import identity, same_server
from durumari_mcp.skill_gateway import SkillGateway
from durumari_mcp.workspace_binding import binding_root


class AccountInstallation:
    def __init__(self, selected: Any, folder: SkillGateway) -> None:
        self.selected, self.folder = selected, folder
        self.profile, self.name, self.client = folder.profile, folder.name, folder.client
        self.root = folder.root

    def check(self) -> None:
        current = load_project_profile(self.name)
        current.check_client(self.client)
        current.check_workspace(str(Path.cwd().resolve()))
        manifest = read_manifest(Path(current.workspace))
        account = self.selected._account.profile
        if (
            current.disabled
            or (current.project_id, current.workspace, current.actor_id)
            != (self.selected.project_id, self.profile.workspace, account.actor_id)
            or not same_server(current.gateway_origin, account.gateway_origin)
            or not same_server(manifest["server"], current.gateway_origin)
            or manifest["project_id"] != current.project_id
        ):
            raise AdapterError("account_skill_folder_target_mismatch")

    def request(
        self, path: str, body: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.check()
        return dict(self.selected.request(path, body, **kwargs))


class UserInstallation:
    """The selected project's copies in the user's data folder, recorded by the account."""

    def __init__(self, selected: Any, client: str) -> None:
        account = selected._account.profile
        project_id = selected.project_id
        server = hashlib.sha256(identity(account.gateway_origin).encode()).hexdigest()[:16]
        root = local_upgrade.user_data_directory() / "account-skills" / server / project_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.selected, self.name, self.client, self.root = selected, selected.name, client, root
        self.profile = SimpleNamespace(project_id=project_id, workspace=str(root))

    def request(
        self, path: str, body: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        return dict(self.selected.request(path, body, **kwargs))


def local_gateway(selected: Any) -> AccountInstallation | UserInstallation:
    root = binding_root(Path.cwd().resolve())
    name = local_profile(root)
    client = "claude" if selected.client == "claude-code" else selected.client
    if name is None:
        return UserInstallation(selected, client)
    folder = SkillGateway(name, client)
    result = AccountInstallation(selected, folder)
    result.check()
    return result
