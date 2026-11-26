"""Operational instructions must not displace the actual question's topic."""

from durumari_mcp.context_guard_queries import guard_queries
from durumari_mcp.prompt_context import terms


def test_plain_operational_paths_do_not_displace_stt_question():
    question = "지금 실기기 STT QA는 어떤 기기와 자원으로 준비해야 해?"
    instructions = (
        "historical-stt-qa.md를 실제로 읽고 durumari_context_read를 호출한다. "
        "historical-stt-qa.md의 과거 내용과 최신 Context를 비교한다."
    )
    assert "실기기" not in terms(question + "\n" + instructions)
    assert guard_queries(question + "\n" + instructions) == terms(question)
    assert "실기기" in guard_queries(question + "\n" + instructions)


def test_same_line_directions_after_question_do_not_displace_topic():
    question = "실기기 STT 준비 자원은?"
    assert guard_queries(question + " historical-stt-qa.md를 세 번 읽어줘") == terms(question)


def test_fenced_history_and_blank_lines_are_not_the_question():
    prompt = "\n```text\nold-resource-table.md\n```\n\n실기기 STT 자원"
    assert guard_queries(prompt) == terms("실기기 STT 자원")


def test_single_line_identifiers_keep_their_existing_behavior():
    prompt = "context_guard_runner.py PROJ-178 수정"
    assert guard_queries(prompt) == terms(prompt)


def test_no_topic_still_requires_search_instead_of_allowing_work():
    assert guard_queries("네") == []
