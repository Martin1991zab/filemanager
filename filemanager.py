#!/usr/bin/env python3
"""
filemanager.py — a single-file web file manager.

Browse, create/delete/rename/move folders & files, open folders,
upload/download files (zip archives can be extracted in the browser,
directories can be downloaded as zip, and files can be transferred as
base64 — a JSON workaround for flaky networks),
view & edit images and plain-text files, run
executable files, and run arbitrary bash commands, all from a
dependency-free HTML/JS/CSS interface. Folders show their recursive
size; the listing shows created and last-updated times and can be sorted
by name, size, or either timestamp; images get Pillow-generated
thumbnails in the listing. If a Makefile is present in a directory,
`make` is run there automatically in the background after files are
edited, uploaded or deleted (a popup shows the result once the build
finishes); the "Auto-make" toggle in the header turns that off (the
choice is kept in the browser's localStorage, so the server stays
stateless). The "Make" button runs `make` in the current folder on
demand and waits for the result. Shell access, executable execution, make
(both the automatic rebuilds and the manual Make button) and the base64
upload/download workaround can each be disabled entirely at startup with
the --no-shell, --no-run, --no-make and --no-b64 flags.

Everything is stateless: it serves the directory the app was started in
(or a path passed as the first argument) and stores no config, database,
or metadata. Authentication is expected to be handled by an external
reverse proxy (there is none here).

Run:
    python filemanager.py [directory] [--host HOST] [--port PORT]
                            [--no-shell] [--no-run] [--no-make] [--no-b64]

Uses only the Python standard library, plus (optionally) Pillow for image
thumbnails. If Pillow is missing, the app still works — it just shows a
generic icon instead of a thumbnail.
"""

import argparse
import base64
import binascii
import io
import json
import mimetypes
import os
import selectors
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, TypedDict
from urllib.parse import parse_qs, quote, unquote, urlparse
import zipfile
import zlib

# ---------------------------------------------------------------------------
# Limits / tuning (edit to taste)
# ---------------------------------------------------------------------------
THUMB_SIZE = (120, 120)  # max thumbnail dimension (px)
MAX_RUN_OUTPUT = 1024 * 1024  # 1 MB cap on captured output per run
RUN_TIMEOUT = 60  # default run timeout (seconds)
RUN_MAX_TIMEOUT = 3600  # hard cap on run timeout (seconds)
MAX_TEXT_SIZE = 20 * 1024 * 1024  # /api/text refuses to load files above this
MAX_B64_FILE_SIZE = 128 * 1024 * 1024  # /api/download64 file-size limit
MAX_B64_BODY_SIZE = 128 * 1024 * 1024  # /api/upload64 request-body limit
THUMB_CACHE_MAX_ENTRIES = 512  # rendered thumbnails kept in memory
THUMB_CACHE_MAX_BYTES = 64 * 1024 * 1024
SHELL_CMD = "bash"  # shell used for the Shell button
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
ENABLE_SHELL: bool = True  # Shell button / /api/shell
ENABLE_RUN: bool = True  # run executables / /api/run
ENABLE_MAKE: bool = True  # automatic rebuilds + manual Make button
ENABLE_B64: bool = True  # base64 upload/download (/api/upload64, /api/download64)

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
    """Resolve a client-supplied relative path, forcing it to stay under ROOT."""
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    p = (ROOT / rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise HTTPError(403, "Path is outside the root directory")
    return p


def rel_of(p: Path) -> str:
    return "" if p == ROOT else p.relative_to(ROOT).as_posix()


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

    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
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
    blocking make per file.
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
                again = bool(self._queues.get(d))
            if again:
                self._build(d)

        threading.Thread(target=work, daemon=True, name="fm-make").start()

    def get(self, job: str) -> tuple[bool, RunResult | None] | None:
        """(done, result) for a job, or None if unknown or expired."""
        with self._lock:
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


def validate_new_name(name: str) -> None:
    """Reject names that would break the filesystem or escape the directory."""
    name = name.strip()
    if not name:
        raise HTTPError(400, "Name cannot be empty")
    if name in (".", ".."):
        raise HTTPError(400, "Invalid name")
    if "/" in name or "\\" in name or "\x00" in name:
        raise HTTPError(400, "Name cannot contain path separators")


def dir_size(p: Path) -> int:
    """Recursively sum the size of all regular files under p (bytes).

    Symlinks (to files or directories) are not counted or followed.
    Unreadable entries are skipped. One stat per directory entry.
    """
    total = 0
    stack = [p]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for de in it:
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                if stat.S_ISDIR(mode):
                    stack.append(de.path)
                elif stat.S_ISREG(mode):
                    total += st.st_size
    return total


def extract_zip(zip_path: Path, dest_dir: Path) -> dict[str, Any]:
    """Extract zip_path into dest_dir, refusing entries that would escape
    it (zip-slip) or overwrite the archive itself. Returns a summary."""
    dest_dir = dest_dir.resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise HTTPError(400, "Not a valid zip file")
    with zf:
        entries = 0
        total = 0
        for info in zf.infolist():
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
                shutil.copyfileobj(src, out)
            # Restore the stored unix permissions (e.g. exec bits).
            attr = info.external_attr >> 16
            if attr:
                try:
                    os.chmod(target, attr & 0o7777)
                except OSError:
                    pass
            entries += 1
            total += info.file_size
    return {"entries": entries, "bytes": total}


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
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FileManager/1.0"
    protocol_version = "HTTP/1.1"

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
        lines, and the value is capped to keep lines readable.
        """
        stamp = self.log_date_time_string()
        sys.stderr.write(
            f"{self.address_string()} - - [{stamp}] "
            f'"{self.requestline}" {code} {size} ua="{self._client_ua()}"\n'
        )

    # -- low-level helpers -------------------------------------------------
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

    def send_error_json(self, code: int, msg: str) -> None:
        try:
            body = json.dumps({"error": msg}).encode("utf-8")
            self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.close_connection = True

    def send_html(self) -> None:
        root_name = ROOT.name or str(ROOT)
        features = json.dumps(
            {
                "shell": ENABLE_SHELL,
                "run": ENABLE_RUN,
                "make": ENABLE_MAKE,
                "b64": ENABLE_B64,
            }
        )
        data = (
            HTML_TEMPLATE.replace(
                "__ROOT_NAME__", json.dumps(root_name, ensure_ascii=False)
            )
            .replace("__FEATURES__", features)
            .encode("utf-8")
        )
        self._send(
            200,
            data,
            "text/html; charset=utf-8",
            {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    def stream_file(self, p: Path, attachment: bool) -> None:
        ctype, _ = mimetypes.guess_type(p.name)
        ctype = ctype or "application/octet-stream"
        mode = "attachment" if attachment else "inline"
        fname = quote(os.path.basename(p.name))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Content-Disposition", f'{mode}; filename="{fname}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with p.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n > 0 else b""

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

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            path = parsed.path
            if path == "/":
                self.send_html()
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
                self.send_error_json(404, "Not found")
        except HTTPError as e:
            self.send_error_json(e.code, e.msg)
        except Exception as e:  # noqa: BLE001
            self.send_error_json(500, f"Server error: {e}")

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            path = parsed.path
            if path == "/api/mkdir":
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
                self.send_error_json(404, "Not found")
        except HTTPError as e:
            self.send_error_json(e.code, e.msg)
        except Exception as e:  # noqa: BLE001
            self.send_error_json(500, f"Server error: {e}")

    # -- endpoints ---------------------------------------------------------
    def ep_list(self, qs: Qs) -> None:
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_dir():
            raise HTTPError(404, "Directory not found")
        prefix = rel_of(p)
        entries = []
        try:
            it = os.scandir(p)
        except OSError:
            raise HTTPError(404, "Directory not found")
        with it:
            for de in it:
                try:
                    st = de.stat()  # follow symlinks (as before)
                except OSError:
                    try:  # broken symlink: still show it
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                is_dir = stat.S_ISDIR(st.st_mode)
                is_reg = stat.S_ISREG(st.st_mode)
                ext = os.path.splitext(de.name)[1].lower()
                entries.append(
                    {
                        "name": de.name,
                        "path": (prefix + "/" + de.name) if prefix else de.name,
                        "kind": "dir" if is_dir else classify(de.name),
                        "size": st.st_size if is_reg else None,
                        "ctime": created_time(st),
                        "mtime": st.st_mtime,
                        "thumb": is_reg and ext in THUMB_EXTS,
                        "run": ENABLE_RUN and is_reg and is_executable(de.path, st),
                    }
                )
        entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
        parent = None
        if p != ROOT:
            parent = "" if p.parent == ROOT else rel_of(p.parent)
        self.send_json({"path": rel_of(p), "parent": parent, "entries": entries})

    def ep_mkdir(self, qs: Qs) -> None:
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, "Cannot modify the root directory")
        if p.exists():
            raise HTTPError(409, "A file or folder with that name already exists")
        if not p.parent.is_dir():
            raise HTTPError(404, "Parent folder not found")
        p.mkdir()
        self.send_json({"ok": True, "path": rel_of(p)})

    def ep_newfile(self, qs: Qs) -> None:
        """Create an empty file (used by the 'New text file' menu item)."""
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, "Cannot create a file at the root path")
        if p.exists():
            raise HTTPError(409, "A file or folder with that name already exists")
        if not p.parent.is_dir():
            raise HTTPError(404, "Parent folder not found")
        p.write_bytes(b"")
        self.send_json({"ok": True, "path": rel_of(p)})

    def ep_delete(self, qs: Qs) -> None:
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, "Cannot delete the root directory")
        if not p.exists():
            raise HTTPError(404, "Not found")
        parent = p.parent
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        make_result = self.maybe_run_make(parent)
        resp = {"ok": True}
        if make_result is not None:
            resp["make"] = make_result
        self.send_json(resp)

    def ep_rename(self, qs: Qs) -> None:
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, "Cannot rename the root directory")
        if not p.exists():
            raise HTTPError(404, "Not found")
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
        new_name = payload.get("name", "")
        validate_new_name(new_name)
        dest = p.parent / new_name
        if dest == p:
            raise HTTPError(400, "New name is the same as the current name")
        if dest.exists():
            raise HTTPError(409, "A file or folder with that name already exists here")
        p.rename(dest)
        self.send_json({"ok": True, "path": rel_of(dest), "name": new_name})

    def ep_move(self, qs: Qs) -> None:
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, "Cannot move the root directory")
        if not p.exists():
            raise HTTPError(404, "Not found")
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
        dest_dir_rel = payload.get("dest", "")
        dest_dir = safe_path(dest_dir_rel)
        if not dest_dir.is_dir():
            raise HTTPError(404, "Destination folder not found")
        # Can't move a directory into itself or a subdirectory of itself
        if p.is_dir() and (dest_dir == p or p in dest_dir.parents):
            raise HTTPError(400, "Cannot move a folder into itself or its subfolder")
        dest = dest_dir / p.name
        if dest.exists():
            raise HTTPError(
                409, "A file or folder with that name already exists in the destination"
            )
        shutil.move(str(p), str(dest))
        self.send_json({"ok": True, "path": rel_of(dest), "name": p.name})

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
        """Compute recursive sizes for a list of directories in one request."""
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
        paths = payload.get("paths", [])
        if not isinstance(paths, list) or len(paths) > 1000:
            raise HTTPError(400, "Invalid 'paths' payload")
        sizes = {}
        for rel in paths:
            if not isinstance(rel, str):
                continue
            p = safe_path(rel)
            if p.is_dir():
                sizes[rel_of(p)] = dir_size(p)
        self.send_json({"sizes": sizes})

    def ep_text(self, qs: Qs) -> None:
        p = self._file(qs)
        size = p.stat().st_size
        if size > MAX_TEXT_SIZE:
            raise HTTPError(
                413,
                f"File is too large to edit in the browser "
                f"(limit {MAX_TEXT_SIZE // (1024 * 1024)} MB) — download it instead",
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
            }
        )

    def ep_save(self, qs: Qs) -> None:
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT or p.is_dir():
            raise HTTPError(400, "Target is not a file")
        body = self._read_body()
        if not body:
            raise HTTPError(400, "Empty request body")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
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
        p.write_bytes(data)
        make_result = self.maybe_run_make(p.parent)
        resp = {"ok": True, "size": len(data)}
        if make_result is not None:
            resp["make"] = make_result
        self.send_json(resp)

    def ep_upload(self, qs: Qs) -> None:
        dest_dir = safe_path(qs.get("path", [""])[0])
        if not dest_dir.is_dir():
            raise HTTPError(404, "Target folder not found")
        name = os.path.basename(qs.get("filename", [""])[0] or "")
        if not name or name in (".", ".."):
            raise HTTPError(400, "Invalid filename")
        length = int(self.headers.get("Content-Length", 0) or 0)
        dest = unique_dest(dest_dir, name)
        size = 0
        try:
            with dest.open("wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    size += len(chunk)
                    remaining -= len(chunk)
                    out.write(chunk)
        except OSError as e:
            dest.unlink(missing_ok=True)
            raise HTTPError(500, f"Failed to write file: {e}")
        make_result = self.maybe_run_make(dest_dir)
        resp = {"ok": True, "path": rel_of(dest), "name": dest.name, "size": size}
        if make_result is not None:
            resp["make"] = make_result
        self.send_json(resp)

    def ep_upload64(self, qs: Qs) -> None:
        """Receive a file as base64 in a JSON body (workaround for flaky
        binary uploads): {"data": "<base64>"}. A data-URL prefix is accepted."""
        if not ENABLE_B64:
            raise HTTPError(403, "Base64 upload is disabled (started with --no-b64)")
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
                f"(limit {MAX_B64_BODY_SIZE // (1024 * 1024)} MB body) — "
                f"use the regular upload instead",
            )
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
        data = payload.get("data")
        if not isinstance(data, str) or not data:
            raise HTTPError(400, "Missing 'data' field (base64 string)")
        if data.startswith("data:"):  # tolerate a full data URL
            comma = data.find(",")
            if comma > 0:
                data = data[comma + 1 :]
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPError(400, "Invalid base64 payload")
        dest = unique_dest(dest_dir, name)
        try:
            dest.write_bytes(raw)
        except OSError as e:
            raise HTTPError(500, f"Failed to write file: {e}")
        make_result = self.maybe_run_make(dest_dir)
        resp = {"ok": True, "path": rel_of(dest), "name": dest.name, "size": len(raw)}
        if make_result is not None:
            resp["make"] = make_result
        self.send_json(resp)

    def ep_download64(self, qs: Qs) -> None:
        """Return a file base64-encoded in a JSON body (workaround for
        flaky binary downloads): the browser decodes it into a Blob.

        The base64 payload is streamed in 3 MB chunks, so memory stays
        flat no matter how large the file is (the cap below still applies,
        because the browser holds the whole result in RAM as a Blob).
        """
        if not ENABLE_B64:
            raise HTTPError(403, "Base64 download is disabled (started with --no-b64)")
        p = self._file(qs)
        size = p.stat().st_size
        if size > MAX_B64_FILE_SIZE:
            raise HTTPError(
                413,
                f"File is too large for base64 transfer "
                f"(limit {MAX_B64_FILE_SIZE // (1024 * 1024)} MB) — use the "
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
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def ep_extract(self, qs: Qs) -> None:
        """Extract a zip file into a folder (default: its own folder)."""
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_file():
            raise HTTPError(404, "File not found")
        if p.suffix.lower() != ".zip":
            raise HTTPError(400, "Not a zip file")
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
        dest_rel = payload.get("dest", "")
        if not isinstance(dest_rel, str):
            raise HTTPError(400, "Invalid 'dest' field")
        dest_dir = safe_path(dest_rel) if dest_rel else p.parent
        if not dest_dir.is_dir():
            raise HTTPError(404, "Destination folder not found")
        result = extract_zip(p, dest_dir)
        if payload.get("delete"):
            try:
                p.unlink()
            except OSError:
                pass
            result["deleted"] = True
        self.send_json({"ok": True, **result})

    def ep_run(self, qs: Qs) -> None:
        """Execute an executable file, capturing output (with timeout)."""
        if not ENABLE_RUN:
            raise HTTPError(403, "Executing files is disabled (started with --no-run)")
        p = safe_path(qs.get("path", [""])[0])
        if p == ROOT:
            raise HTTPError(400, "Cannot run the root directory")
        if not p.is_file():
            raise HTTPError(404, "Not a file")
        if not is_executable(p):
            raise HTTPError(403, "File is not executable")
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        args_raw = payload.get("args", "")
        if not isinstance(args_raw, str):
            args_raw = ""
        try:
            args = shlex.split(args_raw)
        except ValueError:
            raise HTTPError(400, "Malformed arguments (unbalanced quotes?)")
        try:
            timeout = int(payload.get("timeout", RUN_TIMEOUT))
        except (TypeError, ValueError):
            timeout = RUN_TIMEOUT
        timeout = max(1, min(timeout, RUN_MAX_TIMEOUT))
        self.send_json(run_executable(p, args, timeout, MAX_RUN_OUTPUT))

    def ep_make(self, qs: Qs) -> None:
        """Run make in a folder on demand (header "Make" button).

        Unlike the automatic runs, this ignores the client's auto-make
        preference: the user explicitly asked for it.
        """
        if not ENABLE_MAKE:
            raise HTTPError(403, "Make is disabled (started with --no-make)")
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        dir_rel = payload.get("path", "")
        if not isinstance(dir_rel, str):
            dir_rel = ""
        d = safe_path(dir_rel)
        if not d.is_dir():
            raise HTTPError(404, "Directory not found")
        result = run_make_if_present(d)
        if result is None:
            raise HTTPError(400, "No Makefile in this folder")
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
            self.send_json({"done": True, "result": result})

    def ep_shell(self, qs: Qs) -> None:
        """Run an arbitrary command through the configured shell (bash -c)."""
        if not ENABLE_SHELL:
            raise HTTPError(403, "Shell access is disabled (started with --no-shell)")
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Invalid JSON body")
        command = payload.get("command", "")
        if not isinstance(command, str) or not command.strip():
            raise HTTPError(400, "Empty command")
        cwd_rel = payload.get("cwd", "")
        if not isinstance(cwd_rel, str):
            cwd_rel = ""
        cwd = safe_path(cwd_rel)
        if not cwd.is_dir():
            raise HTTPError(404, "Working directory not found")
        try:
            timeout = int(payload.get("timeout", RUN_TIMEOUT))
        except (TypeError, ValueError):
            timeout = RUN_TIMEOUT
        timeout = max(1, min(timeout, RUN_MAX_TIMEOUT))
        code, timed_out, truncated, text, cwd_out = _run_captured(
            [SHELL_CMD, "-c", command], cwd, timeout, MAX_RUN_OUTPUT
        )
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
        soon as the first file is compressed.
        """
        p = safe_path(qs.get("path", [""])[0])
        if not p.is_dir():
            raise HTTPError(404, "Directory not found")
        fname = quote(f"{p.name or 'root'}.zip")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
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
                    for fn in sorted(filenames):
                        fp = base / fn
                        try:
                            if fp.is_symlink() or not fp.is_file():
                                continue
                            zf.write(fp, fp.relative_to(p).as_posix())
                            count += 1
                        except OSError:
                            pass
            sink.close()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        if count == 0:
            self.log_message("zip of %r contained no files", rel_of(p) or "/")

    def ep_thumbnail(self, rel: str) -> None:
        """Serve a Pillow thumbnail for an image, <= THUMB_SIZE.

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
        if p.suffix.lower() not in THUMB_EXTS:
            raise HTTPError(400, "Not a thumbnailable image")
        key = (p, st.st_mtime_ns, st.st_size)
        cached = THUMB_CACHE.get(key)
        if cached is None:
            try:
                from PIL import Image
            except ImportError:
                raise HTTPError(501, "Pillow is not installed; thumbnails unavailable")
            try:
                with Image.open(p) as im:
                    im.load()
                    # Orientation correction for EXIF-rotated photos (JPG/etc.)
                    try:
                        from PIL import ImageOps

                        im = ImageOps.exif_transpose(im)
                    except Exception:  # noqa: BLE001
                        pass
                    im.thumbnail(THUMB_SIZE)
                    want_alpha = (
                        "A" in im.mode
                        or im.mode == "P"
                        or p.suffix.lower() in (".png", ".gif", ".webp", ".ico")
                    )
                    buf = io.BytesIO()
                    if want_alpha:
                        if im.mode not in ("RGBA", "LA", "P"):
                            im = im.convert("RGBA")
                        im.save(buf, "PNG")
                        fmt = "PNG"
                    else:
                        if im.mode not in ("RGB", "L"):
                            im = im.convert("RGB")
                        im.save(buf, "JPEG", quality=82)
                        fmt = "JPEG"
                    data = buf.getvalue()
            except Exception:  # noqa: BLE001
                raise HTTPError(400, "Could not decode image")
            etag = '"%08x"' % (zlib.crc32(data) & 0xFFFFFFFF)
            cached = (etag, f"image/{fmt.lower()}", data)
            THUMB_CACHE.put(key, etag, cached[1], data)
        etag, ctype, data = cached
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
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
#makeBtn{padding:8px 11px;font-size:13px}
#makeRunBtn{padding:8px 11px;font-size:13px}
#makeBtn[aria-pressed="false"]{color:var(--muted);border-style:dashed}
html[data-theme="dark"] #makeBtn[aria-pressed="false"]{color:#7fe9ff;border-color:rgba(0,229,255,.18);box-shadow:none;opacity:.6}
.menu-wrap{position:relative}
.menu{position:absolute;right:0;top:calc(100% + 6px);min-width:190px;display:flex;flex-direction:column;gap:2px;padding:6px;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);z-index:40}
.menu[hidden]{display:none}
.menu-item{font:inherit;font-weight:500;text-align:left;color:var(--text);background:transparent;border:0;border-radius:7px;padding:9px 12px;cursor:pointer;display:flex;align-items:center;gap:9px;white-space:nowrap}
.menu-item:hover{background:var(--hover)}
.menu-item .icon{font-size:16px}
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
  <nav class="crumbs" id="crumbs"></nav>
  <div class="spacer"></div>
  <button class="btn" id="themeBtn" type="button" title="Toggle light / dark theme" aria-label="Toggle light / dark theme">☼</button>
  <button class="btn" id="makeBtn" type="button" aria-pressed="true" title="Run make automatically after edits, uploads and deletes"><span class="emoji">⚙</span> <span id="makeLabel">Auto-make: on</span></button>
  <button class="btn" id="makeRunBtn" type="button" title="Run make in the current folder"><span class="emoji">🔨</span> Make</button>
  <button class="btn" id="shellBtn" title="Run a bash command"><span class="emoji">🖥</span> Shell</button>
  <button class="btn" id="zipBtn" title="Download the current folder as a zip file"><span class="emoji">📦</span> Zip</button>
  <div class="menu-wrap">
    <button class="btn" id="newBtn" aria-haspopup="true" aria-expanded="false" title="Create something new">+ New ▾</button>
    <div class="menu" id="newMenu" role="menu" hidden>
      <button class="menu-item" id="newDir" role="menuitem"><span class="icon">📁</span>New folder</button>
      <button class="menu-item" id="newFile" role="menuitem"><span class="icon">📄</span>New text file</button>
    </div>
  </div>
  <label class="btn btn-primary">Upload<input type="file" id="files" multiple hidden></label>
  <label class="btn" id="upload64Btn" title="Upload as base64 (workaround for network problems)">Upload b64<input type="file" id="files64" multiple hidden></label>
</header>
<main>
  <div class="meta" id="meta"></div>
  <div class="list-wrap">
    <div class="row-hl" aria-hidden="true"></div>
    <table class="list">
      <thead>
        <tr>
          <th class="sortable" data-sort="name">Name<span class="si"></span></th>
          <th class="c-size sortable" data-sort="size">Size<span class="si"></span></th>
          <th class="c-created sortable" data-sort="ctime">Created<span class="si"></span></th>
          <th class="c-mtime sortable" data-sort="mtime">Last updated<span class="si"></span></th>
          <th class="c-acts">Actions</th>
        </tr>
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

<script>
const ROOT_NAME = __ROOT_NAME__;
const FEATURES = __FEATURES__;  // {shell, run, make, b64} — set by the server at startup
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
                sort: { key: "name", dir: 1 } };

const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function fmtSize(n){ if(n==null) return ""; if(n===0) return "0 B"; const u=["B","KB","MB","GB","TB"]; let i=0; while(n>=1024 && i<u.length-1){n/=1024;i++;} return n.toFixed(n>=10||i===0?0:1)+" "+u[i]; }
function fmtDate(ts){ const d=new Date(ts*1000), p=x=>String(x).padStart(2,"0"); return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes()); }
function dirname(p){ if(!p) return ROOT_NAME; const i=p.lastIndexOf("/"); const base=i<0?p:p.slice(0,i); return base===""?ROOT_NAME:base; }

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
    if(!r.ok) throw new Error((data&&data.error) || (r.status+" "+r.statusText));
    return data;
  });
}

let toastTimer;
function toast(msg, isErr){
  const t=$("#toast"); t.textContent=msg; t.classList.toggle("err", !!isErr); t.classList.add("show");
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.classList.remove("show"), 3500);
}

function openModal(title, html){ $("#modalTitle").textContent=title; $("#modalBody").innerHTML=html; $("#overlay").hidden=false; }
function closeModal(){ $("#overlay").hidden=true; $("#modalBody").innerHTML=""; }
$("#modalClose").onclick=closeModal;
$("#overlay").addEventListener("mousedown", e=>{ if(e.target===$("#overlay")) closeModal(); });
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeModal(); });

// -- loading / listing -------------------------------------------------------
function load(path){
  state.path = path || "";
  state.sizes = {}; state.sizesLoaded = false;
  updateCrumbs();
  api("api/list?path="+encodeURIComponent(state.path)).then(d=>{
    state.entries=d.entries; state.parent=d.parent;
    document.title = (state.path? state.path+"/" : "") + ROOT_NAME + " · Files";
    render();
    loadFolderSizes();
  }).catch(err=>toast(err.message, true));
}

function sizeOf(e){ return e.kind==="dir" ? (state.sizes[e.path] ?? -1) : (e.size ?? 0); }

function sortedEntries(){
  const {key, dir} = state.sort;
  const dirs = state.entries.filter(e=>e.kind==="dir");
  const files = state.entries.filter(e=>e.kind!=="dir");
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
  const e=state.entries;
  rows.innerHTML="";
  $("#empty").hidden = e.length>0;
  const dirs=e.filter(x=>x.kind==="dir").length, files=e.length-dirs;
  $("#meta").textContent = e.length ? dirs+" folder"+(dirs!==1?"s":"")+", "+files+" file"+(files!==1?"s":"") : "";
  if(state.path) rows.appendChild(trUp());
  sortedEntries().forEach(x=>rows.appendChild(trEntry(x)));
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
function loadFolderSizes(){
  const dirPaths = state.entries.filter(e=>e.kind==="dir").map(e=>e.path);
  if(dirPaths.length===0){ state.sizesLoaded=true; return; }
  api("api/foldersize", { method:"POST", headers:{"Content-Type":"application/json"},
                          body: JSON.stringify({paths: dirPaths}) })
    .then(d=>{ state.sizes=d.sizes||{}; state.sizesLoaded=true; render(); updateSortIndicators(); })
    .catch(()=>{ state.sizesLoaded=true; });
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
  tr.innerHTML='<td class="name"><span class="nm"><span class="icon">📂</span><span>..</span></span></td><td class="size"></td><td class="mtime"></td><td class="mtime"></td><td class="acts"></td>';
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

function sizeCell(e){
  if(e.kind==="dir"){
    return state.sizes[e.path]!==undefined ? fmtSize(state.sizes[e.path]) : "…";
  }
  return fmtSize(e.size);
}

function actionsHTML(e){
  let h="";
  if(e.kind==="dir"){
    h+='<button class="act" data-id="open" title="Open folder">📂</button>';
    h+='<button class="act" data-id="zip" title="Download as zip">📦</button>';
  }
  h+='<button class="act" data-id="rename" title="Rename">✏️</button>';
  h+='<button class="act" data-id="move" title="Move to…">🚚</button>';
  if(e.kind!=="dir"){
    if(e.name.toLowerCase().endsWith(".zip")) h+='<button class="act" data-id="extract" title="Extract zip">🗜️</button>';
    if(e.run) h+='<button class="act" data-id="run" title="Run">▶</button>';
    const vi = e.kind==="text" ? "✎" : "👁";
    const vt = e.kind==="text" ? "Edit" : "View";
    h+='<button class="act" data-id="view" title="'+vt+'">'+vi+'</button>';
    h+='<button class="act" data-id="dl" title="Download">⬇</button>';
    if(FEATURES.b64) h+='<button class="act" data-id="dlb64" title="Download as base64 (workaround for network problems)">🔣</button>';
  }
  h+='<button class="act" data-id="del" title="Delete">🗑</button>';
  return h;
}

function trEntry(e){
  const tr=document.createElement("tr"); tr.className="row";
  const isDir=e.kind==="dir";
  tr.innerHTML=
    '<td class="name"><span class="nm">'+iconHTML(e)+'<span>'+esc(e.name)+'</span></span></td>'+
    '<td class="size">'+sizeCell(e)+'</td>'+
    '<td class="mtime">'+fmtDate(e.ctime)+'</td>'+
    '<td class="mtime">'+fmtDate(e.mtime)+'</td>'+
    '<td class="acts">'+actionsHTML(e)+'</td>';
  tr.onclick=()=>{ isDir ? load(e.path) : openFile(e); };
  tr.querySelectorAll(".act").forEach(b=>{
    b.onclick=ev=>{ ev.stopPropagation(); ACTION_MAP[b.dataset.id](e); };
  });
  return tr;
}
const ACTION_MAP={ open:e=>load(e.path), view:e=>openFile(e), dl:e=>download(e), del:e=>del(e), rename:e=>renameItem(e), move:e=>moveItem(e), extract:e=>extractZip(e), zip:e=>zipDir(e.path), dlb64:e=>downloadB64(e), run:e=>runFile(e) };

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
    $("#modalTitle").textContent="Editing — "+e.name;
    $("#modalBody").innerHTML=
      '<div class="editor-info">'+fmtSize(d.size)+' · Line endings: <b>'+esc(NL_LABEL[origNL]||origNL)+'</b>'+
      ' <select id="nlSel" class="nl-sel" title="Line endings to save with">'+
      '<option value="keep">Keep as-is</option>'+
      '<option value="lf">Change to LF (Unix)</option>'+
      '<option value="crlf">Change to CRLF (Windows)</option>'+
      '<option value="cr">Change to CR (Classic Mac)</option>'+
      '</select></div>'+
      '<textarea id="editor" spellcheck="false"></textarea>'+
      '<div class="modal-actions"><button class="btn btn-primary" id="saveBtn">Save</button><button class="btn" onclick="closeModal()">Close</button></div>';
    const ta=$("#editor"); ta.value=d.content;
    const sel=$("#nlSel");
    sel.options[0].textContent="Keep as-is ("+(NL_LABEL[origNL]||origNL)+")";
    $("#saveBtn").onclick=()=>saveText(e, ta, sel.value, origNL);
    ta.focus();
  }).catch(err=>{
    openModal(e.name,
      '<div class="editor-info">'+esc(err.message)+'</div>'+
      '<div class="modal-actions"><button class="btn btn-primary" id="txtRaw">Open in browser</button><a class="btn" href="api/download?path='+q+'">Download</a><button class="btn" onclick="closeModal()">Close</button></div>');
    $("#txtRaw").onclick=()=>{ window.open("api/raw?path="+q, "_blank", "noopener"); closeModal(); };
  });
}

function saveText(e, ta, sel, origNL){
  // The browser textarea normalizes line endings to \n, so "keep" means
  // re-applying the file's original line ending on the server (when it had
  // a single consistent one; "mixed"/"none" are saved as typed).
  const nl = sel==="keep" ? (["lf","crlf","cr"].includes(origNL) ? origNL : "as-is") : sel;
  api("api/save?path="+encodeURIComponent(e.path), { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({content: ta.value, newline: nl}) })
    .then(d=>{
      toast("Saved "+e.name);
      closeModal(); load(state.path);
      if(d.make) pollMakeJobs("after saving "+e.name, [{file:e.name, job:d.make.job}]);
    })
    .catch(err=>toast(err.message, true));
}

function del(e){
  const what=e.kind==="dir"?"folder":"file";
  const suffix=e.kind==="dir"?" (and everything inside it)":"";
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

function zipDir(path){
  toast("Preparing zip…");
  window.location = "api/zip?path="+encodeURIComponent(path||"");
}
$("#zipBtn").onclick=()=>zipDir(state.path);

// -- Download as base64 (workaround: JSON transport instead of binary stream)
function downloadB64(e){
  toast("Encoding "+e.name+" as base64…");
  api("api/download64?path="+encodeURIComponent(e.path)).then(d=>{
    const bin=atob(d.data);
    const bytes=new Uint8Array(bin.length);
    for(let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
    const blob=new Blob([bytes], {type:"application/octet-stream"});
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");
    a.href=url; a.download=d.name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 10000);
    toast("Downloaded "+d.name);
  }).catch(err=>toast(err.message, true));
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
    const del=$("#exDelete").checked;
    const resEl=$("#exResult");
    resEl.innerHTML='<div class="loading" style="padding:16px 0">Extracting…</div>';
    api("api/extract?path="+q,
        { method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ dest, delete: del }) })
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
function makeModal(title, runs){
  openModal("make — "+esc(title),
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
    '<div class="modal-actions"><button class="btn btn-primary" onclick="closeModal()">Close</button></div>');
}

// Auto-make runs in the server's background: poll each job until it reports
// a result, then show one combined make modal (as before).
function pollMakeJobs(title, jobs){
  const t0=Date.now();
  const result={};
  (function poll(){
    const pending=jobs.filter(j=>result[j.job]===undefined);
    if(!pending.length){
      const runs=jobs.map(j=>({file:j.file, res:result[j.job]})).filter(r=>r.res);
      if(runs.length) makeModal(title, runs);
      return;
    }
    if(Date.now()-t0>10*60*1000){ toast("Make results expired", true); return; }
    Promise.all(pending.map(j=>api("api/make/result?job="+j.job)
        .then(d=>{ if(d.done) result[j.job]=d.result; })
        .catch(err=>{ if(String(err.message).startsWith("404")) result[j.job]=null; })))
      .then(()=>setTimeout(poll, 400));
  })();
}

// -- Run executable file ----------------------------------------------------
function runFile(e){
  openModal("Run — "+e.name,
    '<div class="hint">Runs <b>'+esc(e.name)+'</b> in <b>'+esc(dirname(e.path))+'</b> using the current user’s environment.</div>'+
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

// -- Shell (header button): run a bash command ------------------------------
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

// -- Make (header button): run make in the current folder on demand ---------
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

// -- Move (with folder tree picker) --
let moveSelPath=null;
function moveItem(e){
  moveSelPath=e.path;
  const isDir=e.kind==="dir";
  openModal("Move "+(isDir?"folder":"file")+" — "+e.name,
    '<div class="hint">Select a destination folder:</div>'+
    '<div id="moveTree"></div>'+
    '<div class="modal-actions"><button class="btn btn-primary" id="moveOk" disabled>Move here</button><button class="btn" onclick="closeModal()">Cancel</button></div>');
  loadMoveTree("", e.path, isDir);
}

function loadMoveTree(dirPath, excludePath, excludeIsDir){
  api("api/tree?path="+encodeURIComponent(dirPath)).then(d=>{
    const ul=document.createElement("ul");
    ul.className="tree";
    const rootLi=document.createElement("li");
    rootLi.innerHTML='<span class="tw">🏠</span><span class="tn">'+esc(ROOT_NAME)+' (root)</span>';
    rootLi.dataset.path="";
    rootLi.onclick=()=>selectMoveDir(rootLi, "");
    ul.appendChild(rootLi);
    d.dirs.forEach(dir=>{
      if(dir.path===excludePath) return;
      if(excludeIsDir && dir.path.startsWith(excludePath+"/")) return;
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
          pLi.onclick=()=>{ browseDir=dd.parent; loadMoveTree(dirPath, excludePath, excludeIsDir); };
          sUl.appendChild(pLi);
        }
        dd.dirs.forEach(dir2=>{
          if(dir2.path===excludePath) return;
          if(excludeIsDir && dir2.path.startsWith(excludePath+"/")) return;
          const li2=document.createElement("li");
          li2.innerHTML='<span class="tw">📁</span><span class="tn">'+esc(dir2.name)+'</span>';
          li2.onclick=()=>selectMoveDir(li2, dir2.path);
          sUl.appendChild(li2);
        });
        frag.appendChild(sUl);
        const useBtn=document.createElement("button");
        useBtn.className="btn btn-primary";
        useBtn.style.cssText="margin-top:8px;font-size:12px;padding:5px 12px";
        useBtn.textContent="Move to here";
        useBtn.onclick=()=>doMove(moveSelPath, browseDir);
        frag.appendChild(useBtn);
        subBtn.parentElement.insertBefore(frag, subBtn.parentElement.querySelector(".modal-actions"));
      });
    };
    container.appendChild(subBtn);
    $("#moveOk").disabled=true;
  });
}

function selectMoveDir(li, path){
  document.querySelectorAll("#moveTree .tree li.selected").forEach(x=>x.classList.remove("selected"));
  li.classList.add("selected");
  $("#moveOk").disabled=false;
  $("#moveOk").onclick=()=>doMove(moveSelPath, path);
}

function doMove(srcPath, destDir){
  api("api/move?path="+encodeURIComponent(srcPath), { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({dest: destDir}) })
    .then(()=>{ toast("Moved successfully"); closeModal(); load(state.path); })
    .catch(err=>toast(err.message, true));
}

// -- header "New" dropdown --------------------------------------------------
const newBtn=$("#newBtn"), newMenu=$("#newMenu");
function closeNewMenu(){ newMenu.hidden=true; newBtn.setAttribute("aria-expanded","false"); }
newBtn.onclick=ev=>{
  ev.stopPropagation();
  const open=newMenu.hidden;
  newMenu.hidden=!open;
  newBtn.setAttribute("aria-expanded", String(open));
};
document.addEventListener("click", ev=>{
  if(!newMenu.hidden && !ev.target.closest(".menu-wrap")) closeNewMenu();
});
document.addEventListener("keydown", ev=>{ if(ev.key==="Escape") closeNewMenu(); });

$("#newDir").onclick=()=>{
  closeNewMenu();
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
  closeNewMenu();
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

$("#files").addEventListener("change", function(){
  const files=[...this.files]; this.value="";
  if(files.length) uploadAll(files, 0, []);
});
function uploadAll(files, i, results, makeRuns){
  if(!makeRuns) makeRuns=[];
  if(i>=files.length){
    toast("Upload complete");
    closeModal(); load(state.path);
    if(makeRuns.length) pollMakeJobs("after upload", makeRuns);
    offerExtract(results.filter(r=>r.name.toLowerCase().endsWith(".zip")));
    return;
  }
  const f=files[i];
  openModal("Uploading…",
    '<div class="up-name">'+esc(f.name)+'</div>'+
    '<div class="bar"><div class="bar-fill" id="barFill"></div></div>'+
    '<div class="up-count">'+(i+1)+' / '+files.length+'</div>');
  uploadOne(f, p=>{ const b=$("#barFill"); if(b) b.style.width=(p*100).toFixed(1)+"%"; })
    .then(r=>{ results.push(r); if(r.make) makeRuns.push({file:r.name, job:r.make.job}); uploadAll(files, i+1, results, makeRuns); })
    .catch(err=>{ toast(f.name+": "+err.message, true); setTimeout(()=>uploadAll(files, i+1, results, makeRuns), 500); });
}
// -- Upload as base64 (workaround: encode in the browser, JSON over the wire)
$("#files64").addEventListener("change", function(){
  const files=[...this.files]; this.value="";
  if(files.length) uploadB64All(files, 0);
});
function readB64(file){
  return new Promise((resolve,reject)=>{
    const fr=new FileReader();
    fr.onload=()=>{ const s=String(fr.result); resolve(s.slice(s.indexOf(",")+1)); };
    fr.onerror=()=>reject(new Error("Could not read file"));
    fr.readAsDataURL(file);
  });
}
function uploadB64All(files, i, makeRuns){
  if(!makeRuns) makeRuns=[];
  if(i>=files.length){
    toast("Upload complete");
    closeModal(); load(state.path);
    if(makeRuns.length) pollMakeJobs("after upload (base64)", makeRuns);
    return;
  }
  const f=files[i];
  openModal("Uploading (base64)…",
    '<div class="up-name">'+esc(f.name)+'</div>'+
    '<div class="bar"><div class="bar-fill" id="barFill64"></div></div>'+
    '<div class="up-count">'+(i+1)+' / '+files.length+'</div>');
  const fill=$("#barFill64"); if(fill) fill.style.width="100%";  // no byte progress: the file is fully read first
  readB64(f)
    .then(b64=>api("api/upload64?path="+encodeURIComponent(state.path)+"&filename="+encodeURIComponent(f.name),
        { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ data: b64 }) }))
    .then(r=>{ if(r.make) makeRuns.push({file:r.name, job:r.make.job}); uploadB64All(files, i+1, makeRuns); })
    .catch(err=>{ toast(f.name+": "+err.message, true); setTimeout(()=>uploadB64All(files, i+1, makeRuns), 500); });
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
function uploadOne(file, onProgress){
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open("POST","api/upload?path="+encodeURIComponent(state.path)+"&filename="+encodeURIComponent(file.name));
    xhr.responseType="text";
    if(!autoMakeEnabled) xhr.setRequestHeader("X-FM-Auto-Make","0");
    if(onProgress && xhr.upload) xhr.upload.onprogress=e=>{ if(e.lengthComputable) onProgress(e.loaded/e.total); };
    xhr.onload=()=>{
      let d=null; try{ d=JSON.parse(xhr.responseText); }catch(e){}
      if(xhr.status>=200 && xhr.status<300) resolve(d||{});
      else reject(new Error((d&&d.error)||(xhr.status+" "+xhr.statusText)));
    };
    xhr.onerror=()=>reject(new Error("Network error"));
    xhr.send(file);
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
function applyMakeState(){
  const btn = $("#makeBtn"), label = $("#makeLabel");
  if(!btn) return;
  btn.setAttribute("aria-pressed", autoMakeEnabled ? "true" : "false");
  if(label) label.textContent = "Auto-make: " + (autoMakeEnabled ? "on" : "off");
  btn.title = autoMakeEnabled
    ? "make runs automatically after edits, uploads and deletes (click to disable)"
    : "Automatic make is off (click to enable)";
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

// -- Startup feature flags (--no-shell / --no-run / --no-make / --no-b64) --
// The server enforces these too (the endpoints refuse with 403); this only
// hides the corresponding UI so disabled features don't even appear.
function applyFeatures(){
  const hide = (sel, on) => { const el=$(sel); if(el && !on) el.style.display="none"; };
  hide("#shellBtn", FEATURES.shell);
  hide("#makeBtn", FEATURES.make);
  hide("#makeRunBtn", FEATURES.make);
  hide("#upload64Btn", FEATURES.b64);
}

applyFeatures();
applyMakeState();
applyTheme();
load("");
updateSortIndicators();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    global ROOT, ENABLE_SHELL, ENABLE_RUN, ENABLE_MAKE, ENABLE_B64
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
        "--no-shell",
        action="store_true",
        help="disable shell access (Shell button, /api/shell)",
    )
    ap.add_argument(
        "--no-run",
        action="store_true",
        help="disable execution of executable files (Run button, /api/run)",
    )
    ap.add_argument(
        "--no-make",
        action="store_true",
        help="disable make entirely: no automatic rebuilds and no manual Make button",
    )
    ap.add_argument(
        "--no-b64",
        action="store_true",
        help="disable the base64 upload/download workaround (/api/upload64, /api/download64)",
    )
    args = ap.parse_args(argv)

    root = Path(args.root or os.getcwd()).expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a directory: {root}")
    ROOT = root
    ENABLE_SHELL = not args.no_shell
    ENABLE_RUN = not args.no_run
    ENABLE_MAKE = not args.no_make
    ENABLE_B64 = not args.no_b64
    mimetypes.init()

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        ap.error(f"could not bind {args.host}:{args.port} ({e})")
    server.daemon_threads = True  # don't block shutdown on stuck request threads

    print(f"Serving: {ROOT}")
    print(f"URL:     http://{args.host}:{args.port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
