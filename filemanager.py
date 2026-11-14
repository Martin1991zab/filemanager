#!/usr/bin/env python3
"""
filemanager.py — a single-file web file manager.

Browse, create/delete/rename/move folders & files, open folders,
upload/download files (zip archives can be extracted in the browser,
directories can be downloaded as zip, and files can be transferred as
base64 — a JSON workaround for flaky networks),
view & edit images and plain-text files, run
executable files, and run arbitrary bash commands, all from a
dependency-free HTML/JS/CSS interface with a Help popup that documents the
whole thing in the browser. The same folder is also served as a WebDAV
share (see --dav-path below), so it can be mounted with davfs2 and edited
with ordinary Linux tools. Folders show their recursive
size; the listing shows created and last-updated times and can be sorted
by name, size, or either timestamp; images get Pillow-generated
thumbnails in the listing. The header holds only what is used every day
(+ New, Upload); the less frequent actions — zip download of the current
folder, base64 upload, make and the shell — are grouped in a Tools menu.
If a Makefile is present in a directory,
`make` is run there automatically in the background after files are
edited, uploaded or deleted (a popup shows the result once the build
finishes); the "Auto-make after edits" item in that menu turns that off
(the choice is kept in the browser's localStorage, so the server stays
stateless) and the "Run make here" item runs `make` in the current folder
on demand and waits for the result. Shell access, executable execution, make
(both the automatic rebuilds and the manual Make action) and the base64
upload/download workaround can each be disabled entirely at startup with
the --no-shell, --no-run, --no-make and --no-b64 flags.

Everything is stateless: it serves the directory the app was started in
(or a path passed as the first argument) and stores no config, database,
or metadata. Authentication is expected to be handled by an external
reverse proxy (there is none here). The only state kept is in memory and
only to save work: rendered thumbnails, folder sizes, unfinished make jobs
and, for WebDAV, the write-lock table — all TTL-bounded, all gone on
restart, nothing to migrate.

Symlinks inside the served tree are listed and browsed; the entry the
listing shows is the one operations act on, so deleting a symlink to a
folder removes the link and leaves the linked folder alone. A symlink may
point outside the served directory (the parent path is still confined).

WebDAV (RFC 4918) is served under a URL prefix (--dav-path, /dav by
default) so clients can mount the same folder the browser shows. The
target client is davfs2 on Linux: OPTIONS, PROPFIND, PROPPATCH, MKCOL,
PUT, DELETE, MOVE, COPY, LOCK, UNLOCK, GET and HEAD are implemented, with
exclusive write locks (compliance class 2) so a file open in the mount
cannot be silently overwritten from the browser, and strong ETags plus
If-Match/If-None-Match so a davfs2 upload that would clobber a newer file
is refused instead. Microsoft extensions, Depth: infinite, shared locks
and partial (Content-Range) uploads are deliberately not implemented.

Run:
    python filemanager.py [directory] [--host HOST] [--port PORT]
                            [--read-only] [--no-shell] [--no-run]
                            [--no-make] [--no-b64] [--no-dav]
                            [--dav-path PREFIX] [--shell-cmd CMD]
                            [--make-cmd CMD] [--version]

GET /health answers a small JSON status document (used by the Docker
health check); everything else under /api/* speaks JSON, everything else
under the WebDAV prefix speaks WebDAV.

Uses only the Python standard library (3.10 or newer), plus (optionally)
Pillow for image thumbnails. If Pillow is missing, the app still works:
it shows a generic icon instead of a thumbnail (SVG previews still work,
because the browser draws those itself).
"""

import argparse
import base64
import binascii
import contextlib
import errno
import importlib.util
import io
import json
import mimetypes
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import OrderedDict
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, TypedDict
from urllib.parse import parse_qs, quote, unquote, urlparse

# ---------------------------------------------------------------------------
# Limits / tuning (edit to taste)
# ---------------------------------------------------------------------------
VERSION = "1.1"
THUMB_SIZE = (120, 120)  # max thumbnail dimension (px)
THUMB_MAX_PIXELS = 100_000_000  # Pillow refuses to decode images above this
MAX_RUN_OUTPUT = 1024 * 1024  # 1 MB cap on captured output per run
RUN_TIMEOUT = 60  # default run timeout (seconds)
RUN_MAX_TIMEOUT = 3600  # hard cap on run timeout (seconds)
MAX_TEXT_SIZE = 20 * 1024 * 1024  # /api/text refuses to load files above this
# Every transfer path has to handle files up to this size: /api/upload and
# /api/download, the zip download, WebDAV PUT/GET and the base64 workaround.
# The first three are streamed, so they need no cap of their own (a full disk
# already answers 413); only base64 needs one, because that workaround holds
# the payload in memory at both ends.
MAX_TRANSFER_SIZE = 1024 * 1024 * 1024  # what every transfer path must carry
MAX_B64_FILE_SIZE = MAX_TRANSFER_SIZE  # /api/download64 file-size limit
# base64 is four bytes for every three, so a base64 upload's JSON body is a
# third larger than the file in it. Body and file used to share one number,
# which quietly capped base64 uploads at three quarters of a download.
MAX_B64_BODY_SIZE = (MAX_B64_FILE_SIZE + 2) // 3 * 4 + 64 * 1024
MAX_JSON_BODY_SIZE = 64 * 1024 * 1024  # cap on any JSON request body
# A 1 GB archive of compressible material expands past itself by more than
# fourfold all the time, so the extract limits sit above the transfer target.
MAX_ZIP_ENTRY_SIZE = 4 * 1024 * 1024 * 1024  # /api/extract per-entry size limit
MAX_ZIP_TOTAL_SIZE = 8 * 1024 * 1024 * 1024  # /api/extract total output limit
READ_TIMEOUT = 30  # idle socket timeout: headers, body and stalled writes
LIST_MAX_ENTRIES = 20_000  # /api/list caps the entries it returns
DIR_SIZE_CACHE_TTL = 30  # seconds a computed folder size may be reused
DIR_SIZE_SCAN_BUDGET = 2_000_000  # directory entries scanned per /api/foldersize
THUMB_CACHE_MAX_ENTRIES = 512  # rendered thumbnails kept in memory
THUMB_CACHE_MAX_BYTES = 64 * 1024 * 1024
SVG_THUMB_LIMIT = 2 * 1024 * 1024  # SVGs are served to <img> as-is, up to this
MAKE_MAX_CONCURRENT = 4  # builds running at once, across all folders
MAKE_JOB_POLL_MS = 400  # how often the browser polls a make job
DAV_MAX_ENTRIES = 5_000  # entries one PROPFIND reports (davfs2 read_timeout is 30 s)
DAV_LOCK_TTL = 1800  # seconds a WebDAV lock survives without refreshing
DAV_LOCK_MAX_TIMEOUT = 86_400  # hard cap on a client-requested lock timeout
DAV_MAX_XML_BODY = 1024 * 1024  # PROPFIND/PROPPATCH/LOCK request-body limit
DAV_MAX_LOCKS = 4096  # locks kept in memory before the oldest go
SHELL_CMD = "bash"  # shell used for the Shell action (override: --shell-cmd)
MAKE_CMD = "make"  # run automatically after edits/uploads when a Makefile is present
# On Windows, X_OK is True for nearly every file, so gate the Run button on
# a known executable/script extension there. On POSIX the exec bit is enough.
NT_EXEC_EXTS = {".exe", ".bat", ".cmd", ".com", ".msc", ".ps1", ".vbs", ".wsf"}

IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".ico",
    ".svg",
    ".heic",
    ".heif",
    ".avif",
}
# Images Pillow can render into a thumbnail. .svg is not in here: those are
# served to the browser as-is and need no decoding (see render_thumbnail).
THUMB_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".ico"}
TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".json",
    ".json5",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".html",
    ".htm",
    ".xml",
    ".py",
    ".pyi",
    ".ipynb",
    ".csv",
    ".tsv",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".log",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".pl",
    ".pm",
    ".lua",
    ".r",
    ".dart",
    ".swift",
    ".scala",
    ".sql",
    ".env",
    ".properties",
    ".vue",
    ".svelte",
    ".asm",
    ".s",
    ".proto",
    ".graphql",
    ".gql",
    ".tf",
    ".tfvars",
    ".lock",
    ".patch",
    ".diff",
    ".dot",
    ".adoc",
}
TEXT_NAMES = {
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
    ".env",
    ".env.local",
    ".npmrc",
    ".gitconfig",
    ".gitattributes",
    ".gitmodules",
    ".dockerfile",
    ".babelrc",
    ".eslintrc",
    ".prettierrc",
    ".nvmrc",
    ".node-version",
    ".python-version",
    "dockerfile",
    "makefile",
    "readme",
    "license",
    "changelog",
    "authors",
}

# MIME types that are textual but not under the "text/" prefix
# (fallback for files whose extension is not in TEXT_EXTS).
TEXT_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-sh",
    "application/x-shellscript",
    "application/x-python",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/graphql",
    "application/sql",
}

# The directory the whole app is locked to (set in main()).
ROOT: Path = Path.cwd().resolve()

# Feature toggles, set from the command line in main().
ENABLE_SHELL: bool = True  # Tools ▸ Shell / /api/shell
ENABLE_RUN: bool = True  # run executables / /api/run
ENABLE_MAKE: bool = True  # automatic rebuilds + the make items in Tools
ENABLE_B64: bool = True  # base64 upload/download (/api/upload64, /api/download64)
ENABLE_WRITE: bool = True  # write access (--read-only turns this off)
ENABLE_DAV: bool = True  # WebDAV mount (davfs2 and friends)
# First URL segment serving WebDAV (set in main from --dav-path). The mount
# cannot live at / because the JSON API and the page already own those names.
DAV_PREFIX = "dav"

# When the process started, reported by /health.
START_TIME = time.time()

# Parsed query string, as returned by urllib.parse.parse_qs.
Qs = dict[str, list[str]]

# Thumbnail cache key/value: (path, mtime_ns, size) -> (etag, content-type, data).
ThumbKey = tuple[Path, int, int]
ThumbVal = tuple[str, str, bytes]


# Shape of a captured command/make result (used by run, shell and auto-make).
class RunResult(TypedDict):
    code: int | None
    timed_out: bool
    truncated: bool
    output: str
    cwd: str


class HTTPError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_path(rel: str) -> Path:
    """Resolve a client-supplied relative path, forcing it to stay under ROOT.

    Everything but the last component is resolved, so ".." and symlinked
    parent folders are still kept inside the root. The last component is
    deliberately returned unresolved: with docs/latest -> docs/1.2 the caller
    must get "docs/latest" back, otherwise delete/rename/move would silently
    act on the linked folder instead of on the link shown in the listing.

    A final symlink may point outside the root: the entry itself is inside
    it, and following it is what the user expects from a single-user tool
    whose access control lives in a reverse proxy.
    """
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if "\x00" in rel:
        raise HTTPError(400, "Path cannot contain a NUL byte")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    name = parts.pop() if parts else ""
    # Fold the parent components starting at the root, so ".." can only ever
    # climb as far as the root and then stops there instead of escaping (or
    # turning a harmless "cd .." too many into an error).
    parent = ROOT
    for part in parts:
        if part == "..":
            if parent != ROOT:
                parent = parent.parent
        else:
            parent = parent / part
    # Resolving here is what catches a symlinked parent folder pointing out of
    # the root; the returned leaf itself stays unresolved (see above).
    parent = parent.resolve()
    if parent != ROOT and ROOT not in parent.parents:
        raise HTTPError(403, "Path is outside the root directory")
    if name in ("", "."):
        return parent
    if name == "..":
        up = parent.parent
        return up if (up == ROOT or ROOT in up.parents) else ROOT
    return parent / name


def entry_exists(p: Path) -> bool:
    """True when p exists, including a dangling symlink."""
    try:
        p.lstat()
    except OSError:
        return False
    return True


def is_link_to_dir(p: Path) -> bool:
    """True for a real directory, but False for a symlink pointing at one."""
    try:
        return stat.S_ISDIR(p.lstat().st_mode)
    except OSError:
        return False


def link_target(p: str | Path) -> str | None:
    """The readlink() target of a symlink, or None for any other entry."""
    try:
        return os.readlink(p)
    except OSError:
        return None


@contextlib.contextmanager
def os_errors(hint: str) -> Iterator[None]:
    """Translate OSError into a proper HTTPError instead of a bare 500.

    Disk-full, quota and permission failures are ordinary outcomes of file
    operations, so they get a client-side message that says what went wrong.
    """
    try:
        yield
    except TimeoutError:
        raise  # a stalled client is not a filesystem error: let it become 408
    except FileNotFoundError:
        raise HTTPError(404, f"{hint}: not found")
    except FileExistsError:
        raise HTTPError(409, "A file or folder with that name already exists")
    except NotADirectoryError:
        raise HTTPError(400, f"{hint}: a component of the path is not a folder")
    except IsADirectoryError:
        raise HTTPError(400, f"{hint}: expected a file, found a folder")
    except PermissionError:
        raise HTTPError(403, f"{hint}: permission denied")
    except OSError as e:
        if e.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG):
            raise HTTPError(413, f"{hint}: no space left on the device")
        raise HTTPError(500, f"{hint}: {e.strerror or e}")


_HAVE_PILLOW: bool | None = None


def pillow_available() -> bool:
    """True when Pillow can be imported; the lookup happens once."""
    global _HAVE_PILLOW
    if _HAVE_PILLOW is None:
        try:
            _HAVE_PILLOW = importlib.util.find_spec("PIL") is not None
        except (ImportError, ValueError):
            _HAVE_PILLOW = False
    return _HAVE_PILLOW


def rel_of(p: Path) -> str:
    return "" if p == ROOT else p.relative_to(ROOT).as_posix()


def fmt_size(n: float) -> str:
    """A byte count as short human text, for error messages.

    The limits at the top of the file are written in bytes and quoted to the
    user, and "1024 MB" reads worse than "1 GB". Rounds like the browser's own
    formatter does: whole numbers from 10 up, one decimal below.
    """
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.{0 if n >= 10 or unit == 'B' else 1}f} {unit}"
        n /= 1024.0
    return f"{n:.0f} PB"


def classify(name: str) -> str:
    """Classify a file as 'image', 'text' or 'file'.

    Checks the extension/name lists first, then falls back to the system
    MIME type (via the mimetypes module, which also reads /etc/mime.types
    and friends), so files with unlisted extensions are still recognized
    when the OS knows they are textual (e.g. `todo.ics` -> text/calendar).
    """
    lower = name.lower()
    if lower in TEXT_NAMES:
        return "text"
    ext = os.path.splitext(lower)[1]
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    ctype, _ = mimetypes.guess_type(name)
    if ctype is not None:
        ctype = ctype.split(";")[0].strip()
        if ctype.startswith("text/") or ctype in TEXT_MIME_TYPES:
            return "text"
    return "file"


def detect_newline(data: bytes) -> str:
    """Detect the line ending(s) used in data: 'crlf', 'lf', 'cr', 'mixed'
    (more than one kind present) or 'none'."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    kinds = [k for k, n in (("crlf", crlf), ("lf", lf), ("cr", cr)) if n]
    if not kinds:
        return "none"
    if len(kinds) > 1:
        return "mixed"
    return kinds[0]


def created_time(st: os.stat_result) -> float:
    """Best-effort creation time for a stat result.

    Uses st_birthtime where the platform exposes it (macOS, BSD, Windows);
    elsewhere falls back to st_ctime (inode change time on POSIX), which is
    the closest value Python can provide without statx.
    """
    birth = getattr(st, "st_birthtime", None)
    return birth if birth else st.st_ctime


def is_executable(p: Path, st: os.stat_result | None = None) -> bool:
    """True if p is a regular file the current user can execute.

    Pass the stat result when one is already at hand (e.g. from a
    directory scan) to avoid an extra syscall.
    """
    if st is None:
        try:
            st = p.stat()
        except OSError:
            return False
    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        if not os.access(p, os.X_OK):
            return False
    except OSError:
        return False
    if os.name == "nt":
        return p.suffix.lower() in NT_EXEC_EXTS
    return True


def _run_captured(
    argv: list[str], cwd: Path, timeout: int, max_out: int
) -> tuple[int | None, bool, bool, str, str]:
    """Run argv in cwd, capture combined stdout+stderr, and kill the whole
    process group on timeout.

    Output is drained with a hard cap of max_out bytes: once the cap is
    reached, further output is read and discarded instead of being kept,
    so a chatty child (e.g. `yes`) can never exhaust server memory no
    matter how long it runs. The child is still allowed to run to
    completion after the cap, so its real exit code is reported.

    Returns (code, timed_out, truncated, text, cwd_rel).
    """
    cwd_str = str(cwd) if (cwd and cwd.is_dir()) else None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd_str,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        raise HTTPError(400, f"Could not start process: {e}")

    deadline = time.monotonic() + timeout
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    head = bytearray()
    truncated = False
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not sel.select(timeout=remaining):
                timed_out = True
                break
            room = max_out - len(head)
            chunk = os.read(proc.stdout.fileno(), room if room > 0 else 65536)
            if not chunk:  # EOF
                break
            if room > 0:
                head += chunk[:room]
            if len(chunk) > room:
                truncated = True
    except OSError:  # the child died mid-read; treat as EOF
        pass
    finally:
        sel.close()
        with contextlib.suppress(OSError):  # the pipe, not the child's memory
            proc.stdout.close()

    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001 - the process may already be gone
            with contextlib.suppress(Exception):
                proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
        proc.wait()

    code = None if timed_out else proc.returncode
    text = bytes(head).decode("utf-8", "replace")
    cwd_rel = rel_of(Path(cwd_str)) if cwd_str else (rel_of(ROOT) or "/")
    return code, timed_out, truncated, text, cwd_rel


def run_executable(p: Path, args: list[str], timeout: int, max_out: int) -> RunResult:
    """Run p in its own directory, capturing combined stdout+stderr."""
    code, timed_out, truncated, text, cwd_rel = _run_captured(
        [str(p)] + args, p.parent, timeout, max_out
    )
    return {
        "code": code,
        "timed_out": timed_out,
        "truncated": truncated,
        "output": text,
        "cwd": cwd_rel,
    }


def run_make_if_present(dirp: Path) -> RunResult | None:
    """Run MAKE_CMD in dirp if it contains a Makefile.

    Automatic rebuild hook after edits/uploads. Uses the same capture,
    timeout and output-cap machinery as the Run/Shell features. Returns the
    result dict, or None when there is no Makefile.
    """
    if not (dirp / "Makefile").is_file():
        return None
    code, timed_out, truncated, text, cwd_rel = _run_captured(
        [MAKE_CMD], dirp, RUN_TIMEOUT, MAX_RUN_OUTPUT
    )
    return {
        "code": code,
        "timed_out": timed_out,
        "truncated": truncated,
        "output": text,
        "cwd": cwd_rel,
    }


class _MakeJobs:
    """Background auto-make runner with per-directory serialization.

    Save/upload/delete handlers call submit() to queue a build; the build
    runs in a daemon thread so the HTTP response returns immediately. The
    client polls the job id until it reports done. At most one make runs
    per directory at a time, and jobs submitted while a build is running
    are coalesced: they wait and share the result of the next build, so
    a burst of uploads triggers one rebuild per burst instead of one
    blocking make per file. Builds in different folders are limited by
    MAKE_MAX_CONCURRENT so a wide tree cannot fork an unbounded number of
    compilers.
    """

    def __init__(self, ttl: float) -> None:
        self._lock = threading.Lock()
        self._results: OrderedDict[str, Any] = (
            OrderedDict()
        )  # job id -> [created, done, result]
        self._queues: dict[
            str, list[str]
        ] = {}  # directory (str) -> [job ids] awaiting a build
        self._active: set[str] = set()  # directories with a build running
        self._slots = threading.BoundedSemaphore(MAKE_MAX_CONCURRENT)
        self._ttl = ttl

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, (t, _d, _r) in self._results.items() if now - t > self._ttl]:
            del self._results[k]

    def submit(self, dirp: Path) -> str:
        """Queue a build of dirp and return the job id to poll for."""
        d = str(dirp)
        job = uuid.uuid4().hex
        with self._lock:
            self._sweep()
            self._results[job] = [time.monotonic(), False, None]
            self._queues.setdefault(d, []).append(job)
            start = d not in self._active
        if start:
            self._build(d)
        return job

    def _build(self, d: str) -> None:
        with self._lock:
            q = self._queues.get(d)
            if not q or d in self._active:
                return
            self._active.add(d)
            batch = q[:]
            q.clear()

        def work():
            try:
                with self._slots:
                    result = run_make_if_present(Path(d))
            except HTTPError as e:
                result = {
                    "code": None,
                    "timed_out": False,
                    "truncated": False,
                    "output": str(e),
                    "cwd": "",
                }
            with self._lock:
                for j in batch:
                    e = self._results.get(j)
                    if e is not None:
                        e[1] = True
                        e[2] = result
                self._active.discard(d)
                left = self._queues.get(d)
                again = bool(left)
                if left is not None and not again:
                    del self._queues[d]  # keep the dict from growing per folder
            if again:
                self._build(d)

        threading.Thread(target=work, daemon=True, name="fm-make").start()

    def get(self, job: str) -> tuple[bool, RunResult | None] | None:
        """(done, result) for a job, or None if unknown or expired."""
        with self._lock:
            if len(self._results) > 128:
                self._sweep()  # pollers keep the table tidy without a sweeper thread
            e = self._results.get(job)
            return (e[1], e[2]) if e else None


MAKE_JOBS = _MakeJobs(ttl=RUN_MAX_TIMEOUT)


class _ThumbCache:
    """Thread-safe LRU cache of rendered thumbnails.

    Keyed by (path, mtime_ns, size), so an edited or replaced image
    invalidates its own entry automatically. Bounded in entry count and
    total bytes.
    """

    def __init__(self, max_entries: int, max_bytes: int) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[ThumbKey, ThumbVal] = (
            OrderedDict()
        )  # key -> (etag, ctype, data)
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: ThumbKey) -> ThumbVal | None:
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                return None
            self._entries.move_to_end(key)
            return e

    def put(self, key: ThumbKey, etag: str, ctype: str, data: bytes) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old[2])
            self._entries[key] = (etag, ctype, data)
            self._bytes += len(data)
            while (
                len(self._entries) > self._max_entries or self._bytes > self._max_bytes
            ):
                _k, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted[2])


THUMB_CACHE = _ThumbCache(THUMB_CACHE_MAX_ENTRIES, THUMB_CACHE_MAX_BYTES)


def unique_dest(dirp: Path, name: str) -> Path:
    """Avoid silently overwriting on upload: append ' (1)', ' (2)', ..."""
    dest = dirp / name
    if not dest.exists():
        return dest
    base, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = dirp / f"{base} ({i}){ext}"
        if not cand.exists():
            return cand
        i += 1


def validate_new_name(name: str) -> str:
    """Reject names that would break the filesystem or escape the directory,
    and return the cleaned name (callers must use the result, otherwise a
    name like " foo " passes validation and is then created with the
    spaces)."""
    cleaned = name.strip()
    if not cleaned:
        raise HTTPError(400, "Name cannot be empty")
    if cleaned in (".", ".."):
        raise HTTPError(400, "Invalid name")
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise HTTPError(400, "Name cannot contain path separators")
    return cleaned


def dir_size(p: Path, budget: int | None = None) -> tuple[int, int, bool]:
    """Recursively sum the size of all regular files under p (bytes).

    Returns (total, entries_scanned, complete). Symlinks (to files or
    directories) are not counted or followed. Unreadable entries are skipped.
    One stat per directory entry. When budget is given the walk stops after
    that many entries and complete is False, so one request can never be
    forced to walk an unbounded tree.
    """
    total = 0
    seen = 0
    stack = [p]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for de in it:
                if budget is not None and seen >= budget:
                    return total, seen, False
                seen += 1
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                if stat.S_ISDIR(mode):
                    stack.append(de.path)
                elif stat.S_ISREG(mode):
                    total += st.st_size
    return total, seen, True


class _DirSizeCache:
    """Short-lived cache of recursive folder sizes.

    Every listing walks all subfolders again (a .git or node_modules tree is
    re-read on every click), so results are reused for a few seconds. Nothing
    is stored on disk and the TTL is short, so a changed tree shows up on the
    next refresh; the app stays stateless in the sense the README means.
    """

    def __init__(self, ttl: float, max_entries: int = 4096) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, int, bool]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[int, bool] | None:
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            if time.monotonic() - hit[0] > self._ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return hit[1], hit[2]

    def put(self, key: str, size: int, complete: bool) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), size, complete)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """Drop key, everything under it and everything containing it.

        Called after a write so the listing next to the changed file does not
        keep showing a size from before the edit.
        """
        with self._lock:
            prefix = key.rstrip(os.sep) + os.sep
            for k in [
                k
                for k in self._entries
                if k == key or k.startswith(prefix) or key.startswith(k + os.sep)
            ]:
                del self._entries[k]


DIR_SIZES = _DirSizeCache(DIR_SIZE_CACHE_TTL)


def sizes_changed(p: Path) -> None:
    """Forget cached recursive sizes affected by a change inside p."""
    DIR_SIZES.invalidate(str(p))


def _copy_capped(src: BinaryIO, out: BinaryIO, room: int, name: str) -> int:
    """Copy at most room bytes from src to out, refusing a bigger entry.

    The declared size in a zip header is only a hint, so the real byte count
    is what decides: a small archive whose headers lie can still not write
    more than the configured limit.
    """
    written = 0
    while True:
        chunk = src.read(1024 * 1024)
        if not chunk:
            break
        written += len(chunk)
        if written > room:
            raise HTTPError(
                413,
                f"Zip entry {name!r} expands beyond the "
                f"{fmt_size(MAX_ZIP_TOTAL_SIZE)} extract limit",
            )
        out.write(chunk)
    return written


def extract_zip(zip_path: Path, dest_dir: Path) -> dict[str, Any]:
    """Extract zip_path into dest_dir, refusing entries that would escape
    it (zip-slip) or overwrite the archive itself. Returns a summary.

    Sizes are checked twice: the declared sizes are summed up front (so an
    obvious zip bomb is refused before writing anything) and the bytes
    actually written are capped while copying (so lying headers cannot
    expand past the limit).
    """
    dest_dir = dest_dir.resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise HTTPError(400, "Not a valid zip file")
    with zf:
        infos = zf.infolist()
        declared = 0
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > MAX_ZIP_ENTRY_SIZE:
                raise HTTPError(
                    413,
                    f"Zip entry {info.filename!r} is {fmt_size(info.file_size)} "
                    f"and exceeds the per-entry limit "
                    f"({fmt_size(MAX_ZIP_ENTRY_SIZE)})",
                )
            declared += info.file_size
        if declared > MAX_ZIP_TOTAL_SIZE:
            raise HTTPError(
                413,
                f"This zip expands to {fmt_size(declared)}, over the "
                f"{fmt_size(MAX_ZIP_TOTAL_SIZE)} extract limit",
            )
        entries = 0
        total = 0
        room = MAX_ZIP_TOTAL_SIZE
        for info in infos:
            member = info.filename.replace("\\", "/")
            target = (dest_dir / member).resolve()
            if target != dest_dir and dest_dir not in target.parents:
                raise HTTPError(
                    400, f"Unsafe zip entry (path escapes folder): {info.filename}"
                )
            if target == zip_path:
                raise HTTPError(
                    400,
                    f"Zip entry would overwrite the archive itself: {info.filename}",
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                written = _copy_capped(src, out, room, info.filename)
            room -= written
            # Restore the stored unix permissions (e.g. exec bits).
            attr = info.external_attr >> 16
            if attr:
                try:
                    os.chmod(target, attr & 0o7777)
                except OSError:
                    pass
            entries += 1
            total += written
    return {"entries": entries, "bytes": total}


# The base64 alphabet, '=' included because padding belongs to a payload (it
# only ever ends one), and the two byte sets bytes.translate() drops while
# looking for it: whitespace, because line-wrapped base64 contains some.
_B64_CHARS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
_B64_WS = b" \t\r\n"
_B64_JUNK = bytes(b for b in range(256) if b not in _B64_CHARS)
_B64_EXPECTED = 'Expected a JSON body of the form {"data": "<base64>"}'


def _b64_group(data: bytes) -> bytes:
    """Decode a whole number of base64 groups, padding the last one."""
    try:
        # Characters are checked by the caller; strict_mode catches what is
        # left: a stray bit of padding in the middle, a lone leftover digit.
        return binascii.a2b_base64(data + b"=" * (-len(data) % 4), strict_mode=True)
    except (binascii.Error, ValueError):
        raise HTTPError(400, "Invalid base64 payload")


class _B64PayloadReader:
    """Pull a base64 payload out of `{"data":"…"}` while the body arrives.

    /api/upload64 used to json.loads() the whole body and decode the string it
    found: the 1 GB file at the top of the transfer limit arrives as a 1.4 GB
    body, so one upload needed several gigabytes of RAM. Here the envelope in
    front of the value is scanned once, the payload is decoded in whole groups
    of four characters as each chunk lands, and only the beginning of an
    unfinished group is carried over, so memory does not follow the file.

    Whatever comes after the closing quote is read but ignored, which also
    drains the socket: that is what keeps the connection usable afterwards.
    """

    HEAD_MAX = 64 * 1024  # envelope bytes to buffer before calling it a lie
    PROBE = 4 * 1024  # ... and bytes at the start of the value, for data URLs

    def __init__(self) -> None:
        self._head = bytearray()  # scanned for the start of the value
        self._group = bytearray()  # payload characters short of a group of four
        self._esc = b""  # a backslash at the end of a chunk, waiting for its letter
        self._in_value = False
        self._ended = False  # saw the closing quote, or the padding
        self._chars = 0  # payload characters seen, so an empty one is caught

    def feed(self, chunk: bytes) -> bytes:
        """Decoded bytes for one chunk of the request body (may be none)."""
        if not chunk or self._ended:
            return b""
        if not self._in_value:
            self._head += chunk
            start = self._value_start()
            if start is None:
                return b""  # the envelope has not arrived in full yet
            chunk = bytes(self._head[start:])
            self._head = bytearray()
            self._in_value = True
        quote = chunk.find(b'"')
        if quote < 0:
            return self._decode(chunk)
        data = self._decode(chunk[:quote])
        self._ended = True
        return data

    def finish(self) -> bytes:
        """Decode the last partial group, once the body has been read."""
        if not self._in_value:
            raise HTTPError(400, _B64_EXPECTED)
        if not self._ended:
            raise HTTPError(400, "The base64 payload ended without its quote")
        if not self._chars:
            raise HTTPError(400, "Missing 'data' field (base64 string)")
        rest = bytes(self._group)
        self._group.clear()
        return _b64_group(rest) if rest else b""

    def _value_start(self) -> int | None:
        """Where the payload begins in _head; None while more is still needed.

        The key is looked for as `"data"`, quotes included: inside a JSON
        string a quote is always written `\\"`, so this cannot match text.
        """
        lead = self._head.lstrip(_B64_WS)
        if lead and not lead.startswith(b"{"):
            raise HTTPError(400, _B64_EXPECTED)
        key = self._head.find(b'"data"')
        if key < 0:
            if len(self._head) > self.HEAD_MAX:
                raise HTTPError(400, _B64_EXPECTED)
            return None
        quote = self._head.find(b'"', key + len(b'"data"'))
        if quote < 0:
            if len(self._head) > self.HEAD_MAX:
                raise HTTPError(400, _B64_EXPECTED)
            return None
        value = bytes(self._head[quote + 1 :])
        if value.startswith(b"data:"):  # a data URL is tolerated, as before
            comma = value.find(b",")
            if comma < 0:
                if len(value) > self.PROBE:
                    raise HTTPError(400, "The data: URL in the body has no comma")
                return None
            return quote + 1 + comma + 1
        if len(value) < 5 and b"data:".startswith(value):
            return None  # too little of the value to tell the two apart
        return quote + 1

    def _decode(self, chunk: bytes) -> bytes:
        # Two C-level passes instead of a per-character loop: this runs over
        # every byte of the upload and a Python loop would cost minutes.
        chunk = self._esc + chunk
        self._esc = b""
        if chunk.endswith(b"\\"):
            self._esc = b"\\"  # the escape itself straddles the boundary
            chunk = chunk[:-1]
        if b"\\" in chunk:
            # A payload written by json.dumps(base64.encodebytes(...)) arrives
            # with its line breaks escaped, as "\n" rather than a newline byte.
            chunk = (
                chunk.replace(b"\\n", b"\n")
                .replace(b"\\r", b"\r")
                .replace(b"\\t", b" ")
            )
        tight = chunk.translate(None, _B64_WS)
        if len(tight.translate(None, _B64_JUNK)) != len(tight):
            raise HTTPError(400, "Invalid base64 payload")
        data = bytes(self._group) + tight if self._group else tight
        pad = data.find(b"=")
        if pad >= 0:
            # Padding ends a payload: what follows it is JSON, not base64.
            end = (pad // 4 + 1) * 4
            self._group.clear()
            self._ended = True
            body, after = data[:end], data[end:]
        else:
            keep = len(data) % 4
            self._group = bytearray(data[len(data) - keep :]) if keep else bytearray()
            body, after = data[: len(data) - keep], b""
        self._chars += len(data) - len(after)
        return _b64_group(body) if body else b""


class _ChunkedZipSink:
    """Zip-file destination that streams over HTTP chunked transfer.

    zipfile treats a file object without tell()/seek() as non-seekable
    and writes data descriptors, so the archive can be produced and sent
    byte by byte — no temp file, no need to know the final size up front.
    """

    def __init__(self, sink: BinaryIO) -> None:
        self.sink = sink
        self._closed = False

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        self.sink.write(b"%X\r\n" % len(data) + data + b"\r\n")
        return len(data)

    def flush(self) -> None:
        self.sink.flush()

    def close(self) -> None:
        if not self._closed:
            self.sink.write(b"0\r\n\r\n")
            self._closed = True


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single "bytes=a-b", "bytes=a-" or "bytes=-n" range.

    Returns an inclusive (start, end) pair, or None when the header asks for
    something this server does not slice (a multi-range list, an unknown
    unit, an unsatisfiable range). Returning None means "send the whole
    file", which is always a valid response.
    """
    if size <= 0:
        return None
    unit, _sep, spec = header.partition("=")
    spec = spec.strip()
    if unit.strip().lower() != "bytes" or not spec or "," in spec:
        return None
    low, _dash, high = spec.partition("-")
    try:
        if low == "":
            suffix = int(high)
            if suffix <= 0:
                return None
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(low)
            end = int(high) if high else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                return None
    except ValueError:
        return None
    return start, end


def content_disposition(mode: str, name: str) -> str:
    """Build a Content-Disposition value with an ASCII fallback plus the
    RFC 6266 filename*, so a non-ASCII name survives everywhere instead of
    arriving percent-encoded."""
    fallback = "".join(
        ch if ch.isascii() and ch.isprintable() and ch != '"' else " "
        for ch in name.replace("\r", " ").replace("\n", " ")
    ).strip()
    if not fallback:
        fallback = "download"
    return f"{mode}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def features() -> dict[str, bool]:
    """Feature switches the server enforces, mirrored into the page."""
    return {
        "shell": ENABLE_SHELL,
        "run": ENABLE_RUN,
        "make": ENABLE_MAKE,
        "b64": ENABLE_B64,
        "write": ENABLE_WRITE,
        "dav": ENABLE_DAV,
    }


def dav_mount() -> str:
    """The URL path a WebDAV client mounts, "" when there is none.

    The page shows this so the Help popup can print a URL that is right
    behind a reverse proxy as well as when the app is reached directly.
    """
    return f"/{DAV_PREFIX}/" if ENABLE_DAV else ""


def dav_limits() -> dict[str, int]:
    """The few tuning values the Help popup quotes, so it cannot drift."""
    return {
        "transfer": MAX_TRANSFER_SIZE,
        "text": MAX_TEXT_SIZE,
        "body": MAX_JSON_BODY_SIZE,
        "b64": MAX_B64_FILE_SIZE,
        "b64Body": MAX_B64_BODY_SIZE,
        "zipEntry": MAX_ZIP_ENTRY_SIZE,
        "zipTotal": MAX_ZIP_TOTAL_SIZE,
        "list": LIST_MAX_ENTRIES,
        "davEntries": DAV_MAX_ENTRIES,
        "runTimeout": RUN_TIMEOUT,
        "runMax": RUN_MAX_TIMEOUT,
        "socket": READ_TIMEOUT,
        "lockTtl": DAV_LOCK_TTL,
    }


def render_thumbnail(p: Path) -> tuple[str, bytes]:
    """Render p as a thumbnail no wider/taller than THUMB_SIZE.

    Returns (image subtype, bytes), e.g. ("jpeg", ...). SVG is passed
    through untouched: the browser draws it itself, so SVG previews work
    without Pillow.
    """
    ext = p.suffix.lower()
    if ext == ".svg":
        try:
            size = p.stat().st_size
        except OSError:
            raise HTTPError(404, "File not found")
        if size > SVG_THUMB_LIMIT:
            raise HTTPError(
                413,
                f"SVG is too large to preview "
                f"(limit {SVG_THUMB_LIMIT // (1024 * 1024)} MB)",
            )
        return ("svg+xml", p.read_bytes())
    try:
        from PIL import Image
    except ImportError:
        raise HTTPError(
            501, "Pillow is not installed; thumbnails unavailable (pip install Pillow)"
        )
    try:
        # Refuse absurdly large images outright (Pillow only warns by default)
        # and let the JPEG decoder downscale while reading: a 48 MP photo costs
        # about the same as a 120 px one when it is never decoded full size.
        Image.MAX_IMAGE_PIXELS = THUMB_MAX_PIXELS
        with Image.open(p) as im:
            im.draft(None, THUMB_SIZE)
            im.load()
            from PIL import ImageOps

            with contextlib.suppress(Exception):  # odd EXIF is common
                im = ImageOps.exif_transpose(im)  # orientation-correct photos
            im.thumbnail(THUMB_SIZE)
            want_alpha = (
                "A" in im.mode
                or im.mode == "P"
                or ext in (".png", ".gif", ".webp", ".ico")
            )
            buf = io.BytesIO()
            if want_alpha:
                if im.mode not in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                im.save(buf, "PNG")
                return ("png", buf.getvalue())
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(buf, "JPEG", quality=82)
            return ("jpeg", buf.getvalue())
    except HTTPError:
        raise
    except Exception:  # noqa: BLE001 - Pillow raises freely on odd files
        # Pillow throws whatever it likes at a bad image: UnidentifiedImageError
        # for a file it cannot recognise, DecodingError from its C decoders,
        # DecompressionBombError above MAX_IMAGE_PIXELS, and plain OSErrors for
        # truncated files. They are all "this file is not a viewable image".
        raise HTTPError(
            400, "Could not decode this image (corrupt, unsupported, or too large)"
        )


# The page is static apart from a handful of placeholders, so it is rendered
# once.
_PAGE_HTML: bytes | None = None


def page_html() -> bytes:
    """The embedded front-end, with the root name and tuning values in."""
    global _PAGE_HTML
    if _PAGE_HTML is None:
        _PAGE_HTML = (
            HTML_TEMPLATE.replace(
                "__ROOT_NAME__", json.dumps(ROOT.name or str(ROOT), ensure_ascii=False)
            )
            .replace("__FEATURES__", json.dumps(features()))
            .replace("__MAKE_POLL_MS__", str(MAKE_JOB_POLL_MS))
            .replace("__DAV_MOUNT__", json.dumps(dav_mount()))
            .replace("__DAV_LIMITS__", json.dumps(dav_limits()))
            .encode("utf-8")
        )
    return _PAGE_HTML


# ---------------------------------------------------------------------------
# WebDAV (a deliberately small RFC 4918 subset: the target client is davfs2)
# ---------------------------------------------------------------------------
# What is implemented: OPTIONS, PROPFIND (Depth 0/1), PROPPATCH, MKCOL, PUT,
# DELETE, MOVE, COPY, LOCK, UNLOCK, GET and HEAD. That is compliance class 2:
# locks exist so a file open on the mount cannot be silently clobbered from
# the browser, and every write carries or checks an ETag so a lost update is
# refused instead of won.
#
# What is not: shared/read locks, Depth: infinite (treated as 1), custom
# properties (PROPPATCH keeps the exec bit and honestly refuses anything else),
# partial (Content-Range) uploads, Microsoft's extensions and report/search.
# None of those are used by davfs2, and each would need a client that misses it.
DAV_NS = "DAV:"
# Where Apache's mod_dav put the executable property, and where davfs2 looks
# for it (it asks for this one by name and spells the value "T" or "F").
DAV_APACHE_NS = "http://apache.org/dav/props/"
ET.register_namespace("D", DAV_NS)
ET.register_namespace("apache", DAV_APACHE_NS)

DAV_ALLOW = (
    "OPTIONS, PROPFIND, PROPPATCH, MKCOL, PUT, DELETE, MOVE, COPY, LOCK, UNLOCK, "
    "GET, HEAD"
)
# The properties PROPFIND answers are whatever dav_known_props() builds; a
# client asking for anything else (a custom property) gets a 404 propstat back,
# which is how it learns that this server does not store it. DAV:executable is
# the one property that is stored: it is the file's own exec bit, so reporting
# it costs nothing and PROPPATCH can write it back with a chmod.
DAV_STATUS_TEXT = {200: "OK", 403: "Forbidden", 404: "Not Found"}
# The spellings XML gives a boolean false, for a PROPPATCH that writes the
# exec bit instead of just being told "yes".
DAV_FALSE = frozenset({"f", "false", "no", "0"})


def _dq(tag: str) -> str:
    """A fully-qualified WebDAV element name, for ElementTree."""
    return f"{{{DAV_NS}}}{tag}"


def _aq(tag: str) -> str:
    """The same, in the namespace Apache and davfs2 use for the exec bit."""
    return f"{{{DAV_APACHE_NS}}}{tag}"


# Both spellings of the executable bit, so neither kind of client is told it
# does not exist. They are the same fact about the same file.
DAV_EXEC_PROPS = (_dq("executable"), _aq("executable"))


def dav_href(rel: str, is_dir: bool) -> str:
    """The URL of an entry under the mount.

    The trailing slash is part of the contract: it is how clients tell a
    collection from a resource, so a folder without one disappears from the
    listing or shows up as an empty file.
    """
    href = "/" + DAV_PREFIX
    if rel:
        href += "/" + quote(rel, safe="/")
    if is_dir and not href.endswith("/"):
        href += "/"
    return href


def dav_etag(st: os.stat_result) -> str:
    """A strong ETag from the two fields that change when content changes."""
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


def dav_iso(ts: float) -> str:
    """creationdate, which the spec mandates as an ISO 8601 timestamp."""
    # datetime.UTC would be shorter but is 3.11, and this file runs on 3.10.
    return datetime.fromtimestamp(ts, timezone.utc).strftime(  # noqa: UP017
        "%Y-%m-%dT%H:%M:%SZ"
    )


def dav_known_props(p: Path, st: os.stat_result, is_dir: bool) -> dict:
    """The live properties of one entry, keyed by fully-qualified name."""
    props: dict[str, Any] = {}
    rtype = ET.Element(_dq("resourcetype"))
    if is_dir:
        ET.SubElement(rtype, _dq("collection"))
    props[_dq("resourcetype")] = rtype
    if not is_dir:
        props[_dq("getcontentlength")] = str(st.st_size)
        ctype, _ = mimetypes.guess_type(p.name)
        props[_dq("getcontenttype")] = ctype or "application/octet-stream"
    # A collection gets an ETag too: it changes whenever the folder's contents
    # change, and clients that ask for one are happier than those that get a
    # 404 propstat back.
    props[_dq("getetag")] = dav_etag(st)
    props[_dq("getlastmodified")] = formatdate(st.st_mtime, usegmt=True)
    props[_dq("creationdate")] = dav_iso(created_time(st))
    props[_dq("displayname")] = p.name or (ROOT.name or "/")
    if stat.S_ISREG(st.st_mode) and st.st_mode & 0o111:
        # The executable bit, which is the only property with somewhere to live
        # because it *is* the file. Both spellings are offered: RFC 3253 put it
        # in DAV: and means "true" by the element being there, while Apache's
        # mod_dav - and davfs2, which follows it - use its own namespace and
        # spell the value out. Anything that is not a plain file has no bit to
        # report, not even a link whose mode happens to be 777.
        props[_dq("executable")] = ET.Element(_dq("executable"))
        props[_aq("executable")] = "T"
    return props


def dav_lock_node(lock: dict) -> ET.Element:
    """An <D:activelock> describing one held lock."""
    node = ET.Element(_dq("activelock"))
    ET.SubElement(ET.SubElement(node, _dq("locktype")), _dq("write"))
    ET.SubElement(ET.SubElement(node, _dq("lockscope")), _dq("exclusive"))
    ET.SubElement(node, _dq("depth")).text = lock["depth"]
    if lock["owner"]:
        ET.SubElement(node, _dq("owner")).text = lock["owner"]
    ET.SubElement(node, _dq("timeout")).text = f"Second-{lock['timeout']}"
    ET.SubElement(ET.SubElement(node, _dq("locktoken")), _dq("href")).text = lock[
        "token"
    ]
    ET.SubElement(ET.SubElement(node, _dq("lockroot")), _dq("href")).text = lock["href"]
    return node


def dav_supported_locks() -> ET.Element:
    node = ET.Element(_dq("supportedlock"))
    entry = ET.SubElement(node, _dq("lockentry"))
    ET.SubElement(ET.SubElement(entry, _dq("lockscope")), _dq("exclusive"))
    ET.SubElement(ET.SubElement(entry, _dq("locktype")), _dq("write"))
    return node


def dav_response(
    p: Path,
    rel: str,
    st: os.stat_result,
    is_dir: bool,
    all_props: bool,
    wanted: set[str],
) -> ET.Element:
    """One <D:response> for an entry: the properties that exist here, and a
    second propstat listing the requested properties that do not."""
    props = dav_known_props(p, st, is_dir)
    discovery = ET.Element(_dq("lockdiscovery"))
    for lock in LOCKS.on_path(str(p)):
        discovery.append(dav_lock_node(lock))
    props[_dq("lockdiscovery")] = discovery
    props[_dq("supportedlock")] = dav_supported_locks()

    known = set(props)
    ok = [name for name in props if all_props or name in wanted]
    missing = sorted(name for name in wanted if name not in known)

    resp = ET.Element(_dq("response"))
    ET.SubElement(resp, _dq("href")).text = dav_href(rel, is_dir)
    propstat = ET.SubElement(resp, _dq("propstat"))
    prop = ET.SubElement(propstat, _dq("prop"))
    for name in ok:
        value = props[name]
        if isinstance(value, ET.Element):
            prop.append(value)
        else:
            ET.SubElement(prop, name).text = value
    ET.SubElement(propstat, _dq("status")).text = "HTTP/1.1 200 OK"
    if missing:
        # Honest "this server does not store that": a client asking for its own
        # properties (or for the exec bit of a file that has none) is not
        # silently answered with a property that just is not there.
        propstat = ET.SubElement(resp, _dq("propstat"))
        prop = ET.SubElement(propstat, _dq("prop"))
        for name in missing:
            ET.SubElement(prop, name)
        ET.SubElement(propstat, _dq("status")).text = "HTTP/1.1 404 Not Found"
    return resp


def dav_requested_props(body: bytes) -> tuple[bool, set[str]]:
    """(<want everything>, {names}) from a PROPFIND request body.

    A missing or empty body and <allprop/> both mean "everything"; so does
    <propname/>, because handing back values the client did not ask for is
    harmless while withholding them is not.
    """
    if not body.strip():
        return True, set()
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise HTTPError(400, "Malformed PROPFIND request body")
    if root.tag != _dq("propfind"):
        raise HTTPError(400, "Not a PROPFIND request body")
    if root.find(_dq("allprop")) is not None or root.find(_dq("propname")) is not None:
        return True, set()
    wanted: set[str] = set()
    for prop in root.iter(_dq("prop")):
        for child in prop:
            wanted.add(child.tag)
    if not wanted:
        return True, set()
    return False, wanted


_DAV_IF_ITEM = re.compile(r"\(([^()]*)\)|<([^>]*)>")
_DAV_IF_INNER = re.compile(r"<([^>]*)>|\"([^\"]*)\"|([^\s\[\]()+]+)")


def dav_conditionals(header: str | None) -> tuple[set[str], set[str]]:
    """Tokens and entity tags from an `If:` header.

    The full grammar (RFC 4918 §10.4: lists, tuples and path literals that
    apply to different resources) is far more than davfs2 ever sends: one
    lock token and, when it wants to avoid losing an update, one entity tag,
    both for the request URI. So every token and every etag in the header is
    collected and either may satisfy the condition. That is looser than the
    spec for a multi-group header and identical for everything davfs2 sends.
    A `<...>` outside parentheses is a path literal and is ignored.
    """
    tokens: set[str] = set()
    etags: set[str] = set()
    if not header:
        return tokens, etags
    for inside, _literal in _DAV_IF_ITEM.findall(header):
        if not inside:
            continue
        for token, etag, bare in _DAV_IF_INNER.findall(inside):
            if token:
                tokens.add(token)
            elif etag:
                etags.add(f'"{etag}"')  # quoted, as an ETag header spells it
            else:
                # (opaquelocktoken:...) — a token written bare, which several
                # clients do instead of wrapping it in angle brackets.
                tokens.add(bare)
    return tokens, etags


def dav_header_tokens(header: str | None) -> set[str]:
    """Tokens from a Lock-Token header (it holds one, but be forgiving)."""
    if not header:
        return set()
    return {part.strip().strip("<>") for part in header.split(",") if part.strip()}


def dav_etag_listed(header: str | None, etag: str | None) -> bool:
    """True when an If-Match / If-None-Match header names this ETag.

    A weak indicator is ignored on purpose: davfs2 echoes back whatever
    ETag it got, and treating W/"x" as "x" is what makes its lost-update
    check work against a server that cannot keep a token per stored version.
    """
    if not header or not header.strip():
        return False
    for part in header.split(","):
        part = part.strip()
        if part.startswith("W/"):
            part = part[2:].strip()
        if part == "*" or (etag is not None and part == etag):
            return True
    return False


def dav_lock_info(body: bytes) -> tuple[str, str, str]:
    """(scope, type, owner) from a <D:lockinfo> body."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise HTTPError(400, "Malformed lock request body")
    if root.tag != _dq("lockinfo"):
        raise HTTPError(400, "Not a lock request body")
    scope = "exclusive" if root.find(f".//{_dq('exclusive')}") is not None else "shared"
    kind = "write" if root.find(f".//{_dq('write')}") is not None else "read"
    owner = ""
    node = root.find(f".//{_dq('owner')}")
    if node is not None:
        owner = " ".join(" ".join(node.itertext()).split())[:200]
    return scope, kind, owner


def dav_patch_tags(body: bytes) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The properties a <D:propertyupdate> body sets and removes.

    Each name comes with the element's text because that is how a client says
    "false": <executable>F</executable> asks for the opposite of <executable/>.
    """
    if not body.strip():
        return [], []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise HTTPError(400, "Malformed PROPPATCH request body")
    if root.tag != _dq("propertyupdate"):
        raise HTTPError(400, "Not a PROPPATCH request body")
    found: dict[str, list[tuple[str, str]]] = {"set": [], "remove": []}
    for verb in ("set", "remove"):
        for block in root.findall(_dq(verb)):
            for prop in block.findall(_dq("prop")):
                for child in prop:
                    entry = (child.tag, " ".join((child.text or "").split()).lower())
                    if entry not in found[verb]:
                        found[verb].append(entry)
    return found["set"], found["remove"]


def dav_if_modified(header: str | None, st: os.stat_result) -> bool | None:
    """True when the file is unchanged since the date a header names.

    None means the header is absent or unreadable, which is treated as
    "no condition". Comparison is at second resolution because that is what
    an HTTP date carries.
    """
    if not header or not header.strip():
        return None
    try:
        since = parsedate_to_datetime(header.strip())
    except (TypeError, ValueError):
        return None
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)  # noqa: UP017 (3.10 has no UTC)
    return int(st.st_mtime) <= int(since.timestamp())


def dav_reason_header(value: str | None, name: str) -> str:
    return (value or name).strip()[:200]


class _Locks:
    """WebDAV write locks, held in memory only.

    davfs2 locks a file when a program opens it for writing and refreshes the
    lock before its timeout expires. Keeping those locks and honouring them
    (in both directions: the mount may not clobber a file the browser locked,
    the browser may not clobber one the mount locked) is the whole reason to
    implement locking: without them, an editor on the mount and the browser
    silently race each other.

    A lock that is never refreshed dies with its timeout, so a crashed client
    or a yanked cable cannot block the file forever. Like the thumbnail and
    folder-size caches this table is bounded, TTL-expired and purely in
    memory: a restart clears it and nothing has to be migrated.
    """

    def __init__(self, ttl: int, max_entries: int) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._by_token: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def _sweep(self) -> None:
        now = time.monotonic()
        for token in [t for t, l in self._by_token.items() if now >= l["expires"]]:
            del self._by_token[token]

    def timeout(self, header: str | None) -> int:
        """Seconds from a `Timeout: Second-1800` header, capped."""
        value = (header or "").strip().lower()
        if value.startswith("second-"):
            try:
                asked = int(value[len("second-") :].strip())
            except ValueError:
                asked = self._ttl
            return max(30, min(asked, DAV_LOCK_MAX_TIMEOUT))
        return self._ttl  # Infinity and anything unparsable

    def add(self, path: str, href: str, depth: str, owner: str, timeout: int) -> dict:
        token = f"opaquelocktoken:{uuid.uuid4()}"
        record = {
            "token": token,
            "path": path,
            "href": href,
            "depth": depth,
            "owner": owner or "unknown",
            "timeout": timeout,
            "expires": time.monotonic() + timeout,
        }
        with self._lock:
            self._sweep()
            self._by_token[token] = record
            while len(self._by_token) > self._max_entries:
                self._by_token.popitem(last=False)
        return record

    def get(self, token: str) -> dict | None:
        with self._lock:
            self._sweep()
            record = self._by_token.get(token)
            if record is not None:
                self._by_token.move_to_end(token)
            return record

    def on_path(self, path: str) -> list[dict]:
        with self._lock:
            self._sweep()
            return [l for l in self._by_token.values() if l["path"] == path]

    def holder(self, path: str) -> dict | None:
        held = self.on_path(path)
        return held[-1] if held else None

    def refresh(self, token: str, timeout: int) -> dict | None:
        """Extend a lock a client already holds (a WebDAV "refresh")."""
        record = self.get(token)
        if record is None:
            return None
        with self._lock:
            record["timeout"] = timeout
            record["expires"] = time.monotonic() + timeout
        return record

    def blocking(self, path: str, tokens: set[str]) -> dict | None:
        """The lock that stops an untokened write to path, if there is one.

        A lock with Depth: infinity covers everything underneath it, so the
        parent folders are checked as well.
        """
        with self._lock:
            self._sweep()
            held = list(self._by_token.values())
        current = Path(path)
        for _ in range(64):  # bounded, so a deep tree cannot spin here
            for record in held:
                if record["token"] in tokens or record["path"] != str(current):
                    continue
                # The lock is on this path: it blocks the request itself, or,
                # when it covers a whole tree, everything inside it too.
                if path == str(current) or record["depth"] == "infinity":
                    return record
            if current.parent == current:
                break
            current = current.parent
        return None

    def drop(self, token: str) -> bool:
        with self._lock:
            return self._by_token.pop(token, None) is not None

    def drop_blocking(self, path: str) -> int:
        """Drop every lock that stops an untokened write to path.

        That is a lock on the path itself and a Depth: infinity lock on one of
        its parent folders. Breaking the ancestor's lock is a wider hammer than
        the Unlock button sounds like, but it is the only lock the caller can
        see: the listing says "this folder is locked", and dropping the one on
        the folder above is what frees it.
        """
        with self._lock:
            self._sweep()
            gone = [
                token
                for token, record in self._by_token.items()
                if record["path"] == path
                or (
                    record["depth"] == "infinity"
                    and path.startswith(record["path"].rstrip(os.sep) + os.sep)
                )
            ]
            for token in gone:
                del self._by_token[token]
            return len(gone)

    def move(self, old: str, new: str) -> None:
        """Follow an entry that was renamed or moved out from under a lock.

        Locks on entries inside a moved folder travel with it: they were taken
        through a path that no longer exists, and dropping them silently would
        hand out write access while a client still believes it holds the file,
        or block a new file that happens to land on the old name.
        """
        old = old.rstrip(os.sep)
        new = new.rstrip(os.sep)
        prefix = old + os.sep
        with self._lock:
            for record in self._by_token.values():
                path = record["path"].rstrip(os.sep)
                if path == old:
                    record["path"] = new
                elif path.startswith(prefix):
                    record["path"] = new + os.sep + path[len(prefix) :]

    def drop_subtree(self, path: str) -> int:
        prefix = path.rstrip(os.sep) + os.sep
        with self._lock:
            gone = [
                t
                for t, l in self._by_token.items()
                if l["path"] == path or l["path"].startswith(prefix)
            ]
            for token in gone:
                del self._by_token[token]
            return len(gone)

    def count(self) -> int:
        with self._lock:
            self._sweep()
            return len(self._by_token)

    def snapshot(self) -> dict[str, list[dict]]:
        """path -> locks, for /api/list; the handler only reads this."""
        with self._lock:
            self._sweep()
            out: dict[str, list[dict]] = {}
            for record in self._by_token.values():
                out.setdefault(record["path"], []).append(dict(record))
            return out


LOCKS = _Locks(ttl=DAV_LOCK_TTL, max_entries=DAV_MAX_LOCKS)


# ---------------------------------------------------------------------------
# WebDAV verbs
# ---------------------------------------------------------------------------
class DavMixin:
    """The WebDAV half of the handler, mixed into Handler below.

    Every entry point starts with the prefix test, so the page, the JSON API
    and the mount share one port and one served folder without ever getting
    in each other's way. Errors leave as JSON carrying the WebDAV status
    code, which is all davfs2 reads from a failed request (it reports the
    number, not the body), and that keeps one error path for the whole app —
    including the awkward part of it: an unread request body has to be
    drained or the connection closed, because davfs2 retries eagerly and a
    leftover body would be parsed as the next request.

    Locks and ETags are enforced by the JSON endpoints too, not only by the
    WebDAV verbs: that is the only reason to have them. A file open in an
    editor on the mount is refused in the browser with 423 and an Unlock
    action instead of being quietly overwritten, and the other way round.
    """

    # -- routing -----------------------------------------------------------
    def _dav_rel(self) -> str | None:
        """The decoded path under the mount, or None when this is not for it.

        Decoding happens after splitting, so a name containing a
        percent-encoded slash stays one name. No Linux name can hold a slash,
        so that only matters for names with spaces, quotes or non-ASCII
        characters — which davfs2 sends as UTF-8 with no conversion (its
        server_charset option is off by default, matching this server).
        """
        if not ENABLE_DAV:
            return None
        path = urlparse(self.path).path
        head, _slash, rest = path.lstrip("/").partition("/")
        if head != DAV_PREFIX:
            return None
        return "/".join(
            unquote(part) for part in rest.split("/") if part not in ("", ".")
        )

    def dav_dispatch(self, method: str, bodyless: bool = False) -> bool:
        """Answer the request when it belongs to the mount.

        True means a response went out; False means the caller (do_GET,
        do_HEAD, do_POST) has routes of its own left to try. bodyless is for
        HEAD, which must not send the JSON error body.
        """
        rel = self._dav_rel()
        if rel is None:
            return False
        endpoint = getattr(self, "_dav_" + method.lower(), None)
        try:
            if endpoint is None:
                raise HTTPError(405, f"{method} is not supported")
            endpoint(rel)
        except HTTPError as e:
            self._fail(e.code, e.msg, bodyless=bodyless)
        except TimeoutError:
            self._fail(408, "Timed out reading the request body", bodyless=bodyless)
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"Server error: {e}", bodyless=bodyless)
        return True

    def _dav_only(self, method: str, bodyless: bool = False) -> None:
        """A verb that exists for the mount only: 405 anywhere else."""
        if not self.dav_dispatch(method, bodyless=bodyless):
            self._fail(
                405,
                f"{method} is a WebDAV method: it only works under /{DAV_PREFIX}/ "
                "(the JSON API at /api/* uses GET and POST)",
                bodyless=bodyless,
            )

    def do_OPTIONS(self) -> None:
        self._dav_only("OPTIONS")

    def do_PUT(self) -> None:
        self._dav_only("PUT")

    def do_DELETE(self) -> None:
        self._dav_only("DELETE")

    def do_PROPFIND(self) -> None:
        self._dav_only("PROPFIND")

    def do_PROPPATCH(self) -> None:
        self._dav_only("PROPPATCH")

    def do_MKCOL(self) -> None:
        self._dav_only("MKCOL")

    def do_MOVE(self) -> None:
        self._dav_only("MOVE")

    def do_COPY(self) -> None:
        self._dav_only("COPY")

    def do_LOCK(self) -> None:
        self._dav_only("LOCK")

    def do_UNLOCK(self) -> None:
        self._dav_only("UNLOCK")

    # -- shared helpers ----------------------------------------------------
    @staticmethod
    def _dav_stat(p: Path) -> os.stat_result:
        """stat() following a symlink, or the link itself when that fails.

        A dangling link must not turn a PROPFIND into a 500: davfs2 has no
        notion of links at all, so such an entry is reported as a plain (odd)
        file rather than making a whole folder fail to list.
        """
        try:
            return p.stat()
        except OSError:
            return p.lstat()

    def _dav_target(self, rel: str) -> tuple[Path, os.stat_result]:
        """An existing entry the request names, or a 404."""
        p = safe_path(rel)
        try:
            return p, self._dav_stat(p)
        except OSError:
            raise HTTPError(404, "Not found")

    def _dav_tokens(self) -> set[str]:
        """Every lock token the request presents, in either header."""
        tokens, _etags = dav_conditionals(self.headers.get("If"))
        return tokens | dav_header_tokens(self.headers.get("Lock-Token"))

    def _dav_unlocked(self, p: Path) -> None:
        """423 unless no foreign lock covers this path."""
        held = LOCKS.blocking(str(p), self._dav_tokens())
        if held is not None:
            raise HTTPError(
                423,
                f"Locked over WebDAV by {held['owner']!r} — close the file on "
                "the mount, or use the Unlock action in the browser",
            )

    def _require_unlocked(self, p: Path) -> None:
        """The check the JSON endpoints use: a browser holds no lock tokens."""
        self._dav_unlocked(p)

    def _dav_overwrite(self) -> bool:
        """Overwrite: F means the client wants a name clash refused."""
        return (self.headers.get("Overwrite") or "T").strip().upper() != "F"

    def _dav_destination(self) -> str:
        """The relative path a Destination header points at, inside the mount.

        The header arrives as an absolute URL (that is what davfs2 sends) or
        as a path. A host other than this one is treated as a path too: the
        result is still confined to the served folder, and replying with a
        redirect would not help — davfs2 keeps follow_redirect off.
        """
        raw = (self.headers.get("Destination") or "").strip().strip("<>")
        if not raw:
            raise HTTPError(400, "MOVE and COPY need a Destination header")
        rest = urlparse(raw).path if "://" in raw else raw
        head, _slash, tail = rest.lstrip("/").partition("/")
        if head != DAV_PREFIX:
            raise HTTPError(
                400, f"Destination is outside the WebDAV mount (/{DAV_PREFIX}/...)"
            )
        dest = "/".join(unquote(p) for p in tail.split("/") if p not in ("", "."))
        if not dest:
            raise HTTPError(400, "Destination cannot be the mounted folder itself")
        return dest

    def _dav_status(self, code: int, headers: dict[str, str] | None = None) -> None:
        """A status with no body (201, 204): what most writes answer."""
        self.send_response(code)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _dav_xml(
        self, code: int, root: ET.Element, headers: dict[str, str] | None = None
    ) -> None:
        self._send(
            code,
            ET.tostring(root, encoding="utf-8", xml_declaration=True),
            "application/xml; charset=utf-8",
            headers,
        )

    def _dav_multistatus(self, responses: list[ET.Element]) -> ET.Element:
        root = ET.Element(_dq("multistatus"))
        for resp in responses:
            root.append(resp)
        return root

    def _dav_changed(self, dirp: Path) -> None:
        """React to a change that arrived over the mount.

        The make job is queued exactly as it is for a browser edit; only its
        result has nobody waiting for it, so no popup is opened for it.
        """
        self.maybe_run_make(dirp)

    @staticmethod
    def _dav_replace(p: Path) -> None:
        """Remove an entry the client is about to replace."""
        if not entry_exists(p):
            return
        if p.is_symlink() or not p.is_dir():
            p.unlink()
        else:
            shutil.rmtree(p)

    # -- verbs -------------------------------------------------------------
    def _dav_options(self, rel: str) -> None:
        """The advertisement a client sizes this server up from."""
        self.send_response(204)
        self.send_header("DAV", "1, 2")
        self.send_header("Allow", DAV_ALLOW)
        self.send_header("MS-Author-Via", "DAV")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dav_propfind(self, rel: str) -> None:
        """Report one entry, or a folder and its children (Depth 0/1).

        Depth: infinity is answered as Depth: 1: one request must not become
        an unbounded walk of a whole tree, and davfs2 never asks for more
        than a folder's own children anyway.
        """
        all_props, wanted = dav_requested_props(self._read_body(DAV_MAX_XML_BODY))
        depth = (self.headers.get("Depth") or "infinity").strip().lower()
        p, st = self._dav_target(rel)
        is_dir = stat.S_ISDIR(st.st_mode)
        responses = [dav_response(p, rel, st, is_dir, all_props, wanted)]
        if is_dir and depth != "0":
            try:
                scan = os.scandir(p)
            except OSError:
                scan = None
            if scan is not None:
                with scan:
                    for de in scan:
                        if len(responses) > DAV_MAX_ENTRIES:
                            self.log_message(
                                "PROPFIND of %r capped at %d entries",
                                rel,
                                len(responses),
                            )
                            break
                        try:
                            child_st = de.stat()
                        except OSError:
                            try:
                                child_st = de.stat(follow_symlinks=False)
                            except OSError:
                                continue  # vanished mid-listing
                        child = (rel + "/" + de.name) if rel else de.name
                        responses.append(
                            dav_response(
                                Path(de.path),
                                child,
                                child_st,
                                stat.S_ISDIR(child_st.st_mode),
                                all_props,
                                wanted,
                            )
                        )
        self._dav_xml(207, self._dav_multistatus(responses))

    def _dav_proppatch(self, rel: str) -> None:
        """Apply the one property that has somewhere to live, refuse the rest.

        That property is the executable bit, in either of its two spellings: it
        is the file's own exec bit, which is how davfs2 decides whether a file on
        the mount may be run and what chmod tries to hand back. Everything else
        has nowhere to be stored here - the app's promise is that the served
        folder is the only state - so setting it is answered 403, which davfs2
        copes with by keeping the value locally. A 200 that lied about storing it
        would be worse.
        """
        p, st = self._dav_target(rel)
        self._require_write("Changing properties")
        self._dav_unlocked(p)
        sets, removes = dav_patch_tags(self._read_body(DAV_MAX_XML_BODY))
        regular = stat.S_ISREG(st.st_mode)
        done: list[str] = []
        refused: list[str] = []
        for verb, tags in (("set", sets), ("remove", removes)):
            for tag, text in tags:
                if tag in DAV_EXEC_PROPS and regular:
                    # <executable/> means true; a client that spells the value
                    # says what it means, and "F" is as false as "false".
                    if self._dav_exec_bit(p, verb == "set" and text not in DAV_FALSE):
                        done.append(tag)
                    else:
                        refused.append(tag)
                else:
                    refused.append(tag)
        resp = ET.Element(_dq("response"))
        ET.SubElement(resp, _dq("href")).text = dav_href(rel, stat.S_ISDIR(st.st_mode))
        for tags, code in ((done, 200), (refused, 403)):
            if not tags:
                continue
            propstat = ET.SubElement(resp, _dq("propstat"))
            prop = ET.SubElement(propstat, _dq("prop"))
            for tag in tags:
                ET.SubElement(prop, tag)
            ET.SubElement(
                propstat, _dq("status")
            ).text = f"HTTP/1.1 {code} {DAV_STATUS_TEXT[code]}"
        self._dav_xml(207, self._dav_multistatus([resp]))

    def _dav_exec_bit(self, p: Path, on: bool) -> bool:
        """Move the exec bit for a PROPPATCH; False when the file refuses."""
        try:
            mode = p.stat().st_mode
            p.chmod(mode | 0o111 if on else mode & ~0o111)
        except OSError:
            return False
        return True

    def _dav_get(self, rel: str) -> None:
        self._dav_stream(rel, head_only=False)

    def _dav_head(self, rel: str) -> None:
        self._dav_stream(rel, head_only=True)

    def _dav_stream(self, rel: str, head_only: bool) -> None:
        """GET/HEAD of a file, with the conditionals davfs2 caches with."""
        p, st = self._dav_target(rel)
        if stat.S_ISDIR(st.st_mode):
            raise HTTPError(
                405,
                "A folder has no content of its own: it is listed with "
                "PROPFIND (and browsed at / by the browser interface)",
            )
        # No lock check: an exclusive write lock says nothing about reading,
        # and refusing GET would stop the mount (and everyone else) from
        # reading a file an editor on another mount happens to have open.
        etag = dav_etag(st)
        inm = self.headers.get("If-None-Match")
        if (inm and dav_etag_listed(inm, etag)) or dav_if_modified(
            self.headers.get("If-Modified-Since"), st
        ):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        imm = self.headers.get("If-Match")
        if imm and not dav_etag_listed(imm, etag):
            raise HTTPError(412, "That is not the version you asked for")
        self.stream_file(p, attachment=False, head_only=head_only, dav=True)

    def _dav_post(self, rel: str) -> None:
        """Plain POST is not part of WebDAV; only the -extended variants are."""
        raise HTTPError(501, "This WebDAV mount does not implement POST")

    def _dav_mkcol(self, rel: str) -> None:
        self._require_write("Creating folders")
        if not rel:
            raise HTTPError(405, "The mounted folder already exists")
        p = safe_path(rel)
        if entry_exists(p):
            raise HTTPError(405, "A folder with that name already exists")
        self._require_unlocked(p)
        # Unlike the browser API, WebDAV forbids creating several levels at
        # once, so a missing parent is a conflict, not something to fix up.
        if not p.parent.is_dir():
            raise HTTPError(409, "The parent folder does not exist")
        with os_errors("Could not create the folder"):
            p.mkdir()
        sizes_changed(p.parent)
        self._dav_changed(p.parent)
        self._dav_status(201)

    def _dav_put(self, rel: str) -> None:
        """Store a whole file — davfs2 re-uploads the entire file, always."""
        self._require_write("Uploading")
        if not rel:
            raise HTTPError(405, "The mounted folder cannot be replaced")
        p = safe_path(rel)
        self._dav_unlocked(p)
        if not p.parent.is_dir():
            raise HTTPError(409, "The parent folder does not exist")
        if self.headers.get("Content-Range"):
            raise HTTPError(501, "Partial (Content-Range) uploads are not supported")
        before: os.stat_result | None = None
        if entry_exists(p):
            before = self._dav_stat(p)
            if stat.S_ISDIR(before.st_mode):
                raise HTTPError(405, "That name is a folder")
        old_etag = dav_etag(before) if before else None
        if_unmod = self.headers.get("If-Unmodified-Since")
        _tokens, if_etags = dav_conditionals(self.headers.get("If"))
        refused = [
            # If-None-Match names the version that must not be there (or *).
            before is not None
            and dav_etag_listed(self.headers.get("If-None-Match"), old_etag),
            bool(self.headers.get("If-Match"))
            and not dav_etag_listed(self.headers.get("If-Match"), old_etag),
            before is not None
            and bool(if_unmod)
            and dav_if_modified(if_unmod, before) is False,
            # An etag inside If: is davfs2's "do not lose my newer file".
            bool(if_etags) and (old_etag is None or old_etag not in if_etags),
        ]
        if any(refused):
            raise HTTPError(412, "The file changed since you read it")
        # Written to a temporary name and swapped in: a client that gives up
        # half way through an upload (davfs2 retries up to
        # max_upload_attempts times) must not leave a half file where a
        # reader — the browser or another mount — takes it for the real thing.
        tmp = p.with_name(f"{p.name}.uploading-{uuid.uuid4().hex[:8]}")
        with os_errors("Upload failed"):
            try:
                size = self._read_body_to_file(tmp)
            except Exception:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise
            if before is not None:
                # os.replace swaps the inode, so carry the old permissions
                # over: a +x script must not lose its bit by being edited on
                # the mount.
                with contextlib.suppress(OSError):
                    os.chmod(tmp, stat.S_IMODE(before.st_mode))
            os.replace(tmp, p)
            etag = dav_etag(p.stat())
        sizes_changed(p.parent)
        self._dav_changed(p.parent)
        self._dav_status(
            204 if before is not None else 201,
            {"ETag": etag, "X-FM-Bytes": str(size)},
        )

    def _dav_delete(self, rel: str) -> None:
        self._require_write("Deleting")
        if not rel:
            raise HTTPError(400, "Cannot delete the mounted folder")
        p = safe_path(rel)
        self._dav_unlocked(p)
        if not entry_exists(p):
            raise HTTPError(404, "Not found")
        parent = p.parent
        with os_errors("Could not delete"):
            # Files and links (even to a folder): the entry itself, never
            # whatever a link happens to point at. A plain folder goes with
            # its contents, because WebDAV deletes a collection recursively.
            if p.is_symlink() or not p.is_dir():
                p.unlink()
            else:
                shutil.rmtree(p)
        LOCKS.drop_subtree(str(p))
        sizes_changed(parent)
        self._dav_changed(parent)
        self._dav_status(204)

    def _dav_move(self, rel: str) -> None:
        self._require_write("Moving")
        if not rel:
            raise HTTPError(400, "Cannot move the mounted folder")
        src = safe_path(rel)
        if not entry_exists(src):
            raise HTTPError(404, "Not found")
        self._dav_unlocked(src)
        dest = safe_path(self._dav_destination())
        if dest == src:
            raise HTTPError(400, "That is already its name")
        self._dav_unlocked(dest.parent)
        if src.is_dir() and src in dest.parents:
            raise HTTPError(400, "Cannot move a folder into itself")
        existed = entry_exists(dest)
        if existed and not self._dav_overwrite():
            raise HTTPError(412, "A file or folder with that name is already there")
        with os_errors("Could not move"):
            # MOVE replaces the destination, and clearing it first lets a
            # rename inside one filesystem be the single atomic os.rename.
            self._dav_replace(dest)
            if src.parent == dest.parent:
                os.rename(src, dest)
            else:
                shutil.move(str(src), str(dest))
        LOCKS.move(str(src), str(dest))
        sizes_changed(src.parent)
        sizes_changed(dest.parent)
        self._dav_changed(src.parent)
        self._dav_changed(dest.parent)
        self._dav_status(204 if existed else 201)

    def _dav_copy(self, rel: str) -> None:
        self._require_write("Copying")
        src, _st = self._dav_target(rel)
        dest = safe_path(self._dav_destination())
        if dest == src or src in dest.parents:
            raise HTTPError(400, "Cannot copy a folder into itself")
        self._dav_unlocked(dest.parent)
        # Copying onto a collection puts the copy inside it: that is what the
        # spec says and what a client that just PROPFINDed the target expects.
        if dest.is_dir():
            dest = dest / src.name
        existed = entry_exists(dest)
        if existed and not self._dav_overwrite():
            raise HTTPError(412, "A file or folder with that name is already there")
        shallow = (self.headers.get("Depth") or "infinity").strip() == "0"
        with os_errors("Could not copy"):
            self._dav_replace(dest)
            dav_copy_entry(src, dest, shallow)
        sizes_changed(dest.parent)
        self._dav_changed(dest.parent)
        self._dav_status(204 if existed else 201)

    def _dav_lock(self, rel: str) -> None:
        """Take, refresh or reuse an exclusive write lock on one path."""
        self._require_write("Locking files")
        p = safe_path(rel)
        timeout = LOCKS.timeout(self.headers.get("Timeout"))
        tokens = self._dav_tokens()
        held = next((LOCKS.get(t) for t in tokens if LOCKS.get(t)), None)
        body = self._read_body(DAV_MAX_XML_BODY)
        if not body.strip():
            # A LOCK with no body is a refresh of a lock the client holds.
            if held is None:
                raise HTTPError(409, "Unknown lock token")
            self._dav_lock_reply(LOCKS.refresh(held["token"], timeout) or held)
            return
        scope, kind, owner = dav_lock_info(body)
        if kind != "write":
            raise HTTPError(400, "Only write locks are supported")
        if scope != "exclusive":
            raise HTTPError(400, "Only exclusive locks are supported")
        if held is not None and held["path"] == str(p):
            # Re-locking the file this token already locks is a refresh.
            self._dav_lock_reply(LOCKS.refresh(held["token"], timeout) or held)
            return
        earlier = LOCKS.holder(str(p))
        if earlier is not None:
            # A mount that died without unlocking leaves its lock behind, and
            # the owner string is how a client recognises its own: the same
            # owner takes over, a different one means the file is busy.
            if earlier["owner"] != (owner or "unknown"):
                raise HTTPError(
                    423, f"Already locked over WebDAV by {earlier['owner']!r}"
                )
            LOCKS.drop(earlier["token"])
        depth = "infinity" if (self.headers.get("Depth") or "") == "infinity" else "0"
        record = LOCKS.add(str(p), dav_href(rel, p.is_dir()), depth, owner, timeout)
        self._dav_lock_reply(record)

    def _dav_lock_reply(self, lock: dict) -> None:
        root = ET.Element(_dq("prop"))
        ET.SubElement(root, _dq("lockdiscovery")).append(dav_lock_node(lock))
        self._dav_xml(200, root, {"Lock-Token": f"<{lock['token']}>"})

    def _dav_unlock(self, rel: str) -> None:
        self._require_write("Unlocking files")
        tokens = self._dav_tokens()
        if not tokens:
            raise HTTPError(400, "UNLOCK needs a Lock-Token header")
        record = next((LOCKS.get(t) for t in tokens if LOCKS.get(t)), None)
        if record is None:
            raise HTTPError(409, "Unknown lock token")
        if record["path"] != str(safe_path(rel)):
            raise HTTPError(400, "That lock belongs to a different entry")
        LOCKS.drop(record["token"])
        self._dav_status(204)


def dav_copy_entry(src: Path, dest: Path, shallow: bool) -> None:
    """Copy a file, or a whole folder (Depth: 0 takes only its plain files).

    Depth: 0 on a collection means "the folder and its immediate files", so
    subfolders are left out instead of being followed; links are copied as
    links, which is what the browser API does too.
    """
    if src.is_dir():
        if not shallow:
            shutil.copytree(src, dest, symlinks=True)
            return
        dest.mkdir()
        with os.scandir(src) as scan:
            for de in scan:
                try:
                    if de.is_dir(follow_symlinks=False):
                        continue
                    shutil.copy2(de.path, dest / de.name, follow_symlinks=False)
                except OSError:
                    continue  # one unreadable child does not fail the COPY
    elif src.is_symlink():
        os.symlink(os.readlink(src), dest)
    else:
        shutil.copy2(src, dest)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(DavMixin, BaseHTTPRequestHandler):
    server_version = f"FileManager/{VERSION}"
    protocol_version = "HTTP/1.1"
    # A client that sends a header or body byte and then stalls can no longer
    # park a request thread forever (slow-loris); idle keep-alive sockets are
    # closed after the same idle period.
    timeout = READ_TIMEOUT
    # Small JSON responses go out immediately instead of waiting for the
    # peer's next ACK, which is what makes the UI feel snappy.
    disable_nagle_algorithm = True

    # Set once a status line has been written for the current request; see _fail.
    _resp_started = False
    # "unread" | "done" | "unknown": whether the request body can still be
    # skipped without breaking keep-alive; see _body_chunks and _fail.
    _body_state = "unread"
    # True while waiting for a request line on an idle keep-alive socket.
    _at_boundary = False

    # -- per-request plumbing ----------------------------------------------
    def handle_one_request(self) -> None:
        """Wrap the stock request loop with body bookkeeping.

        Two things happen around every request: the per-request state is
        reset (one handler instance serves a whole keep-alive connection),
        and a body that no endpoint read is discarded afterwards. Without
        that, the leftovers are parsed as the next request line and the
        client gets a 501 for a request it never sent.
        """
        self._begin_request()
        self._at_boundary = True
        super().handle_one_request()
        self._at_boundary = False
        if (
            self._body_state == "unread"
            and not self.close_connection
            and not self._drain_body()
        ):
            self.close_connection = True

    def parse_request(self) -> bool:
        self._at_boundary = False
        return super().parse_request()

    def log_error(self, fmt: str, *args: Any) -> None:
        """Drop the one message that is not an error.

        The stock handler reports a socket timeout as a failed request, but a
        client that keeps a connection open and then goes quiet for longer than
        READ_TIMEOUT has not failed anything: a mounted davfs2 does exactly that
        between two folder refreshes, and the line made the log look broken. The
        socket is still closed. A timeout in the middle of a request - a stalled
        body or a client that stopped reading - is logged as before.
        """
        if self._at_boundary and "timed out" in fmt:
            return
        super().log_error(fmt, *args)

    # -- request logging ---------------------------------------------------
    def _client_ua(self) -> str:
        """Return the request's User-Agent, sanitized for log safety."""
        try:
            ua = self.headers.get("User-Agent") or "-"
        except Exception:  # noqa: BLE001 - headers may not be parsed yet
            ua = "-"
        return "".join(ch if ch.isprintable() else " " for ch in ua)[:200].strip()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Log the request line plus the client's User-Agent.

        The stock handler omits request headers, which makes anonymous
        probes (e.g. a monitor hitting /health) impossible to attribute.
        Control characters are stripped so a crafted UA cannot forge log
        lines, and the value is capped to keep lines readable. The health
        check is silent: it runs on a timer and would drown the log.
        """
        if (self.path or "").startswith("/health"):
            return
        stamp = self.log_date_time_string()
        sys.stderr.write(
            f"{self.address_string()} - - [{stamp}] "
            f'"{self.requestline}" {code} {size} ua="{self._client_ua()}"\n'
        )

    # -- low-level helpers -------------------------------------------------
    def _begin_request(self) -> None:
        """Reset per-request bookkeeping (each handler instance is reused for
        every request on a keep-alive connection)."""
        self._resp_started = False
        self._body_state = "unread"

    def send_response(self, code: int, message: str | None = None) -> None:
        self._resp_started = True
        super().send_response(code, message)

    def _send(
        self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj: Any, code: int = 200) -> None:
        self._send(
            code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8"
        )

    def send_error_json(
        self,
        code: int,
        msg: str,
        keep_alive: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        try:
            body = json.dumps({"error": msg}).encode("utf-8")
            self.close_connection = not keep_alive
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            if not keep_alive:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            self.close_connection = True

    def _fail(
        self,
        code: int,
        msg: str,
        bodyless: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        """Report an error as JSON, reusing the connection where that is safe.

        An error is usually raised before any output has been written, so the
        response is complete and only an unread request body can stop the
        connection from being reused: such a body is drained when it is small
        enough to discard cheaply. Once headers are on the wire the response
        is already committed, so the connection has to go.

        bodyless is for HEAD, which must not send a body at all.
        """
        if bodyless:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if extra is None and code == 405:
            # RFC 9110 wants an Allow header on a 405, and which set applies
            # depends on which half of the server the URL belongs to.
            mount = self._dav_rel() is not None
            extra = {"Allow": DAV_ALLOW if mount else "GET, HEAD, POST, OPTIONS"}
        if self._resp_started:
            self.send_error_json(code, msg, extra=extra)
            return
        keep_alive = self._body_state == "unread" and self._drain_body()
        self.send_error_json(code, msg, keep_alive=keep_alive, extra=extra)

    def _fail_http(self, err: HTTPError, extra: dict[str, str] | None = None) -> None:
        self._fail(err.code, err.msg, extra=extra)

    def _body_chunks(self) -> Iterator[bytes]:
        """Yield the request body, for both Content-Length and chunked bodies.

        Chunked bodies used to be read as zero bytes, which turned a valid
        upload into a silently truncated (or empty) file.
        """
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            while True:
                line = self.rfile.readline(128)
                try:
                    n = int(line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    raise HTTPError(400, "Malformed chunked request body")
                if n == 0:
                    self._read_trailers()
                    return
                got = 0
                while got < n:
                    chunk = self.rfile.read(min(n - got, 1024 * 1024))
                    if not chunk:
                        raise HTTPError(400, "Incomplete chunked request body")
                    got += len(chunk)
                    yield chunk
                self.rfile.read(2)  # the CRLF that closes every chunk
            return
        try:
            remaining = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise HTTPError(400, "Invalid Content-Length header")
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise HTTPError(
                    400,
                    "Incomplete request body: the client sent fewer bytes than "
                    "Content-Length promised",
                )
            remaining -= len(chunk)
            yield chunk

    def _read_trailers(self) -> None:
        """Consume and ignore the trailer block that closes a chunked body."""
        while True:
            line = self.rfile.readline(1024)
            if not line or line in (b"\r\n", b"\n"):
                return

    def _read_body(self, max_bytes: int | None = None) -> bytes:
        """Read a request body into memory, capped so a client cannot OOM us.

        The cap is looked up per call rather than as a default argument, which
        would have frozen the constant when this module was imported.
        """
        limit = MAX_JSON_BODY_SIZE if max_bytes is None else max_bytes
        self._body_state = "unknown"
        parts: list[bytes] = []
        size = 0
        for chunk in self._body_chunks():
            size += len(chunk)
            if size > limit:
                raise HTTPError(
                    413,
                    f"Request body is too large (limit {fmt_size(limit)})",
                )
            parts.append(chunk)
        self._body_state = "done"
        return b"".join(parts)

    def _read_body_to_file(self, dest: Path) -> int:
        """Stream the request body into dest and return the byte count."""
        self._body_state = "unknown"
        size = 0
        with dest.open("wb") as out:
            for chunk in self._body_chunks():
                size += len(chunk)
                out.write(chunk)
        self._body_state = "done"
        return size

    def _read_b64_to_file(self, dest: Path, max_bytes: int) -> int:
        """Decode a base64 JSON body into dest; return the decoded byte count.

        Decoded as it arrives rather than through json.loads(), so a request
        cannot make the server hold a whole file in memory (see
        _B64PayloadReader). A failure here leaves no half-written file.
        """
        reader = _B64PayloadReader()
        too_big = (
            f"Upload is too large for base64 transfer "
            f"(limit {fmt_size(max_bytes)}) — use the regular upload instead"
        )
        written = 0
        self._body_state = "unknown"
        try:
            with dest.open("wb") as out:
                for chunk in self._body_chunks():
                    data = reader.feed(chunk)
                    if not data:
                        continue
                    written += len(data)
                    out.write(data)
                    if written > max_bytes:
                        raise HTTPError(413, too_big)
                tail = reader.finish()
                written += len(tail)
                out.write(tail)
                if written > max_bytes:
                    raise HTTPError(413, too_big)
        except Exception:
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)
            raise
        self._body_state = "done"
        return written

    def _drain_body(self, limit: int = 4 * 1024 * 1024) -> bool:
        """Discard an unread request body; False means close the connection."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if (
                "chunked" not in (self.headers.get("Transfer-Encoding") or "").lower()
                and length > limit
            ):
                return False
            self._body_state = "unknown"
            for chunk in self._body_chunks():
                limit -= len(chunk)
                if limit < 0:
                    return False
            self._body_state = "done"
            return True
        except (HTTPError, OSError, ValueError):
            return False

    def _json_body(self, max_bytes: int | None = None) -> dict[str, Any]:
        """Parse a JSON object body, or {} when there is none."""
        body = self._read_body(max_bytes)
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
        if not isinstance(payload, dict):
            raise HTTPError(400, "Expected a JSON object")
        return payload

    def _require_write(self, what: str) -> None:
        if not ENABLE_WRITE:
            raise HTTPError(
                403,
                f"{what.capitalize()} is not allowed: "
                f"this server runs in read-only mode",
            )

    def _file(self, qs: Qs) -> Path:
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_file():
            raise HTTPError(404, "File not found")
        return p

    def auto_make_disabled(self) -> bool:
        """True when the client asked to skip automatic make runs.

        The UI toggle sends "X-FM-Auto-Make: 0"; the preference itself
        lives in the browser (localStorage), keeping the server stateless.
        """
        return (self.headers.get("X-FM-Auto-Make") or "").strip() == "0"

    def maybe_run_make(self, dirp: Path) -> dict[str, str] | None:
        """Queue a background make in dirp after a change, unless the client
        disabled it or the folder has no Makefile.

        The build no longer blocks this response: it returns {"job": id},
        which the client polls via /api/make/result until the build
        finishes. Returns None when nothing needs to be built.
        """
        if not ENABLE_MAKE or self.auto_make_disabled():
            return None
        if not (dirp / "Makefile").is_file():
            return None
        return {"job": MAKE_JOBS.submit(dirp)}

    def make_hint(self, resp: dict[str, Any], dirp: Path) -> dict[str, Any]:
        """Add the background-make info to a mutation response, if due."""
        result = self.maybe_run_make(dirp)
        if result is not None:
            resp["make"] = result
        return resp

    def send_html(self) -> None:
        self._send(
            200,
            page_html(),
            "text/html; charset=utf-8",
            {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    def ep_health(self, head_only: bool = False) -> None:
        """Liveness probe: cheap to serve, silent in the log."""
        body = json.dumps(
            {
                "ok": True,
                "version": VERSION,
                "uptime": round(time.time() - START_TIME, 1),
                "root": ROOT.name or str(ROOT),
                "read_only": not ENABLE_WRITE,
                "pillow": pillow_available(),
                "dav": ENABLE_DAV,
                "dav_prefix": f"/{DAV_PREFIX}" if ENABLE_DAV else None,
                "locks": LOCKS.count(),
            }
        ).encode("utf-8")
        if head_only:
            self._head_only(200, "application/json; charset=utf-8", len(body))
        else:
            self._send(200, body, "application/json; charset=utf-8")

    def stream_file(
        self,
        p: Path,
        attachment: bool,
        head_only: bool = False,
        dav: bool = False,
    ) -> None:
        """Send a file, honouring a single byte Range so media can seek and
        downloads can resume; HEAD gets the same headers without the body.

        The ETag and Last-Modified are the same values the WebDAV half reports,
        so a file read through both doors looks like one file. A DAV response
        leaves out Content-Disposition — davfs2 has no use for it — and says
        no-cache instead of no-store, because its whole design is to revalidate
        with those two headers rather than to refetch.
        """
        ctype, _ = mimetypes.guess_type(p.name)
        ctype = ctype or "application/octet-stream"
        try:
            st = p.stat()
        except OSError:
            raise HTTPError(404, "File not found")
        size = st.st_size
        start, end = 0, max(size - 1, 0)
        partial = False
        header = self.headers.get("Range")
        if header and not head_only:
            found = parse_byte_range(header, size)
            if found is not None:
                start, end = found
                partial = True
        mode = "attachment" if attachment else "inline"
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(max(end - start + 1, 0)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", dav_etag(st))
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        if not dav:
            self.send_header("Content-Disposition", content_disposition(mode, p.name))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache" if dav else "no-store")
        self.end_headers()
        if head_only:
            return
        try:
            with p.open("rb") as f:
                if start:
                    f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            path = parsed.path
            if self.dav_dispatch("GET"):
                pass
            elif path == "/":
                self.send_html()
            elif path == "/health":
                self.ep_health()
            elif path == "/api/list":
                self.ep_list(qs)
            elif path == "/api/download":
                self.stream_file(self._file(qs), attachment=True)
            elif path == "/api/raw":
                self.stream_file(self._file(qs), attachment=False)
            elif path == "/api/download64":
                self.ep_download64(qs)
            elif path == "/api/text":
                self.ep_text(qs)
            elif path == "/api/tree":
                self.ep_tree(qs)
            elif path == "/api/zip":
                self.ep_zip(qs)
            elif path == "/api/make/result":
                self.ep_make_result(qs)
            elif path.startswith("/api/thumbnail/"):
                rel = unquote(path[len("/api/thumbnail/") :])
                self.ep_thumbnail(rel)
            else:
                self._fail(404, "Not found")
        except HTTPError as e:
            self._fail_http(e)
        except TimeoutError:
            self.send_error_json(408, "Timed out reading the request")
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"Server error: {e}")

    def do_HEAD(self) -> None:
        """Headers only, so curl -I, link checkers and media players work."""
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            path = parsed.path
            if self.dav_dispatch("HEAD", bodyless=True):
                pass
            elif path == "/":
                self._head_only(200, "text/html; charset=utf-8", len(page_html()))
            elif path == "/health":
                self.ep_health(head_only=True)
            elif path in ("/api/download", "/api/raw"):
                self.stream_file(
                    self._file(qs), attachment=path == "/api/download", head_only=True
                )
            elif path.startswith("/api/thumbnail/"):
                rel = unquote(path[len("/api/thumbnail/") :])
                self.ep_thumbnail(rel, head_only=True)
            else:
                self._fail(404, "Not found", bodyless=True)
        except HTTPError as e:
            self._fail(e.code, e.msg, bodyless=True)
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"Server error: {e}", bodyless=True)

    def _head_only(
        self, code: int, ctype: str, length: int, headers: dict[str, str] | None = None
    ) -> None:
        """Answer a HEAD request: the headers a GET would produce, no body."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            path = parsed.path
            if self.dav_dispatch("POST"):
                pass
            elif path == "/api/mkdir":
                self.ep_mkdir(qs)
            elif path == "/api/newfile":
                self.ep_newfile(qs)
            elif path == "/api/delete":
                self.ep_delete(qs)
            elif path == "/api/save":
                self.ep_save(qs)
            elif path == "/api/upload":
                self.ep_upload(qs)
            elif path == "/api/rename":
                self.ep_rename(qs)
            elif path == "/api/move":
                self.ep_move(qs)
            elif path == "/api/copy":
                self.ep_copy(qs)
            elif path == "/api/unlock":
                self.ep_unlock(qs)
            elif path == "/api/foldersize":
                self.ep_foldersize(qs)
            elif path == "/api/extract":
                self.ep_extract(qs)
            elif path == "/api/upload64":
                self.ep_upload64(qs)
            elif path == "/api/run":
                self.ep_run(qs)
            elif path == "/api/make":
                self.ep_make(qs)
            elif path == "/api/shell":
                self.ep_shell(qs)
            else:
                self._fail(404, "Not found")
        except HTTPError as e:
            self._fail_http(e)
        except TimeoutError:
            self.send_error_json(408, "Timed out reading the request body")
        except Exception as e:  # noqa: BLE001
            self._fail(500, f"Server error: {e}")

    # -- endpoints ---------------------------------------------------------
    def ep_list(self, qs: Qs) -> None:
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_dir():
            raise HTTPError(404, "Directory not found")
        prefix = rel_of(p)
        have_pillow = pillow_available()
        entries: list[dict[str, Any]] = []
        # Locks are per absolute path; one snapshot per listing keeps the cost
        # at one pass over the (small, TTL-bounded) table.
        held_locks = LOCKS.snapshot() if ENABLE_DAV else {}
        try:
            it = os.scandir(p)
        except OSError:
            raise HTTPError(404, "Directory not found")
        with it:
            for de in it:
                try:
                    st = de.stat()  # follow symlinks: show the target's kind
                except OSError:
                    try:  # broken symlink: still show it
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                is_dir = stat.S_ISDIR(st.st_mode)
                is_reg = stat.S_ISREG(st.st_mode)
                ext = os.path.splitext(de.name)[1].lower()
                is_link = de.is_symlink()
                entries.append(
                    {
                        "name": de.name,
                        "path": (prefix + "/" + de.name) if prefix else de.name,
                        "kind": "dir" if is_dir else classify(de.name),
                        "size": st.st_size if is_reg else None,
                        "ctime": created_time(st),
                        "mtime": st.st_mtime,
                        # SVG renders straight from the file, everything else
                        # needs Pillow; without it the icon fallback is used
                        # and no thumbnail is even requested.
                        "thumb": bool(
                            is_reg
                            and (ext == ".svg" or (have_pillow and ext in THUMB_EXTS))
                        ),
                        "run": ENABLE_RUN and is_reg and is_executable(de.path, st),
                        "link": link_target(de.path) if is_link else None,
                        "broken": not is_dir and not is_reg,
                        # A lock held by a WebDAV client: the browser cannot
                        # write to this entry until it is released, so the row
                        # says so and offers Unlock instead of failing on save.
                        "locked": bool(held_locks.get(de.path)),
                        "lock_owner": (
                            held_locks[de.path][-1]["owner"]
                            if held_locks.get(de.path)
                            else None
                        ),
                    }
                )
        total = len(entries)
        entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
        # A huge folder is truncated (folders first, then by name) rather than
        # turning into a multi-megabyte response and tens of thousands of rows.
        truncated = total > LIST_MAX_ENTRIES
        if truncated:
            entries = entries[:LIST_MAX_ENTRIES]
        parent = None
        if p != ROOT:
            parent = "" if p.parent == ROOT else rel_of(p.parent)
        # The folder itself can be locked too (a Depth: infinity lock covers
        # everything under it), which stops creating or deleting entries here.
        here_blocked = LOCKS.blocking(str(p), set()) if ENABLE_DAV else None
        self.send_json(
            {
                "path": rel_of(p),
                "parent": parent,
                "entries": entries,
                "total": total,
                "truncated": truncated,
                "locked": here_blocked is not None,
                "lock_owner": here_blocked["owner"] if here_blocked else None,
            }
        )

    def ep_mkdir(self, qs: Qs) -> None:
        self._require_write("Creating folders")
        p = safe_path(qs.get("path", [""])[0])
        self._require_unlocked(p)
        if p == ROOT:
            raise HTTPError(400, "Cannot modify the root directory")
        validate_new_name(p.name)
        if entry_exists(p):
            raise HTTPError(409, "A file or folder with that name already exists")
        if not p.parent.is_dir():
            raise HTTPError(404, "Parent folder not found")
        with os_errors("Could not create the folder"):
            p.mkdir()
        sizes_changed(p.parent)
        self.send_json({"ok": True, "path": rel_of(p)})

    def ep_newfile(self, qs: Qs) -> None:
        """Create an empty file (used by the 'New text file' menu item)."""
        self._require_write("Creating files")
        p = safe_path(qs.get("path", [""])[0])
        self._require_unlocked(p)
        if p == ROOT:
            raise HTTPError(400, "Cannot create a file at the root path")
        validate_new_name(p.name)
        if entry_exists(p):
            raise HTTPError(409, "A file or folder with that name already exists")
        if not p.parent.is_dir():
            raise HTTPError(404, "Parent folder not found")
        with os_errors("Could not create the file"):
            p.write_bytes(b"")
        sizes_changed(p.parent)
        self.send_json({"ok": True, "path": rel_of(p)})

    def ep_delete(self, qs: Qs) -> None:
        self._require_write("Deleting")
        p = safe_path(qs.get("path", [""])[0])
        self._require_unlocked(p)
        if p == ROOT:
            raise HTTPError(400, "Cannot delete the root directory")
        if not entry_exists(p):
            raise HTTPError(404, "Not found")
        parent = p.parent
        with os_errors("Could not delete"):
            if is_link_to_dir(p):
                shutil.rmtree(p)
            else:
                # Files, symlinks (even to a folder) and special entries: remove
                # the entry itself, never whatever a link happens to point at.
                p.unlink()
        LOCKS.drop_subtree(str(p))  # locks on something gone are meaningless
        sizes_changed(parent)
        self.send_json(self.make_hint({"ok": True}, parent))

    def ep_rename(self, qs: Qs) -> None:
        self._require_write("Renaming")
        p = safe_path(qs.get("path", [""])[0])
        self._require_unlocked(p)
        if p == ROOT:
            raise HTTPError(400, "Cannot rename the root directory")
        if not entry_exists(p):
            raise HTTPError(404, "Not found")
        payload = self._json_body()
        new_name = payload.get("name", "")
        if not isinstance(new_name, str):
            raise HTTPError(400, "'name' must be a string")
        new_name = validate_new_name(new_name)
        dest = p.parent / new_name
        if dest == p:
            raise HTTPError(400, "New name is the same as the current name")
        if entry_exists(dest):
            raise HTTPError(409, "A file or folder with that name already exists here")
        with os_errors("Could not rename"):
            p.rename(dest)
        LOCKS.move(str(p), str(dest))  # a lock on the mount follows the rename
        sizes_changed(p.parent)
        self.send_json(
            self.make_hint(
                {"ok": True, "path": rel_of(dest), "name": new_name}, p.parent
            )
        )

    def _move_copy_target(self, qs: Qs, action: str) -> tuple[Path, Path]:
        """Shared validation for move/copy: returns (source, dest folder)."""
        self._require_write(action)
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, f"Cannot {action} the root directory")
        if not entry_exists(p):
            raise HTTPError(404, "Not found")
        payload = self._json_body()
        dest_rel = payload.get("dest", "")
        if not isinstance(dest_rel, str):
            dest_rel = ""
        dest_dir = safe_path(dest_rel)
        if not dest_dir.is_dir():
            raise HTTPError(404, "Destination folder not found")
        self._require_unlocked(dest_dir)
        return p, dest_dir

    def ep_move(self, qs: Qs) -> None:
        p, dest_dir = self._move_copy_target(qs, "move")
        self._require_unlocked(p)
        # Can't move a directory into itself or a subdirectory of itself
        if is_link_to_dir(p) and (dest_dir == p or p in dest_dir.parents):
            raise HTTPError(400, "Cannot move a folder into itself or its subfolder")
        dest = dest_dir / p.name
        if dest == p:
            raise HTTPError(400, "The file is already there")
        if entry_exists(dest):
            raise HTTPError(
                409, "A file or folder with that name already exists in the destination"
            )
        with os_errors("Could not move"):
            shutil.move(str(p), str(dest))
        LOCKS.move(str(p), str(dest))
        sizes_changed(p.parent)
        sizes_changed(dest_dir)
        resp = {"ok": True, "path": rel_of(dest), "name": p.name}
        self.send_json(self.make_hint(resp, dest_dir))

    def ep_copy(self, qs: Qs) -> None:
        """Copy a file or folder into another folder, server-side.

        Copies never overwrite: on a name clash the same " (1)" numbering the
        upload path uses is applied, so copying "in place" yields a second
        entry instead of replacing the first.
        """
        p, dest_dir = self._move_copy_target(qs, "copy")
        if p.is_dir() and (dest_dir == p or p in dest_dir.parents):
            raise HTTPError(400, "Cannot copy a folder into itself or its subfolder")
        dest = unique_dest(dest_dir, p.name)
        with os_errors("Could not copy"):
            if p.is_dir():
                shutil.copytree(p, dest, symlinks=True)
            else:
                shutil.copy2(p, dest)
        sizes_changed(dest_dir)
        resp = {"ok": True, "path": rel_of(dest), "name": dest.name}
        self.send_json(self.make_hint(resp, dest_dir))

    def ep_unlock(self, qs: Qs) -> None:
        """Break the WebDAV locks keeping one entry busy, from the browser.

        This is recovery, not permission: a mount whose client died leaves a
        lock that outlives its owner, and without this the only ways to edit
        the file from here would be to wait for the timeout or to shell out to
        cadaver. Answering 200 with nothing unlocked is right when the lock
        expired in the meantime: that is the state the caller wanted.
        """
        if not ENABLE_DAV:
            raise HTTPError(403, "WebDAV is disabled (started with --no-dav)")
        self._require_write("Unlocking files")
        p = safe_path(qs.get("path", [""])[0])
        self.send_json({"ok": True, "unlocked": LOCKS.drop_blocking(str(p))})

    def ep_tree(self, qs: Qs) -> None:
        """Return a minimal folder tree for the move-to picker."""
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_dir():
            raise HTTPError(404, "Directory not found")
        dirs = []
        for item in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir():
                dirs.append({"name": item.name, "path": rel_of(item)})
        parent = None
        if p != ROOT:
            parent = "" if p.parent == ROOT else rel_of(p.parent)
        self.send_json({"path": rel_of(p), "parent": parent, "dirs": dirs})

    def ep_foldersize(self, qs: Qs) -> None:
        """Compute recursive sizes for a list of directories in one request.

        Paths are deduplicated and results cached for a few seconds, and the
        whole call has a scan budget: without those, one request naming the
        same big folder 1000 times cost as many walks as it says.
        """
        payload = self._json_body()
        paths = payload.get("paths", [])
        if not isinstance(paths, list) or len(paths) > 1000:
            raise HTTPError(400, "Invalid 'paths' payload")
        sizes: dict[str, int] = {}
        partial: list[str] = []
        budget = DIR_SIZE_SCAN_BUDGET
        for rel in dict.fromkeys(p for p in paths if isinstance(p, str)):
            p = safe_path(rel)
            if not p.is_dir():
                continue
            key = rel_of(p)
            cached = DIR_SIZES.get(str(p))
            if cached is None:
                total, seen, complete = dir_size(p, budget=max(budget, 1))
                budget -= seen
                DIR_SIZES.put(str(p), total, complete)
            else:
                total, complete = cached
            sizes[key] = total
            if not complete or budget <= 0:
                partial.append(key)
        self.send_json({"sizes": sizes, "partial": partial})

    def ep_text(self, qs: Qs) -> None:
        p = self._file(qs)
        try:
            st = p.stat()
        except OSError:
            raise HTTPError(404, "File not found")
        size = st.st_size
        if size > MAX_TEXT_SIZE:
            raise HTTPError(
                413,
                f"File is too large to edit in the browser "
                f"(limit {fmt_size(MAX_TEXT_SIZE)}) — download it instead",
            )
        raw = p.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPError(400, "Not a UTF-8 text file.")
        self.send_json(
            {
                "path": rel_of(p),
                "name": p.name,
                "size": size,
                "content": content,
                "newline": detect_newline(raw),
                # Microseconds, not nanoseconds: the browser must be able to
                # send this back exactly (JS numbers lose ns precision).
                "mtime_us": st.st_mtime_ns // 1000,
            }
        )

    def ep_save(self, qs: Qs) -> None:
        self._require_write("Editing files")
        p = safe_path(qs.get("path", [""])[0])
        self._require_unlocked(p)
        if p == ROOT or p.is_dir():
            raise HTTPError(400, "Target is not a file")
        payload = self._json_body()
        if not payload:
            raise HTTPError(400, "Empty request body")
        content = payload.get("content")
        if not isinstance(content, str):
            raise HTTPError(400, "Missing 'content' field")
        # Optional line-ending conversion: "as-is" (default) keeps the text
        # exactly as sent; "lf"/"crlf"/"cr" normalize everything to that
        # line ending (useful because the browser textarea sends plain \n).
        newline = payload.get("newline", "as-is")
        if newline not in ("as-is", "lf", "crlf", "cr"):
            raise HTTPError(400, "Invalid 'newline' field")
        text = content
        if newline != "as-is":
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if newline == "crlf":
                text = text.replace("\n", "\r\n")
            elif newline == "cr":
                text = text.replace("\n", "\r")
        data = text.encode("utf-8")
        # The editor can hand back the mtime it read; if the file moved on in
        # the meantime (second tab, an editor outside the browser, a make that
        # regenerated it) refuse to overwrite it silently.
        expect = payload.get("if_mtime_us")
        if expect is not None and not isinstance(expect, int):
            raise HTTPError(400, "'if_mtime_us' must be a number")
        tmp = p.with_name(f"{p.name}.saving-{uuid.uuid4().hex[:8]}")
        with os_errors("Could not save"):
            if expect is not None:
                try:
                    current = p.stat().st_mtime_ns // 1000
                except FileNotFoundError:
                    raise HTTPError(409, "The file no longer exists")
                if current != expect:
                    raise HTTPError(
                        409,
                        "The file changed on disk since you opened it — "
                        "reload it before saving",
                    )
            try:
                with tmp.open("wb") as fh:
                    fh.write(data)
                if entry_exists(p):
                    # os.replace swaps inodes, so keep the old permissions
                    # (a +x script must not become 0644 on save).
                    with contextlib.suppress(OSError):
                        os.chmod(tmp, stat.S_IMODE(p.stat().st_mode))
                os.replace(tmp, p)
                mtime_us = p.stat().st_mtime_ns // 1000
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
        sizes_changed(p.parent)
        resp: dict[str, Any] = {"ok": True, "size": len(data), "mtime_us": mtime_us}
        self.send_json(self.make_hint(resp, p.parent))

    def ep_upload(self, qs: Qs) -> None:
        self._require_write("Uploading")
        dest_dir = safe_path(qs.get("path", [""])[0])
        if not dest_dir.is_dir():
            raise HTTPError(404, "Target folder not found")
        name = os.path.basename(qs.get("filename", [""])[0] or "")
        if not name or name in (".", ".."):
            raise HTTPError(400, "Invalid filename")
        dest = unique_dest(dest_dir, name)
        self._require_unlocked(dest)
        with os_errors("Upload failed"):
            try:
                # Streams straight to disk and knows chunked bodies: a body
                # without Content-Length used to arrive as an empty file.
                size = self._read_body_to_file(dest)
            except Exception:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
                raise
        sizes_changed(dest_dir)
        self.send_json(
            self.make_hint(
                {"ok": True, "path": rel_of(dest), "name": dest.name, "size": size},
                dest_dir,
            )
        )

    def ep_upload64(self, qs: Qs) -> None:
        """Receive a file as base64 in a JSON body (workaround for flaky
        binary uploads): {"data": "<base64>"}. A data-URL prefix is accepted.

        The payload is decoded to disk as it arrives, so a transfer at the top
        of the limit costs the server a megabyte of memory instead of about
        four times the file.
        """
        if not ENABLE_B64:
            raise HTTPError(403, "Base64 upload is disabled (started with --no-b64)")
        self._require_write("Uploading")
        dest_dir = safe_path(qs.get("path", [""])[0])
        if not dest_dir.is_dir():
            raise HTTPError(404, "Target folder not found")
        name = os.path.basename(qs.get("filename", [""])[0] or "")
        if not name or name in (".", ".."):
            raise HTTPError(400, "Invalid filename")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_B64_BODY_SIZE:
            raise HTTPError(
                413,
                f"Upload is too large for base64 transfer "
                f"(limit {fmt_size(MAX_B64_BODY_SIZE)} of JSON body) — "
                f"use the regular upload instead",
            )
        # The destination is settled before the body is read: a 1.4 GB upload
        # should fail on a locked name without first being streamed up.
        dest = unique_dest(dest_dir, name)
        self._require_unlocked(dest)
        with os_errors("Upload failed"):
            size = self._read_b64_to_file(dest, MAX_B64_FILE_SIZE)
        sizes_changed(dest_dir)
        self.send_json(
            self.make_hint(
                {"ok": True, "path": rel_of(dest), "name": dest.name, "size": size},
                dest_dir,
            )
        )

    def ep_download64(self, qs: Qs) -> None:
        """Return a file base64-encoded in a JSON body (workaround for
        flaky binary downloads): the browser decodes it into a Blob.

        The base64 payload is streamed in 3 MB chunks and the page decodes it
        as it arrives, so neither end holds the encoded form of a 1 GB file;
        the cap below still applies because the browser keeps the finished
        Blob in memory until the download has been handed to the user.
        """
        if not ENABLE_B64:
            raise HTTPError(403, "Base64 download is disabled (started with --no-b64)")
        p = self._file(qs)
        size = p.stat().st_size
        if size > MAX_B64_FILE_SIZE:
            raise HTTPError(
                413,
                f"File is too large for base64 transfer "
                f"(limit {fmt_size(MAX_B64_FILE_SIZE)}) — use the "
                f"regular Download instead",
            )
        head = (
            '{"path":'
            + json.dumps(rel_of(p))
            + ',"name":'
            + json.dumps(p.name)
            + f',"size":{size},"data":"'
        )
        tail = '"}'
        b64_len = 4 * ((size + 2) // 3)  # exact encoded length, padding included
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(head) + b64_len + len(tail)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(head.encode("utf-8"))
            # Encode exact multiples of 3 bytes so chunk boundaries never
            # interact with base64 padding; only the last chunk gets it.
            step = 1024 * 1024 * 3
            with p.open("rb") as f:
                while True:
                    chunk = f.read(step)
                    if not chunk:
                        break
                    self.wfile.write(base64.b64encode(chunk))
            self.wfile.write(tail.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True

    def ep_extract(self, qs: Qs) -> None:
        """Extract a zip file into a folder (default: its own folder)."""
        self._require_write("Extracting archives")
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_file():
            raise HTTPError(404, "File not found")
        if p.suffix.lower() != ".zip":
            raise HTTPError(400, "Not a zip file")
        payload = self._json_body()
        dest_rel = payload.get("dest", "")
        if not isinstance(dest_rel, str):
            raise HTTPError(400, "Invalid 'dest' field")
        dest_dir = safe_path(dest_rel) if dest_rel else p.parent
        if not dest_dir.is_dir():
            raise HTTPError(404, "Destination folder not found")
        self._require_unlocked(dest_dir)
        if payload.get("delete"):
            self._require_unlocked(p)
        with os_errors("Extraction failed"):
            result = extract_zip(p, dest_dir)
            if payload.get("delete"):
                try:
                    p.unlink()
                    result["deleted"] = True
                except OSError:
                    result["deleted"] = False
        sizes_changed(dest_dir)
        self.send_json(self.make_hint({"ok": True, **result}, dest_dir))

    def _run_timeout(self, payload: dict[str, Any]) -> int:
        try:
            timeout = int(payload.get("timeout", RUN_TIMEOUT))
        except (TypeError, ValueError):
            timeout = RUN_TIMEOUT
        return max(1, min(timeout, RUN_MAX_TIMEOUT))

    def ep_run(self, qs: Qs) -> None:
        """Execute an executable file, capturing output (with timeout)."""
        if not ENABLE_RUN:
            raise HTTPError(403, "Executing files is disabled (started with --no-run)")
        self._require_write("Running files")
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, "Cannot run the root directory")
        if not p.is_file():
            raise HTTPError(404, "Not a file")
        if not is_executable(p):
            raise HTTPError(403, "File is not executable")
        payload = self._json_body()
        args_raw = payload.get("args", "")
        if not isinstance(args_raw, str):
            args_raw = ""
        try:
            args = shlex.split(args_raw)
        except ValueError:
            raise HTTPError(400, "Malformed arguments (unbalanced quotes?)")
        self.send_json(
            run_executable(p, args, self._run_timeout(payload), MAX_RUN_OUTPUT)
        )

    def ep_make(self, qs: Qs) -> None:
        """Run make in a folder on demand (Tools ▸ Run make here).

        Unlike the automatic runs, this ignores the client's auto-make
        preference: the user explicitly asked for it.
        """
        if not ENABLE_MAKE:
            raise HTTPError(403, "Make is disabled (started with --no-make)")
        self._require_write("Running make")
        payload = self._json_body()
        dir_rel = payload.get("path", "")
        if not isinstance(dir_rel, str):
            dir_rel = ""
        d = safe_path(dir_rel)
        if not d.is_dir():
            raise HTTPError(404, "Directory not found")
        result = run_make_if_present(d)
        if result is None:
            raise HTTPError(400, "No Makefile in this folder")
        sizes_changed(d)
        self.send_json(result)

    def ep_make_result(self, qs: Qs) -> None:
        """Poll a background auto-make job started by save/upload/delete."""
        job = qs.get("job", [""])[0]
        got = MAKE_JOBS.get(job)
        if got is None:
            raise HTTPError(404, "Unknown or expired make job")
        done, result = got
        if not done:
            self.send_json({"done": False})
        else:
            if result is not None:
                sizes_changed(safe_path(result.get("cwd", "")))
            self.send_json({"done": True, "result": result})

    def ep_shell(self, qs: Qs) -> None:
        """Run an arbitrary command through the configured shell (bash -c)."""
        if not ENABLE_SHELL:
            raise HTTPError(403, "Shell access is disabled (started with --no-shell)")
        self._require_write("Running shell commands")
        payload = self._json_body()
        command = payload.get("command", "")
        if not isinstance(command, str) or not command.strip():
            raise HTTPError(400, "Empty command")
        cwd_rel = payload.get("cwd", "")
        if not isinstance(cwd_rel, str):
            cwd_rel = ""
        cwd = safe_path(cwd_rel)
        if not cwd.is_dir():
            raise HTTPError(404, "Working directory not found")
        code, timed_out, truncated, text, cwd_out = _run_captured(
            [SHELL_CMD, "-c", command], cwd, self._run_timeout(payload), MAX_RUN_OUTPUT
        )
        sizes_changed(cwd)
        self.send_json(
            {
                "code": code,
                "timed_out": timed_out,
                "truncated": truncated,
                "output": text,
                "cwd": cwd_out,
            }
        )

    def ep_zip(self, qs: Qs) -> None:
        """Zip a directory (recursively) and stream it as a download.

        The archive is written straight to the socket (chunked transfer
        encoding): no temp file, and the first bytes reach the client as
        soon as the first file is compressed. Everything is stored under a
        folder named after the source so unpacking gives one folder instead
        of a spill of files, and empty folders survive the round trip.
        """
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_dir():
            raise HTTPError(404, "Directory not found")
        wrapper = p.name or "root"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header(
            "Content-Disposition", content_disposition("attachment", f"{wrapper}.zip")
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        count = 0
        sink = _ChunkedZipSink(self.wfile)
        try:
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
                for dirpath, dirnames, filenames in os.walk(
                    p, onerror=lambda e: None, followlinks=False
                ):
                    base = Path(dirpath)
                    dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
                    rel_dir = base.relative_to(p).as_posix()
                    arc_dir = wrapper if rel_dir == "." else f"{wrapper}/{rel_dir}"
                    if not dirnames and not filenames:
                        zf.writestr(f"{arc_dir}/", b"")  # keep empty folders
                    for fn in sorted(filenames):
                        fp = base / fn
                        try:
                            if fp.is_symlink() or not fp.is_file():
                                continue
                            zf.write(fp, f"{arc_dir}/{fn}")
                            count += 1
                        except OSError:
                            pass
            sink.close()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True
            return
        if count == 0:
            self.log_message("zip of %r contained no files", rel_of(p) or "/")

    def ep_thumbnail(self, rel: str, head_only: bool = False) -> None:
        """Serve a thumbnail for an image, <= THUMB_SIZE.

        Rendered thumbnails are kept in a process-local LRU cache keyed by
        (path, mtime, size), so re-browsing a folder is a dict lookup
        instead of a full-resolution decode. Responses carry an ETag;
        clients that revalidate (Cache-Control: no-cache) get a cheap 304.
        """
        p = safe_path(rel)
        try:
            st = p.stat()
        except OSError:
            raise HTTPError(404, "File not found")
        if not stat.S_ISREG(st.st_mode):
            raise HTTPError(404, "File not found")
        ext = p.suffix.lower()
        if ext not in THUMB_EXTS and ext != ".svg":
            raise HTTPError(400, "Not a thumbnailable image")
        key = (p, st.st_mtime_ns, st.st_size)
        cached = THUMB_CACHE.get(key)
        if cached is None:
            fmt, data = render_thumbnail(p)
            etag = '"%08x"' % (zlib.crc32(data) & 0xFFFFFFFF)
            ctype = f"image/{fmt}"
            THUMB_CACHE.put(key, etag, ctype, data)
            cached = (etag, ctype, data)
        etag, ctype, data = cached
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if head_only:
            self._head_only(
                200,
                ctype,
                len(data),
                {"Cache-Control": "no-cache", "ETag": etag},
            )
            return
        self._send(200, data, ctype, {"Cache-Control": "no-cache", "ETag": etag})


# ---------------------------------------------------------------------------
# Front-end (pure HTML/CSS/JS, embedded)
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Files</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🗂️</text></svg>">
<meta name="theme-color" content="#ffffff">
<script>
(function(){
  var KEY = "fm-theme", pref = null;
  try { pref = localStorage.getItem(KEY); } catch (e) {}
  var dark = pref ? pref === "dark"
                  : !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
})();
</script>
<style>
:root{--bg:#f6f1dc;--panel:#fdf8e6;--border:#e2d3ab;--text:#3c3323;--muted:#8a7b5e;--primary:#3f7d4a;--primary-d:#33663d;--danger:#c0392b;--hover:#f4ecd2;--radius:10px;--shadow:0 1px 2px rgba(90,70,30,.06),0 8px 24px rgba(90,70,30,.10);--input-bg:#fffdf5;--th-bg:#f6eed4;--act-hover:#fffdf5;--sel-bg:rgba(63,125,74,.14);--overlay-bg:rgba(60,50,30,.48);--checker-a:#efe7d2;--checker-b:#f8f2e2;--runout-bg:#2f2a22;--focus-ring:rgba(63,125,74,.18);color-scheme:light}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text)}
header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--border)}
.brand{font-weight:700;font-size:16px}
.brand-icon{margin-right:6px}
.spacer{flex:1 1 auto}
.crumbs{display:flex;align-items:center;flex-wrap:wrap;gap:1px;font-size:13px}
.crumb{color:var(--primary);cursor:pointer;padding:2px 5px;border-radius:5px}
.crumb:hover{background:var(--hover)}
.crumb.current{color:var(--text);font-weight:600;cursor:default}
.crumb.current:hover{background:transparent}
.sep{color:var(--muted);user-select:none}
.btn{font:inherit;font-weight:600;color:var(--text);background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:8px 14px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--hover)}
.btn-primary{background:var(--primary);border-color:var(--primary);color:#fff}
.btn-primary:hover{background:var(--primary-d)}
#themeBtn{font-size:16px;line-height:1;padding:8px 11px}
/* The two view utilities (theme, help) sit apart from the action buttons. */
.hgroup{display:flex;align-items:center;gap:6px;padding-left:12px;margin-left:2px;border-left:1px solid var(--border)}
/* The Tools menu is where the secondary actions live, so the one piece of
   state that matters on its own — automatic make is off — rides on the
   button and stays visible with the menu closed. */
.dot{width:7px;height:7px;border-radius:50%;background:var(--danger);flex:none}
.menu-wrap{position:relative}
.menu{position:absolute;right:0;top:calc(100% + 6px);min-width:238px;max-width:min(340px,calc(100vw - 16px));max-height:calc(100vh - 12px);overflow:auto;display:flex;flex-direction:column;gap:2px;padding:6px;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);z-index:40}
.menu[hidden]{display:none}
.menu-label{font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);padding:8px 12px 3px}
.menu-sep{height:1px;background:var(--border);margin:5px 6px}
.menu-item{font:inherit;font-weight:500;text-align:left;color:var(--text);background:transparent;border:0;border-radius:7px;padding:9px 12px;cursor:pointer;display:flex;align-items:center;gap:9px;white-space:nowrap}
.menu-item:hover,.menu-item:focus-visible{background:var(--hover)}
.menu-item:focus-visible{outline:2px solid var(--primary);outline-offset:-2px}
/* role="menuitemcheckbox" items spell their own state out on the right. */
.menu-item[aria-checked="false"]{color:var(--muted)}
.menu-item .icon{font-size:16px}
.menu-item .state{margin-left:auto;padding-left:16px;font-size:11px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;color:var(--primary)}
.menu-item[aria-checked="false"] .state{color:var(--muted)}
main{position:relative;z-index:1;padding:18px}
.meta{color:var(--muted);font-size:12px;margin:0 2px 10px}
.list-wrap{position:relative;isolation:isolate;overflow:hidden;border-radius:var(--radius);background:var(--panel);border:1px solid var(--border);box-shadow:var(--shadow)}
/* Hover highlight: one pre-painted layer translated over the hovered row, so
   moving the mouse over the listing never triggers a row repaint. */
.row-hl{position:absolute;left:0;right:0;top:0;height:0;z-index:-1;pointer-events:none;background:var(--hover);will-change:transform;transform:translateY(-9999px)}
.list{width:100%;border-collapse:separate;border-spacing:0;background:transparent}
.list th{background:var(--th-bg);text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);padding:11px 14px;border-bottom:1px solid var(--border)}
.list th.sortable{cursor:pointer;user-select:none}
.list th.sortable:hover{color:var(--text)}
.list th.sortable.active{color:var(--primary)}
.list th .si{font-size:10px;margin-left:3px}
.list td{padding:10px 14px;border-bottom:1px solid var(--border)}
.list tbody tr:last-child td{border-bottom:0}
.list tbody tr.row{cursor:pointer}
.list tbody tr.up td{color:var(--muted)}
.c-size,.c-acts{text-align:right}
.c-created,.c-mtime{white-space:nowrap}
.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40vw}
.nm{display:inline-flex;align-items:center;gap:9px;min-width:0}
.nm>span:last-child{overflow:hidden;text-overflow:ellipsis}
.icon{font-size:18px;line-height:1;flex:none}
.thumb{width:40px;height:40px;object-fit:cover;border-radius:6px;border:1px solid var(--border);flex:none;background:var(--hover)}
.size,.mtime{color:var(--muted);font-size:13px;white-space:nowrap}
.size{text-align:right}
.acts{white-space:nowrap}
.act{font:inherit;border:1px solid transparent;background:transparent;border-radius:7px;padding:4px 6px;cursor:pointer;opacity:.7;font-size:15px;line-height:1}
.act:hover{background:var(--act-hover);border-color:var(--border);opacity:1}
.empty{text-align:center;color:var(--muted);padding:48px 0;font-size:15px}
.overlay{position:fixed;inset:0;background:var(--overlay-bg);display:flex;align-items:center;justify-content:center;z-index:50;padding:20px}
.overlay[hidden]{display:none}
.modal{background:var(--panel);border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.35);width:100%;max-width:840px;max-height:92vh;display:flex;flex-direction:column;overflow:hidden}
.modal-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;border-bottom:1px solid var(--border)}
.modal-title{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.icon-btn{font-size:24px;line-height:1;border:none;background:transparent;cursor:pointer;color:var(--muted);border-radius:7px;padding:0 8px}
.icon-btn:hover{background:var(--hover);color:var(--text)}
.modal-body{padding:18px;overflow:auto}
.imgwrap{display:flex;justify-content:center;background:repeating-conic-gradient(var(--checker-a) 0 25%,var(--checker-b) 0 50%) 0 0/18px 18px;border-radius:10px;padding:14px;overflow:auto}
.imgwrap img{max-width:100%;max-height:64vh;border-radius:6px;box-shadow:0 4px 14px rgba(0,0,0,.18)}
.modal-actions{display:flex;gap:10px;margin-top:14px;justify-content:flex-end}
.field label{display:block;font-size:11px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}
.field input{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input-bg);color:var(--text)}
.field input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--focus-ring)}
.field.narrow input{width:160px}
.cmdin{width:100%;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--input-bg);color:var(--text)}
.cmdin:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--focus-ring)}
textarea.cmdin{min-height:120px;resize:vertical;white-space:pre;overflow:auto}
#editor{width:100%;min-height:46vh;max-height:62vh;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:12px;border:1px solid var(--border);border-radius:8px;resize:vertical;white-space:pre;tab-size:4;background:var(--input-bg);color:var(--text)}
.editor-info{color:var(--muted);font-size:12px;margin-bottom:8px}
.nl-sel{font:inherit;font-size:12px;padding:2px 6px;margin-left:6px;border:1px solid var(--border);border-radius:6px;background:var(--input-bg);color:var(--text);cursor:pointer}
.nl-sel:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--focus-ring)}
.badge{display:inline-block;padding:3px 11px;border-radius:999px;font:700 12px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.4px}
.badge.ok{background:#dcfce7;color:#15803d}
.badge.err{background:#fee2e2;color:#b91c1c}
.badge.warn{background:#fef3c7;color:#b45309}
.runout{margin:8px 0 0;padding:12px;background:var(--runout-bg);color:#e5e7eb;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;border-radius:8px;max-height:48vh;overflow:auto;white-space:pre-wrap;word-break:break-word}
.loading{text-align:center;color:var(--muted);padding:40px}
.up-name{font-weight:600;margin-bottom:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{height:10px;background:var(--hover);border-radius:999px;overflow:hidden;border:1px solid var(--border)}
.bar-fill{height:100%;width:0;background:var(--primary);transition:width .12s}
.up-count{text-align:right;color:var(--muted);font-size:12px;margin-top:8px}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(16px);background:#1f2937;color:#fff;padding:11px 18px;border-radius:10px;box-shadow:var(--shadow);opacity:0;pointer-events:none;transition:.25s;max-width:80vw;z-index:60}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{background:var(--danger)}
.hide-sm{display:none}
.tree{list-style:none;padding:0;margin:0;max-height:340px;overflow:auto;border:1px solid var(--border);border-radius:8px}
.tree li{display:flex;align-items:center;gap:6px;padding:6px 10px;cursor:pointer;font-size:13px}
.tree li:hover{background:var(--hover)}
.tree li.selected{background:var(--sel-bg);color:var(--primary);font-weight:600}
.tree li .tw{width:18px;text-align:center;flex:none;user-select:none}
.tree li .tn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hint{color:var(--muted);font-size:12px;margin-bottom:10px}
/* Filter box in the header */
.filter{font:inherit;font-size:13px;padding:7px 11px;width:180px;border:1px solid var(--border);border-radius:var(--radius);background:var(--input-bg);color:var(--text)}
.filter:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--focus-ring)}
.filter::-webkit-search-cancel-button{-webkit-appearance:none}
/* Read-only badge next to the brand */
.badge-ro{font-size:11px;font-weight:700;letter-spacing:.4px;color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:2px 8px;text-transform:uppercase}
/* Selection: checkbox column plus the bulk-action bar above the listing */
.list td.c-sel,.list th.c-sel{width:34px;padding-right:0;text-align:center}
.cbx{width:15px;height:15px;margin:0;accent-color:var(--primary);cursor:pointer;vertical-align:middle}
.list tbody tr.selected td{background:var(--sel-bg)}
.bulk{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 2px 10px;padding:8px 12px;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);font-size:13px}
.bulk[hidden]{display:none}
.bulk b{font-weight:700}
.bulk .btn{padding:5px 10px;font-size:12px}
/* Symlink and broken-entry markers in the name column */
.linkto{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
.badge-broken{color:#b45309;font-size:11px;font-weight:700;letter-spacing:.4px}
html[data-theme="dark"] .badge-broken{color:#ffc861}
/* WebDAV lock marker in the name column and the folder meta line */
.badge-lock{color:#b45309;font-size:11px;font-weight:700;letter-spacing:.4px}
html[data-theme="dark"] .badge-lock{color:#ffc861}
.linkbtn{border:none;background:none;padding:0;cursor:pointer;font:inherit;font-size:12px;color:var(--primary);text-decoration:underline}
/* Help popup: a document, so it reads like one */
.help{font-size:13px;line-height:1.55;max-width:76ch}
.help h3{font-size:13px;margin:20px 0 6px;color:var(--primary)}
.help h3:first-child{margin-top:0}
.help p{margin:0 0 8px}
.help ul{margin:0 0 8px;padding-left:20px}
.help li{margin:2px 0}
.help code,.help kbd{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--input-bg);border:1px solid var(--border);border-radius:4px;padding:1px 5px}
.help pre{margin:0 0 10px;padding:11px 13px;background:var(--runout-bg);color:#e5e7eb;border-radius:8px;overflow-x:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.help pre code{background:none;border:0;padding:0;color:inherit}
.help .keys{display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 0 8px;color:var(--muted);font-size:12px}
.help .legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin:0 0 8px}
.help .legend span{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
.help .muted{color:var(--muted)}
/* Drag-and-drop upload overlay */
.drop{position:fixed;inset:0;z-index:45;display:flex;align-items:center;justify-content:center;pointer-events:none;background:rgba(63,125,74,.13);box-shadow:inset 0 0 0 4px var(--primary)}
html[data-theme="dark"] .drop{background:rgba(0,229,255,.09)}
.drop[hidden]{display:none}
.drop span{background:var(--panel);border:1px solid var(--primary);color:var(--primary);font-weight:700;padding:14px 22px;border-radius:12px;box-shadow:var(--shadow)}

/* ---- Light theme: summer window desk ---- */
#scenery{display:none}
html[data-theme="light"] #scenery{
  display:block;position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden;
  background:linear-gradient(180deg,#78bfe8 0%,#a9d8f2 32%,#d9edf7 48%,#eaf3d9 50%,#a8d47a 50.2%,#7ab85a 70%,#5c9742 100%);
}
html[data-theme="light"] #scenery::before{
  content:"";position:absolute;inset:0;background:
    radial-gradient(120px 42px at 18% 16%,rgba(255,255,255,.9),transparent 70%),
    radial-gradient(170px 54px at 27% 21%,rgba(255,255,255,.7),transparent 70%),
    radial-gradient(100px 34px at 76% 11%,rgba(255,255,255,.8),transparent 70%);
  filter:blur(7px);
}
html[data-theme="light"] #scenery::after{
  content:"";position:absolute;inset:0;
  background:linear-gradient(112deg,rgba(255,249,205,.30),transparent 38%,transparent 68%,rgba(255,242,190,.16));
  mix-blend-mode:screen;
}
html[data-theme="light"] #scenery .sun{
  position:absolute;top:5%;right:9%;width:150px;height:150px;border-radius:50%;
  background:radial-gradient(circle,#fffbe0 0 18%,#fff3b0 32%,rgba(255,236,150,.45) 52%,transparent 72%);
  filter:blur(2px);box-shadow:0 0 90px 30px rgba(255,240,170,.35);
}
html[data-theme="light"] #scenery .field{
  position:absolute;left:0;right:0;bottom:22%;height:28%;
  background:
    radial-gradient(70% 60% at 28% 10%,rgba(255,255,210,.22),transparent 70%),
    linear-gradient(180deg,#a8d47a 0%,#83bd5a 38%,#5f9c42 100%);
}
html[data-theme="light"] #scenery .field::before{
  content:"";position:absolute;inset:0;
  background:
    repeating-linear-gradient(100deg,rgba(255,255,255,.10) 0 1px,transparent 1px 7px),
    radial-gradient(60% 45% at 22% 35%,rgba(255,255,255,.12),transparent 70%),
    radial-gradient(55% 45% at 78% 65%,rgba(20,70,10,.10),transparent 70%);
  opacity:.8;
}
html[data-theme="light"] #scenery .field::after{
  content:"";position:absolute;left:0;right:0;top:-10px;height:26px;
  background:linear-gradient(180deg,rgba(55,95,40,.22),transparent);
}
html[data-theme="light"] #scenery .forest{
  position:absolute;left:-2%;right:-2%;bottom:50%;width:104%;height:110px;
  fill:#3f6b3a;opacity:.92;
}
html[data-theme="light"] #scenery .forest.far{fill:#5b8a50;opacity:.55;bottom:51%;height:80px;transform:scaleX(-1)}
html[data-theme="light"] #scenery .desk{
  position:absolute;left:0;right:0;bottom:0;height:22%;
  background-color:#b98a5a;
  background-image:
    linear-gradient(180deg,rgba(255,230,180,.30),transparent 30%),
    repeating-linear-gradient(3deg,rgba(90,55,25,.10) 0 1px,transparent 1px 7px),
    repeating-linear-gradient(92deg,rgba(255,220,170,.06) 0 1px,transparent 1px 11px);
  border-top:3px solid #8f6845;
  box-shadow:inset 0 10px 22px rgba(255,225,170,.25),0 -8px 24px rgba(60,40,20,.18);
}
html[data-theme="light"] #scenery .desk::before{
  content:"";position:absolute;left:0;right:0;top:0;height:12px;
  background:linear-gradient(180deg,rgba(255,225,170,.35),transparent);
}
html[data-theme="light"] #scenery .pencil{
  position:absolute;right:4%;bottom:9%;width:250px;height:auto;
  transform:rotate(-18deg);transform-origin:80% 50%;
  filter:drop-shadow(0 12px 14px rgba(60,40,10,.28));opacity:.95;
}
html[data-theme="light"] .brand{font-family:Georgia,"Iowan Old Style","Palatino Linotype",serif;font-size:18px;font-weight:700;color:#3c3323}
html[data-theme="light"] .brand-icon{font-size:0}
html[data-theme="light"] .brand-icon::before{content:"✎";font-size:21px;color:var(--primary);margin-right:2px}
html[data-theme="light"] header{
  background:linear-gradient(180deg,rgba(255,253,245,.97),rgba(252,245,222,.95));
  -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
  border-bottom:1px solid #d9c89a;
  box-shadow:inset 0 -1px 0 rgba(255,255,255,.8),0 4px 16px rgba(120,90,40,.10);
}
html[data-theme="light"] .list-wrap{
  border-color:#e2d3ab;background-color:rgba(253,248,230,.74);
  -webkit-backdrop-filter:blur(8px) saturate(105%);backdrop-filter:blur(8px) saturate(105%);
  background-image:
    radial-gradient(circle at 20% 30%,rgba(170,140,80,.045) 0 1px,transparent 1px),
    radial-gradient(circle at 70% 60%,rgba(170,140,80,.035) 0 1px,transparent 1px);
  background-size:7px 7px,11px 11px;
  box-shadow:0 1px 2px rgba(90,70,30,.06),0 10px 26px rgba(90,70,30,.12),5px 5px 0 -1px #f8f0d9,5px 5px 0 0 #e2d3ab,inset 3px 0 0 rgba(196,90,70,.12);
}
html[data-theme="light"] .list th{color:#8a7b5e;border-bottom:1px solid #d9c89a;background:rgba(246,238,212,.72)}
html[data-theme="light"] .list td{border-bottom:1px solid rgba(130,160,190,.20)}
html[data-theme="light"] .row-hl{background:#f6eed4}
html[data-theme="light"] .modal{
  background-color:#fdf8e6;
  background-image:radial-gradient(circle at 20% 30%,rgba(170,140,80,.04) 0 1px,transparent 1px);
  background-size:8px 8px;
  border:1px solid #e2d3ab;
  box-shadow:0 20px 60px rgba(60,45,20,.30),5px 5px 0 -1px #f8f0d9,5px 5px 0 0 #e2d3ab;
}
html[data-theme="light"] .modal-title{font-family:Georgia,"Iowan Old Style","Palatino Linotype",serif;font-size:17px}
html[data-theme="light"] .modal-head{background:rgba(246,238,212,.55);border-bottom-color:#e2d3ab}
html[data-theme="light"] .menu{border-color:#e2d3ab;box-shadow:0 10px 30px rgba(60,45,20,.18),4px 4px 0 -1px #f8f0d9,4px 4px 0 0 #e2d3ab}
html[data-theme="light"] .btn{background:#fffdf5;border-color:#e2d3ab}
html[data-theme="light"] .btn:hover{background:#f6eed4}
html[data-theme="light"] .btn-primary{background:var(--primary);border-color:var(--primary);color:#fff}
html[data-theme="light"] .btn-primary:hover{background:var(--primary-d);border-color:var(--primary-d)}
html[data-theme="light"] #themeBtn{color:#d99a2b}
html[data-theme="light"] .meta{display:inline-block;padding:3px 9px;background:rgba(253,248,230,.88);border:1px solid rgba(226,211,171,.9);border-radius:5px}
html[data-theme="light"] .meta:empty{display:none}
html[data-theme="light"] .empty{max-width:460px;margin:44px auto;padding:30px 26px;background:rgba(253,248,230,.92);border:1px solid var(--border);border-radius:6px;box-shadow:0 10px 26px rgba(90,70,30,.12);font-family:Georgia,"Iowan Old Style","Palatino Linotype",serif;font-style:italic;color:#7c6d52}
html[data-theme="light"] .toast{background:#3c3323;border:1px solid #5a4c33;box-shadow:0 10px 26px rgba(60,45,20,.35)}
html[data-theme="light"] ::selection{background:rgba(63,125,74,.22)}
html[data-theme="light"] *{scrollbar-color:#c9b98d #f3ecd8}
@media(max-width:640px){
  html[data-theme="light"] #scenery .pencil{display:none}
}

/* ---- Dark theme: cyan neon cyberpunk ---- */
html[data-theme="dark"]{
  --bg:#02060a;--panel:rgba(3,14,20,.82);--border:rgba(0,229,255,.30);--text:#d9fbff;
  --muted:#6f9fb0;--primary:#00e5ff;--primary-d:#5cf2ff;--danger:#ff2d78;--hover:rgba(0,229,255,.10);
  --shadow:0 0 0 1px rgba(0,229,255,.10),0 0 18px rgba(0,229,255,.10),0 16px 40px rgba(0,0,0,.65);
  --input-bg:rgba(1,8,12,.92);--th-bg:rgba(0,229,255,.06);--act-hover:rgba(0,229,255,.12);
  --sel-bg:rgba(0,229,255,.16);--overlay-bg:rgba(0,3,6,.80);
  --checker-a:#041018;--checker-b:#071a24;--runout-bg:rgba(0,4,8,.96);
  --focus-ring:rgba(0,229,255,.30);--radius:4px;
  color-scheme:dark;
}
#stars{display:none}
html[data-theme="dark"] #stars{display:block;position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
#stars::before{content:"";position:absolute;inset:-25%;background:
  radial-gradient(45% 38% at 16% 10%,rgba(0,200,235,.14),transparent 70%),
  radial-gradient(38% 34% at 84% 6%,rgba(255,45,120,.07),transparent 70%),
  radial-gradient(60% 45% at 50% 108%,rgba(0,140,190,.12),transparent 72%);
  filter:blur(18px)}
#stars::after{content:"";position:absolute;inset:-2px;background-image:linear-gradient(rgba(0,229,255,.10) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,.10) 1px,transparent 1px);background-size:64px 64px;opacity:.5;-webkit-mask-image:radial-gradient(ellipse at 50% 35%,#000 10%,transparent 72%);mask-image:radial-gradient(ellipse at 50% 35%,#000 10%,transparent 72%)}
.star{position:absolute;border-radius:50%;background:#d8fbff;box-shadow:0 0 4px rgba(0,229,255,.65)}
.star:nth-child(5n){background:#a9f7ff}
.star:nth-child(11n){background:#ffb3d4;box-shadow:0 0 5px rgba(255,45,120,.55)}
.star.big{box-shadow:0 0 7px 1px rgba(0,229,255,.9),0 0 18px 2px rgba(0,229,255,.35)}

/* ---- Dark theme: space-station view over Earth ---- */
#space{display:none}
html[data-theme="dark"] #space{display:block;position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
html[data-theme="dark"] #space .sun{
  position:absolute;top:40%;right:8%;width:36px;height:36px;border-radius:50%;
  background:radial-gradient(circle,#fff 0 30%,#e8ffff 55%,#8df4ff 78%,#2fd6ff 100%);
  filter:blur(.8px);
  box-shadow:
    0 0 12px 3px rgba(180,252,255,.85),
    0 0 44px 14px rgba(80,235,255,.50),
    0 0 120px 55px rgba(0,190,245,.28),
    0 0 260px 130px rgba(0,120,200,.14);
}
html[data-theme="dark"] #space .sun::before{
  content:"";position:absolute;left:38%;right:38%;top:-340%;bottom:-340%;
  background:linear-gradient(180deg,transparent,rgba(170,248,255,.35) 50%,transparent);
  filter:blur(3px);
}
html[data-theme="dark"] #space .sun::after{
  content:"";position:absolute;top:42%;bottom:42%;left:-900%;right:-900%;
  background:linear-gradient(90deg,transparent,rgba(170,248,255,.30) 50%,transparent);
  filter:blur(3px);
}
html[data-theme="dark"] #space .earth{
  position:absolute;left:50%;top:63%;width:250vw;height:250vw;transform:translateX(-50%);
  border-radius:50%;overflow:hidden;
  background:
    radial-gradient(130% 105% at 80% -8%,
      rgba(160,245,255,.90) 0%,
      rgba(70,190,240,.80) 1.1%,
      rgba(24,105,165,.92) 2.6%,
      rgba(9,52,95,.97) 4.6%,
      rgba(4,24,46,1) 7.5%,
      #01040a 14%);
  box-shadow:
    0 -1px 0 rgba(200,252,255,.95),
    0 -4px 18px rgba(120,238,255,.55),
    0 -16px 70px rgba(0,150,220,.40),
    0 -40px 180px rgba(0,90,170,.25);
}
html[data-theme="dark"] #space .earth::before{
  content:"";position:absolute;inset:0;
  background:
    radial-gradient(22% 2.2% at 26% 1.4%,rgba(215,250,255,.30),transparent 70%),
    radial-gradient(30% 2.6% at 48% 3.2%,rgba(190,240,255,.20),transparent 72%),
    radial-gradient(18% 2.0% at 66% 1.9%,rgba(220,252,255,.24),transparent 72%),
    radial-gradient(40% 3.4% at 38% 5.6%,rgba(160,225,255,.12),transparent 74%);
  filter:blur(9px);mix-blend-mode:screen;
}
html[data-theme="dark"] #space .earth .lights{
  position:absolute;left:0;right:0;top:0;height:42vh;pointer-events:none;
}
html[data-theme="dark"] #space .lights i{
  position:absolute;border-radius:50%;background:#ffcf8a;
  box-shadow:0 0 5px rgba(255,190,120,.75),0 0 10px rgba(255,160,80,.30);
}
html[data-theme="dark"] #space .earth::after{
  content:"";position:absolute;inset:0;
  background:radial-gradient(80% 5% at 62% 0%,rgba(40,150,190,.16),transparent 72%);
  mix-blend-mode:screen;
}
html[data-theme="dark"] #space .glass{
  position:absolute;inset:0;
  background:
    linear-gradient(118deg,rgba(170,245,255,.07) 0%,rgba(170,245,255,.02) 14%,transparent 30%,transparent 68%,rgba(170,245,255,.045) 100%),
    radial-gradient(120% 85% at 50% 42%,transparent 52%,rgba(0,0,0,.42) 100%);
}
html[data-theme="dark"] #space .frame{
  position:absolute;inset:0;border-radius:16px;
  box-shadow:
    inset 0 0 0 9px rgba(1,6,10,.55),
    inset 0 0 0 10px rgba(0,229,255,.22),
    inset 0 0 90px rgba(0,0,0,.55);
}
html[data-theme="dark"] #space .frame::before{
  content:"";position:absolute;inset:-1px;
  background:
    linear-gradient(#00e5ff,#00e5ff) left top/54px 2px no-repeat,
    linear-gradient(#00e5ff,#00e5ff) left top/2px 54px no-repeat,
    linear-gradient(#00e5ff,#00e5ff) right bottom/54px 2px no-repeat,
    linear-gradient(#00e5ff,#00e5ff) right bottom/2px 54px no-repeat;
  opacity:.35;
  filter:drop-shadow(0 0 6px rgba(0,229,255,.7));
}
@media(max-width:640px){
  html[data-theme="dark"] #space .sun{width:26px;height:26px;top:54%;right:5%}
}
html[data-theme="dark"] body{
  background-color:#02060a;
  background-image:radial-gradient(1200px 760px at 50% -16%,rgba(0,229,255,.10),transparent 62%),radial-gradient(900px 600px at 92% 112%,rgba(255,45,120,.06),transparent 60%);
  background-attachment:fixed;color:var(--text);
}
html[data-theme="dark"] body::after{
  content:"";position:fixed;inset:0;z-index:15;pointer-events:none;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.30) 0 1px,transparent 1px 3px);
  opacity:.22;
}
html[data-theme="dark"] header{
  background:rgba(1,8,12,.88);-webkit-backdrop-filter:blur(10px) saturate(140%);backdrop-filter:blur(10px) saturate(140%);
  border-bottom:1px solid rgba(0,229,255,.50);
  box-shadow:0 0 20px rgba(0,229,255,.22),0 1px 0 rgba(120,248,255,.35),inset 0 -1px 18px rgba(0,229,255,.06);
}
html[data-theme="dark"] .list-wrap,html[data-theme="dark"] .modal,html[data-theme="dark"] .menu,html[data-theme="dark"] .tree{-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
html[data-theme="dark"] .list-wrap{border-radius:2px;background-color:rgba(3,14,20,.74);border-color:rgba(0,229,255,.35);box-shadow:0 0 0 1px rgba(0,229,255,.06),0 0 24px rgba(0,229,255,.12),0 20px 50px rgba(0,0,0,.7)}
html[data-theme="dark"] .modal{border-radius:4px;border:1px solid rgba(0,229,255,.45);box-shadow:0 0 0 1px rgba(0,229,255,.10),0 0 40px rgba(0,229,255,.18),0 30px 80px rgba(0,0,0,.8)}
html[data-theme="dark"] .modal-head{border-bottom:1px solid rgba(0,229,255,.30);background:rgba(0,229,255,.04)}
html[data-theme="dark"] .modal-title{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.06em;color:#c9fbff}
html[data-theme="dark"] .brand{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;text-transform:uppercase;letter-spacing:.22em;color:#e8ffff;text-shadow:0 0 6px rgba(0,229,255,.9),0 0 20px rgba(0,229,255,.5)}
html[data-theme="dark"] .brand .brand-icon{display:none}
html[data-theme="dark"] .brand::before{content:"◈";color:var(--primary);margin-right:8px;text-shadow:0 0 10px rgba(0,229,255,.95),0 0 24px rgba(0,229,255,.6)}
html[data-theme="dark"] .crumb{color:var(--primary);text-shadow:0 0 8px rgba(0,229,255,.4)}
html[data-theme="dark"] .crumb.current{color:var(--text);text-shadow:0 0 8px rgba(0,229,255,.25)}
html[data-theme="dark"] .list th{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:1.4px;color:#7fe9ff;border-bottom:1px solid rgba(0,229,255,.40);text-shadow:0 0 8px rgba(0,229,255,.45)}
html[data-theme="dark"] .list th.sortable.active{color:#bffaff;text-shadow:0 0 10px rgba(0,229,255,.9)}
html[data-theme="dark"] .list td{border-bottom:1px solid rgba(0,229,255,.10)}
html[data-theme="dark"] .nm{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
html[data-theme="dark"] .icon,html[data-theme="dark"] .act,html[data-theme="dark"] .emoji{filter:grayscale(1) brightness(1.55) sepia(1) hue-rotate(155deg) saturate(5)}
html[data-theme="dark"] .size,html[data-theme="dark"] .mtime{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
html[data-theme="dark"] .btn{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;letter-spacing:.08em;text-transform:uppercase;border-radius:3px;background:rgba(2,14,20,.80);border-color:rgba(0,229,255,.35);color:#bff8ff;box-shadow:inset 0 0 12px rgba(0,229,255,.05)}
html[data-theme="dark"] .btn:hover{background:rgba(0,229,255,.12);border-color:rgba(0,229,255,.85);color:#eaffff;box-shadow:0 0 12px rgba(0,229,255,.45),inset 0 0 16px rgba(0,229,255,.12)}
html[data-theme="dark"] .btn-primary{background:linear-gradient(180deg,#00e5ff,#00a8c8);border-color:#7df4ff;color:#001014;text-shadow:none;box-shadow:0 0 14px rgba(0,229,255,.55),0 0 34px rgba(0,229,255,.25)}
html[data-theme="dark"] .btn-primary:hover{background:linear-gradient(180deg,#5cf2ff,#00c8e8);box-shadow:0 0 20px rgba(0,229,255,.8),0 0 50px rgba(0,229,255,.4)}
html[data-theme="dark"] .act:hover{background:rgba(0,229,255,.14);border-color:rgba(0,229,255,.55);box-shadow:0 0 12px rgba(0,229,255,.45)}
html[data-theme="dark"] .row-hl{background:linear-gradient(90deg,rgba(0,229,255,.14),rgba(0,229,255,.03));border-left:2px solid var(--primary)}
html[data-theme="dark"] .menu{border-radius:3px;border-color:rgba(0,229,255,.40);box-shadow:0 0 24px rgba(0,229,255,.18),0 14px 40px rgba(0,0,0,.7)}
html[data-theme="dark"] .menu-item{border-radius:2px}
html[data-theme="dark"] .menu-item:hover{background:rgba(0,229,255,.12);box-shadow:inset 2px 0 0 var(--primary)}
html[data-theme="dark"] .tree{border-radius:3px}
html[data-theme="dark"] .tree li.selected{background:rgba(0,229,255,.14);box-shadow:inset 2px 0 0 var(--primary),0 0 14px rgba(0,229,255,.18)}
html[data-theme="dark"] .field input,html[data-theme="dark"] .cmdin,html[data-theme="dark"] #editor,html[data-theme="dark"] .nl-sel{background:var(--input-bg);color:var(--text);border-radius:3px}
html[data-theme="dark"] .field input:focus,html[data-theme="dark"] .cmdin:focus,html[data-theme="dark"] #editor:focus,html[data-theme="dark"] .nl-sel:focus{box-shadow:0 0 0 2px var(--focus-ring),0 0 18px rgba(0,229,255,.35)}
html[data-theme="dark"] ::placeholder{color:rgba(111,159,176,.7)}
html[data-theme="dark"] ::selection{background:rgba(0,229,255,.35);color:#fff}
html[data-theme="dark"] .badge{border-radius:2px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:1px;text-transform:uppercase}
html[data-theme="dark"] .badge.ok{background:rgba(0,255,170,.10);color:#5cffc4;border:1px solid rgba(0,255,170,.40);box-shadow:0 0 10px rgba(0,255,170,.22)}
html[data-theme="dark"] .badge.err{background:rgba(255,45,120,.12);color:#ff9ec4;border:1px solid rgba(255,45,120,.45);box-shadow:0 0 10px rgba(255,45,120,.25)}
html[data-theme="dark"] .badge.warn{background:rgba(255,196,0,.10);color:#ffd66b;border:1px solid rgba(255,196,0,.40);box-shadow:0 0 10px rgba(255,196,0,.20)}
html[data-theme="dark"] .toast{border-radius:3px;background:rgba(1,10,14,.96);border:1px solid rgba(0,229,255,.45);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;letter-spacing:.04em;box-shadow:0 0 24px rgba(0,229,255,.30),0 12px 30px rgba(0,0,0,.65)}
html[data-theme="dark"] .toast.err{background:rgba(36,0,14,.96);border-color:rgba(255,45,120,.65);box-shadow:0 0 24px rgba(255,45,120,.40)}
html[data-theme="dark"] .runout{border-radius:3px;border:1px solid rgba(0,229,255,.20);box-shadow:inset 0 0 30px rgba(0,229,255,.06)}
html[data-theme="dark"] .imgwrap{border-radius:3px}
html[data-theme="dark"] .imgwrap img{border-radius:3px;box-shadow:0 0 24px rgba(0,229,255,.28)}
html[data-theme="dark"] .thumb{border-radius:2px}
html[data-theme="dark"] .bar{border-color:rgba(0,229,255,.30);border-radius:2px}
html[data-theme="dark"] .bar-fill{background:linear-gradient(90deg,#00a8c8,#00e5ff);box-shadow:0 0 12px rgba(0,229,255,.8)}
html[data-theme="dark"] #themeBtn{color:var(--primary);font-size:18px;text-shadow:0 0 8px rgba(0,229,255,.9),0 0 20px rgba(0,229,255,.5)}
html[data-theme="dark"] .dot{box-shadow:0 0 8px rgba(255,45,120,.8)}
html[data-theme="dark"] .hgroup{border-left-color:rgba(0,229,255,.28)}
html[data-theme="dark"] *{scrollbar-color:rgba(0,229,255,.55) rgba(1,8,12,.7)}
html[data-theme="dark"] .overlay{-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px)}
@media(max-width:640px){.hide-sm{display:none}main{padding:12px}.name{max-width:55vw}}
</style>
</head>
<body>
<div id="scenery" aria-hidden="true">
  <div class="sun"></div>
  <svg class="forest far" viewBox="0 0 1440 160" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <pattern id="treesFar" x="0" y="0" width="180" height="160" patternUnits="userSpaceOnUse">
        <path d="M0 160 L20 86 L40 160 Z"/>
        <path d="M28 160 L50 58 L72 160 Z"/>
        <path d="M58 160 L76 104 L94 160 Z"/>
        <path d="M80 160 L100 70 L120 160 Z"/>
        <path d="M112 160 L130 92 L148 160 Z"/>
        <path d="M136 160 L156 64 L176 160 Z"/>
        <path d="M166 160 L184 98 L202 160 Z"/>
      </pattern>
    </defs>
    <path d="M0 160 L0 112 Q 45 98 90 110 T 180 108 T 270 112 T 360 102 T 450 110 T 540 106 T 630 112 T 720 100 T 810 108 T 900 104 T 990 112 T 1080 102 T 1170 110 T 1260 106 T 1350 112 T 1440 104 L1440 160 Z" fill="#5b8a50"/>
    <rect width="1440" height="160" fill="url(#treesFar)"/>
  </svg>
  <svg class="forest" viewBox="0 0 1440 160" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <pattern id="treesNear" x="0" y="0" width="240" height="160" patternUnits="userSpaceOnUse">
        <path d="M0 160 L18 72 L36 160 Z"/>
        <path d="M20 160 L40 38 L60 160 Z"/>
        <path d="M44 160 L58 96 L72 160 Z"/>
        <path d="M54 160 L74 54 L94 160 Z"/>
        <path d="M88 160 L106 80 L124 160 Z"/>
        <path d="M110 160 L130 30 L150 160 Z"/>
        <path d="M144 160 L160 90 L176 160 Z"/>
        <path d="M164 160 L184 48 L204 160 Z"/>
        <path d="M198 160 L216 78 L234 160 Z"/>
        <path d="M218 160 L236 60 L254 160 Z"/>
      </pattern>
    </defs>
    <path d="M0 160 L0 104 Q 40 88 80 100 T 160 98 T 240 102 T 320 92 T 400 100 T 480 96 T 560 102 T 640 90 T 720 98 T 800 94 T 880 102 T 960 92 T 1040 100 T 1120 96 T 1200 102 T 1280 90 T 1360 98 T 1440 94 L1440 160 Z" fill="#3f6b3a"/>
    <rect width="1440" height="160" fill="url(#treesNear)"/>
  </svg>
  <div class="field"></div>
  <div class="desk"></div>
  <svg class="pencil" viewBox="0 0 260 30" aria-hidden="true">
    <rect x="42" y="7" width="190" height="16" rx="2" fill="#f2c14e"/>
    <rect x="42" y="7" width="190" height="5" fill="#f8d97a"/>
    <rect x="42" y="17" width="190" height="2" fill="#d9a441" opacity=".55"/>
    <polygon points="42,7 8,15 42,23" fill="#e8c48a"/>
    <polygon points="8,15 20,12 20,18" fill="#3b3325"/>
    <rect x="232" y="7" width="18" height="16" rx="2" fill="#f2a7b8"/>
    <rect x="224" y="7" width="9" height="16" fill="#c9c9c9"/>
  </svg>
</div>
<div id="stars" aria-hidden="true"></div>
<div id="space" aria-hidden="true">
  <div class="sun"></div>
  <div class="earth"><div class="lights"></div></div>
  <div class="glass"></div>
  <div class="frame"></div>
</div>
<header>
  <div class="brand"><span class="brand-icon">🗂️</span>Files</div>
  <span class="badge-ro" id="roBadge" hidden title="This server was started with --read-only: nothing can be changed">read-only</span>
  <nav class="crumbs" id="crumbs"></nav>
  <div class="spacer"></div>
  <input class="filter" id="filter" type="search" placeholder="Filter this folder" aria-label="Filter this folder" autocomplete="off" spellcheck="false">
  <div class="menu-wrap" id="newWrap">
    <button class="btn" id="newBtn" type="button" aria-haspopup="menu" aria-controls="newMenu" aria-expanded="false" title="Create a folder or a text file (press n)">+ New ▾</button>
    <div class="menu" id="newMenu" role="menu" hidden>
      <button class="menu-item" id="newDir" role="menuitem" tabindex="-1"><span class="icon">📁</span>New folder</button>
      <button class="menu-item" id="newFile" role="menuitem" tabindex="-1"><span class="icon">📄</span>New text file</button>
    </div>
  </div>
  <button class="btn btn-primary" id="uploadBtn" type="button" title="Upload files into the current folder (press u)">Upload</button>
  <div class="menu-wrap">
    <button class="btn" id="toolsBtn" type="button" aria-haspopup="menu" aria-controls="toolsMenu" aria-expanded="false" title="Zip download, base64 transfer, make and shell"><span class="emoji">⚙</span> Tools ▾<span class="dot" id="toolsDot" hidden title="Automatic make is off"></span></button>
    <div class="menu" id="toolsMenu" role="menu" hidden>
      <div class="menu-label">This folder</div>
      <button class="menu-item" id="zipBtn" role="menuitem" tabindex="-1" title="Download the current folder as a zip file"><span class="icon">📦</span>Download folder as zip</button>
      <button class="menu-item" id="upload64Btn" role="menuitem" tabindex="-1" title="Upload as base64 inside JSON: a workaround for a network that mangles binary uploads"><span class="icon">🔣</span>Upload as base64</button>
      <div class="menu-sep" data-grp="run"></div>
      <div class="menu-label" data-grp="run">Build and run</div>
      <button class="menu-item" id="makeRunBtn" role="menuitem" tabindex="-1" title="Run make in the current folder"><span class="icon">🔨</span>Run make here</button>
      <button class="menu-item" id="makeBtn" role="menuitemcheckbox" aria-checked="true" tabindex="-1" title="make runs automatically after edits, uploads and deletes (choose to disable it)"><span class="icon">🔁</span>Auto-make after edits<span class="state" id="makeLabel">on</span></button>
      <button class="menu-item" id="shellBtn" role="menuitem" tabindex="-1" title="Run a bash command on the server"><span class="icon">🖥</span>Shell</button>
    </div>
  </div>
  <div class="hgroup">
    <button class="btn" id="themeBtn" type="button" title="Toggle light / dark theme" aria-label="Toggle light / dark theme">☼</button>
    <button class="btn" id="helpBtn" type="button" title="Help: what everything here does, and how to mount this folder with WebDAV" aria-label="Help">?</button>
  </div>
  <input type="file" id="files" multiple hidden>
  <input type="file" id="files64" multiple hidden>
</header>
<main>
  <div class="meta" id="meta"></div>
  <div class="bulk" id="bulk" hidden></div>
  <div class="list-wrap">
    <div class="row-hl" aria-hidden="true"></div>
    <table class="list">
      <thead>
        <tr>
          <th class="c-sel"><input type="checkbox" class="cbx" id="selAll" title="Select everything in this folder" aria-label="Select everything in this folder"></th>
          <th class="sortable" data-sort="name">Name<span class="si"></span></th>
          <th class="c-size sortable" data-sort="size">Size<span class="si"></span></th>
          <th class="c-created sortable" data-sort="ctime">Created<span class="si"></span></th>
          <th class="c-mtime sortable" data-sort="mtime">Last updated<span class="si"></span></th>
          <th class="c-acts">Actions</th>        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div class="empty" id="empty" hidden>This folder is empty.</div>
</main>

<div class="overlay" id="overlay" hidden>
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <span class="modal-title" id="modalTitle"></span>
      <button class="icon-btn" id="modalClose" title="Close">&times;</button>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>

<div class="toast" id="toast"></div>
<div class="drop" id="drop" hidden><span>Drop files to upload them here</span></div>

<script>
const ROOT_NAME = __ROOT_NAME__;
const FEATURES = __FEATURES__;  // {shell, run, make, b64, write, dav} — set by the server at startup
const MAKE_POLL_MS = __MAKE_POLL_MS__;  // how often a background make job is polled
const DAV_MOUNT = __DAV_MOUNT__;  // "/dav/" when the WebDAV share is on, "" when off
const LIMITS = __DAV_LIMITS__;  // the tuning values the Help popup quotes
const $ = s => document.querySelector(s);
const rows = $("#rows"), crumbs = $("#crumbs");

// Row hover highlight: a pre-painted layer (.row-hl) is moved with transform
// over the hovered row, so sweeping the mouse over the listing never triggers
// a row repaint (which used to dominate CPU on every mouse move).
{
  const hl = $(".row-hl");
  if (hl && rows) {
    const table = rows.closest("table");
    const hideHl = () => { hl.style.transform = "translateY(-9999px)"; };
    rows.addEventListener("mouseover", ev => {
      const r = ev.target.closest("tr.row");
      if (!r) { hideHl(); return; }
      const rect = r.getBoundingClientRect(), top = table.getBoundingClientRect();
      const h = Math.round(rect.height);
      if (hl.style.height !== h + "px") hl.style.height = h + "px";
      hl.style.transform = "translateY(" + Math.round(rect.top - top.top) + "px)";
    });
    rows.addEventListener("mouseleave", hideHl);
  }
}
const state = { path: "", parent: null, entries: [], sizes: {}, sizesLoaded: false,
                sort: { key: "name", dir: 1 }, total: 0, truncated: false,
                filter: "", sel: new Set(), locked: false, lockOwner: null };
let dirty = false;      // editor has unsaved changes (see beforeunload)
let uploadBusy = false; // an upload is running (see beforeunload)

const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function fmtSize(n){ if(n==null) return ""; if(n===0) return "0 B"; const u=["B","KB","MB","GB","TB"]; let i=0; while(n>=1024 && i<u.length-1){n/=1024;i++;} return n.toFixed(n>=10||i===0?0:1)+" "+u[i]; }
function fmtDate(ts){ const d=new Date(ts*1000), p=x=>String(x).padStart(2,"0"); return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes()); }
// Folder part of a path: "" means the root. The old version returned the file
// name for a path without a slash, which made "Extract into" default to a file.
function dirname(p){ const i=String(p||"").lastIndexOf("/"); return i<0 ? "" : p.slice(0,i); }
function dirLabel(p){ const d=dirname(p); return d==="" ? ROOT_NAME : d; }

const MAKE_KEY = "fm-auto-make";
// Auto-make preference: "0" in localStorage disables it; absent means on.
// The choice is sent to the server per request, so the server keeps no state.
function storedAutoMake(){ try{ return localStorage.getItem(MAKE_KEY) !== "0"; }catch(e){ return true; } }
let autoMakeEnabled = storedAutoMake();

function api(path, opts){
  opts = opts || {};
  if(!autoMakeEnabled){
    const h = new Headers(opts.headers || {});
    h.set("X-FM-Auto-Make", "0");
    opts.headers = h;
  }
  return fetch(path, opts).then(async r=>{
    let data=null;
    if((r.headers.get("content-type")||"").includes("json")){ try{ data=await r.json(); }catch(e){} }
    if(!r.ok){
      const err = new Error((data&&data.error) || (r.status+" "+r.statusText));
      err.status = r.status;  // callers distinguish 404 (expired job) from 409 (conflict)
      throw err;
    }
    return data;
  });
}

let toastTimer;
function toast(msg, isErr){
  const t=$("#toast"); t.textContent=msg; t.classList.toggle("err", !!isErr); t.classList.add("show");
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.classList.remove("show"), 3500);
}

let modalQueue=[];  // finished background make runs waiting for the modal to be free
function openModal(title, html){ $("#modalTitle").textContent=title; $("#modalBody").innerHTML=html; $("#overlay").hidden=false; }
function closeModal(force){
  // Losing typed text to a stray Escape is the one way this tool can eat work.
  if(!force && dirty && !confirm("Discard your unsaved changes?")) return;
  dirty=false;
  $("#overlay").hidden=true; $("#modalBody").innerHTML="";
  const next=modalQueue.shift();
  if(next) showMakeResult(next.title, next.runs);
}
$("#modalClose").onclick=()=>closeModal();
$("#overlay").addEventListener("mousedown", e=>{ if(e.target===$("#overlay")) closeModal(); });
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeModal(); });
window.addEventListener("beforeunload", e=>{
  if(dirty || uploadBusy){ e.preventDefault(); e.returnValue=""; }
});

// -- loading / listing -------------------------------------------------------
// The current folder lives in the URL hash, so the back button, reloads and
// pasted links all work; the server never sees a hash, so it stays stateless.
function pathFromHash(){
  const raw=(location.hash||"").replace(/^#\/?/, "");
  if(!raw) return "";
  try { return decodeURIComponent(raw); } catch(e) { return raw; }
}
function setHash(path){
  const want = "#/" + String(path||"").split("/").filter(Boolean).map(encodeURIComponent).join("/");
  if(location.hash !== want) location.hash = want;  // adds a history entry
}
window.addEventListener("hashchange", ()=>{
  const p = pathFromHash();
  if(p !== state.path) load(p);
});

let loadSeq = 0;  // guards against a slow listing overwriting a newer one
function load(path){
  state.path = path || "";
  state.sizes = {}; state.sizesLoaded = false;
  state.sel.clear(); state.filter="";
  const f=$("#filter"); if(f) f.value="";
  setHash(state.path);
  updateCrumbs();
  render();  // show the loading state for the new folder right away
  const seq=++loadSeq;
  api("api/list?path="+encodeURIComponent(state.path)).then(d=>{
    if(seq!==loadSeq) return;  // superseded by a newer click
    state.entries=d.entries; state.parent=d.parent;
    state.total=d.total; state.truncated=!!d.truncated;
    state.locked=!!d.locked; state.lockOwner=d.lock_owner||null;
    document.title = (state.path? state.path+"/" : "") + ROOT_NAME + " · Files";
    render();
    loadFolderSizes(seq);
  }).catch(err=>{ if(seq===loadSeq) toast(err.message, true); });
}

function sizeOf(e){ return e.kind==="dir" ? (state.sizes[e.path] ?? -1) : (e.size ?? 0); }

function sortedEntries(){
  const {key, dir} = state.sort;
  const needle = state.filter.trim().toLowerCase();
  const list = needle
    ? state.entries.filter(e=>e.name.toLowerCase().includes(needle))
    : state.entries;
  const dirs = list.filter(e=>e.kind==="dir");
  const files = list.filter(e=>e.kind!=="dir");
  const cmp = {
    name:  (a,b)=>a.name.localeCompare(b.name, undefined, {sensitivity:"base"}),
    size:  (a,b)=>sizeOf(a)-sizeOf(b),
    ctime: (a,b)=>a.ctime-b.ctime,
    mtime: (a,b)=>a.mtime-b.mtime
  }[key];
  dirs.sort(cmp); files.sort(cmp);
  if(dir===-1){ dirs.reverse(); files.reverse(); }
  return dirs.concat(files);
}

function render(){
  const shown=sortedEntries();
  rows.innerHTML="";
  const dirsAll=state.entries.filter(x=>x.kind==="dir").length;
  const filesAll=state.entries.length-dirsAll;
  let meta = state.entries.length
    ? dirsAll+" folder"+(dirsAll!==1?"s":"")+", "+filesAll+" file"+(filesAll!==1?"s":"")
    : "";
  if(state.truncated){
    // The server capped the listing; say so instead of implying this is all.
    meta += " · showing the first "+state.entries.length+" of "+state.total+" entries";
  } else if(state.filter){
    meta += " · "+shown.length+" of "+state.entries.length+" match “"+esc(state.filter)+"”";
  }
  // A lock on this folder covers everything inside it, so say so once here
  // instead of letting every write fail on its own.
  if(state.locked){
    meta += ' · <span class="badge-lock">🔒 locked over WebDAV'
      + (state.lockOwner ? " by "+esc(state.lockOwner) : "")+"</span>"
      + ' <button class="linkbtn" id="unlockHere">Unlock</button>';
  }
  $("#meta").innerHTML = meta;
  const unlockHere=$("#unlockHere");
  if(unlockHere){
    unlockHere.onclick=()=>unlockEntry({ name: state.path || ROOT_NAME, path: state.path });
  }
  $("#empty").hidden = shown.length>0 || !state.entries.length;
  $("#empty").textContent = state.entries.length
    ? "Nothing here matches “"+state.filter+"”."
    : "This folder is empty.";
  if(state.path) rows.appendChild(trUp());
  shown.forEach(x=>rows.appendChild(trEntry(x)));
  renderBulk();
  const all=$("#selAll");
  if(all){
    const paths=state.entries.map(e=>e.path);
    all.checked = paths.length>0 && paths.every(p=>state.sel.has(p));
    all.disabled = paths.length===0;
  }
}

function updateCrumbs(){
  const parts = state.path ? state.path.split("/") : [];
  let html = '<a class="crumb" data-p=""><span class="emoji">🏠</span> '+esc(ROOT_NAME)+'</a>';
  let acc="";
  parts.forEach((seg,i)=>{
    acc += (acc?"/":"")+seg;
    html += '<span class="sep">/</span>';
    html += (i===parts.length-1)
      ? '<span class="crumb current">'+esc(seg)+'</span>'
      : '<a class="crumb" data-p="'+esc(acc)+'">'+esc(seg)+'</a>';
  });
  crumbs.innerHTML=html;
  crumbs.querySelectorAll("a.crumb").forEach(a=>a.onclick=()=>load(a.dataset.p));
}

// -- folder sizes (recursive, one batch request) ----------------------------
function loadFolderSizes(seq){
  const dirPaths = [...new Set(state.entries.filter(e=>e.kind==="dir").map(e=>e.path))];
  if(dirPaths.length===0){ state.sizesLoaded=true; return; }
  const mine = seq===undefined || seq===loadSeq;
  api("api/foldersize", { method:"POST", headers:{"Content-Type":"application/json"},
                          body: JSON.stringify({paths: dirPaths}) })
    .then(d=>{
      if(!mine) return;
      state.sizes=d.sizes||{}; state.sizesLoaded=true; render(); updateSortIndicators();
    })
    .catch(()=>{ if(mine) state.sizesLoaded=true; });
}

// -- sorting ----------------------------------------------------------------
function updateSortIndicators(){
  document.querySelectorAll("th.sortable").forEach(th=>{
    const si=th.querySelector(".si");
    if(th.dataset.sort===state.sort.key){ si.textContent = state.sort.dir===1 ? "▲" : "▼"; th.classList.add("active"); }
    else { si.textContent=""; th.classList.remove("active"); }
  });
}
function toggleSort(key){
  if(state.sort.key===key) state.sort.dir*=-1;
  else state.sort={ key, dir: (key==="mtime"||key==="ctime") ? -1 : 1 };  // dates default newest-first
  updateSortIndicators();
  if(key==="size" && !state.sizesLoaded){ loadFolderSizes(); }
  else { render(); }
}
document.querySelectorAll("th.sortable").forEach(th=>{
  th.onclick=()=>toggleSort(th.dataset.sort);
});

// -- rows -------------------------------------------------------------------
function trUp(){
  const tr=document.createElement("tr"); tr.className="row up";
  tr.innerHTML='<td class="c-sel"></td><td class="name"><span class="nm"><span class="icon">📂</span><span>..</span></span></td><td class="size"></td><td class="mtime"></td><td class="mtime"></td><td class="acts"></td>';
  tr.onclick=()=>{ if(state.parent!==null && state.parent!==undefined) load(state.parent); };
  return tr;
}

function iconHTML(e){
  if(e.kind==="dir") return '<span class="icon">📁</span>';
  if(e.kind==="image") return e.thumb
    ? '<img class="thumb" loading="lazy" src="api/thumbnail/'+encodeURIComponent(e.path)+'" onerror="this.outerHTML=\'<span class=icon>🖼️</span>\'">'
    : '<span class="icon">🖼️</span>';
  if(e.kind==="text") return '<span class="icon">📄</span>';
  return '<span class="icon">🗎</span>';
}

// Symlinks are shown as the entry they are, so say what they point at: it is
// the link that delete/rename/move act on, not its target.
function markHTML(e){
  if(e.broken) return ' <span class="badge-broken" title="This symbolic link points at nothing">broken</span>';
  if(e.locked) return ' <span class="badge-lock" title="'+esc(lockTip(e))+'">🔒 locked</span>';
  if(e.link) return ' <span class="linkto" title="Symbolic link to '+esc(e.link)+'">↗ '+esc(e.link)+'</span>';
  return "";
}

function lockTip(e){
  return "Locked over WebDAV"+(e.lock_owner ? " by "+e.lock_owner : "")
    +": a client that has this folder mounted is writing to it. "
    + "Close the file there, or unlock it here.";
}

function sizeCell(e){
  if(e.kind==="dir"){
    return state.sizes[e.path]!==undefined ? fmtSize(state.sizes[e.path]) : "…";
  }
  return fmtSize(e.size);
}

// Every row action in one table, because the buttons in the listing and the
// legend in the Help popup have to agree; the popup prints this table.
const ACT = {
  open:    ["📂", "Open this folder"],
  zip:     ["📦", "Download this folder as a zip file"],
  rename:  ["✏️", "Rename"],
  move:    ["🚚", "Move to another folder"],
  copy:    ["📋", "Copy to another folder"],
  extract: ["🗜️", "Extract this zip archive into the folder"],
  run:     ["▶", "Run this executable"],
  view:    ["👁", "Open in the viewer"],
  edit:    ["✎", "Open in the editor"],
  dl:      ["⬇", "Download the file"],
  dlb64:   ["🔣", "Transfer as base64 (a workaround for a flaky network)"],
  unlock:  ["🔓", "Break the WebDAV lock on this entry"],
  del:     ["🗑", "Delete"],
};

function act(id, title){
  const [icon, hint] = ACT[id];
  return '<button class="act" data-id="'+id+'" title="'+esc(title || hint)+'">'+icon+'</button>';
}

function actionsHTML(e){
  let h="";
  if(e.kind==="dir"){
    h+=act("open")+act("zip");
  }
  if(FEATURES.write){
    h+=act("rename")+act("move")+act("copy");
  }
  if(e.kind!=="dir"){
    if(FEATURES.write && e.name.toLowerCase().endsWith(".zip")) h+=act("extract");
    if(e.run) h+=act("run");
    h+=act(e.kind==="text" ? "edit" : "view");
    h+=act("dl");
    if(FEATURES.b64) h+=act("dlb64", "Download as base64 (workaround for network problems)");
  }
  // A lock held through the mount: unlocking is the only way to keep working
  // here without hunting down whichever editor has the file open.
  if(FEATURES.dav && FEATURES.write && e.locked) h+=act("unlock", lockTip(e));
  if(FEATURES.write) h+=act("del");
  return h;
}

function trEntry(e){
  const tr=document.createElement("tr"); tr.className="row";
  const isDir=e.kind==="dir";
  if(state.sel.has(e.path)) tr.classList.add("selected");
  const cb = FEATURES.write
    ? '<td class="c-sel"><input type="checkbox" class="cbx" aria-label="Select '+esc(e.name)+'"'+(state.sel.has(e.path)?" checked":"")+'></td>'
    : '<td class="c-sel"></td>';
  tr.innerHTML=cb+
    '<td class="name"><span class="nm">'+iconHTML(e)+'<span>'+esc(e.name)+'</span></span>'+markHTML(e)+'</td>'+
    '<td class="size">'+sizeCell(e)+'</td>'+
    '<td class="mtime">'+fmtDate(e.ctime)+'</td>'+
    '<td class="mtime">'+fmtDate(e.mtime)+'</td>'+
    '<td class="acts">'+actionsHTML(e)+'</td>';
  tr.onclick=()=>{ isDir ? load(e.path) : openFile(e); };
  const box=tr.querySelector(".cbx");
  if(box){
    box.onclick=ev=>{ ev.stopPropagation(); setSel(e.path, box.checked, tr); };
  }
  tr.querySelectorAll(".act").forEach(b=>{
    b.onclick=ev=>{ ev.stopPropagation(); ACTION_MAP[b.dataset.id](e); };
  });
  return tr;
}
const ACTION_MAP={ open:e=>load(e.path), view:e=>openFile(e), edit:e=>openFile(e),
  dl:e=>download(e), del:e=>del(e), rename:e=>renameItem(e), move:e=>moveItem(e),
  copy:e=>copyItem(e), extract:e=>extractZip(e), zip:e=>zipDir(e.path),
  dlb64:e=>downloadB64(e), run:e=>runFile(e), unlock:e=>unlockEntry(e) };

// -- multi-select ------------------------------------------------------------
function setSel(path, on, tr){
  if(on) state.sel.add(path); else state.sel.delete(path);
  if(tr) tr.classList.toggle("selected", on);
  renderBulk();
  const all=$("#selAll");
  if(all){
    const paths=state.entries.map(e=>e.path);
    all.checked = paths.length>0 && paths.every(p=>state.sel.has(p));
  }
}
$("#selAll").onchange=function(){
  const paths=state.entries.map(e=>e.path);
  if(this.checked) paths.forEach(p=>state.sel.add(p));
  else paths.forEach(p=>state.sel.delete(p));
  render();
};

// Everything selected at once runs one request per entry: this tool has no
// batch endpoint, and the folders it deals with are small enough for that.
function bulkEach(kind, dest, verb){
  const paths=[...state.sel];
  if(!paths.length) return;
  const jobs=[];
  const step=i=>{
    if(i>=paths.length){
      state.sel.clear();
      closeModal();
      load(state.path);
      toast(verb+" "+paths.length+" item"+(paths.length===1?"":"s"));
      if(jobs.length) pollMakeJobs("after "+verb.toLowerCase()+"ing the selection", jobs);
      return;
    }
    const q="?path="+encodeURIComponent(paths[i]);
    const opts = kind==="delete" ? {method:"POST"}
      : { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({dest}) };
    api("api/"+kind+q, opts)
      .then(d=>{ if(d && d.make) jobs.push({file:paths[i], job:d.make.job}); })
      .catch(err=>toast(paths[i]+": "+err.message, true))
      .then(()=>step(i+1));
  };
  step(0);
}

function renderBulk(){
  const bar=$("#bulk"), n=state.sel.size;
  if(!n || !FEATURES.write){ bar.hidden=true; bar.innerHTML=""; return; }
  bar.hidden=false;
  bar.innerHTML='<b>'+n+' selected</b>'+
    '<button class="btn" id="bulkMove">🚚 Move…</button>'+
    '<button class="btn" id="bulkCopy">📋 Copy…</button>'+
    '<button class="btn" id="bulkDel">🗑 Delete</button>'+
    '<div class="spacer"></div>'+
    '<button class="btn" id="bulkNone">Clear selection</button>';
  $("#bulkMove").onclick=()=>pickFolder("Move "+n+" items", "Move", "", false, dest=>bulkEach("move", dest, "Moved"));
  $("#bulkCopy").onclick=()=>pickFolder("Copy "+n+" items", "Copy", "", false, dest=>bulkEach("copy", dest, "Copied"));
  $("#bulkDel").onclick=()=>{
    if(!confirm("Delete the "+n+" selected item"+(n===1?"":"s")+"?")) return;
    bulkEach("delete", "", "Deleted");
  };
  $("#bulkNone").onclick=()=>{ state.sel.clear(); render(); };
}

function openFile(e){
  if(e.kind==="image") viewImage(e);
  else if(e.kind==="text") editText(e);
  else askPreview(e);
}

// No built-in viewer for this type: offer to open it in the browser (the
// browser may render it, e.g. PDF/video/audio, or fall back to downloading).
function askPreview(e){
  const q=encodeURIComponent(e.path);
  let actions="";
  if(e.run) actions+='<button class="btn btn-primary" id="askRun">▶ Run</button>';
  actions+='<button class="btn'+(e.run?"":" btn-primary")+'" id="askRaw">Open in browser</button>';
  actions+='<button class="btn" id="askText">View/edit as text</button>';
  actions+='<a class="btn" href="api/download?path='+q+'">Download</a>';
  actions+='<button class="btn" onclick="closeModal()">Cancel</button>';
  openModal(e.name,
    '<div class="hint">There is no built-in preview for this file type. You can try opening it in a new browser tab (the browser may download it instead), or download the file.</div>'+
    '<div class="modal-actions">'+actions+'</div>');
  $("#askRaw").onclick=()=>{ window.open("api/raw?path="+q, "_blank", "noopener"); closeModal(); };
  $("#askText").onclick=()=>editText(e);
  const runBtn=$("#askRun");
  if(runBtn) runBtn.onclick=()=>runFile(e);
}

function viewImage(e){
  const q=encodeURIComponent(e.path);
  openModal(e.name,
    '<div class="imgwrap"><img src="api/raw?path='+q+'" alt="'+esc(e.name)+'"></div>'+
    '<div class="modal-actions"><a class="btn btn-primary" href="api/download?path='+q+'">Download</a><button class="btn" onclick="closeModal()">Close</button></div>');
}

const NL_LABEL={ lf:"LF (Unix)", crlf:"CRLF (Windows)", cr:"CR (Classic Mac)", mixed:"Mixed", none:"No line breaks" };
function editText(e){
  const q=encodeURIComponent(e.path);
  openModal("Editing — "+e.name, '<div class="loading">Loading…</div>');
  api("api/text?path="+q).then(d=>{
    const origNL=d.newline||"none";
    let mtimeUs=d.mtime_us;  // what the server says the file is; a save must match it
    $("#modalTitle").textContent="Editing — "+e.name;
    $("#modalBody").innerHTML=
      '<div class="editor-info">'+fmtSize(d.size)+' · Line endings: <b>'+esc(NL_LABEL[origNL]||origNL)+'</b>'+
      ' <select id="nlSel" class="nl-sel" title="Line endings to save with">'+
      '<option value="keep">Keep as-is</option>'+
      '<option value="lf">Change to LF (Unix)</option>'+
      '<option value="crlf">Change to CRLF (Windows)</option>'+
      '<option value="cr">Change to CR (Classic Mac)</option>'+
      '</select>'+
      (FEATURES.write?' · <b>Ctrl/Cmd+S</b> saves':'</div>')+'</div>'+
      '<textarea id="editor" spellcheck="false"'+(FEATURES.write?'':' readonly')+'></textarea>'+
      '<div class="modal-actions">'+(FEATURES.write?'<button class="btn btn-primary" id="saveBtn">Save</button>':'')+'<button class="btn" onclick="closeModal()">Close</button></div>';
    const ta=$("#editor"); ta.value=d.content;
    const sel=$("#nlSel");
    sel.options[0].textContent="Keep as-is ("+(NL_LABEL[origNL]||origNL)+")";
    const save=$("#saveBtn");
    const doSave=()=>{ if(save) save.disabled=true; saveText(e, ta, sel.value, origNL, mtimeUs, d=>{ mtimeUs=d; if(save) save.disabled=false; }); };
    if(save) save.onclick=doSave;
    ta.addEventListener("input", ()=>{ dirty=true; });
    ta.addEventListener("keydown", ev=>{
      if((ev.ctrlKey||ev.metaKey) && ev.key.toLowerCase()==="s" && FEATURES.write){
        ev.preventDefault(); doSave();
      }
    });
    ta.focus();
  }).catch(err=>{
    openModal(e.name,
      '<div class="editor-info">'+esc(err.message)+'</div>'+
      '<div class="modal-actions"><button class="btn btn-primary" id="txtRaw">Open in browser</button><a class="btn" href="api/download?path='+q+'">Download</a><button class="btn" onclick="closeModal()">Close</button></div>');
    $("#txtRaw").onclick=()=>{ window.open("api/raw?path="+q, "_blank", "noopener"); closeModal(); };
  });
}

function saveText(e, ta, sel, origNL, ifMtime, onSaved){
  // The browser textarea normalizes line endings to \n, so "keep" means
  // re-applying the file's original line ending on the server (when it had
  // a single consistent one; "mixed"/"none" are saved as typed).
  const nl = sel==="keep" ? (["lf","crlf","cr"].includes(origNL) ? origNL : "as-is") : sel;
  const body = { content: ta.value, newline: nl };
  if(ifMtime != null) body.if_mtime_us = ifMtime;  // refuse to clobber an outside edit
  api("api/save?path="+encodeURIComponent(e.path), { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body) })
    .then(d=>{
      dirty=false;                 // the buffer matches the file again
      if(onSaved) onSaved(d.mtime_us);
      toast("Saved "+e.name);
      load(state.path);            // keep editing: the modal stays open
      if(d.make) pollMakeJobs("after saving "+e.name, [{file:e.name, job:d.make.job}]);
    })
    .catch(err=>{
      if(err.status===409){
        // The file moved on underneath us (second tab, a make, an outside
        // editor). Say so and offer to reload instead of overwriting it.
        if(confirm(err.message+"\n\nReload the version on disk? Choosing Cancel keeps your current edits.")) editText(e);
      } else {
        toast(err.message, true);
      }
    });
}

function del(e){
  const what=e.kind==="dir"?"folder":"file";
  let suffix=e.kind==="dir"?" (and everything inside it)":"";
  if(e.link) suffix=" — this is a symbolic link, so only the link is deleted";
  if(!confirm('Delete '+what+' "'+e.name+'"?'+suffix)) return;
  api("api/delete?path="+encodeURIComponent(e.path), {method:"POST"})
    .then(d=>{
      toast("Deleted "+e.name);
      load(state.path);
      if(d.make) pollMakeJobs("after deleting "+e.name, [{file:e.name, job:d.make.job}]);
    })
    .catch(err=>toast(err.message, true));
}

function download(e){ window.location = "api/download?path="+encodeURIComponent(e.path); }

// Unlocking is recovery, not a permission: it is here so a mount whose client
// died (or a laptop that walked away with the file open) does not lock the
// entry for the whole timeout. The client on the mount finds out on its next
// write, which is exactly what it is built to handle.
function unlockEntry(e){
  if(!confirm('Break the WebDAV lock on "'+e.name+'"?'+
      '\n\nWhatever has the file open through the mount will lose its lock and '
      +'may have to reload the file.')) return;
  api("api/unlock?path="+encodeURIComponent(e.path), {method:"POST"})
    .then(d=>{
      toast(d.unlocked ? "Unlocked "+e.name : e.name+" was already unlocked");
      load(state.path);
    })
    .catch(err=>toast(err.message, true));
}

function zipDir(path){
  toast("Preparing zip…");
  window.location = "api/zip?path="+encodeURIComponent(path||"");
}
$("#zipBtn").onclick=()=>zipDir(state.path);

// -- Download as base64 (workaround: JSON transport instead of binary stream)
// The answer is read as a stream rather than with r.json(): at the top of the
// transfer limit the document is 1.4 GB of base64, and JSON.parse of that asks
// the browser for several gigabytes at once. It is turned back into bytes in
// whole groups of four characters as it arrives, so only one network chunk of
// it is held as text and the peak is the file itself.
function b64ToBytes(group){
  const bin=atob(group), out=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++) out[i]=bin.charCodeAt(i);
  return out;
}

async function receiveB64(e){
  const r=await fetch("api/download64?path="+encodeURIComponent(e.path));
  if(!r.ok){
    let msg=r.status+" "+r.statusText;
    try{ const d=await r.json(); if(d && d.error) msg=d.error; }catch(err){}
    throw new Error(msg);
  }
  if(!r.body || !r.body.getReader) throw new Error("This browser cannot stream the answer");
  const utf8=new TextDecoder(), reader=r.body.getReader();
  let text="", undecoded="", meta=null, parts=[], bytes=0, ended=false;
  const paint=()=>{
    const fill=$("#barFill64");
    if(fill) fill.style.width=(meta.size?Math.min(100, 100*bytes/meta.size):100).toFixed(1)+"%";
    const cnt=$("#upCount64");
    if(cnt) cnt.textContent=fmtSize(bytes)+" of "+fmtSize(meta.size)+" (base64 in JSON)";
  };
  while(!ended){
    const got=await reader.read();
    ended=got.done;
    text += utf8.decode(got.value || new Uint8Array(), {stream:!ended});
    if(!meta){
      const at=text.indexOf(',"data":"');
      if(at<0){
        if(ended) throw new Error("Unexpected answer from the server");
        continue;  // the {"path":…,"size":…} header is not complete yet
      }
      try{ meta=JSON.parse(text.slice(0,at)+"}"); }   // the header ends with the size
      catch(err){ throw new Error("Unexpected answer from the server"); }
      text=text.slice(at+9);
      openModal("Downloading — "+meta.name,
        '<div class="up-name">'+esc(meta.name)+' · base64</div>'+
        '<div class="bar"><div class="bar-fill" id="barFill64"></div></div>'+
        '<div class="up-count" id="upCount64"></div>');
    }
    undecoded += text; text="";
    // Two characters are always held back: the document closes with `"}` and
    // that must not be taken for base64. What is decoded is a whole number of
    // groups of four, so a group never spans two flushes.
    let take=undecoded.length-2;
    if(take>0){
      take -= take%4;
      if(take>0){
        const part=b64ToBytes(undecoded.slice(0,take));
        parts.push(part); bytes+=part.length; undecoded=undecoded.slice(take);
        paint();
      }
    }
  }
  if(!meta) throw new Error("Unexpected answer from the server");
  if(!undecoded.endsWith('"}')) throw new Error("The transfer stopped early");
  if(bytes!==meta.size) throw new Error("The transfer stopped early");
  return {name: meta.name, blob:new Blob(parts, {type:"application/octet-stream"})};
}

function downloadB64(e){
  toast("Downloading "+e.name+" as base64…");
  receiveB64(e).then(({name, blob})=>{
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");
    a.href=url; a.download=name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 10000);
    closeModal(true);
    toast("Downloaded "+name+" ("+fmtSize(blob.size)+" of base64)");
  }).catch(err=>{ closeModal(true); toast(err.message, true); });
}

// -- Extract zip -------------------------------------------------------------
function extractZip(e){
  const q=encodeURIComponent(e.path);
  openModal("Extract — "+e.name,
    '<div class="hint">Extracts the contents of <b>'+esc(e.name)+'</b> into the folder below.</div>'+
    '<div class="field"><label>Destination folder (relative to root)</label><input id="exDest" value="'+esc(dirname(e.path))+'" autocomplete="off" spellcheck="false" placeholder="(same folder as the zip)"></div>'+
    '<label style="display:flex;gap:8px;align-items:center;margin-top:12px;font-size:13px;cursor:pointer"><input type="checkbox" id="exDelete" checked style="width:auto">Delete the zip after extracting</label>'+
    '<div id="exResult"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="exGo">🗜️ Extract</button><button class="btn" onclick="closeModal()">Cancel</button></div>');
  const inp=$("#exDest"); inp.focus();
  const doEx=()=>{
    const dest=inp.value.trim();
    const delZip=$("#exDelete").checked;
    const resEl=$("#exResult");
    resEl.innerHTML='<div class="loading" style="padding:16px 0">Extracting…</div>';
    api("api/extract?path="+q,
        { method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ dest, delete: delZip }) })
      .then(d=>{
        let extra=d.entries+" file"+(d.entries===1?"":"s")+" · "+fmtSize(d.bytes)+(d.deleted?" · zip deleted":"");
        resEl.innerHTML=
          '<div style="margin-top:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
            '<span class="badge ok">EXTRACTED</span>'+
            '<span class="editor-info" style="margin:0">'+esc(extra)+'</span>'+
            (d.deleted?'':'<button class="btn" id="exCleanup" style="font-size:12px;padding:5px 10px">Delete zip</button>')+
          '</div>';
        const c=$("#exCleanup");
        if(c) c.onclick=()=>del({ path: decodeURIComponent(q), name: e.name, kind: "file" });
        load(state.path);
      })
      .catch(err=>{
        resEl.innerHTML='<div style="margin-top:12px"><span class="badge err">ERROR</span> <span class="editor-info" style="margin:0">'+esc(err.message)+'</span></div>';
      });
  };
  inp.onkeydown=ev=>{ if(ev.key==="Enter") doEx(); };
  $("#exGo").onclick=doEx;
}

// -- shared run-result renderer (colored exit-code badge) -------------------
function renderRunResult(d, resEl){
  let cls, label;
  if(d.timed_out){ cls="warn"; label="TIMEOUT"; }
  else if(d.code===0){ cls="ok"; label="EXIT 0"; }
  else { cls="err"; label="EXIT "+d.code; }
  const extra=[];
  if(d.cwd) extra.push("in /"+esc(d.cwd));
  if(d.truncated) extra.push("output truncated");
  resEl.innerHTML=
    '<div style="margin-top:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
      '<span class="badge '+cls+'">'+label+'</span>'+
      '<span class="editor-info" style="margin:0">'+extra.join(" · ")+'</span>'+
    '</div>'+
    '<pre id="runOut" class="runout"></pre>';
  const pre=document.getElementById("runOut");
  pre.textContent = d.output ? d.output : "(no output)";
}

// -- Automatic make results (after edit/upload in a dir with a Makefile) ----
// A build can finish while another dialog is open (the editor, an upload), and
// overwriting that dialog lost the output, so finished builds queue up and are
// shown one at a time as soon as the modal is free again.
function makeModal(title, runs){
  openModal("make — "+title,
    runs.map(r=>{
      const d=r.res;
      const cls=d.timed_out?"warn":(d.code===0?"ok":"err");
      const label=d.timed_out?"TIMEOUT":("EXIT "+d.code);
      const extra=[];
      if(d.cwd) extra.push("in /"+esc(d.cwd));
      if(d.truncated) extra.push("output truncated");
      return '<div style="margin-top:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
        '<span class="editor-info" style="margin:0">'+esc(r.file)+'</span>'+
        '<span class="badge '+cls+'">'+label+'</span>'+
        '<span class="editor-info" style="margin:0">'+extra.join(" · ")+'</span></div>'+
        '<pre class="runout">'+esc(d.output||"(no output)")+'</pre>';
    }).join("")+
    '<div class="modal-actions"><button class="btn btn-primary" onclick="closeModal(true)">Close</button></div>');
}
function showMakeResult(title, runs){
  if(!runs || !runs.length) return;
  if(!$("#overlay").hidden){
    modalQueue.push({title, runs});
    toast("make finished — its output is queued behind this dialog");
    return;
  }
  makeModal(title, runs);
}

// Auto-make runs in the server's background: poll each job until it reports
// a result, then show one combined make modal (as before).
function pollMakeJobs(title, jobs){
  const t0=Date.now();
  const result={};
  (function poll(){
    const pending=jobs.filter(j=>result[j.job]===undefined);
    if(!pending.length){
      showMakeResult(title, jobs.map(j=>({file:j.file, res:result[j.job]})).filter(r=>r.res));
      return;
    }
    if(Date.now()-t0>10*60*1000){ toast("Make results expired", true); return; }
    Promise.all(pending.map(j=>api("api/make/result?job="+j.job)
        .then(d=>{ if(d.done) result[j.job]=d.result; })
        // An expired job never reports anything, so stop asking about it. The
        // status is on the error, not in its message.
        .catch(err=>{ if(err.status===404) result[j.job]=null; })))
      .then(()=>setTimeout(poll, MAKE_POLL_MS));
  })();
}

// -- Run executable file ----------------------------------------------------
function runFile(e){
  openModal("Run — "+e.name,
    '<div class="hint">Runs <b>'+esc(e.name)+'</b> in <b>'+esc(dirLabel(e.path))+'</b> using the current user’s environment.</div>'+
    '<div class="field"><label>Arguments (optional, quote multi-word values)</label><input id="runArgs" placeholder="--flag 42  input.txt" autocomplete="off" spellcheck="false"></div>'+
    '<div class="field narrow" style="margin-top:12px"><label>Timeout (seconds)</label><input id="runTimeout" type="number" min="1" max="3600" step="1" value="60"></div>'+
    '<div id="runResult"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="runGo">▶ Run</button><button class="btn" onclick="closeModal()">Close</button></div>');
  const inp=$("#runArgs"); inp.focus();
  const doRun=()=>{
    const args=inp.value;
    const timeout=Math.max(1, Math.min(3600, parseInt($("#runTimeout").value,10)||60));
    const resEl=$("#runResult");
    resEl.innerHTML='<div class="loading" style="padding:16px 0">Running…</div>';
    api("api/run?path="+encodeURIComponent(e.path),
        { method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ args, timeout }) })
      .then(d=>renderRunResult(d, resEl))
      .catch(err=>{
        resEl.innerHTML='<div style="margin-top:12px"><span class="badge err">ERROR</span> <span class="editor-info" style="margin:0">'+esc(err.message)+'</span></div>';
      });
  };
  inp.onkeydown=ev=>{ if(ev.key==="Enter"){ ev.preventDefault(); doRun(); } };
  $("#runGo").onclick=doRun;
}

// -- Shell (Tools menu): run a bash command ---------------------------------
let shellHistory=[];
let shellHistIdx=-1;
function openShell(){
  openModal("Shell",
    '<div class="hint">Runs in <b>bash</b> using the current user’s environment. <b>Ctrl/Cmd+Enter</b> to run; <b>Ctrl/Cmd+↑/↓</b> for history.</div>'+
    '<div class="field"><label>Working directory (relative to root)</label><input id="shellCwd" value="'+esc(state.path)+'" autocomplete="off" spellcheck="false" placeholder="(root)"></div>'+
    '<div class="field" style="margin-top:12px"><label>Command (multi-line)</label><textarea id="shellCmd" class="cmdin" autocomplete="off" spellcheck="false" placeholder="ls -la"></textarea></div>'+
    '<div class="field narrow" style="margin-top:12px"><label>Timeout (seconds)</label><input id="shellTimeout" type="number" min="1" max="3600" step="1" value="60"></div>'+
    '<div id="shellResult"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="shellGo">▶ Run</button><button class="btn" onclick="closeModal()">Close</button></div>');
  const cmd=$("#shellCmd"); cmd.focus();
  const doRun=()=>{
    const command=cmd.value.trim();
    if(!command){ toast("Enter a command", true); return; }
    const cwd=$("#shellCwd").value.trim();
    const timeout=Math.max(1, Math.min(3600, parseInt($("#shellTimeout").value,10)||60));
    shellHistory.push(command); shellHistIdx=shellHistory.length;
    const resEl=$("#shellResult");
    resEl.innerHTML='<div class="loading" style="padding:16px 0">Running…</div>';
    api("api/shell",
        { method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ command, cwd, timeout }) })
      .then(d=>renderRunResult(d, resEl))
      .catch(err=>{
        resEl.innerHTML='<div style="margin-top:12px"><span class="badge err">ERROR</span> <span class="editor-info" style="margin:0">'+esc(err.message)+'</span></div>';
      });
  };
  cmd.onkeydown=ev=>{
    const mod = ev.ctrlKey || ev.metaKey;
    if(ev.key==="Enter" && mod){ ev.preventDefault(); doRun(); }
    else if(ev.key==="ArrowUp" && mod){ ev.preventDefault(); if(shellHistIdx>0){ shellHistIdx--; cmd.value=shellHistory[shellHistIdx]||""; } }
    else if(ev.key==="ArrowDown" && mod){ ev.preventDefault(); if(shellHistIdx<shellHistory.length){ shellHistIdx++; cmd.value=shellHistory[shellHistIdx]||""; } }
  };
  $("#shellGo").onclick=doRun;
}
$("#shellBtn").onclick=openShell;

// -- Make (Tools menu): run make in the current folder on demand ------------
function runMake(){
  const where = state.path || ROOT_NAME;
  openModal("make — "+where,
    '<div class="hint">Runs <b>make</b> in this folder.</div>'+
    '<div id="makeRunOut"><div class="loading" style="padding:16px 0">Running…</div></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="makeAgain">▶ Run again</button><button class="btn" onclick="closeModal()">Close</button></div>');
  const run=()=>{
    const out=$("#makeRunOut");
    if(!out) return;
    out.innerHTML='<div class="loading" style="padding:16px 0">Running…</div>';
    api("api/make", { method:"POST", headers:{"Content-Type":"application/json"},
                      body: JSON.stringify({ path: state.path }) })
      .then(d=>{
        const el=$("#makeRunOut");
        if(el) renderRunResult(d, el);
        load(state.path);
      })
      .catch(err=>{
        const el=$("#makeRunOut");
        if(el) el.innerHTML='<div style="margin-top:12px"><span class="badge err">ERROR</span> <span class="editor-info" style="margin:0">'+esc(err.message)+'</span></div>';
      });
  };
  $("#makeAgain").onclick=run;
  run();
}
$("#makeRunBtn").onclick=runMake;

// -- Rename --
function renameItem(e){
  openModal("Rename "+(e.kind==="dir"?"folder":"file"),
    '<div class="field"><label>New name</label><input id="renameInput" value="'+esc(e.name)+'" autocomplete="off"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="renameOk">Rename</button><button class="btn" onclick="closeModal()">Cancel</button></div>');
  const inp=$("#renameInput");
  const lastDot=e.kind!=="dir"&&e.name.lastIndexOf(".");
  inp.focus(); inp.select();
  if(lastDot>0) inp.setSelectionRange(0, lastDot);
  inp.onkeydown=ev=>{ if(ev.key==="Enter") doRename(e, inp); };
  $("#renameOk").onclick=()=>doRename(e, inp);
}
function doRename(e, inp){
  const name=inp.value.trim();
  if(!name){ toast("Name cannot be empty", true); return; }
  if(name===e.name){ closeModal(); return; }
  api("api/rename?path="+encodeURIComponent(e.path), { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({name}) })
    .then(()=>{ toast("Renamed to "+name); closeModal(); load(state.path); })
    .catch(err=>toast(err.message, true));
}

// -- Move / Copy (shared folder tree picker) ---------------------------------
// One picker for moving, copying and the bulk actions: it hands the chosen
// destination folder to a callback instead of owning the action itself.
let pickCb=null, pickVerb="Move", pickExcl="", pickExclDir=false;
function pickFolder(title, verb, excludePath, excludeIsDir, onPick){
  pickCb=onPick; pickVerb=verb; pickExcl=excludePath||""; pickExclDir=!!excludeIsDir;
  openModal(title,
    '<div class="hint">Select a destination folder:</div>'+
    '<div id="moveTree"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="pickOk" disabled>'+esc(pickVerb)+' here</button><button class="btn" onclick="closeModal()">Cancel</button></div>');
  loadMoveTree("");
}
function pickExcluded(p){ return p===pickExcl || (pickExclDir && p.startsWith(pickExcl+"/")); }

function loadMoveTree(dirPath){
  api("api/tree?path="+encodeURIComponent(dirPath)).then(d=>{
    const ul=document.createElement("ul");
    ul.className="tree";
    const rootLi=document.createElement("li");
    rootLi.innerHTML='<span class="tw">🏠</span><span class="tn">'+esc(ROOT_NAME)+' (root)</span>';
    rootLi.dataset.path="";
    rootLi.onclick=()=>selectMoveDir(rootLi, "");
    ul.appendChild(rootLi);
    d.dirs.forEach(dir=>{
      if(pickExcluded(dir.path)) return;
      const li=document.createElement("li");
      li.innerHTML='<span class="tw">📁</span><span class="tn">'+esc(dir.name)+'</span>';
      li.dataset.path=dir.path;
      li.onclick=()=>selectMoveDir(li, dir.path);
      ul.appendChild(li);
    });
    const container=$("#moveTree");
    container.innerHTML="";
    container.appendChild(ul);
    const subBtn=document.createElement("div");
    subBtn.style.cssText="margin-top:8px;display:flex;gap:8px;align-items:center";
    subBtn.innerHTML='<button class="btn" style="font-size:12px;padding:5px 10px" id="browseSub">📂 Browse subfolders…</button><span style="font-size:12px;color:var(--muted)" id="browseHint">root</span>';
    let browseDir="";
    subBtn.querySelector("#browseSub").onclick=()=>{
      api("api/tree?path="+encodeURIComponent(browseDir)).then(dd=>{
        const frag=document.createElement("div");
        frag.style.cssText="margin-top:8px;padding:8px 0;border-top:1px solid var(--border)";
        frag.innerHTML='<div style="font-size:12px;color:var(--muted);margin-bottom:6px">Folders in /'+esc(browseDir)+':</div>';
        const sUl=document.createElement("ul");
        sUl.className="tree";
        sUl.style.maxHeight="180px";
        if(dd.parent!==null){
          const pLi=document.createElement("li");
          pLi.innerHTML='<span class="tw">⬆</span><span class="tn">..</span>';
          pLi.onclick=()=>{ browseDir=dd.parent; loadMoveTree(""); };
          sUl.appendChild(pLi);
        }
        dd.dirs.forEach(dir2=>{
          if(pickExcluded(dir2.path)) return;
          const li2=document.createElement("li");
          li2.innerHTML='<span class="tw">📁</span><span class="tn">'+esc(dir2.name)+'</span>';
          li2.onclick=()=>selectMoveDir(li2, dir2.path);
          sUl.appendChild(li2);
        });
        frag.appendChild(sUl);
        const useBtn=document.createElement("button");
        useBtn.className="btn btn-primary";
        useBtn.style.cssText="margin-top:8px;font-size:12px;padding:5px 12px";
        useBtn.textContent=pickVerb+" to here";
        useBtn.onclick=()=>pickCb(browseDir);
        frag.appendChild(useBtn);
        subBtn.parentElement.insertBefore(frag, subBtn.parentElement.querySelector(".modal-actions"));
      });
    };
    container.appendChild(subBtn);
    $("#pickOk").disabled=true;
  });
}

function selectMoveDir(li, path){
  document.querySelectorAll("#moveTree .tree li.selected").forEach(x=>x.classList.remove("selected"));
  li.classList.add("selected");
  $("#pickOk").disabled=false;
  $("#pickOk").onclick=()=>pickCb(path);
}

function moveItem(e){
  const isDir=e.kind==="dir";
  pickFolder("Move "+(isDir?"folder":"file")+" — "+e.name, "Move", e.path, isDir,
             dest=>transfer("move", e, dest));
}
function copyItem(e){
  const isDir=e.kind==="dir";
  pickFolder("Copy "+(isDir?"folder":"file")+" — "+e.name, "Copy", e.path, isDir,
             dest=>transfer("copy", e, dest));
}
function transfer(kind, e, dest){
  api("api/"+kind+"?path="+encodeURIComponent(e.path),
      { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({dest}) })
    .then(d=>{
      closeModal();
      toast(kind==="move" ? "Moved "+e.name : "Copied "+e.name+" into "+(dest||ROOT_NAME));
      load(state.path);
      if(d.make) pollMakeJobs("after "+(kind==="move"?"moving":"copying")+" "+e.name, [{file:e.name, job:d.make.job}]);
    })
    .catch(err=>toast(err.message, true));
}

// -- header dropdowns: "+ New" and "Tools" ----------------------------------
// Both menus behave alike: the button toggles one, opening it closes the
// other, and Escape or a click anywhere else closes them. Arrow keys walk the
// items, and choosing one closes the menu before its action runs, so a menu is
// never left hanging over the dialog it just opened. Only a checkbox item
// stays open, because its on/off label is the feedback for that click.
const MENUS = [];
function closeMenus(){
  MENUS.forEach(m=>{ m.menu.hidden=true; m.btn.setAttribute("aria-expanded","false"); });
}
function setupMenu(btnSel, menuSel){
  const btn=$(btnSel), menu=$(menuSel);
  if(!btn || !menu) return;
  // Items the feature flags removed are not in the tab order either.
  const items=()=>[...menu.querySelectorAll(".menu-item")].filter(el=>el.style.display!=="none");
  const focusAt=i=>{ const list=items(); if(list.length) list[((i%list.length)+list.length)%list.length].focus(); };
  const close=()=>{ menu.hidden=true; btn.setAttribute("aria-expanded","false"); };
  // A menu hangs from its button's right edge, which on a narrow (wrapped)
  // header puts it partly off screen, so nudge it back inside the viewport.
  // It stays absolutely positioned on purpose: the header's backdrop-filter
  // would make it the containing block of a fixed element anyway.
  const place=()=>{
    menu.style.right="0px";
    const r=menu.getBoundingClientRect();
    const shift=Math.max(0, r.right-(innerWidth-8)) - Math.max(0, 8-r.left);
    if(shift) menu.style.right=shift+"px";
  };
  const open=()=>{
    closeMenus();
    menu.hidden=false;
    btn.setAttribute("aria-expanded","true");
    place();
    focusAt(0);
  };
  btn.onclick=ev=>{ ev.stopPropagation(); if(menu.hidden) open(); else close(); };
  btn.onkeydown=ev=>{ if(ev.key==="ArrowDown"){ ev.preventDefault(); open(); } };
  menu.addEventListener("click", ev=>{
    const item = ev.target.closest(".menu-item");
    if(!item) return;
    // Ticking a checkbox item leaves the menu open: that is where its on/off
    // label is, and closing it would hide the result of the click.
    if(item.getAttribute("role") === "menuitemcheckbox") return;
    close();
  });
  menu.addEventListener("keydown", ev=>{
    const at=items().indexOf(document.activeElement);
    if(ev.key==="ArrowDown"){ ev.preventDefault(); focusAt(at+1); }
    else if(ev.key==="ArrowUp"){ ev.preventDefault(); focusAt(at-1); }
    else if(ev.key==="Home"){ ev.preventDefault(); focusAt(0); }
    else if(ev.key==="End"){ ev.preventDefault(); focusAt(-1); }
    else if(ev.key==="Tab"){ close(); }
    else if(ev.key==="Escape"){ ev.preventDefault(); close(); btn.focus(); }
  });
  MENUS.push({btn, menu, open, close});
}
setupMenu("#newBtn", "#newMenu");
setupMenu("#toolsBtn", "#toolsMenu");
document.addEventListener("click", ev=>{
  if(!ev.target || !ev.target.closest(".menu-wrap")) closeMenus();
});
document.addEventListener("keydown", ev=>{ if(ev.key==="Escape") closeMenus(); });
// Resizing or rotating a device moves the buttons out from under an open menu.
window.addEventListener("resize", closeMenus);
// Opens a menu by the button's id, for the keyboard shortcuts below; false
// when the menu was removed along with the features it holds (offsetParent is
// null for a hidden button and for one inside a hidden group).
function openMenu(btnSel){
  const m=MENUS.find(x=>x.btn.matches(btnSel));
  if(!m || m.btn.offsetParent === null) return false;
  m.open();
  return true;
}

$("#newDir").onclick=()=>{
  openModal("New folder",
    '<div class="field"><label>Folder name</label><input id="dirName" placeholder="my-new-folder" autocomplete="off"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="mkOk">Create</button><button class="btn" onclick="closeModal()">Cancel</button></div>');
  const inp=$("#dirName"); inp.focus();
  inp.onkeydown=ev=>{ if(ev.key==="Enter") doMkdir(inp); };
  $("#mkOk").onclick=()=>doMkdir(inp);
};
function doMkdir(inp){
  const name=inp.value.trim();
  if(!name){ toast("Enter a folder name", true); return; }
  const rel = state.path ? state.path+"/"+name : name;
  api("api/mkdir?path="+encodeURIComponent(rel), {method:"POST"})
    .then(()=>{ toast("Created "+name); closeModal(); load(state.path); })
    .catch(err=>toast(err.message, true));
}

$("#newFile").onclick=()=>{
  openModal("New text file",
    '<div class="field"><label>File name</label><input id="fileName" value="untitled.txt" placeholder="untitled.txt" autocomplete="off" spellcheck="false"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="nfOk">Create</button><button class="btn" onclick="closeModal()">Cancel</button></div>');
  const inp=$("#fileName"); inp.focus(); inp.select();
  inp.onkeydown=ev=>{ if(ev.key==="Enter") doNewFile(inp); };
  $("#nfOk").onclick=()=>doNewFile(inp);
};
function doNewFile(inp){
  let name=inp.value.trim();
  if(!name){ toast("Enter a file name", true); return; }
  if(!name.includes(".")) name+=".txt";
  const rel = state.path ? state.path+"/"+name : name;
  api("api/newfile?path="+encodeURIComponent(rel), {method:"POST"})
    .then(d=>{
      toast("Created "+name);
      closeModal();
      load(state.path);
      editText({ path: d.path, name });
    })
    .catch(err=>toast(err.message, true));
}

// -- Uploads -----------------------------------------------------------------
const UPLOAD_PARALLEL = 3;  // uploads in flight at once
// Both pickers are hidden inputs in the header rather than inside the buttons
// that open them, so a menu item can close its menu and still reach the file
// dialog.
$("#uploadBtn").onclick=()=>$("#files").click();
$("#upload64Btn").onclick=()=>$("#files64").click();
$("#files").addEventListener("change", function(){
  closeMenus();
  const files=[...this.files]; this.value="";
  if(files.length) uploadAll(files);
});

function folderLabel(rel){ return rel ? rel : ROOT_NAME; }

// Uploads go to the folder that was open when they started: browsing around
// while a big batch runs must not send the rest of it somewhere else.
function uploadAll(files, target){
  target = target == null ? state.path : target;
  uploadBusy = true;
  const totalBytes=files.reduce((n,f)=>n+f.size,0);
  const results=[], makeRuns=[], failed=[];
  let i=0, active=0, sent=0, done=0;
  openModal("Uploading to "+folderLabel(target),
    '<div class="up-name" id="upName"></div>'+
    '<div class="bar"><div class="bar-fill" id="barFill"></div></div>'+
    '<div class="up-count" id="upCount"></div>');
  const paint=()=>{
    const fill=$("#barFill"); if(fill) fill.style.width=(totalBytes?100*sent/totalBytes:100).toFixed(1)+"%";
    const cnt=$("#upCount"); if(cnt) cnt.textContent=done+" / "+files.length+" files · "+fmtSize(sent)+" of "+fmtSize(totalBytes);
    const nm=$("#upName"); if(nm) nm.textContent=files.length+" file"+(files.length===1?"":"s")+" → "+folderLabel(target);
  };
  paint();
  const finish=()=>{
    uploadBusy=false;
    closeModal(true);
    load(state.path);
    if(failed.length) toast("Failed: "+failed.join(", "), true);
    else toast("Uploaded "+files.length+" file"+(files.length===1?"":"s")+" to "+folderLabel(target));
    if(makeRuns.length) pollMakeJobs("after upload", makeRuns);
    offerExtract(results.filter(r=>r.name.toLowerCase().endsWith(".zip")));
  };
  const pump=()=>{
    while(active<UPLOAD_PARALLEL && i<files.length){
      const f=files[i++]; active++;
      uploadOne(f, bytes=>{ sent+=bytes; paint(); }, target)
        .then(r=>{ results.push(r); if(r.make) makeRuns.push({file:r.name, job:r.make.job}); })
        .catch(err=>{ failed.push(f.name); toast(f.name+": "+err.message, true); })
        .then(()=>{ active--; done++; paint(); pump(); });
    }
    if(active===0 && i>=files.length) finish();
  };
  pump();
}
function uploadOne(file, onBytes, target){
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open("POST","api/upload?path="+encodeURIComponent(target||"")+"&filename="+encodeURIComponent(file.name));
    xhr.responseType="text";
    if(!autoMakeEnabled) xhr.setRequestHeader("X-FM-Auto-Make","0");
    let last=0;
    if(onBytes && xhr.upload){
      // The server reports how many bytes landed; the bar wants increments so
      // several uploads can share one total.
      xhr.upload.onprogress=e=>{ if(e.lengthComputable){ onBytes(e.loaded-last); last=e.loaded; } };
    }
    xhr.onload=()=>{
      let d=null; try{ d=JSON.parse(xhr.responseText); }catch(e){}
      if(xhr.status>=200 && xhr.status<300) resolve(d||{});
      else reject(new Error((d&&d.error)||(xhr.status+" "+xhr.statusText)));
    };
    xhr.onerror=()=>reject(new Error("Network error"));
    xhr.send(file);
  });
}

// -- Drag and drop upload ----------------------------------------------------
// Only plain files are taken: a dropped folder would need the filesystem
// entry API to read its contents, which is a lot of machinery for a dropped
// tree, so it is reported instead.
let dragNesting=0;  // dragenter/leave fire per element, so count them
const isFileDrop = e => !!e.dataTransfer && [...e.dataTransfer.types].includes("Files");
document.addEventListener("dragenter", e=>{
  if(!FEATURES.write || !isFileDrop(e)) return;
  e.preventDefault(); dragNesting++; $("#drop").hidden=false;
});
document.addEventListener("dragover", e=>{
  if(!FEATURES.write || !isFileDrop(e)) return;
  e.preventDefault(); e.dataTransfer.dropEffect="copy";
});
document.addEventListener("dragleave", ()=>{
  if(dragNesting>0) dragNesting--;
  if(dragNesting===0) $("#drop").hidden=true;
});
document.addEventListener("drop", e=>{
  dragNesting=0; $("#drop").hidden=true;
  if(!FEATURES.write || !isFileDrop(e)) return;
  e.preventDefault();
  const items=[...(e.dataTransfer.items||[])];
  const dirs=items.filter(it=>{
    let entry=null;
    try{ entry = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; }catch(err){}
    return entry && entry.isDirectory;
  }).map(it=>it.name||"?");
  const files=[...(e.dataTransfer.files||[])].filter(f=>f.size>0 || f.type);
  if(dirs.length) toast("Ignored "+dirs.join(", ")+": drop individual files, or zip the folder first", true);
  if(files.length) uploadAll(files, state.path);
});
// -- Upload as base64 (workaround: encode in the browser, JSON over the wire)
$("#files64").addEventListener("change", function(){
  closeMenus();
  const files=[...this.files]; this.value="";
  if(files.length) uploadB64All(files, 0, [], state.path);
});
const B64_SLICE = 3 * 1024 * 1024;  // file bytes per read: a multiple of 3, so
                                    // each slice encodes without padding and
                                    // the pieces join into one payload
// Encoded in slices rather than in one FileReader pass, and sent as a Blob:
// a 1 GB file becomes a 1.4 GB base64 string, and JSON.stringify around it
// would want a second one on top of that (a browser refuses a string that
// long anyway). Each slice goes straight into a Blob part, which lets the
// string it came from go, and a Blob is uploaded straight from its parts —
// and the browser can report real byte progress while it goes up.
function readB64Parts(file){
  const parts=[];
  let at=0;
  const step=()=>{
    if(at>=file.size) return Promise.resolve(parts);
    return new Promise((resolve,reject)=>{
      const fr=new FileReader();
      fr.onload=()=>{ const s=String(fr.result); parts.push(new Blob([s.slice(s.indexOf(",")+1)])); resolve(); };
      fr.onerror=()=>reject(new Error("Could not read the file"));
      fr.readAsDataURL(file.slice(at, Math.min(at+B64_SLICE, file.size)));
    }).then(()=>{ at+=B64_SLICE; return step(); });
  };
  return step();
}

function uploadB64One(file, target, onProgress){
  return readB64Parts(file).then(parts=>{
    const body=new Blob(['{"data":"'].concat(parts,['"}']), {type:"application/json"});
    return new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest();
      xhr.open("POST","api/upload64?path="+encodeURIComponent(target||"")+"&filename="+encodeURIComponent(file.name));
      xhr.responseType="text";
      xhr.setRequestHeader("Content-Type","application/json");
      if(!autoMakeEnabled) xhr.setRequestHeader("X-FM-Auto-Make","0");
      if(onProgress && xhr.upload){
        xhr.upload.onprogress=e=>{ if(e.lengthComputable) onProgress(e.loaded); };
      }
      xhr.onload=()=>{
        let d=null; try{ d=JSON.parse(xhr.responseText); }catch(e){}
        if(xhr.status>=200 && xhr.status<300) resolve(d||{});
        else reject(new Error((d&&d.error)||(xhr.status+" "+xhr.statusText)));
      };
      xhr.onerror=()=>reject(new Error("Network error"));
      xhr.send(body);
    });
  });
}

function uploadB64All(files, i, makeRuns, target){
  if(!makeRuns) makeRuns=[];
  if(i>=files.length){
    uploadBusy=false;
    toast("Upload complete");
    closeModal(true); load(state.path);
    if(makeRuns.length) pollMakeJobs("after upload (base64)", makeRuns);
    return;
  }
  const f=files[i];
  uploadBusy = true;
  // Four characters per three bytes of file, plus the JSON around them.
  const total=Math.ceil(f.size/3)*4+12;
  openModal("Uploading to "+folderLabel(target)+" (base64)…",
    '<div class="up-name">'+esc(f.name)+'</div>'+
    '<div class="bar"><div class="bar-fill" id="barFill64"></div></div>'+
    '<div class="up-count" id="upCount64"></div>');
  const paint=(sent, phase)=>{
    const fill=$("#barFill64");
    if(fill) fill.style.width=(total?Math.min(100,100*sent/total):100).toFixed(1)+"%";
    const cnt=$("#upCount64");
    if(cnt) cnt.textContent=(i+1)+" / "+files.length+" · "+fmtSize(sent)+" of "+
      fmtSize(total)+(phase?" · "+phase:"");
  };
  paint(0, "encoding to base64…");   // reading the file comes first
  uploadB64One(f, target, sent=>paint(sent))
    .then(r=>{ if(r.make) makeRuns.push({file:r.name, job:r.make.job}); uploadB64All(files, i+1, makeRuns, target); })
    .catch(err=>{ toast(f.name+": "+err.message, true); setTimeout(()=>uploadB64All(files, i+1, makeRuns, target), 500); });
}

function offerExtract(zips){
  if(!zips.length) return;
  const names=zips.map(z=>'"'+z.name+'"').join(", ");
  if(!confirm("Extract "+names+" in the current folder?")) return;
  zips.forEach(z=>{
    api("api/extract?path="+encodeURIComponent(z.path),
        { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({}) })
      .then(()=>{ toast("Extracted "+z.name); load(state.path); })
      .catch(err=>toast(z.name+": "+err.message, true));
  });
}

// -- Theme: auto-detect (prefers-color-scheme) + manual toggle --------------
const THEME_KEY = "fm-theme";
const themeMq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
let starsBuilt = false;

function storedTheme(){
  try{
    const v = localStorage.getItem(THEME_KEY);
    return (v === "light" || v === "dark") ? v : null;
  }catch(e){ return null; }
}
function systemTheme(){ return themeMq && themeMq.matches ? "dark" : "light"; }
function currentTheme(){ return storedTheme() || systemTheme(); }

// Stars are generated once, as absolutely-positioned dots spread over the
// viewport. Percent coordinates mean no rebuild is needed on resize.
function buildStars(){
  if(starsBuilt) return;
  const box = $("#stars");
  if(!box) return;
  starsBuilt = true;
  const frag = document.createDocumentFragment();
  const n = window.innerWidth < 700 ? 90 : 180;
  for(let i=0;i<n;i++){
    const s = document.createElement("span");
    const big = Math.random() < 0.05;
    s.className = "star" + (big ? " big" : "");
    const size = big ? 2 + Math.random()*1.6 : 0.6 + Math.random()*1.4;
    s.style.left = (Math.random()*100).toFixed(2) + "%";
    s.style.top = (Math.random()*100).toFixed(2) + "%";
    s.style.width = s.style.height = size.toFixed(2) + "px";
    // Static brightness varies per star so the field doesn't look uniform.
    s.style.opacity = (0.35 + Math.random()*0.65).toFixed(2);
    frag.appendChild(s);
  }
  box.appendChild(frag);
}

// City lights on the Earth's night side: clustered warm dots, built once.
let earthLightsBuilt = false;
function buildEarthLights(){
  if(earthLightsBuilt) return;
  const box = document.querySelector("#space .earth .lights");
  if(!box) return;
  earthLightsBuilt = true;
  const frag = document.createDocumentFragment();
  const clusters = [[14,26],[27,58],[38,38],[52,74],[64,46],[79,66]];
  for(const [cx, cy] of clusters){
    const n = 16 + Math.floor(Math.random()*12);
    for(let i=0;i<n;i++){
      const s = document.createElement("i");
      const x = Math.max(1, Math.min(99, cx + (Math.random()-0.5)*15));
      const y = Math.max(2, Math.min(96, cy + (Math.random()-0.5)*30));
      const size = 0.7 + Math.random()*1.5;
      const night = x < 58 ? 1 : 0.45;
      s.style.left = x.toFixed(2) + "%";
      s.style.top = y.toFixed(2) + "%";
      s.style.width = s.style.height = size.toFixed(2) + "px";
      s.style.opacity = ((0.25 + Math.random()*0.55) * night).toFixed(2);
      frag.appendChild(s);
    }
  }
  box.appendChild(frag);
}

function applyTheme(){
  const theme = currentTheme();
  document.documentElement.setAttribute("data-theme", theme);
  if(theme === "dark"){ buildStars(); buildEarthLights(); }
  const btn = $("#themeBtn");
  if(btn){
    btn.textContent = theme === "dark" ? "☾" : "☼";
    const label = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
    btn.title = label + (storedTheme() ? "" : " (auto; Shift-click to reset)");
    btn.setAttribute("aria-label", label);
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if(meta) meta.content = theme === "dark" ? "#03071a" : "#ffffff";
}

$("#themeBtn").onclick = ev => {
  if(ev.shiftKey){
    try{ localStorage.removeItem(THEME_KEY); }catch(e){}
    applyTheme();
    toast("Theme follows your system setting again");
    return;
  }
  const next = currentTheme() === "dark" ? "light" : "dark";
  try{ localStorage.setItem(THEME_KEY, next); }catch(e){}
  applyTheme();
};

if(themeMq){
  const onSystemChange = () => { if(!storedTheme()) applyTheme(); };
  if(themeMq.addEventListener) themeMq.addEventListener("change", onSystemChange);
  else if(themeMq.addListener) themeMq.addListener(onSystemChange);
}
window.addEventListener("storage", e => { if(e.key === THEME_KEY) applyTheme(); });

// -- Auto-make toggle: skip `make` after edits/uploads/deletes -------------
// The toggle lives in the Tools menu now, so its state would be out of sight
// when the menu is closed: the dot on the Tools button is what says "rebuilds
// are not happening" without opening anything.
function applyMakeState(){
  const btn = $("#makeBtn"), label = $("#makeLabel"), dot = $("#toolsDot");
  if(!btn) return;
  btn.setAttribute("aria-checked", autoMakeEnabled ? "true" : "false");
  if(label) label.textContent = autoMakeEnabled ? "on" : "off";
  btn.title = autoMakeEnabled
    ? "make runs automatically after edits, uploads and deletes (choose to disable it)"
    : "Automatic make is off (choose to enable it)";
  if(dot) dot.hidden = autoMakeEnabled || !FEATURES.make || !FEATURES.write;
}
$("#makeBtn").onclick = () => {
  autoMakeEnabled = !autoMakeEnabled;
  try{ localStorage.setItem(MAKE_KEY, autoMakeEnabled ? "1" : "0"); }catch(e){}
  applyMakeState();
  toast(autoMakeEnabled ? "Automatic make enabled" : "Automatic make disabled");
};
window.addEventListener("storage", e => {
  if(e.key === MAKE_KEY){ autoMakeEnabled = storedAutoMake(); applyMakeState(); }
});

// -- Help -------------------------------------------------------------------
// The documentation lives in the page it describes: nothing to keep in sync
// with a README, and the parts the server was started without are left out
// instead of being explained and then missing.
function helpHTML(){
  const url = location.origin + DAV_MOUNT;
  const rows = [];
  const add = (title, body) => rows.push('<h3>'+title+'</h3>'+body);

  add("Browse", `
    <ul>
      <li>Click a folder to open it, a file to open it here. The path lives in the
        URL, so the back button, a reload and a pasted link all land on the same
        folder.</li>
      <li><b>Filter</b> narrows the listing as you type (Esc clears it). Every column
        header sorts; folders always stay together, and dates sort newest first.</li>
      <li>Folder sizes are counted on demand and cached for a moment, so a big tree
        does not slow the listing down. A symlink is shown as the entry it is:
        renaming, moving and deleting act on the link, not on what it points at.</li>
      <li>Tick the boxes on the left (or the box in the header) to act on a whole
        set of entries at once.</li>
    </ul>`);

  add("Create, upload, download", `
    <ul>
      <li>The header keeps the two everyday actions in plain sight — <b>+ New</b>
        (a folder or an empty text file) and <b>Upload</b> — and everything else is
        under <b>⚙ Tools</b>. Files can also be dropped anywhere onto the page.
        A folder can be dropped only on the WebDAV mount: a browser cannot send a
        whole tree in one request.</li>
      <li><b>Tools ▸ Download folder as zip</b> zips the folder you are looking at,
        recursively. A .zip file has an <b>Extract</b> action, which unpacks it next
        to itself or into a folder you pick.</li>
      <li><b>Download</b> is a plain streaming download that can be resumed.
        ${FEATURES.b64 ? `<b>🔣</b> transfers the same file as base64 inside JSON, which
        survives a proxy or VPN that mangles binary downloads; the reverse of it,
        <b>Tools ▸ Upload as base64</b>, uploads a file the same way.`
        : "Base64 transfer is disabled on this server."}</li>
    </ul>`);

  add("Editing text", `
    <ul>
      <li>A text file opens in the editor. <kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>S</kbd>
        saves; a dot in the title bar means there are unsaved changes, and closing or
        navigating away asks before throwing them away.</li>
      <li>The line ending is chosen per save, because a browser textarea always sends
        plain <code>\\n</code>.</li>
      <li>Saving refuses to overwrite a file that changed on the server since you
        opened it, and files arrive complete or not at all: an upload that dies
        half way leaves nothing behind.</li>
    </ul>`);

  if(FEATURES.make) add("Make", `
    <ul>
      <li>When the folder you are working in has a Makefile, saving, uploading or
        deleting in it starts <code>make</code> there in the background, and the
        result appears when the build finishes.</li>
      <li><b>Tools ▸ Auto-make after edits</b> turns that off (kept in this browser,
        not on the server); while it is off the <b>Tools</b> button carries a red dot
        so it is not forgotten. <b>Tools ▸ Run make here</b> runs it right now and
        waits for the output.</li>
    </ul>`);

  if(FEATURES.shell || FEATURES.run) add("Running things", `
    <ul>
      ${FEATURES.run ? "<li>An executable file has a <b>▶</b> action, which runs it and shows its output.</li>" : ""}
      ${FEATURES.shell ? "<li><b>Tools ▸ Shell</b> runs a command in a box on the server and shows you the output. It is the same box the files are in, so <code>ls</code> sees exactly this folder.</li>" : ""}
      <li>Everything the server runs is capped in time and in output size, so a
        command that never ends cannot wedge it.</li>
    </ul>`);

  add("Actions in each row", `
    <div class="legend">${Object.entries(ACT).map(([id, [icon, title]]) =>
      '<span><span class="icon">'+icon+'</span>'+esc(title)+'</span>').join("")}</div>
    <p class="muted">Disabled features simply do not appear, and in read-only mode
      everything that changes something is gone.</p>`);

  add("Keys", `
    <div class="keys">
      <span><kbd>?</kbd> this help</span>
      <span><kbd>/</kbd> filter box</span>
      <span><kbd>n</kbd> the + New menu</span>
      <span><kbd>u</kbd> upload</span>
      <span><kbd>↑</kbd><kbd>↓</kbd> inside an open menu</span>
      <span><kbd>Esc</kbd> close, clear</span>
      <span><kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>S</kbd> save</span>
      <span><kbd>Enter</kbd> accept a prompt</span>
    </div>
    <p class="muted">The theme button switches between the daylight and night themes;
      Shift-click it to follow your system setting again.</p>`);

  if(FEATURES.dav) add("Mount this folder with WebDAV", `
    <p>Every file and folder here is also a WebDAV share, at</p>
    <pre><code>${esc(url)}</code></pre>
    <p>so it can be mounted and used with ordinary programs. The client this is built
      for is <b>davfs2</b> on Linux.</p>
    <p>Mount it once:</p>
    <pre><code>sudo mkdir -p /mnt/this
sudo mount -t davfs ${esc(url.replace(/\/$/, ""))} /mnt/this</code></pre>
    <p>Or keep it in <code>/etc/fstab</code> so your own user can mount and unmount it
      without a password (<code>noauto</code> means it is not mounted at boot):</p>
    <pre><code>${esc(url.replace(/\/$/, ""))} /mnt/this davfs noauto,user,_netdev 0 0</code></pre>
    <pre><code>mount /mnt/this
umount /mnt/this     # or: fusermount -u /mnt/this</code></pre>
    <ul>
      <li>Allow your user to mount WebDAV once: <code>sudo usermod -aG davfs2 $USER</code>,
        then log out and in again. Distributions differ here — some rely on
        <code>/usr/sbin/mount.davfs</code> being setuid root, which lets
        <code>sudo mount -t davfs -o uid=$(id -u),gid=$(id -g) …</code> mount it.</li>
      <li><b>davfs2 asks for credentials anyway.</b> This server never asks who you
        are, but <code>mount.davfs</code> still prompts for a username and password
        the first time it sees this URL: press Return twice, which is what an empty
        answer means here. It reads <code>~/.davfs2/secrets</code> before asking, so
        one line <code>${esc(url.replace(/\/$/, ""))} guest guest</code> in that file
        (mode 600) keeps it quiet — and note that a mount done with <code>sudo</code>
        reads <em>root's</em> copy of it. A proxy with authentication in front of this
        server wants its credentials in the same place rather than in fstab.</li>
      <li>If you reach this page through a proxy under a sub-path, mount that same
        URL with <code>/dav</code> on the end — the address above is already built
        from the address you are looking at.</li>
      <li><code>--read-only</code> makes the mount refuse writes. To mount it read-only
        from the client side anyway, use <code>-o ro</code>.</li>
    </ul>`);

  if(FEATURES.dav) add("Mounting it: how it behaves", `
    <ul>
      <li><b>Writes are late by design.</b> davfs2 keeps a changed file locally and
        re-uploads the whole thing about ten seconds after the program closes it, so
        the browser may show the old version for a moment. Put
        <code>delay_upload 0</code> in <code>~/.davfs2/davfs2.conf</code> if you want
        writes to go over at once.</li>
      <li><b>Listings are cached too</b> (a minute by default), so a file the browser
        just created may appear on the mount a little later. <code>dir_refresh 0</code>
        turns that off if you would rather have it live.</li>
      <li><b>A file open on the mount is locked</b>, and the browser will be told it
        is busy instead of quietly overwriting it — the row says
        <span class="badge-lock">🔒 locked</span> and has a 🔓 action to break the
        lock if whoever has it open walked away. The other way round too: a file the
        browser holds a lock on is refused on the mount, where that looks like
        <i>Permission denied</i> on a write and an I/O error on a rename. Only
        exclusive write locks exist here, and a lock that is never refreshed dies
        after ${LIMITS.lockTtl/60} minutes, so a crashed program cannot block a file
        forever. Reading is never refused, from either side.</li>
      <li><b>A lost update is refused, not won.</b> Every file carries a strong ETag
        and the server checks <code>If-Match</code>/<code>If-None-Match</code>, so an
        upload of a stale version comes back as "the file changed since you read it"
        rather than replacing what is there now.</li>
      <li><b>Edit one file from both sides</b> and davfs2 gets the last word: it is
        told its upload was stale, re-reads the file, then sends its own version and
        leaves the copy it started from in <code>lost+found</code> on the mount — so
        the browser's edit is overwritten, but nothing is thrown away. Breaking a
        lock with 🔓 and then saving is what invites this. Here the guard is the
        same in the other direction: saving a file that changed on disk since it was
        opened is refused and asks you to reload it.</li>
      <li><b>The executable bit does cross over.</b> A script made executable on the
        mount can be run from the browser, and one made executable here is runnable
        there. WebDAV has exactly one yes-or-no for that, so <code>chmod 700</code>
        and <code>chmod 755</code> look the same on the other side.</li>
      <li><b>Not carried over WebDAV:</b> symbolic links (davfs2 cannot express
        them, so a link looks like an ordinary file whose contents are the name it
        points at, and a link to a folder looks like the folder), owner and group,
        empty folders on a read-only server, and partial writes into the middle of a
        file (the whole file is always re-uploaded).</li>
      <li>That listing and those file modes are what davfs2 <em>cached</em>, and its
        cache survives an unmount (<code>~/.cache/davfs2</code>, or
        <code>/var/cache/davfs2</code> when mounted as root). After changing files
        outside the mount, it is the mount that is a minute behind — or delete that
        cache directory.</li>
      <li><code>lost+found</code> — and on some versions <code>.DAVFS</code> — exists
        only inside the mount: davfs2's own bookkeeping, and its place for writes that
        failed while the connection was down. This server never hears about them, so
        the shared folder stays clean and this listing never shows them.</li>
      <li>Edits and uploads that arrive through the mount start <code>make</code> in
        that folder exactly as a browser edit does, only with nobody waiting for the
        result.</li>
      <li>A folder's WebDAV listing stops at ${LIMITS.davEntries} entries (the browser
        listing stops at ${LIMITS.list}); a folder bigger than that is fine to use,
        just not fully listed in one request.</li>
    </ul>`);

  add("Limits on this server", `
    <ul>
      <li>Files up to ${fmtSize(LIMITS.transfer)} go through on every path: upload,
        download, zip download, WebDAV and the base64 workaround. All of them are
        streamed, so a big file costs the server a chunk of memory rather than the
        file itself; the limit that bites next is usually free disk.</li>
      <li>The text editor loads files up to ${fmtSize(LIMITS.text)}; bigger ones are
        download-only.</li>
      <li>A JSON request body is capped at ${fmtSize(LIMITS.body)} and a base64 transfer
        at ${fmtSize(LIMITS.b64)} — that upload arrives as a JSON body of up to
        ${fmtSize(LIMITS.b64Body)}, because base64 is a third larger than the file.
        Extracting a zip stops at ${fmtSize(LIMITS.zipEntry)} per entry and
        ${fmtSize(LIMITS.zipTotal)} in total.</li>
      <li>A command returns after ${LIMITS.runTimeout} seconds by default and never
        later than ${LIMITS.runMax}.</li>
      <li>An idle connection is closed after ${LIMITS.socket} seconds, which is what
        a WebDAV client expects when a proxy keeps a socket open.</li>
      <li>Behind a reverse proxy its own request-size limit applies first and tends to
        bite far below these numbers: nginx refuses a body larger than 1 MB unless
        <code>client_max_body_size</code> says otherwise.</li>
    </ul>`);

  add("Keeping things in place", `
    <p>Nothing is installed and nothing is configured: the files themselves are the
    only state. What is kept in memory — thumbnails, folder sizes, finished make
    runs, WebDAV locks — expires on its own and is gone after a restart. There is no
    login here: whoever can reach this URL can read and (unless the server runs with
    <code>--read-only</code>) change everything in it, so put it behind a proxy that
    authenticates before pointing it at anything you care about.</p>`);

  return '<div class="help">'+rows.join("")+"</div>";
}

function showHelp(){
  openModal("Help — " + ROOT_NAME, helpHTML());
}
$("#helpBtn").onclick = () => showHelp();

document.addEventListener("keydown", ev => {
  // ?, /, n and u as shortcuts, except while typing somewhere.
  const t = ev.target;
  if(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT"
           || t.isContentEditable)) return;
  if(ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if(ev.key === "?"){ ev.preventDefault(); showHelp(); }
  else if(ev.key === "/"){ ev.preventDefault(); const f=$("#filter"); if(f) f.focus(); }
  // The header only keeps the two actions used most, so the rest stay
  // reachable from the keyboard: n opens the menu they are in, u uploads.
  else if(ev.key === "n"){ ev.preventDefault(); openMenu("#newBtn"); }
  else if(ev.key === "u"){
    ev.preventDefault();
    if(FEATURES.write) $("#files").click();
  }
});

// -- Startup feature flags (--no-shell / --no-run / --no-make / --no-b64 /
// --read-only) --------------------------------------------------------------
// The server enforces these too (the endpoints refuse with 403); this only
// hides the corresponding UI so disabled features don't even appear.
function applyFeatures(){
  const hide = (sel, on) =>
    document.querySelectorAll(sel).forEach(el => { if(!on) el.style.display="none"; });
  // A menu group hides whole, label and separator included, so a folder listing
  // never shows a heading with nothing under it.
  const makeOn = FEATURES.make && FEATURES.write;
  const runGroup = makeOn || (FEATURES.shell && FEATURES.write);
  hide("#makeBtn", makeOn);
  hide("#makeRunBtn", makeOn);
  hide("#shellBtn", FEATURES.shell && FEATURES.write);
  hide('[data-grp="run"]', runGroup);
  hide("#upload64Btn", FEATURES.b64);
  hide("#uploadBtn", FEATURES.write);
  hide("#newWrap", FEATURES.write);  // the "+ New" menu creates things
  const ro=$("#roBadge");
  if(ro) ro.hidden = !!FEATURES.write;
  if(!FEATURES.write) document.querySelectorAll(".c-sel").forEach(el=>el.style.display="none");
}
$("#filter").addEventListener("input", ev=>{ state.filter=ev.target.value; render(); });
$("#filter").addEventListener("keydown", ev=>{ if(ev.key==="Escape"){ ev.target.value=""; state.filter=""; render(); } });

applyFeatures();
applyMakeState();
applyTheme();
load(pathFromHash());
updateSortIndicators();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    global ROOT, ENABLE_SHELL, ENABLE_RUN, ENABLE_MAKE, ENABLE_B64, ENABLE_WRITE
    global SHELL_CMD, MAKE_CMD, ENABLE_DAV, DAV_PREFIX
    # This file is run by whatever Python a user has on PATH, not by a project
    # with a pinned requires-python, so the floor really is checked here.
    if sys.version_info < (3, 10):  # noqa: UP036
        sys.exit(
            "filemanager.py needs Python 3.10 or newer "
            f"(this is {sys.version.split()[0]})"
        )
    ap = argparse.ArgumentParser(
        description="Single-file web file manager (stdlib + optional Pillow)."
    )
    ap.add_argument(
        "root",
        nargs="?",
        default=None,
        help="directory to serve (default: current working directory)",
    )
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1; use 0.0.0.0 for a remote reverse proxy)",
    )
    ap.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    ap.add_argument(
        "--read-only",
        action="store_true",
        help="refuse every change: no creating, editing, uploading, moving, "
        "deleting, extracting or running (browsing, viewing and downloading "
        "still work)",
    )
    ap.add_argument(
        "--no-shell",
        action="store_true",
        help="disable shell access (Tools ▸ Shell, /api/shell)",
    )
    ap.add_argument(
        "--no-run",
        action="store_true",
        help="disable execution of executable files (Run button, /api/run)",
    )
    ap.add_argument(
        "--no-make",
        action="store_true",
        help="disable make entirely: no automatic rebuilds and no manual make "
        "(the Tools menu)",
    )
    ap.add_argument(
        "--no-b64",
        action="store_true",
        help="disable the base64 upload/download workaround (/api/upload64, /api/download64)",
    )
    ap.add_argument(
        "--no-dav",
        action="store_true",
        help="disable the WebDAV share (mounted with davfs2; see the Help button)",
    )
    ap.add_argument(
        "--dav-path",
        default=DAV_PREFIX,
        metavar="PREFIX",
        help=f"first URL segment of the WebDAV share, one path component "
        f"(default: {DAV_PREFIX})",
    )
    ap.add_argument(
        "--shell-cmd",
        default=SHELL_CMD,
        metavar="PROGRAM",
        help=f"shell used by the Shell action (default: {SHELL_CMD})",
    )
    ap.add_argument(
        "--make-cmd",
        default=MAKE_CMD,
        metavar="PROGRAM",
        help=f"command used for make (default: {MAKE_CMD}; e.g. gmake, ninja)",
    )
    ap.add_argument(
        "--version",
        action="version",
        version=f"filemanager.py {VERSION}",
    )
    args = ap.parse_args(argv)

    root = Path(args.root or os.getcwd()).expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a directory: {root}")
    # The mount lives at one URL segment, so it cannot contain a slash or be
    # one of the names the page and the JSON API already answer to; a prefix
    # like "api" would silently swallow every endpoint under /api.
    prefix = args.dav_path.strip().strip("/")
    if not prefix or "/" in prefix or prefix in (".", ".."):
        ap.error(f"--dav-path must be one path segment, not {args.dav_path!r}")
    if prefix in ("api", "health"):
        ap.error(
            f"--dav-path cannot be {prefix!r}: that URL already belongs to the API"
        )
    ROOT = root
    ENABLE_SHELL = not args.no_shell
    ENABLE_RUN = not args.no_run
    ENABLE_MAKE = not args.no_make
    ENABLE_B64 = not args.no_b64
    ENABLE_WRITE = not args.read_only
    ENABLE_DAV = not args.no_dav
    DAV_PREFIX = prefix
    SHELL_CMD = args.shell_cmd
    MAKE_CMD = args.make_cmd
    mimetypes.init()

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        ap.error(f"could not bind {args.host}:{args.port} ({e})")
    server.daemon_threads = True  # don't block shutdown on stuck request threads

    modes = ["read-only" if not ENABLE_WRITE else "read-write"]
    if ENABLE_SHELL:
        modes.append(f"shell={SHELL_CMD}")
    if ENABLE_MAKE:
        modes.append(f"make={MAKE_CMD}")
    modes.append("thumbnails=on" if pillow_available() else "thumbnails=off (Pillow)")
    print(f"Serving: {ROOT}")
    print(f"URL:     http://{args.host}:{args.port}/")
    print("Mode:    " + ", ".join(modes))
    if ENABLE_DAV:
        # The davfs2 line is printed because that is the client this mount is
        # built for, and the details that matter (no auth, one path segment)
        # are the ones a user otherwise gets wrong.
        print(f"WebDAV:  http://{args.host}:{args.port}/{DAV_PREFIX}/")
        print(
            f"         mount as: mount -t davfs "
            f"http://{args.host}:{args.port}/{DAV_PREFIX}/ /mnt "
            f"(or add it to /etc/fstab; see the Help button)"
        )
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
