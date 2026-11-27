"""Read the Antigravity CLI (``agy``) terminal screen.

Pure functions over the text of one ``tmux capture-pane`` so they can be tested
on recorded frames. The wording they match was recorded from ``agy`` 1.5.15 on
24 Nov 2026 (see docs/design/skuld-antigravity-gemini.md); ``TmuxAgyTransport``
refuses to start any other version, so a reworded screen cannot be misread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))")
_MENU_ROW_RE = re.compile(r"^\s*(>)?\s*([1-9][0-9]*)\.\s+(\S.*?)\s*$")
_TRUST_ROW_RE = re.compile(r"^\s*(>)?\s*(Yes, I trust this folder|No, exit)\s*$")
_QUESTION_HEADER_RE = re.compile(r"^\s*Question (\d+)/(\d+): (.*?)\s*$")
_STATUS_RE = re.compile(r"(\S+) · (.+?) · (low|medium|high|xhigh|max)\s*$")

SIGNED_OUT_MARKER = "You are currently not signed in"
READY_MARKER = "? for shortcuts"
BUSY_MARKER = "esc to cancel"
TRUST_QUESTION = "Do you trust the contents of this project?"
TRUST_WORKSPACE_LABEL = "Accessing workspace:"
TRUST_YES = "Yes, I trust this folder"
PERMISSION_HEADER = "Requesting permission for:"
MENU_FOOTER = "↑/↓ Navigate"
WRITE_IN_ROW = "Write-in..."


def clean(text: str) -> str:
    """Screen text without terminal escapes or carriage returns."""
    return _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Menu:
    """A numbered selection menu: its rows, the highlighted row and the text above it."""

    rows: tuple[tuple[int, str], ...]
    highlighted: int
    detail: tuple[str, ...] = ()
    question_index: int = 0
    question_count: int = 0
    question: str = ""
    #: The title agy draws above an approval menu's rule: "Command", "MCP", ...
    kind: str = ""

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(label for _, label in self.rows)

    def position(self, label: str) -> int | None:
        """The 0-based row index of ``label``, matched exactly."""
        for index, (_, row_label) in enumerate(self.rows):
            if row_label == label:
                return index
        return None

    @property
    def highlighted_position(self) -> int:
        return next(i for i, (digit, _) in enumerate(self.rows) if digit == self.highlighted)


@dataclass(frozen=True)
class TrustPrompt:
    workspace: str
    yes_highlighted: bool


def _lines(screen: str) -> list[str]:
    return clean(screen).splitlines()


def _menu_after(lines: list[str], start: int) -> tuple[list[str], list[tuple[bool, int, str]]]:
    """Detail lines, then numbered rows, ending at the navigation footer.

    Returns no rows unless the footer follows them: a half-drawn menu, or a numbered
    list in the agent's own output, is not a live menu.
    """
    detail: list[str] = []
    rows: list[tuple[bool, int, str]] = []
    for line in lines[start:]:
        match = _MENU_ROW_RE.match(line)
        if match:
            rows.append((bool(match[1]), int(match[2]), match[3]))
            continue
        if rows and not line.strip():
            continue  # agy leaves a blank row between the menu and its footer
        if rows:
            return (detail, rows) if MENU_FOOTER in line else (detail, [])
        if line.strip():
            detail.append(line.strip())
    return detail, []


def _build_menu(
    detail: list[str], rows: list[tuple[bool, int, str]], **extra: object
) -> Menu | None:
    highlighted = [digit for marked, digit, _ in rows if marked]
    if len(highlighted) != 1:
        return None
    return Menu(
        rows=tuple((digit, label) for _, digit, label in rows),
        highlighted=highlighted[0],
        detail=tuple(detail),
        **extra,  # type: ignore[arg-type]
    )


def _is_rule(line: str) -> bool:
    text = line.strip()
    return bool(text) and set(text) == {"─"}


def _approval_block(lines: list[str]) -> tuple[str, list[str], list[tuple[bool, int, str]]] | None:
    """The menu at the bottom of the screen: its title, the text under its rule, its rows.

    agy draws every approval menu the same way, whatever the tool (recorded from 1.5.16:
    a command, an MCP tool): a title line, a rule, what is asked, numbered rows and the
    navigation footer. Read from the footer up, so a numbered list higher up in the
    conversation is never taken for the menu.
    """
    footer = max((i for i, line in enumerate(lines) if MENU_FOOTER in line), default=None)
    if footer is None:
        return None
    index = footer - 1
    rows: list[tuple[bool, int, str]] = []
    while index >= 0:
        line = lines[index]
        match = _MENU_ROW_RE.match(line)
        if match:
            rows.insert(0, (bool(match[1]), int(match[2]), match[3]))
        elif line.strip():
            break
        index -= 1
    if not rows:
        return None
    detail: list[str] = []
    while index >= 0 and not _is_rule(lines[index]):
        if lines[index].strip():
            detail.insert(0, lines[index].strip())
        index -= 1
    if index < 0:
        return None
    title = next((lines[k].strip() for k in range(index - 1, -1, -1) if lines[k].strip()), "")
    return title, detail, rows


def permission_menu(screen: str) -> Menu | None:
    """The live tool-approval menu, for any tool agy asks about (command, MCP tool, ...).

    Not a question page (``Question i/n``) and not the workspace trust menu: those have
    their own readers.
    """
    lines = _lines(screen)
    block = _approval_block(lines)
    if block is None:
        return None
    title, detail, rows = block
    if any(_QUESTION_HEADER_RE.match(line) for line in detail) or TRUST_QUESTION in detail:
        return None
    if any(label in {TRUST_YES, "No, exit"} for _, _, label in rows):
        return None
    detail = [line for line in detail if line != PERMISSION_HEADER]
    return _build_menu(detail, rows, kind=title)


def unread_menu(screen: str) -> str:
    """The text of a menu agy shows that no reader here understands, or "" when none.

    Numbered rows just above the navigation footer that are not an approval, a question
    or the trust menu: Forge cannot offer them, so the session shows the menu's text,
    from the rule above it (or the top of the screen) down to the footer.
    """
    lines = _lines(screen)
    footer = max((i for i, line in enumerate(lines) if MENU_FOOTER in line), default=None)
    if footer is None:
        return ""
    index = footer - 1
    has_rows = False
    while index >= 0 and (not lines[index].strip() or _MENU_ROW_RE.match(lines[index])):
        has_rows = has_rows or bool(_MENU_ROW_RE.match(lines[index]))
        index -= 1
    if not has_rows or permission_menu(screen) or question_menu(screen) or trust_prompt(screen):
        return ""
    while index >= 0 and not _is_rule(lines[index]):
        index -= 1
    return "\n".join(line.strip() for line in lines[index + 1 : footer] if line.strip())


def question_menu(screen: str) -> Menu | None:
    """The live ``ask_question`` page (``Question i/n: <text>`` … rows … footer)."""
    lines = _lines(screen)
    found = [(i, m) for i, line in enumerate(lines) if (m := _QUESTION_HEADER_RE.match(line))]
    if not found:
        return None
    header, match = found[-1]
    detail, rows = _menu_after(lines, header + 1)
    if not rows:
        return None
    return _build_menu(
        detail,
        rows,
        question_index=int(match[1]),
        question_count=int(match[2]),
        question=match[3],
    )


def trust_prompt(screen: str) -> TrustPrompt | None:
    """The workspace trust menu ``agy`` shows for a folder it has not seen."""
    lines = _lines(screen)
    if TRUST_QUESTION not in "\n".join(lines):
        return None
    label = next((i for i, line in enumerate(lines) if TRUST_WORKSPACE_LABEL in line), None)
    if label is None:
        return None
    # The path follows the label, after a blank line; a long one wraps over several rows.
    path_rows: list[str] = []
    for line in lines[label + 1 :]:
        if line.strip():
            path_rows.append(line.strip())
            continue
        if path_rows:
            break
    rows = [m for line in lines if (m := _TRUST_ROW_RE.match(line))]
    marked = [m[2] for m in rows if m[1]]
    if not path_rows or len(rows) != 2 or len(marked) != 1:
        return None
    return TrustPrompt(workspace="".join(path_rows), yes_highlighted=marked[0] == TRUST_YES)


def signed_out(screen: str) -> bool:
    return SIGNED_OUT_MARKER in clean(screen)


def last_line(screen: str) -> str:
    return next((line for line in reversed(_lines(screen)) if line.strip()), "")


def composer_ready(screen: str) -> bool:
    """The idle composer: the shortcut hint, and no menu or trust prompt over it."""
    return (
        READY_MARKER in last_line(screen)
        and permission_menu(screen) is None
        and question_menu(screen) is None
        and trust_prompt(screen) is None
    )


def busy(screen: str) -> bool:
    return BUSY_MARKER in last_line(screen)


def status(screen: str) -> tuple[str, str, str] | None:
    """(mode, model display name, effort) from the status line, e.g. auto · Gemini 4 Neon · high."""
    match = _STATUS_RE.search(last_line(screen))
    return (match[1], match[2], match[3]) if match else None
