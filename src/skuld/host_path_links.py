"""Resolve a file path as a session wrote it into a file on the session's own host (LEX-2110).

A session's text names files the way a person would: ``~/agent/notes.md``,
``/home/thor/agent/notes.md:12``, ``file:///Users/xteo/x.md``, ``src/app.py#L10-L20``,
or a relative path under the workspace, often wrapped in backticks or followed by a
full stop. The app sends the link as written; the broker decodes it here, on the host
the session runs on, and answers with a record the presented-file route serves.

Three steps, kept apart on purpose:

1. :func:`decode_link_path` turns prose into absolute candidate paths. Only the
   original input is decoded; a stored path is never decoded again.
2. :func:`check_path` judges one literal path: inside the session roots (their written
   and canonical spellings), not secret-shaped, a regular file with one link, under the
   size cap. Resolving runs it; so does serving.
3. :func:`open_link` opens the file without following a final symlink, checks the open
   descriptor is the very inode that was judged, and judges the descriptor's own
   canonical path again. The bytes are streamed from that descriptor, never reopened
   by name, so a file swapped after the check is never served.

The roots are the ones ``present-file`` accepts (the session workspace and the session
home) and no others. Inside them, secret-shaped paths are refused. The rules are
deliberately constants, not configuration: anything able to edit them could open
``~/.ssh`` to a link.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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

# --- The secret rules (compared after NFKC normalisation and casefolding) ------------

#: Directly under the session home (outside an exempt workspace), every dot-entry, file
#: or directory, is refused: that is where tools keep keys, tokens, cookies and history
#: (``~/.github-pat``, ``~/.codex-x/auth.json``, ``~/.gstack/chromium-profile``). Only
#: these are open. A pattern ending in ``/`` opens a tree, any other names one file; ``*``
#: matches one path component. Every other rule below still applies inside them.
ALLOWED_HOME_DOT_ENTRIES = (
    ".lexi/flow/",
    ".lexi/config/",
    ".lexi/notes/",
    ".claude/CLAUDE.md",
    ".claude/skills/",
    ".claude/projects/*/memory/",
)
#: Non-dot trees under the session home refused as a whole (macOS application state,
#: cookies and keychains). The dot-entry rule already covers ``~/.config``.
DENIED_HOME_TREES = ("Library",)
#: Directory names whose contents are refused at any depth below the matched root.
SECRET_DIRECTORIES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".gbrain",
        ".password-store",
        ".mozilla",
        "keychains",  # ~/Library/Keychains, /Library/Keychains (macOS)
        "keyrings",  # ~/.local/share/keyrings (GNOME)
    }
)
#: A path component below the matched root containing one of these is refused
#: (``~/.lexi/secrets``, ``fleet-secrets``, ``github-token``, ``credentials.json``),
#: except as :data:`CODE_EXTENSIONS` and :data:`DOC_EXTENSIONS` say.
SECRET_SUBSTRINGS = ("secret", "token", "credential")
#: A final name with one of these skips the substring rule (``token_scope.py``); inside
#: the workspace, so do the non-dot directories above it. Dot names never skip it.
CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".swift",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".m",
        ".mm",
        ".rb",
        ".cs",
    }
)
#: A final name with one of these skips the substring rule only inside the workspace.
DOC_EXTENSIONS = frozenset({".md", ".rst"})
#: Exact basenames that are refused.
SECRET_NAMES = frozenset({".netrc", ".pgpass", ".npmrc", ".pypirc", ".claude.json"})
#: Basename prefixes that are refused.
SECRET_NAME_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
#: Basename suffixes that are refused.
SECRET_NAME_SUFFIXES = (
    ".env",
    ".pem",
    ".key",
    ".p8",
    ".p12",
    ".pfx",
    ".keychain",
    ".keychain-db",
    "_history",  # .bash_history, .python_history, .psql_history
)
#: (parent directory, basename) pairs: agent CLI login tokens.
SECRET_PAIRS = frozenset({(".codex", "auth.json")})


@dataclass(frozen=True)
class ResolvedLink:
    """A regular file inside the session roots, pinned to the inode that was checked."""

    path: Path
    size: int
    mtime_ns: int
    dev: int
    ino: int


@dataclass(frozen=True)
class LinkRoots:
    """The session roots and the host, as the broker knows them."""

    home: str
    workspace: str
    host: str
    max_bytes: int

    def spellings(self) -> tuple[str, ...]:
        """Each root as written and canonical: a root may itself be a symlink."""
        out: list[str] = []
        for root in (self.workspace, self.home):
            for spelling in (os.path.abspath(root), os.path.realpath(root)):
                if spelling not in out:
                    out.append(spelling)
        return tuple(out)

    def homes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((os.path.abspath(self.home), os.path.realpath(self.home))))

    def workspaces(self) -> tuple[str, ...]:
        """The workspace as written and canonical."""
        return tuple(
            dict.fromkeys((os.path.abspath(self.workspace), os.path.realpath(self.workspace)))
        )

    def workspace_exempt(self) -> bool:
        """Whether files in the workspace skip the home-area rules (dot-entries, Library).

        Only when every spelling of the workspace sits strictly below the area it is in:
        ``~/.niuu/workspaces/x`` is exempt; ``~/.config`` itself (or a symlink to it) is
        not, and neither is home, or a workspace that holds home."""
        for spelling in self.workspaces():
            relative = _relative_parts(spelling, self.homes())
            if relative is None:
                if _relative_parts(self.home, (spelling,)) is not None or _same(
                    spelling, self.home
                ):
                    return False
                continue
            if not relative:
                return False
            depth = _denied_home_depth(relative)
            if depth is not None and len(relative) <= depth:
                return False
        return True


def _fold(text: str) -> str:
    """Compare names the way a case-insensitive, normalising volume might (APFS)."""
    return unicodedata.normalize("NFKC", text).casefold()


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
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise LinkPathError(422, f"cannot decode {text!r}: malformed file URL ({exc})") from None
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


def _same(left: str, right: str) -> bool:
    return _fold(left).rstrip("/") == _fold(right).rstrip("/")


def _relative_parts(path: str, roots: tuple[str, ...]) -> tuple[str, ...] | None:
    """The folded components of *path* strictly below the deepest of *roots*, or None.

    ``()`` when *path* is the root itself."""
    folded = _fold(path).rstrip("/")
    best: tuple[str, ...] | None = None
    for root in roots:
        prefix = _fold(root).rstrip("/")
        if folded == prefix:
            relative: tuple[str, ...] = ()
        elif folded.startswith(prefix + "/"):
            relative = PurePosixPath(folded[len(prefix) + 1 :]).parts
        else:
            continue
        if best is None or len(relative) < len(best):
            best = relative
    return best


def _pattern_matches(pattern: str, relative: tuple[str, ...]) -> bool:
    is_tree = pattern.endswith("/")
    parts = PurePosixPath(_fold(pattern.rstrip("/"))).parts
    if is_tree and len(relative) <= len(parts):
        return False
    if not is_tree and len(relative) != len(parts):
        return False
    return all(p == "*" or p == r for p, r in zip(parts, relative, strict=False))


def _denied_home_depth(relative: tuple[str, ...]) -> int | None:
    """How deep the denied home area *relative* falls in is, or None when it is open."""
    if not relative:
        return None
    for tree in DENIED_HOME_TREES:
        parts = PurePosixPath(_fold(tree)).parts
        if relative[: len(parts)] == parts:
            return len(parts)
    if relative[0].startswith(".") and not any(
        _pattern_matches(pattern, relative) for pattern in ALLOWED_HOME_DOT_ENTRIES
    ):
        return 1
    return None


def _substring_hit(part: str) -> str | None:
    return next((word for word in SECRET_SUBSTRINGS if word in part), None)


def secret_reason(
    path: str | Path,
    homes: tuple[str, ...] = (),
    workspaces: tuple[str, ...] = (),
    *,
    workspace_exempt: bool = True,
) -> str | None:
    """Name the rule that makes *path* secret-shaped, or ``None``.

    *homes* and *workspaces* are the spellings of the session roots. Only the
    components below the matched root are judged (a worktree called
    ``…-token-fix`` or a home ``/Users/secretary`` is not itself refused). The
    home-area rule (dot-entries, ``~/Library``) applies outside the workspace, and
    inside it unless *workspace_exempt* is false.
    """
    text = os.fspath(path)
    in_workspace = _relative_parts(text, workspaces)
    below_home = _relative_parts(text, homes)
    if below_home is not None and not (in_workspace is not None and workspace_exempt):
        depth = _denied_home_depth(below_home)
        if depth is not None:
            return f"under ~/{'/'.join(below_home[:depth])}"
    if in_workspace is not None:
        parts = in_workspace
    elif below_home is not None:
        parts = below_home
    else:
        parts = tuple(_fold(p) for p in PurePosixPath(text).parts if p != "/")
    if not parts:
        return None
    for part in parts:
        if part in SECRET_DIRECTORIES:
            return f"in a {part} directory"
    name = parts[-1]
    extension = PurePosixPath(name).suffix
    is_code = extension in CODE_EXTENSIONS and not name.startswith(".")
    is_doc = extension in DOC_EXTENSIONS and not name.startswith(".")
    name_skips = is_code or (is_doc and in_workspace is not None)
    directories_skip = is_code and in_workspace is not None
    for part in parts[:-1]:
        if directories_skip and not part.startswith("."):
            continue
        word = _substring_hit(part)
        if word is not None:
            return f"named like a {word} ({part})"
    word = None if name_skips else _substring_hit(name)
    if word is not None:
        return f"named like a {word} ({name})"
    if name in SECRET_NAMES or name.startswith(SECRET_NAME_PREFIXES):
        return f"a {name} file"
    if name.endswith(SECRET_NAME_SUFFIXES):
        return f"a {name} file"
    if len(parts) >= 2 and (parts[-2], name) in SECRET_PAIRS:
        return f"a {parts[-2]}/{name} file"
    return None


def _judge_metadata(path: str, info: os.stat_result, roots: LinkRoots) -> None:
    if stat.S_ISDIR(info.st_mode):
        raise LinkPathError(403, f"{path} is a directory, not a file")
    if not stat.S_ISREG(info.st_mode):
        raise LinkPathError(403, f"{path} is not a regular file")
    if info.st_nlink > 1:
        # A hard link is a second name for some other file, perhaps a denied one.
        raise LinkPathError(403, f"{path} is refused: it has {info.st_nlink} hard links")
    if info.st_size > roots.max_bytes:
        raise LinkPathError(
            413,
            f"{path} is {info.st_size} bytes, over max_presented_file_bytes={roots.max_bytes}",
        )


def _judge_path(spelling: str, roots: LinkRoots, *, label: str) -> Path:
    """Roots and secret rules for one spelling; returns its canonical path."""
    try:
        resolved = resolve_path_in_roots(spelling, roots.spellings(), strict=False)
    except UnsafePathError:
        raise LinkPathError(403, f"{spelling} is outside the session roots") from None
    for which, candidate in ((label, spelling), ("target", str(resolved))):
        reason = secret_reason(
            candidate,
            roots.homes(),
            roots.workspaces(),
            workspace_exempt=roots.workspace_exempt(),
        )
        if reason is not None:
            raise LinkPathError(403, f"{candidate} is refused: {which} is {reason}")
    return resolved


def check_path(path: str, roots: LinkRoots) -> ResolvedLink:
    """Judge one literal absolute path (never decoded again) and pin its inode.

    403: outside the roots (a symlink that leaves them included), a secret-shaped
    path, a directory, device, FIFO or a file with more than one hard link.
    404: no such file. 413: over the size cap.
    """
    resolved = _judge_path(path, roots, label="link")
    try:
        info = os.stat(resolved)
    except FileNotFoundError:
        raise LinkPathError(404, f"no such file on {roots.host}: {resolved}") from None
    except OSError as exc:
        raise LinkPathError(403, f"{resolved} cannot be read: {exc.strerror}") from None
    _judge_metadata(str(resolved), info, roots)
    return ResolvedLink(
        path=resolved,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        dev=info.st_dev,
        ino=info.st_ino,
    )


def resolve_link(raw: str, roots: LinkRoots) -> ResolvedLink:
    """Decode *raw* as written and judge it; 422 when nothing decodable is left."""
    candidates = decode_link_path(raw, home=roots.home, workspace=roots.workspace)
    # The literal spelling wins when it exists; otherwise the last (decoded) one answers.
    chosen = next((c for c in candidates if os.path.lexists(c)), candidates[-1])
    return check_path(chosen, roots)


def _descriptor_path(fd: int) -> str:
    """The canonical path of an open descriptor, from the kernel."""
    if sys.platform == "darwin":
        buffer = bytes(os.pathconf(fd, "PC_PATH_MAX"))
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, buffer).split(b"\0", 1)[0])
    if sys.platform.startswith("linux"):
        return os.readlink(f"/proc/self/fd/{fd}")
    raise RuntimeError(f"host-path links cannot verify an open file on {sys.platform}")


def open_link(link: ResolvedLink, roots: LinkRoots) -> int:
    """Open the pinned file for reading and return the descriptor, or raise.

    The stored path is judged again literally; it is opened without following a final
    symlink (and without blocking on a FIFO swapped in); the descriptor must be the same
    inode, at the same size and mtime, still with one link; and its kernel path must
    still pass the roots and secret rules. 410 when the file changed since it was
    resolved (the app resolves the link again).
    """
    current = check_path(str(link.path), roots)
    if (current.dev, current.ino) != (link.dev, link.ino):
        raise LinkPathError(410, "file changed since the link was resolved; resolve it again")
    try:
        fd = os.open(link.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LinkPathError(410, "file changed since the link was resolved") from None
        if exc.errno == errno.ENOENT:
            raise LinkPathError(404, f"no such file on {roots.host}: {link.path}") from None
        raise LinkPathError(403, f"{link.path} cannot be read: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (link.dev, link.ino) or (
            info.st_size,
            info.st_mtime_ns,
        ) != (link.size, link.mtime_ns):
            raise LinkPathError(410, "file changed since the link was resolved; resolve it again")
        _judge_metadata(str(link.path), info, roots)
        _judge_path(_descriptor_path(fd), roots, label="open file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def link_file_id(key: bytes, session_id: str, link: ResolvedLink) -> str:
    """The same file at the same version always gets the same id from this broker.

    Keyed with the broker's own random secret, so an id can't be computed from a path.
    Shaped like a present-file id (``pf_`` + 32 hex) so the presented-file route serves it.
    """
    message = f"{session_id}\0{link.path}\0{link.dev}\0{link.ino}\0{link.mtime_ns}\0{link.size}"
    digest = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    return "pf_" + digest[:32]


def display_path(path: Path, home: str) -> str:
    """``~/…`` for a file under home, the absolute path otherwise."""
    home_root = os.path.realpath(home).rstrip(os.sep)
    text = str(path)
    if text == home_root:
        return "~"
    if text.startswith(home_root + os.sep):
        return "~" + text[len(home_root) :]
    return text
