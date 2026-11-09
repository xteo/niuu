"""LEX-2022: a running Claude session switches model and effort for itself alone.

A typed `/model X` or `/effort X` saves X as the default for every new Claude Code
session on the host. The pickers' "s" (this session only) does not, so the switch
drives the pickers. Screens are Claude Code 2.1.321 captures.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from skuld.transports.tmux_interactive import (
    SESSION_SWITCH_MODELS,
    TmuxInteractiveTransport,
    _picker_rows,
    _slider_level,
)

PICKER = """\
❯ /model
────
  Select model
  Switch between Claude models. Your pick becomes the default for new sessions.
    1. Default (recommended)  Use the default model (currently Opus 5.5) · $4/$20 per Mtok
  {c2} 2. Opus ✔                 Opus 5.5 · Best for everyday, complex tasks · $4/$20 per Mtok
  {c3} 3. Fable                  Fable 5.2 · Most capable for your hardest tasks · $8/$40 per Mtok
    4. Sonnet                 Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok
  ◉ xHigh effort ←/→ to adjust
  Enter to set as default · s to use this session only · Esc to cancel
"""

SLIDER = """\
❯ /effort
────
  Effort
                         Faster                             Smarter
                         {bar}      Ultracode  off
                         low     medium     high     xhigh      max      Tab to toggle
  ←/→ to adjust · Enter to confirm · s for this session only · Esc to cancel
"""
_BAR_XHIGH = "──────────────────────────────▲───────────"
_BAR_HIGH = "────────────────────▲─────────────────────"


def _picker(cursor: int) -> str:
    return PICKER.format(c2="❯" if cursor == 2 else " ", c3="❯" if cursor == 3 else " ")


class _Pane:
    """Feeds the transport the screens a real CLI would show after each key."""

    def __init__(self, screens: list[str]) -> None:
        self.screens = screens
        self.keys: list[str] = []

    async def capture(self, *_a, **_k) -> str:
        return self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)

    async def key(self, key: str, **_k) -> None:
        self.keys.append(key)


def _claude(tmp_path, pane: _Pane, model="claude-opus-5-5") -> TmuxInteractiveTransport:
    claude = TmuxInteractiveTransport(str(tmp_path), model=model, reasoning_effort="xhigh")
    claude._wait_for_repl_ready = AsyncMock()
    claude._send_slash_command = AsyncMock()
    claude._capture_pane_text = pane.capture
    claude._send_key = pane.key
    claude._pane_poll_interval_s = 0
    return claude


def test_screens_parse():
    assert [(r[0], r[1]) for r in _picker_rows(_picker(2))] == [
        (False, 1),
        (True, 2),
        (False, 3),
        (False, 4),
    ]
    assert _slider_level(SLIDER.format(bar=_BAR_XHIGH)) == "xhigh"
    assert _slider_level(SLIDER.format(bar=_BAR_HIGH)) == "high"
    assert _slider_level("no slider here") is None


async def test_model_switch_uses_the_picker_session_only_key(tmp_path):
    ack = "❯ /model\n  ⎿  Set model to Fable 5.2 for this session only\n  ● high · /effort"
    pane = _Pane([_picker(2), _picker(3), ack])
    claude = _claude(tmp_path, pane)

    await claude.send_control("set_runtime_options", options={"model": "claude-fable-5-2"})

    claude._send_slash_command.assert_awaited_once_with("model")
    assert pane.keys == ["Down", "s"]
    assert claude._model == "claude-fable-5-2"
    assert claude._reasoning_effort == "high"  # Fable 5.2 brought its own effort


async def test_effort_switch_uses_the_slider_session_only_key(tmp_path):
    ack = "❯ /effort\n  ⎿  Set effort level to high (this session only): Comprehensive"
    pane = _Pane([SLIDER.format(bar=_BAR_XHIGH), SLIDER.format(bar=_BAR_HIGH), ack])
    claude = _claude(tmp_path, pane, model="claude-fable-5-2")

    await claude._set_effort("high")

    claude._send_slash_command.assert_awaited_once_with("effort")
    assert pane.keys == ["Left", "s"]
    assert (await claude.get_effort())["current"] == "high"


async def test_a_saved_default_is_a_hard_error(tmp_path):
    saved = "❯ /model\n  ⎿  Set model to Fable 5.2 and saved as your default for new sessions"
    pane = _Pane([_picker(2), _picker(3), saved])
    claude = _claude(tmp_path, pane)

    with pytest.raises(RuntimeError, match="saved the /model change as the host default"):
        await claude._set_model("claude-fable-5-2")


async def test_a_picker_without_the_model_is_closed_and_refused(tmp_path):
    old = _picker(2).replace("Fable 5.2", "Fable 5.1")
    pane = _Pane([old])
    claude = _claude(tmp_path, pane)

    with pytest.raises(RuntimeError, match="offers no Fable 5.2 row"):
        await claude._set_model("claude-fable-5-2")
    assert pane.keys == ["Escape"]


async def test_runtime_options_offer_only_session_switchable_models(tmp_path):
    claude = _claude(tmp_path, _Pane([""]), model="claude-fable-5-1")

    state = await claude.get_runtime_options()

    assert [m["id"] for m in state["models"]] == list(SESSION_SWITCH_MODELS)
    assert {m["id"]: m["default_effort"] for m in state["models"]} == {
        "claude-opus-5-5": "xhigh",
        "claude-fable-5-2": "high",
    }
    assert state["current"]["model"] == "claude-fable-5-1"
    with pytest.raises(ValueError, match="needs the session's model set"):
        await claude.send_control("set_runtime_options", options={"model": "claude-sonnet-5"})


def test_parsing_ignores_text_above_the_open_picker():
    """A model list or level names earlier in the conversation never steer the picker."""
    old_text = (
        "  ❯ 3. Fable                  Fable 5.2 · quoted in an earlier answer\n"
        "low     medium     high     xhigh      max\n"
    )
    assert [r[1] for r in _picker_rows(old_text + _picker(2)) if r[0]] == [2]
    assert _slider_level(old_text + SLIDER.format(bar=_BAR_XHIGH)) == "xhigh"


async def test_an_older_identical_ack_never_counts(tmp_path):
    """Only an acknowledgement under the latest /model echo proves this switch."""
    old = "❯ /model\n  ⎿  Set model to Fable 5.2 for this session only\n"
    pane = _Pane([old + _picker(2), old + _picker(3), old + "❯ /model\n  ⎿  (picker open)"])
    claude = _claude(tmp_path, pane)
    claude._effort_control_timeout = 0.05

    with pytest.raises(RuntimeError, match="did not acknowledge /model"):
        await claude._set_model("claude-fable-5-2")
    assert claude._model == "claude-opus-5-5"
    assert pane.keys[-1] == "Escape"


async def test_a_slow_redraw_is_waited_for(tmp_path):
    """The cursor check polls: a capture taken before Ink redraws is not a failure."""
    ack = "❯ /model\n  ⎿  Set model to Fable 5.2 for this session only"
    pane = _Pane([_picker(2), _picker(2), _picker(2), _picker(3), ack])
    claude = _claude(tmp_path, pane)

    await claude._set_model("claude-fable-5-2")

    assert pane.keys == ["Down", "s"]
