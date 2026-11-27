"""Resolve a file path as a session wrote it into a file on the session's own host (LEX-2110).

A session's text names files the way a person would: ``~/agent/notes.md``,
``/home/thor/agent/notes.md:12``, ``file:///Users/xteo/x.md``, ``src/app.py#L10-L20``,
or a relative path under the workspace, often wrapped in backticks or followed by a
full stop. The app sends the link as written; the broker decodes it here, on the host
the session runs on, and answers with a record the presented-file route serves.

The roots are the ones ``present-file`` accepts (the session workspace and the session
home) and no others. Inside them, secret-shaped paths are refused: present-file has no
such list, so this module owns it. The list is deliberately a constant, not
configuration: anything able to edit it could open ``~/.ssh`` to a link.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from skuld.path_security import UnsafePathError, resolve_path_in_roots

#: Advertised wherever the session's capabilities are listed (broker ``/api/capabilities``
#: and the WebSocket ``capabilities`` frame). ``present_by_path`` is the name the app's
#: first request used (lexi-ios 2548 release notes); kept as an alias.
LINK_CAPABILITIES = {"resolve_path": True, "present_by_path": True}


class LinkPathError(Exception):
    """A link path the broker refuses. ``status`` is the HTTP answer."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


#: Characters that wrap a path in prose or Markdown: `x`, "x", 'x', (x), [x], <x>, {x}.
_WRAPPERS = {"`": "`", '"': '"', "'": "'", "(": ")", "[": "]", "<": ">", "{": "}"}
#: Punctuation that ends a sentence after a path ("saved at ~/a.md.").
_TRAILING_PUNCTUATION = ".,;:!?"
_CLOSERS = ")]}>`\"'"
#: ``#L12``, ``#L12-L20``, ``#L12-20`` (GitHub style anchors), optionally with columns.
_LINE_ANCHOR_RE = re.compile(r"#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?$")
#: ``:12`` or ``:12:3`` (compiler / editor style).
_LINE_SUFFIX_RE = re.compile(r"(?::\d+){1,2}$")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_HOME_VAR_RE = re.compile(r"^(?:\$HOME|\$\{HOME\})(?=/|$)")

#: Directory names whose contents are never served by a link, at any depth.
SECRET_DIRECTORIES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        ".password-store",
        "keychains",  # ~/Library/Keychains, /Library/Keychains (macOS)
    }
)
#: Basename prefixes that are never served by a link.
SECRET_NAME_PREFIXES = (
    ".env",  # .env, .env.local, .envrc
    "credentials",  # ~/.config/**/credentials*, ~/.aws/credentials
    ".credentials",  # ~/.claude/.credentials.json
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)
#: Exact basenames that are never served by a link.
SECRET_NAMES = frozenset({".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc"})
#: Basename suffixes that are never served by a link.
SECRET_NAME_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keychain", ".keychain-db")
#: (parent directory, basename) pairs: agent CLI login tokens.
SECRET_PAIRS = frozenset({(".codex", "auth.json")})


@dataclass(frozen=True)
class ResolvedLink:
    """A regular file inside the session roots, identified by its content version."""

    path: Path
    size: int
    mtime_ns: int


def _strip_wrappers(text: str) -> str:
    """Trim surrounding backticks, quotes, brackets and sentence punctuation."""
    previous = None
    while text != previous:
        previous = text
        text = text.strip()
        while text and text[-1] in _TRAILING_PUNCTUATION:
            text = text[:-1]
        if len(text) >= 2 and _WRAPPERS.get(text[0]) == text[-1]:
            text = text[1:-1]
            continue
        # A lone opener or closer left from a wrapper the sentence split ("(see ~/a.md)").
        if text and text[0] in _WRAPPERS and _WRAPPERS[text[0]] not in text[1:]:
            text = text[1:]
        if text and text[-1] in _CLOSERS:
            opener = next(k for k, v in _WRAPPERS.items() if v == text[-1])
            if opener == text[-1] or opener not in text[:-1]:
                text = text[:-1]
    return text


def _strip_file_scheme(text: str) -> tuple[str, bool]:
    """Return the path of a ``file:`` URL, and whether it was one."""
    if not text.lower().startswith("file:"):
        return text, False
    parts = urlsplit(text)
    if parts.netloc not in ("", "localhost"):
        raise LinkPathError(422, f"cannot decode {text!r}: file URL names another host")
    return parts.path, True


def decode_link_path(raw: str, *, home: str, workspace: str) -> list[str]:
    """Decode a link as written into absolute candidate paths on this host.

    Returns one path, or two when the text carries percent escapes: the literal
    spelling first (a file may really be called ``a%20b``) and the decoded one second.
    Raises :class:`LinkPathError` (422) when nothing usable is left.
    """
    if not isinstance(raw, str):
        raise LinkPathError(422, "path must be a string")
    if "\0" in raw:
        raise LinkPathError(422, "cannot decode path: it contains a NUL byte")
    text = _strip_wrappers(raw)
    text, was_url = _strip_file_scheme(text)
    spellings = [unquote(text)] if was_url else [text]
    if not was_url and _PERCENT_ESCAPE_RE.search(text):
        spellings.append(unquote(text))
    candidates: list[str] = []
    for spelling in spellings:
        if "\0" in spelling:
            raise LinkPathError(422, "cannot decode path: it contains a NUL byte")
        path = _LINE_ANCHOR_RE.sub("", spelling)
        path = _LINE_SUFFIX_RE.sub("", path)
        path = _HOME_VAR_RE.sub(lambda _m: home, path)
        if path == "~" or path.startswith("~/"):
            path = home + path[1:]
        elif path.startswith("~"):
            raise LinkPathError(422, f"cannot decode {raw!r}: ~user paths are not supported")
        if not path.strip():
            raise LinkPathError(422, f"cannot decode {raw!r}: no path left")
        if not os.path.isabs(path):
            path = os.path.join(workspace, path)
        path = os.path.normpath(path)
        if path not in candidates:
            candidates.append(path)
    return candidates


def secret_reason(path: str | Path) -> str | None:
    """Name the rule that makes *path* secret-shaped, or ``None``.

    Matched case-insensitively: macOS volumes ignore case, so ``~/.SSH`` is ``~/.ssh``.
    """
    parts = [part.lower() for part in Path(path).parts]
    if not parts:
        return None
    for part in parts[:-1]:
        if part in SECRET_DIRECTORIES:
            return f"a {part} directory"
    name = parts[-1]
    if name in SECRET_DIRECTORIES:
        return f"a {name} directory"
    if name in SECRET_NAMES or name.startswith(SECRET_NAME_PREFIXES):
        return f"a {name} file"
    if name.endswith(SECRET_NAME_SUFFIXES):
        return f"a {name} file"
    if len(parts) >= 2 and (parts[-2], name) in SECRET_PAIRS:
        return f"a {parts[-2]}/{name} file"
    return None


def resolve_link(
    raw: str,
    *,
    home: str,
    workspace: str,
    host: str,
    max_bytes: int,
) -> ResolvedLink:
    """Resolve *raw* to a regular file inside the session roots, or raise LinkPathError.

    403: outside the roots (a symlink that leaves them included), a secret-shaped path,
    a directory, device or other non-regular file. 404: no such file. 413: over
    ``max_bytes`` (present-file's cap). 422: nothing decodable.
    """
    roots = (workspace, home)
    candidates = decode_link_path(raw, home=home, workspace=workspace)
    # The literal spelling wins when it exists; otherwise the last (decoded) one answers.
    chosen = next((c for c in candidates if os.path.lexists(c)), candidates[-1])
    try:
        resolved = resolve_path_in_roots(chosen, roots, strict=False)
    except UnsafePathError:
        raise LinkPathError(403, f"{chosen} is outside the session roots") from None
    for label, candidate in (("link", chosen), ("target", str(resolved))):
        reason = secret_reason(candidate)
        if reason is not None:
            raise LinkPathError(403, f"{candidate} is refused: {label} is {reason}")
    try:
        info = os.stat(resolved)
    except FileNotFoundError:
        raise LinkPathError(404, f"no such file on {host}: {resolved}") from None
    except OSError as exc:
        raise LinkPathError(403, f"{resolved} cannot be read: {exc.strerror}") from None
    if stat.S_ISDIR(info.st_mode):
        raise LinkPathError(403, f"{resolved} is a directory, not a file")
    if not stat.S_ISREG(info.st_mode):
        raise LinkPathError(403, f"{resolved} is not a regular file")
    if info.st_size > max_bytes:
        raise LinkPathError(
            413,
            f"{resolved} is {info.st_size} bytes, over max_presented_file_bytes={max_bytes}",
        )
    return ResolvedLink(path=resolved, size=info.st_size, mtime_ns=info.st_mtime_ns)


def link_file_id(session_id: str, link: ResolvedLink) -> str:
    """The same file at the same mtime and size always gets the same id.

    Shaped like a present-file id (``pf_`` + 32 hex) so the presented-file route serves it.
    """
    key = f"{session_id}\0{link.path}\0{link.mtime_ns}\0{link.size}".encode()
    return "pf_" + hashlib.sha256(key).hexdigest()[:32]


def display_path(path: Path, home: str) -> str:
    """``~/…`` for a file under home, the absolute path otherwise."""
    home_root = os.path.realpath(home).rstrip(os.sep)
    text = str(path)
    if text == home_root:
        return "~"
    if text.startswith(home_root + os.sep):
        return "~" + text[len(home_root) :]
    return text
