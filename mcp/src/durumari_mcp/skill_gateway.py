"""Explicit native Skill operations use the current repository-bound credential."""

from typing import Any

from durumari_mcp.project_gateway import ProjectGateway


class SkillGateway(ProjectGateway):
    def request(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        action: str | None = None,
        method: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.context_request(
            path,
            body,
            method=method or ("POST" if body is not None else "GET"),
            idempotency_key=idempotency_key,
        )
