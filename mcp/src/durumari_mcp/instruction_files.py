"""AGENTS.md is the one instruction source; CLAUDE.md and GEMINI.md only import it.

Codex, Cursor, Copilot and OpenCode read AGENTS.md; Claude Code
reads CLAUDE.md and Gemini CLI reads GEMINI.md, so those two hold the managed block
with a single import line and nothing copied. Only the text between the managed
markers is ours; everything else in a user's file is kept as written.

Standard library only: the repository's CI loads this file by path to check that the
managed text was not edited by hand (tools.delivery.agent_guard).
"""

import re
from pathlib import Path

START, END = "<!-- DURUMARI MANAGED BEGIN -->", "<!-- DURUMARI MANAGED END -->"
# Earlier markers that a managed block may still carry (durumari_mcp.legacy).
LEGACY_MARKERS = {
    "<!-- MALMOA MANAGED BEGIN -->": START,
    "<!-- MALMOA MANAGED END -->": END,
}
SOURCE = "AGENTS.md"
IMPORTERS = {"claude": "CLAUDE.md", "gemini": "GEMINI.md"}
INSTRUCTION_FILES = (SOURCE, *IMPORTERS.values())
IMPORT_LINE = "@" + SOURCE
_IMPORT = re.compile(r"(?m)^[ \t]*@(?:\./)?AGENTS\.md[ \t]*$")
CURRENT_CONTEXT_CONTRACT = (
    "Before an answer, plan or action that depends on current space facts, decisions, "
    "resources or task status, search server Context and read the relevant current sections. "
    "Bind read evidence to this space, session and turn, including section revision and "
    "sha256. Search snippets, binding status, local files, caches, memory and another chat's "
    "reads do not replace current reads. Treat pinned revisions as historical evidence; "
    "distinguish historical text from current decisions. Keep direct code/device observations "
    "distinct from server plans. Revalidate after resume or compaction and before dependent "
    "actions. If lookup fails, is unauthorized, returns no result or has uncertain relevance, "
    "state that limitation and withhold dependent conclusions and actions; never substitute "
    "stale values. Instructions and installed hooks alone do not prove enforcement: report "
    "only verified host protection and its limits."
)


class ManagedTextError(ValueError):
    """The managed markers are damaged; the file is left for the user to repair."""


def agents_block(project_id: str) -> str:
    """Durumari's default rules only: a contract that never changes per team or user.

    Values that change (response language, team rules) arrive with the session-start
    injection, so committed files never churn and no team's rules leak into another's.
    """
    return (
        START
        + "\n## Durumari Cloud Context\n\nProject: "
        + project_id
        + "\n\n"
        + "- This project is a folder hint, not an account access boundary. When account "
        "tools are available, use the person's explicit target or search authorized "
        "Context with durumari_account_context_search. Pass target_project_id from "
        "the selected result to Context tools, or keep an existing conversation's "
        "project_ref; never pass both. Changing folders, Git remotes or this hint "
        "does not change account permissions.\n"
        + "- The Durumari MCP connection's server instructions and tool descriptions "
        "carry the Context, Policy, Skill and request rules; follow them. Folder hooks "
        "inject this project's rules (Policy index), response language and Context at "
        "session start and after compaction. Injected values take precedence over local "
        "docs, memory and instruction files; when they differ, tell the user what differs.\n"
        + "- If the selected connection is disconnected, has only cached rules or cannot "
        "read current Context, tell the user before dependent work. An absent folder "
        "hook notice alone does not mean account MCP is disconnected. Without MCP, use "
        "the approved workspace's durumari-mcp context search/list/read CLI and the "
        "durumari-context skill; never substitute local memory. Without a response "
        "language, answer in the language of the user's latest message.\n"
        + "- " + CURRENT_CONTEXT_CONTRACT + "\n"
        + "- Regenerate Durumari's managed blocks and managed settings files only "
        "through their generation tools; do not edit them by hand.\n"
        + "- Keep one Context tagged work-log per task: current Agent, goal and "
        "remaining steps in the summary, followed by an append-only progress log. "
        "Follow the durumari-context skill's work-log format.\n"
        + "- Before using an external tool, plugin or skill, check the project Policy "
        "index (shown at session start, or durumari_policy_index). When a Policy's "
        "when-line matches the work, read that Policy first and follow it.\n"
        + "- If a Policy conflicts with the user's request or the tool's own usage "
        "rules, ask the user before acting. Agent reports do not constitute human "
        "approval or independent verification.\n"
        + "- Keep generated Skill copies (.durumari/skills/<name>) and local state out "
        "of Git; keep authored Skill sources and shared project declarations versioned. "
        "Credentials and device private keys stay in the approved OS secure store, "
        "outside project Skill files.\n" + END
    )


def import_block(*, with_import: bool = True) -> str:
    """CLAUDE.md/GEMINI.md block: the import, unless the file already imports it."""
    note = "<!-- AGENTS.md holds this project's instructions; Durumari keeps this block. -->"
    return START + "\n" + note + "\n" + (IMPORT_LINE + "\n" if with_import else "") + END


def normalized(text: str) -> str:
    for old, new in LEGACY_MARKERS.items():
        text = text.replace(old, new)
    return text


def split(text: str) -> tuple[str, str | None, str]:
    """(before, block or None, after); damaged markers raise instead of guessing."""
    text = normalized(text)
    if START not in text and END not in text:
        return text, None, ""
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
        raise ManagedTextError("invalid_managed_instructions")
    begin, end = text.index(START), text.index(END) + len(END)
    return text[:begin], text[begin:end], text[end:]


def replace_block(text: str, block: str) -> str:
    before, current, after = split(text)
    if current is None:
        return (before.rstrip() + "\n\n" if before.strip() else "") + block + "\n"
    return before + block + after


def imports_outside(text: str) -> bool:
    before, _, after = split(text)
    return bool(_IMPORT.search(before) or _IMPORT.search(after))


def importer_text(text: str) -> str:
    return replace_block(text, import_block(with_import=not imports_outside(text)))


def plan(current: dict[str, str | None], agents: list[str], project_id: str) -> dict[str, str]:
    """New text for each instruction file the selected hosts need, or already managed.

    ``current`` maps each of INSTRUCTION_FILES to its text (None when missing).
    AGENTS.md always carries the full block so the importing files never point at a
    missing source. An unchanged file is still returned; the writer skips it.
    """
    wanted = {IMPORTERS[agent] for agent in agents if agent in IMPORTERS}
    result = {SOURCE: replace_block(current.get(SOURCE) or "", agents_block(project_id))}
    for name in IMPORTERS.values():
        text = current.get(name)
        if name in wanted or (text is not None and split(text)[1] is not None):
            result[name] = importer_text(text or "")
    return result


def upgrade(current: dict[str, str | None], project_id: str) -> dict[str, str]:
    """Run-time conversion of an existing binding: only files already managed change.

    Older runtimes copied the full block into CLAUDE.md/GEMINI.md. Those now import
    AGENTS.md; AGENTS.md gains the block when an importing file needs it.
    """
    managed = {name for name, text in current.items() if text and split(text)[1] is not None}
    if not managed:
        return {}
    changes: dict[str, str] = {}
    importers = managed & set(IMPORTERS.values())
    if SOURCE in managed or importers:
        source = current.get(SOURCE) or ""
        desired = replace_block(source, agents_block(project_id))
        if desired != source:
            changes[SOURCE] = desired
    for name in importers:
        text = current[name] or ""
        desired = importer_text(text)
        if desired != text:
            changes[name] = desired
    return changes


def read(root: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in INSTRUCTION_FILES:
        path = root / name
        result[name] = path.read_text(encoding="utf-8") if path.is_file() else None
    return result


def problems(current: dict[str, str | None], project_id: str, *, whole: bool) -> list[str]:
    """Hand edits to managed text. ``whole``: importing files hold only the block."""
    found = []
    try:
        source = current.get(SOURCE)
        _, block, _ = split(source or "")
        if block != agents_block(project_id):
            found.append(SOURCE + ": managed block differs from the generated text")
        for name in IMPORTERS.values():
            text = current.get(name)
            if text is None:
                found.append(name + ": missing")
            elif whole and text != import_block() + "\n":
                found.append(name + ": must hold only the managed import block")
            elif not whole and importer_text(text) != text:
                found.append(name + ": managed block differs from the generated text")
    except ManagedTextError:
        found.append("managed markers are damaged")
    return found
