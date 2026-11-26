"""One explicitly scoped exploration, using the normal authorized Context reads."""

from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field


def explore(target: Any, ids: list[str], query: str) -> dict[str, Any]:
    if not ids and len(query.strip()) < 2:
        raise ToolError("exploration_requires_seed_or_query")
    candidates = target.read("/search", q=query, budget_tokens=1500) if not ids else None
    hits = [row["id"] for row in (candidates or {}).get("items", [])[:3]]
    seeds = list(dict.fromkeys(ids or hits))
    events: list[dict[str, Any]] = []
    project: str | None = None
    skipped_project = False

    def emit(kind: str, **payload: Any) -> None:
        events.append({"event_id": str(uuid4()), "sequence": len(events) + 1,
                       "type": kind, **payload})

    for seed in seeds:
        source = target.read("/" + seed, budget_tokens=1500, view="document")
        if (not isinstance(source, dict) or source.get("id") != seed
                or not isinstance(source.get("project_id"), str)
                or type(source.get("revision")) is not int or source["revision"] < 1):
            raise ToolError("invalid_exploration_read")
        if project is not None and source["project_id"] != project:
            if not ids:
                skipped_project = True
                continue
            raise ToolError("exploration_scope_changed")
        project = source["project_id"]
        emit("read", document=source)
        for neighbour in source.get("neighbors", [])[:12]:
            if (not isinstance(neighbour, dict) or not isinstance(neighbour.get("id"), str)
                    or type(neighbour.get("revision")) is not int):
                continue
            emit("discovered", document={"id": neighbour["id"], "project_id": project,
                 "revision": neighbour["revision"], "title": neighbour.get("title", "Context"),
                 "sections": []})
            relation = neighbour.get("relation")
            if isinstance(relation, str) and relation:
                incoming = neighbour.get("direction") == "in"
                emit("relation", edge={"from": neighbour["id"] if incoming else seed,
                     "to": seed if incoming else neighbour["id"],
                     "kind": "document", "label": relation[:200]})
    if project is None:
        # Empty search has no authorized project identity to invent a scope from.
        return {"schema_version": 1, "outcome": "empty", "events": [],
                "message": "관련 Context를 찾지 못했습니다."}
    scope = {"session_ref": str(uuid4()), "workspace_id": str(uuid4()), "project_id": project}
    return {"schema_version": 1, "scope": scope,
            "events": [{"scope": scope, **event} for event in events],
            "partial": skipped_project or bool(candidates and candidates.get("truncated"))}


def register_exploration_tools(targeted: Callable[..., Any], register: Any) -> None:
    from durumari_mcp.activity_ui import UI_META
    from durumari_mcp.cloud_protocol import Id, Reference, safe

    def durumari_context_explore(
        context_ids: Annotated[list[Id] | None, Field(max_length=8)] = None,
        query: Annotated[str, Field(max_length=200)] = "",
        project_ref: Reference | None = None,
        view: Literal[
            "explore", "synthesis", "impact", "decision", "resume", "report", "meeting"
        ] = "explore",
    ) -> Any:
        """Open a progressively expanding graph for collecting several Context documents.

        Specify seed context_ids or a short query. Uses current read permission and
        returns bounded original reads once, with discovered/read and actual relations
        distinguished. Each call starts its own workspace; it never borrows another
        chat's state. Follow nodes inside the view to expand this exploration.
        """
        result = safe(lambda: explore(targeted(project_ref), context_ids or [], query))
        if not result.is_error:
            result.meta = {**(result.meta or {}), "exploration": {
                "schema_version": 1, "project_ref": project_ref}}
            if view != "explore":
                result.meta["domain"] = {"tool": "durumari_context_explore",
                                         "view": view, "project_ref": project_ref}
        return result

    register(durumari_context_explore, title="Explore Context connections", read=True, meta=UI_META)
