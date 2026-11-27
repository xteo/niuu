"""agy screen parsing, on frames recorded from agy 1.5.16 (tests/test_skuld/fixtures/agy)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skuld.transports import agy_screen

FIXTURES = Path(__file__).parent / "fixtures" / "agy"


def _frame(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def test_the_permission_menu_is_read_with_its_rows_highlight_and_command() -> None:
    menu = agy_screen.permission_menu(_frame("permission"))

    assert menu is not None
    assert menu.labels == (
        "Yes, run command",
        "Yes, and always allow in this conversation for commands that start with 'uname'",
        "Yes, and always allow for commands that start with 'uname' (Persist to settings.json)",
        "No, cancel",
    )
    assert menu.highlighted == 1
    assert menu.highlighted_position == 0
    assert menu.detail == ("uname -m", "Run this command?")
    assert menu.position("No, cancel") == 3
    assert menu.position("Maybe") is None


def test_the_mcp_approval_menu_is_read_with_its_title_tool_and_six_rows() -> None:
    menu = agy_screen.permission_menu(_frame("permission_mcp"))  # recorded live, 27 Nov 2026

    assert menu is not None
    assert menu.kind == "MCP"
    assert menu.detail == ("forge/get_session", "Allow calling this tool?")
    assert menu.labels[0] == "Yes, allow tool call"
    assert (
        menu.labels[-1] == "No, and always deny tool 'forge/get_session' (Persist to settings.json)"
    )
    assert menu.highlighted_position == 0
    assert agy_screen.unread_menu(_frame("permission_mcp")) == ""


def test_the_command_menu_keeps_its_title() -> None:
    assert agy_screen.permission_menu(_frame("permission")).kind == "Command"


def test_screens_without_a_numbered_menu_have_no_unread_menu() -> None:
    for name in ("ready", "busy", "question", "trust"):
        assert agy_screen.unread_menu(_frame(name)) == ""


def test_the_question_page_is_read_with_its_number_text_and_options() -> None:
    menu = agy_screen.question_menu(_frame("question"))

    assert menu is not None
    assert (menu.question_index, menu.question_count) == (1, 1)
    assert menu.question == "Tea or coffee?"
    assert menu.labels == ("Tea", "Coffee", agy_screen.WRITE_IN_ROW)
    assert agy_screen.permission_menu(_frame("question")) is None


def test_the_trust_prompt_names_its_workspace_and_highlight() -> None:
    prompt = agy_screen.trust_prompt(_frame("trust"))

    assert prompt == agy_screen.TrustPrompt(workspace="/work/project", yes_highlighted=True)
    assert not agy_screen.composer_ready(_frame("trust"))


def test_a_wrapped_workspace_path_is_joined() -> None:
    screen = (
        "Accessing workspace:\n\n/very/long/\npath/to/project\n\n"
        f"{agy_screen.TRUST_QUESTION}\n\n  Yes, I trust this folder\n> No, exit\n"
    )

    assert agy_screen.trust_prompt(screen) == agy_screen.TrustPrompt(
        workspace="/very/long/path/to/project", yes_highlighted=False
    )


def test_the_idle_composer_shows_ready_and_the_status_line() -> None:
    screen = _frame("ready")

    assert agy_screen.composer_ready(screen)
    assert not agy_screen.busy(screen)
    assert agy_screen.status(screen) == ("accept-edits", "Gemini 4 Neon", "medium")


def test_a_running_turn_is_busy_not_ready() -> None:
    screen = _frame("busy")

    assert agy_screen.busy(screen)
    assert not agy_screen.composer_ready(screen)


@pytest.mark.parametrize("name", ["permission", "question"])
def test_an_open_menu_is_not_a_ready_composer(name: str) -> None:
    assert not agy_screen.composer_ready(_frame(name))


def test_a_menu_without_its_footer_is_not_live() -> None:
    half_drawn = "Requesting permission for:\n   ls\n> 1. Yes, run command\n  2. No, cancel\n"

    assert agy_screen.permission_menu(half_drawn) is None


def test_a_numbered_list_in_the_reply_is_not_a_menu() -> None:
    reply = (
        "Steps:\n1. build\n2. test\n────\n>\n────\n? for shortcuts   auto · Gemini 4 Neon · high"
    )

    assert agy_screen.permission_menu(reply) is None
    assert agy_screen.question_menu(reply) is None
    assert agy_screen.composer_ready(reply)


def test_a_menu_without_a_single_highlight_is_not_read() -> None:
    screen = "Question 1/1: Pick\n  1. A\n  2. B\n  ↑/↓ Navigate · enter Select\n"

    assert agy_screen.question_menu(screen) is None


def test_the_signed_out_welcome_is_recognised() -> None:
    welcome = (
        " Welcome to the Antigravity CLI. You are currently not signed in.\n\n > 1. Google OAuth"
    )

    assert agy_screen.signed_out(welcome)
    assert not agy_screen.signed_out(_frame("ready"))


def test_escapes_are_removed() -> None:
    assert agy_screen.clean("\x1b[1mbold\x1b[0m\r\n") == "bold\n"
