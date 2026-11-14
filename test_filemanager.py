#!/usr/bin/env python3
"""Tests for filemanager.py.

Run with:
    python3 -m unittest -v test_filemanager.py
or, if pytest is installed:
    python3 test_filemanager.py

The pure helpers are tested directly; the parts where a bug only shows up on
the wire (request bodies, ranges, symlinks, keep-alive) are tested against a
real server on a random port with a temporary root.
"""

import base64
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import filemanager as fm

HAVE_PILLOW = fm.pillow_available()


def _silent(self, *args, **kwargs) -> None:
    """Stand-in for the handler's logging methods (see HttpCase.setUp)."""


class TempRoot(unittest.TestCase):
    """Base class with ROOT pointed at a fresh temporary directory."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="filemanager-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self._swap(fm, "ROOT", self.root.resolve())

    def _swap(self, module, name, value):
        old = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, old)
        return old

    def write(self, rel: str, data: str | bytes = "") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        return p


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------
class SafePathTests(TempRoot):
    def test_root_and_children(self):
        self.assertEqual(fm.safe_path(""), self.root)
        self.assertEqual(fm.safe_path("."), self.root)
        self.assertEqual(fm.safe_path("a"), self.root / "a")
        self.assertEqual(fm.safe_path("a/b/c.txt"), self.root / "a" / "b" / "c.txt")

    def test_leading_slashes_and_backslashes(self):
        self.assertEqual(fm.safe_path("/a/b"), self.root / "a" / "b")
        self.assertEqual(fm.safe_path("a\\b\\c"), self.root / "a" / "b" / "c")

    def test_dot_dot_cannot_escape(self):
        self.assertEqual(fm.safe_path(".."), self.root)
        self.assertEqual(fm.safe_path("../../etc/passwd"), self.root / "etc" / "passwd")
        self.assertEqual(fm.safe_path("a/../../b"), self.root / "b")
        self.assertEqual(fm.safe_path("a/.."), self.root)
        self.assertEqual(fm.safe_path("/.."), self.root)

    def test_nul_byte_is_a_client_error(self):
        # Used to reach pathlib and surface as "500 embedded null character".
        with self.assertRaises(fm.HTTPError) as ctx:
            fm.safe_path("root.txt\x00x")
        self.assertEqual(ctx.exception.code, 400)

    def test_trailing_whitespace_is_kept(self):
        # A " " .strip() here made a file called "trail " impossible to open.
        self.assertEqual(fm.safe_path("trail "), self.root / "trail ")
        self.assertEqual(fm.safe_path("  x  "), self.root / "  x  ")

    def test_symlink_leaf_is_not_resolved(self):
        # The entry the listing shows must be the entry operations act on.
        (self.root / "target").mkdir()
        (self.root / "link").symlink_to("target")
        self.assertEqual(fm.safe_path("link"), self.root / "link")
        # Only the last component stays unresolved; a folder in the middle of a
        # path is resolved, which is what keeps the containment check honest.
        self.assertEqual(
            fm.safe_path("link/inside.txt"), self.root / "target" / "inside.txt"
        )

    def test_parent_chain_is_resolved(self):
        (self.root / "real").mkdir()
        (self.root / "alias").symlink_to("real")
        self.assertEqual(fm.safe_path("alias/x"), self.root / "real" / "x")

    def test_parent_folder_outside_the_root_is_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="filemanager-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (self.root / "out").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(fm.HTTPError) as ctx:
            fm.safe_path("out/anything.txt")
        self.assertEqual(ctx.exception.code, 403)

    def test_too_many_dot_dot_stay_at_the_root(self):
        # Overshooting the root is a "cd .." too many, not an escape.
        self.assertEqual(fm.safe_path("../../x"), self.root / "x")
        self.assertEqual(fm.safe_path("a/../../../../x"), self.root / "x")


class HelperTests(TempRoot):
    def test_fmt_size_names_its_own_unit(self):
        # The limits are written in bytes and quoted back to the user, and a
        # hand-rolled "// 1024*1024" happily prints "1024 MB".
        for n, text in (
            (0, "0 B"),
            (999, "999 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (10 * 1024, "10 KB"),
            (1024 * 1024, "1.0 MB"),
            (1024**3, "1.0 GB"),
            (1024**4, "1.0 TB"),
            (1024**5, "1 PB"),  # past the last named unit, rounded without a decimal
        ):
            with self.subTest(n=n):
                self.assertEqual(fm.fmt_size(n), text)

    def test_the_limits_quoted_to_the_user_agree_with_each_other(self):
        # base64 needs four characters per three bytes: when the body limit and
        # the file limit were the same number, a base64 upload silently stopped
        # at three quarters of what a download carried.
        self.assertGreaterEqual(
            fm.MAX_B64_BODY_SIZE, (fm.MAX_B64_FILE_SIZE + 2) // 3 * 4
        )
        self.assertGreaterEqual(fm.MAX_B64_FILE_SIZE, fm.MAX_TRANSFER_SIZE)
        self.assertLessEqual(fm.MAX_ZIP_ENTRY_SIZE, fm.MAX_ZIP_TOTAL_SIZE)
        self.assertLessEqual(fm.MAX_JSON_BODY_SIZE, fm.MAX_B64_BODY_SIZE)

    def test_classify(self):
        self.assertEqual(fm.classify("a.PNG"), "image")
        self.assertEqual(fm.classify("a.svg"), "image")
        self.assertEqual(fm.classify("notes.md"), "text")
        self.assertEqual(fm.classify("Makefile"), "text")
        self.assertEqual(fm.classify("archive.zip"), "file")

    def test_detect_newline(self):
        self.assertEqual(fm.detect_newline(b"a\r\nb\r\n"), "crlf")
        self.assertEqual(fm.detect_newline(b"a\nb\n"), "lf")
        self.assertEqual(fm.detect_newline(b"a\rb\r"), "cr")
        self.assertEqual(fm.detect_newline(b"a\r\nb\nc"), "mixed")
        self.assertEqual(fm.detect_newline(b"abc"), "none")

    def test_unique_dest_never_overwrites(self):
        # The counter goes before the extension so the type stays recognisable.
        (self.root / "x.txt").write_text("1")
        self.assertEqual(fm.unique_dest(self.root, "x.txt").name, "x (1).txt")
        (self.root / "x (1).txt").write_text("2")
        self.assertEqual(fm.unique_dest(self.root, "x.txt").name, "x (2).txt")
        self.assertEqual(fm.unique_dest(self.root, "new.txt").name, "new.txt")
        self.assertEqual(fm.unique_dest(self.root, "noext").name, "noext")
        (self.root / "noext").write_text("1")
        self.assertEqual(fm.unique_dest(self.root, "noext").name, "noext (1)")

    def test_validate_new_name_returns_the_cleaned_name(self):
        self.assertEqual(fm.validate_new_name("  a.txt  "), "a.txt")
        for bad in ("", "   ", ".", "..", "a/b", "a\\b", "a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(fm.HTTPError):
                fm.validate_new_name(bad)

    def test_parse_byte_range(self):
        self.assertEqual(fm.parse_byte_range("bytes=0-499", 1000), (0, 499))
        self.assertEqual(fm.parse_byte_range("bytes=500-", 1000), (500, 999))
        self.assertEqual(fm.parse_byte_range("bytes=-200", 1000), (800, 999))
        self.assertEqual(fm.parse_byte_range("bytes=900-5000", 1000), (900, 999))
        for header in (
            "bytes=0-1,4-5",
            "bits=0-1",
            "bytes=",
            "bytes=x-y",
            "bytes=2000-",
        ):
            with self.subTest(header=header):
                self.assertIsNone(fm.parse_byte_range(header, 1000))
        self.assertIsNone(fm.parse_byte_range("bytes=0-", 0))  # empty file

    def test_content_disposition(self):
        self.assertEqual(
            fm.content_disposition("inline", "a.txt"),
            "inline; filename=\"a.txt\"; filename*=UTF-8''a.txt",
        )
        headed = fm.content_disposition("attachment", "ünï.txt")
        # The ASCII fallback only serves clients that ignore filename*: it may
        # lose characters, but it must stay a valid header value...
        fallback = headed.split('filename="')[1].split('"')[0]
        self.assertTrue(fallback.isascii())
        self.assertTrue(fallback)
        # ...and the exact name travels in the RFC 6266 parameter.
        self.assertIn("filename*=UTF-8''%C3%BCn%C3%AF.txt", headed)
        # Header injection through a file name is not possible.
        evil = fm.content_disposition("attachment", 'a"b\r\nX-Evil: 1')
        self.assertNotIn("\r", evil)
        self.assertNotIn("\n", evil)
        self.assertNotIn("\r\nX-Evil", evil.replace('"', ""))

    def test_entry_exists_and_link_helpers(self):
        (self.root / "dir").mkdir()
        (self.root / "file").write_text("x")
        (self.root / "link_dir").symlink_to("dir", target_is_directory=True)
        (self.root / "dangling").symlink_to("nowhere")
        self.assertTrue(fm.entry_exists(self.root / "dangling"))
        self.assertFalse(fm.entry_exists(self.root / "gone"))
        self.assertFalse(fm.is_link_to_dir(self.root / "link_dir"))
        self.assertTrue(fm.is_link_to_dir(self.root / "dir"))
        self.assertEqual(fm.link_target(self.root / "link_dir"), "dir")
        self.assertIsNone(fm.link_target(self.root / "file"))


# ---------------------------------------------------------------------------
# Recursive sizes and their cache
# ---------------------------------------------------------------------------
class DirSizeTests(TempRoot):
    def test_sums_files_only(self):
        self.write("a/b/c.txt", "x" * 10)
        self.write("a/d.txt", "y" * 5)
        (self.root / "link").symlink_to("a/b/c.txt")
        total, seen, complete = fm.dir_size(self.root)
        self.assertEqual(total, 15)
        self.assertTrue(complete)
        self.assertGreaterEqual(seen, 3)

    def test_symlinked_folders_are_not_followed(self):
        (self.root / "big").mkdir()
        self.write("big/blob.bin", "z" * 100)
        (self.root / "alias").symlink_to("big", target_is_directory=True)
        total, _seen, _complete = fm.dir_size(self.root)
        self.assertEqual(total, 100)  # once, not twice

    def test_budget_stops_the_walk(self):
        for i in range(20):
            self.write(f"d/f{i}.txt", "x")
        total, seen, complete = fm.dir_size(self.root, budget=5)
        self.assertEqual(seen, 5)
        self.assertFalse(complete)
        self.assertLessEqual(total, 5)

    def test_cache_expires_and_is_invalidated(self):
        cache = fm._DirSizeCache(ttl=60, max_entries=8)
        key = str(self.root / "a")
        cache.put(key, 123, True)
        self.assertEqual(cache.get(key), (123, True))
        cache.put(str(self.root / "a" / "b"), 1, True)
        cache.put(str(self.root.parent), 9, True)
        cache.invalidate(key)
        self.assertIsNone(cache.get(key))  # the entry itself
        self.assertIsNone(cache.get(str(self.root / "a" / "b")))  # below it
        self.assertIsNone(cache.get(str(self.root.parent)))  # above it


class ExtractZipTests(TempRoot):
    def make_zip(self, name: str, members: dict[str, str]) -> Path:
        p = self.root / name
        with zipfile.ZipFile(p, "w") as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return p

    def test_extracts_and_keeps_empty_folders(self):
        zp = self.make_zip("a.zip", {"one.txt": "1", "empty/": "", "d/two.txt": "22"})
        out = self.root / "out"
        out.mkdir()
        result = fm.extract_zip(zp, out)
        self.assertEqual(result["entries"], 2)
        self.assertEqual((out / "one.txt").read_text(), "1")
        self.assertEqual((out / "d" / "two.txt").read_text(), "22")
        self.assertTrue((out / "empty").is_dir())

    def test_zip_slip_is_refused(self):
        zp = self.make_zip("bad.zip", {"../escaped.txt": "boom"})
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(fm.HTTPError) as ctx:
            fm.extract_zip(zp, out)
        self.assertEqual(ctx.exception.code, 400)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_archive_cannot_overwrite_itself(self):
        zp = self.make_zip("a.zip", {"a.zip": "boom"})
        with self.assertRaises(fm.HTTPError):
            fm.extract_zip(zp, self.root)

    def test_total_limit_uses_real_bytes_not_headers(self):
        # A 600 KB archive can claim any size it likes; the bytes written are
        # what decide, so a lying header cannot fill the disk either way.
        zp = self.root / "bomb.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("huge.txt", "x" * (200 * 1024))
        self._swap(fm, "MAX_ZIP_TOTAL_SIZE", 64 * 1024)
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(fm.HTTPError) as ctx:
            fm.extract_zip(zp, out)
        self.assertEqual(ctx.exception.code, 413)
        # The half-written member is cleaned up rather than left as a giant
        # fragment of a file that was never going to finish.
        self.assertFalse((out / "huge.txt").exists())


# ---------------------------------------------------------------------------
# Streaming base64 payloads
# ---------------------------------------------------------------------------
class Base64PayloadTests(unittest.TestCase):
    """/api/upload64's decoder, fed by hand at boundaries it did not choose."""

    @staticmethod
    def unwrap(body: bytes, writes: int) -> bytes:
        """Hand a request body over in `writes` pieces; return the file bytes."""
        reader = fm._B64PayloadReader()
        out = bytearray()
        size = -(-len(body) // writes)  # up, so even a short body gets split
        for start in range(0, len(body), size):
            out += reader.feed(body[start : start + size])
        out += reader.finish()
        return bytes(out)

    def refused(self, body: bytes, writes: int = 3) -> fm.HTTPError:
        try:
            self.unwrap(body, writes)
        except fm.HTTPError as err:
            return err
        self.fail(f"{body!r} was accepted")

    def test_a_payload_cut_up_at_odd_places_comes_back_whole(self):
        blob = os.urandom(9000)
        body = json.dumps(
            {
                "path": "sub",
                "filename": "x.bin",
                "data": base64.b64encode(blob).decode(),
            }
        ).encode()
        for writes in (1, 2, 3, 5, 17, 64, 900):
            with self.subTest(writes=writes):
                self.assertEqual(self.unwrap(body, writes), blob)

    def test_every_length_from_one_byte_to_a_group(self):
        # Padding is what separates these cases: a payload that ends on a group
        # of four is finished by feed(), one that does not has to be by finish().
        for n in range(1, 13):
            blob = bytes(range(n))
            body = json.dumps({"data": base64.b64encode(blob).decode()}).encode()
            for writes in (1, 2, 3, 7):
                with self.subTest(bytes=n, writes=writes):
                    self.assertEqual(self.unwrap(body, writes), blob)

    def test_line_breaks_are_tolerated_in_either_form(self):
        # base64.encodebytes() wraps at 76 columns and json.dumps() then escapes
        # those breaks, so "\n" (two bytes) is what a Python client really sends.
        blob = b"a line wrapped payload\n" * 300
        plain = base64.b64encode(blob).decode()
        wrapped = "\n".join(plain[i : i + 76] for i in range(0, len(plain), 76))
        for text in (plain, wrapped):
            with self.subTest(escaped=text is wrapped):
                body = json.dumps({"data": text}).encode()
                self.assertEqual(self.unwrap(body, 13), blob)
        # A raw newline inside the string is not valid JSON, but it is no worse
        # than an escaped one and used to work, so it still decodes.
        self.assertEqual(self.unwrap(b'{"data":"' + wrapped.encode() + b'"}', 13), blob)

    def test_a_data_url_prefix_is_still_accepted(self):
        body = json.dumps(
            {"data": "data:image/png;base64," + base64.b64encode(b"png").decode()}
        ).encode()
        self.assertEqual(self.unwrap(body, 2), b"png")

    def test_padding_ends_the_payload(self):
        # A payload that reaches its padding early is complete: what follows is
        # the rest of the JSON, not more base64.
        self.assertEqual(self.unwrap(b'{"note":"x","data":"QUI="}', 1), b"AB")
        self.assertEqual(self.unwrap(b'{"data":"QQ==","x":1}', 5), b"A")

    def test_bodies_that_are_not_a_payload_are_refused(self):
        for body in (
            b'{"path":"x"}',  # no data field at all
            b'{"data":""}',  # an empty payload is not a file
            b'"just a string"',  # not an object
            b'{"data":"not base64!!"}',  # symbols outside the alphabet
            b'{"data":"QUJ',  # cut off before its closing quote
            b"nonsense",  # not JSON either
        ):
            with self.subTest(body=body):
                self.assertEqual(self.refused(body).code, 400)

    def test_garbage_after_the_payload_does_not_stop_the_drain(self):
        # Whatever follows the closing quote is read and ignored, which is what
        # keeps the connection usable: the reader must not need to see it.
        reader = fm._B64PayloadReader()
        out = reader.feed(b'{"data":"QUJ') + reader.feed(b'D"} trailing junk')
        self.assertEqual(out + reader.finish(), b"ABC")


# ---------------------------------------------------------------------------
# Page and thumbnails
# ---------------------------------------------------------------------------
class PageTests(TempRoot):
    def setUp(self):
        super().setUp()
        self._swap(fm, "_PAGE_HTML", None)

    def test_placeholders_are_substituted(self):
        page = fm.page_html().decode("utf-8")
        for token in (
            "__ROOT_NAME__",
            "__FEATURES__",
            "__MAKE_POLL_MS__",
            "__DAV_MOUNT__",
            "__DAV_LIMITS__",
        ):
            self.assertNotIn(token, page)
        self.assertIn('"write": true', page.replace("'", '"'))
        self.assertIn('"/dav/"', page)
        self.assertIn('"lockTtl":', page)

    def test_page_is_cached(self):
        self.assertIs(fm.page_html(), fm.page_html())


class ThumbnailTests(TempRoot):
    def test_svg_is_passed_through_without_pillow(self):
        svg = self.write("i/a.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')
        fmt, data = fm.render_thumbnail(svg)
        self.assertEqual(fmt, "svg+xml")
        self.assertTrue(data.startswith(b"<svg"))

    @unittest.skipUnless(HAVE_PILLOW, "Pillow is not installed")
    def test_png_becomes_a_small_jpeg_or_png(self):
        from PIL import Image

        p = self.root / "big.png"
        Image.new("RGB", (900, 700), (10, 20, 30)).save(p)
        fmt, data = fm.render_thumbnail(p)
        self.assertIn(fmt, ("png", "jpeg"))
        with io.BytesIO(data) as fh, Image.open(fh) as im:
            self.assertLessEqual(max(im.size), max(fm.THUMB_SIZE))

    def test_undecodable_image_is_a_client_error(self):
        p = self.write("broken.png", "not really a png")
        if not HAVE_PILLOW:
            self.skipTest("Pillow is not installed")
        with self.assertRaises(fm.HTTPError) as ctx:
            fm.render_thumbnail(p)
        self.assertEqual(ctx.exception.code, 400)


# ---------------------------------------------------------------------------
# HTTP level
# ---------------------------------------------------------------------------
class HttpCase(TempRoot):
    """Runs the real handler on a random port."""

    def setUp(self):
        super().setUp()
        self._swap(fm, "ENABLE_WRITE", True)
        if not os.environ.get("FM_TEST_LOG"):
            # The access log would bury the test output; FM_TEST_LOG=1 brings
            # it back when a test fails on the wire.
            self._swap(fm.Handler, "log_request", _silent)
            self._swap(fm.Handler, "log_message", _silent)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), fm.Handler)
        self.server.daemon_threads = True
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        ).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    # -- helpers -----------------------------------------------------------
    def conn(self) -> http.client.HTTPConnection:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        self.addCleanup(c.close)
        return c

    def call(self, method, path, body=None, headers=None, conn=None):
        c = conn or self.conn()
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data

    def json_call(self, method, path, body=None, **kw):
        headers = {"Content-Type": "application/json"}
        payload = b"" if body is None else json.dumps(body).encode()
        status, _hdrs, raw = self.call(method, path, payload, headers, **kw)
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = raw
        return status, parsed

    def raw(self, request: bytes, close_write: bool = True) -> bytes:
        """Send a hand-written request and return the raw response bytes.

        close_write half-closes the socket after sending: the server answers,
        sees EOF and hangs up, so the read loop ends. Without it a keep-alive
        connection just idles, and this call waits for nothing.
        """
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(request)
            if close_write:
                s.shutdown(socket.SHUT_WR)
            parts = []
            while True:
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    break
                if not chunk:
                    break
                parts.append(chunk)
            return b"".join(parts)

    def post_body(
        self, path: str, body: bytes, piece: int = 0, chunked: bool = False
    ) -> tuple[int, bytes]:
        """POST a body over a raw socket; return the response's status and body.

        `piece` breaks the body into that many bytes per write (or per chunked
        frame, with chunked=True), which is how a base64 payload gets made to
        straddle chunk boundaries. Writing on and reading on the same socket is
        what a real upload does, so a send error is not a verdict: an endpoint
        that refuses a body hangs up while the client is still writing.
        """
        size = piece or len(body) or 1
        head = "\r\n".join(
            [
                f"POST {path} HTTP/1.1",
                "Host: x",
                "Content-Type: application/json",
                "Transfer-Encoding: chunked"
                if chunked
                else f"Content-Length: {len(body)}",
                "",
                "",
            ]
        ).encode()
        parts = [body[start : start + size] for start in range(0, len(body), size)]
        if chunked:
            parts = [b"%X\r\n%s\r\n" % (len(p), p) for p in parts] + [b"0\r\n\r\n"]
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as s:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                s.sendall(head)
            for part in parts:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    s.sendall(part)
            # One response, not "until the socket closes": after an upload that
            # worked the connection is still open and waiting for the next call.
            answer = http.client.HTTPResponse(s)
            answer.begin()
            return answer.status, answer.read()

    def get_json(self, path):
        return self.json_call("GET", path)


class ApiTests(HttpCase):
    def make_tree(self):
        self.write("root.txt", "hello world\n")
        self.write("sub/a.txt", "a" * 100)
        self.write("sub/deep/b.txt", "b" * 200)
        self.write("page.html", "<p>hi</p>")
        (self.root / "real").mkdir()
        self.write("real/inside.txt", "linked content")
        (self.root / "link").symlink_to("real", target_is_directory=True)
        (self.root / "dangling").symlink_to("nowhere")

    # -- listing -----------------------------------------------------------
    def test_list_reports_links_and_totals(self):
        self.make_tree()
        status, d = self.get_json("/api/list?path=")
        self.assertEqual(status, 200)
        self.assertFalse(d["truncated"])
        self.assertEqual(d["total"], len(d["entries"]))
        by_name = {e["name"]: e for e in d["entries"]}
        self.assertEqual(by_name["link"]["kind"], "dir")
        self.assertEqual(by_name["link"]["link"], "real")
        self.assertFalse(by_name["link"]["broken"])
        self.assertTrue(by_name["dangling"]["broken"])
        self.assertIsNone(by_name["root.txt"]["link"])
        self.assertIsNone(d["parent"])
        # Folders sort first.
        self.assertEqual(d["entries"][0]["kind"], "dir")

    def test_list_truncation(self):
        for i in range(6):
            self.write(f"many/f{i}.txt", "x")
        self._swap(fm, "LIST_MAX_ENTRIES", 3)
        _status, d = self.get_json("/api/list?path=many")
        self.assertTrue(d["truncated"])
        self.assertEqual(len(d["entries"]), 3)
        self.assertEqual(d["total"], 6)

    def test_list_of_missing_folder(self):
        status, d = self.get_json("/api/list?path=nope")
        self.assertEqual(status, 404)
        self.assertIn("error", d)

    def test_list_follows_a_symlinked_folder(self):
        self.make_tree()
        status, d = self.get_json("/api/list?path=link")
        self.assertEqual(status, 200)
        self.assertEqual([e["name"] for e in d["entries"]], ["inside.txt"])

    def test_text_of_a_file_with_a_trailing_space(self):
        self.write("trail ", "still readable")
        status, d = self.get_json("/api/text?path=trail%20")
        self.assertEqual(status, 200)
        self.assertEqual(d["content"], "still readable")
        self.assertIsInstance(d["mtime_us"], int)

    # -- symlinks and destructive operations -------------------------------
    def test_deleting_a_folder_symlink_keeps_the_target(self):
        # The regression this guards: deleting "link" used to rmtree "real".
        self.make_tree()
        status, _d = self.json_call("POST", "/api/delete?path=link")
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "link").exists())
        self.assertTrue((self.root / "real" / "inside.txt").is_file())

    def test_deleting_a_dangling_symlink(self):
        self.make_tree()
        status, _d = self.json_call("POST", "/api/delete?path=dangling")
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "dangling").exists())

    def test_deleting_a_symlinked_folder_by_name_still_works(self):
        self.make_tree()
        status, _d = self.json_call("POST", "/api/delete?path=real")
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "real").exists())

    def test_rename_and_move_act_on_the_symlink(self):
        self.make_tree()
        status, d = self.json_call("POST", "/api/rename?path=link", {"name": "renamed"})
        self.assertEqual(status, 200)
        self.assertEqual(d["path"], "renamed")
        self.assertTrue((self.root / "renamed").is_symlink())
        self.assertTrue((self.root / "real").is_dir())
        status, d = self.json_call("POST", "/api/move?path=renamed", {"dest": "sub"})
        self.assertEqual(status, 200)
        self.assertEqual(d["path"], "sub/renamed")
        self.assertTrue((self.root / "sub" / "renamed").is_symlink())

    def test_rename_rejects_a_separator_and_reuses_a_name(self):
        self.write("a.txt", "1")
        self.write("b.txt", "2")
        status, _d = self.json_call("POST", "/api/rename?path=a.txt", {"name": "x/y"})
        self.assertEqual(status, 400)
        status, _d = self.json_call("POST", "/api/rename?path=a.txt", {"name": "b.txt"})
        self.assertEqual(status, 409)

    def test_mkdir_validates_and_reports_conflicts(self):
        self.write("file.txt", "x")
        self.assertEqual(self.json_call("POST", "/api/mkdir?path=new")[0], 200)
        self.assertTrue((self.root / "new").is_dir())
        self.assertEqual(self.json_call("POST", "/api/mkdir?path=new")[0], 409)
        # A file where a parent folder should be is "not found" as far as the
        # endpoint is concerned; a NUL byte used to be an unhandled 500.
        self.assertEqual(self.json_call("POST", "/api/mkdir?path=file.txt/x")[0], 404)
        self.assertEqual(self.json_call("POST", "/api/mkdir?path=a%00b")[0], 400)

    # -- copy --------------------------------------------------------------
    def test_copy_never_overwrites(self):
        self.write("x.txt", "data")
        status, d = self.json_call("POST", "/api/copy?path=x.txt", {"dest": ""})
        self.assertEqual(status, 200)
        self.assertEqual(d["name"], "x (1).txt")
        self.assertEqual((self.root / "x.txt").read_text(), "data")

    def test_copy_of_a_folder(self):
        self.write("src/a.txt", "1")
        self.write("src/sub/b.txt", "2")
        status, _d = self.json_call("POST", "/api/copy?path=src", {"dest": ""})
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "src (1)" / "sub" / "b.txt").read_text(), "2")

    def test_copy_into_itself_is_refused(self):
        (self.root / "src").mkdir()
        self.assertEqual(
            self.json_call("POST", "/api/copy?path=src", {"dest": "src"})[0], 400
        )

    def test_move_and_copy_need_an_existing_folder(self):
        self.write("x.txt", "1")
        for endpoint in ("move", "copy"):
            status, _d = self.json_call(
                "POST", f"/api/{endpoint}?path=x.txt", {"dest": "ghost"}
            )
            self.assertEqual(status, 404, endpoint)

    # -- editor ------------------------------------------------------------
    def test_save_detects_a_stale_buffer(self):
        self.write("doc.md", "original")
        _status, loaded = self.get_json("/api/text?path=doc.md")
        # Someone else changes the file (a make, a second tab, an editor).
        (self.root / "doc.md").write_text("changed outside")
        status, d = self.json_call(
            "POST",
            "/api/save?path=doc.md",
            {"content": "my edit", "if_mtime_us": loaded["mtime_us"] - 7},
        )
        self.assertEqual(status, 409)
        self.assertEqual((self.root / "doc.md").read_text(), "changed outside")
        # With the mtime from before the outside change, saving is refused;
        # reloading and saving again wins.
        _status, fresh = self.get_json("/api/text?path=doc.md")
        status, d = self.json_call(
            "POST",
            "/api/save?path=doc.md",
            {"content": "my edit", "if_mtime_us": fresh["mtime_us"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "doc.md").read_text(), "my edit")
        self.assertEqual(d["size"], len("my edit"))

    def test_save_keeps_the_executable_bit(self):
        p = self.write("script.sh", "#!/bin/sh\necho hi\n")
        p.chmod(0o755)
        status, _d = self.json_call(
            "POST", "/api/save?path=script.sh", {"content": "x"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(os.access(p, os.X_OK))
        self.assertFalse(list(self.root.glob("*.saving-*")))  # temp file cleaned up

    def test_save_line_endings(self):
        self.write("crlf.txt", "a\r\nb\r\n")
        _status, d = self.get_json("/api/text?path=crlf.txt")
        self.assertEqual(d["newline"], "crlf")
        status, _d = self.json_call(
            "POST", "/api/save?path=crlf.txt", {"content": "a\nb", "newline": "crlf"}
        )
        self.assertEqual(status, 200)
        # The textarea hands over "a\nb" with no trailing break: only the line
        # endings that are there get converted, none are invented.
        self.assertEqual((self.root / "crlf.txt").read_bytes(), b"a\r\nb")

    # -- uploads -----------------------------------------------------------
    def test_chunked_upload_is_not_empty(self):
        # A chunked body used to be read as zero bytes: the upload "succeeded"
        # with an empty file.
        request = (
            b"POST /api/upload?path=&filename=chunked.bin HTTP/1.1\r\n"
            b"Host: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\n12345\r\n5\r\n54321\r\n0\r\n\r\n"
        )
        response = self.raw(request)
        self.assertIn(b"200 OK", response.split(b"\r\n", 1)[0])
        self.assertEqual((self.root / "chunked.bin").read_bytes(), b"1234554321")

    def test_short_body_is_an_error_not_a_truncated_success(self):
        # Content-Length promises 100 bytes, the client sends 5 and stops.
        request = (
            b"POST /api/upload?path=&filename=short.bin HTTP/1.1\r\n"
            b"Host: x\r\nContent-Length: 100\r\n\r\n12345"
        )
        response = self.raw(request, close_write=True)  # the client stops early
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertFalse((self.root / "short.bin").exists())

    def test_upload_reuses_a_name_instead_of_overwriting(self):
        self.write("dup.txt", "old")
        status, _h, raw = self.call(
            "POST", "/api/upload?path=&filename=dup.txt", b"new"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["name"], "dup (1).txt")
        self.assertEqual((self.root / "dup.txt").read_text(), "old")
        self.assertEqual((self.root / "dup (1).txt").read_bytes(), b"new")

    def test_upload64_roundtrip(self):
        status, _d = self.json_call(
            "POST", "/api/upload64?path=&filename=b64.bin", {"data": "aGVsbG8="}
        )
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "b64.bin").read_bytes(), b"hello")
        status, got = self.json_call("GET", "/api/download64?path=b64.bin")
        self.assertEqual(status, 200)
        self.assertEqual(got["data"], "aGVsbG8=")

    def test_upload64_rejects_garbage(self):
        status, _d = self.json_call(
            "POST", "/api/upload64?path=&filename=x.bin", {"data": "not base64!!"}
        )
        self.assertEqual(status, 400)

    def test_upload64_of_a_file_larger_than_the_read_chunks(self):
        # Three megabytes of noise in 997-byte writes: groups of four base64
        # characters straddle the chunk joins all the way through, which is
        # where a streaming decoder loses or duplicates a byte.
        blob = os.urandom(3 * 1024 * 1024 + 17)
        body = json.dumps({"path": "", "data": base64.b64encode(blob).decode()})
        status, _body = self.post_body(
            "/api/upload64?path=&filename=noisy.bin", body.encode(), piece=997
        )
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "noisy.bin").read_bytes(), blob)
        # And the other way round, in one JSON document.
        status, got = self.json_call("GET", "/api/download64?path=noisy.bin")
        self.assertEqual(status, 200)
        self.assertEqual(got["size"], len(blob))
        self.assertEqual(base64.b64decode(got["data"]), blob)

    def test_upload64_of_a_chunked_body(self):
        # A browser posting a Blob without Content-Length gets chunked transfer,
        # so the payload arrives with no size promised up front.
        blob = os.urandom(256 * 1024 + 3)
        body = json.dumps({"data": base64.b64encode(blob).decode()}).encode()
        status, _body = self.post_body(
            "/api/upload64?path=&filename=chunky.bin", body, piece=1009, chunked=True
        )
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "chunky.bin").read_bytes(), blob)

    def test_keep_alive_survives_base64_uploads(self):
        c = self.conn()
        for name in ("one", "two"):
            status, _d = self.json_call(
                "POST",
                f"/api/upload64?path=&filename={name}.bin",
                {"data": base64.b64encode(name.encode()).decode()},
                conn=c,
            )
            self.assertEqual(status, 200, name)
        self.assertEqual(self.call("GET", "/health", conn=c)[0], 200)
        self.assertEqual((self.root / "two.bin").read_bytes(), b"two")

    # -- archives ----------------------------------------------------------
    def test_extract_defaults_to_the_zips_folder(self):
        zp = self.root / "pkg.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("in.txt", "content")
        status, _d = self.json_call("POST", "/api/extract?path=pkg.zip", {})
        self.assertEqual(status, 200)
        self.assertEqual((self.root / "in.txt").read_text(), "content")

    def test_extract_rejects_a_file_as_destination(self):
        # The front-end used to send the zip's own name here because dirname()
        # was wrong for a file in the root.
        self.write("pkg.zip", "not really a zip")
        self.write("other.txt", "x")
        status, _d = self.json_call(
            "POST", "/api/extract?path=pkg.zip", {"dest": "other.txt"}
        )
        self.assertEqual(status, 404)

    def test_zip_download_wraps_everything_in_one_folder(self):
        self.write("dir/a.txt", "a")
        self.write("dir/empty/x/keep.txt", "k")
        status, hdrs, body = self.call("GET", "/api/zip?path=dir")
        self.assertEqual(status, 200)
        self.assertIn("attachment", hdrs["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
        self.assertIn("dir/a.txt", names)
        self.assertIn("dir/empty/x/keep.txt", names)

    def test_zip_of_empty_folder_is_a_valid_archive(self):
        (self.root / "void").mkdir()
        status, _h, body = self.call("GET", "/api/zip?path=void")
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertEqual(zf.namelist(), ["void/"])

    # -- ranges, HEAD, keep-alive -----------------------------------------
    def test_head_requests_are_answered(self):
        self.write("f.txt", "hello")
        status, hdrs, body = self.call("HEAD", "/api/raw?path=f.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(hdrs["Content-Length"], "5")
        self.assertEqual(hdrs["Accept-Ranges"], "bytes")
        self.assertEqual(self.call("HEAD", "/health")[0], 200)
        self.assertEqual(self.call("HEAD", "/")[0], 200)
        self.assertEqual(self.call("HEAD", "/api/nope")[0], 404)

    def test_range_requests(self):
        self.write("f.txt", "0123456789")
        status, hdrs, body = self.call(
            "GET", "/api/raw?path=f.txt", headers={"Range": "bytes=2-4"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, b"234")
        self.assertEqual(hdrs["Content-Range"], "bytes 2-4/10")
        self.assertEqual(hdrs["Content-Length"], "3")
        # A multi-range request is answered with the whole file, which is legal.
        status, _hdrs, body = self.call(
            "GET", "/api/raw?path=f.txt", headers={"Range": "bytes=0-1,4-5"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"0123456789")

    def test_keep_alive_survives_errors_and_unread_bodies(self):
        c = self.conn()
        self.assertEqual(self.call("GET", "/api/nope", conn=c)[0], 404)
        self.assertEqual(self.call("GET", "/health", conn=c)[0], 200)
        # A body nobody reads (mkdir takes its name from the query string).
        self.assertEqual(
            self.call("POST", "/api/mkdir?path=lefty", b"{ ignored body }", conn=c)[0],
            200,
        )
        status, _h, body = self.call("GET", "/health", conn=c)
        self.assertEqual(status, 200, body)
        self.assertIn(b'"ok"', body if isinstance(body, bytes) else b"")

    def test_a_quiet_keep_alive_connection_is_closed_without_a_fuss(self):
        # A client that keeps the connection open and then says nothing for
        # longer than the socket timeout has not failed a request: a mounted
        # davfs2 does exactly that between two refreshes. The socket is closed
        # and the log stays clean.
        lines = []
        self._swap(fm.Handler, "timeout", 0.2)
        self._swap(fm.Handler, "log_message", lambda self, *a: lines.append(a))
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as s:
            self.assertEqual(s.recv(1024), b"")  # closed, and nothing answered
        self.assertEqual(lines, [])

    def test_a_stalled_request_is_still_reported(self):
        # Same timeout, but this client got as far as a request line: waiting
        # for the rest of it is a stalled request, and that is worth a log line.
        lines = []
        self._swap(fm.Handler, "timeout", 0.2)
        self._swap(fm.Handler, "log_message", lambda self, *a: lines.append(a))
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as s:
            s.sendall(b"GET /health HTTP/1.1\r\n")
            self.assertEqual(s.recv(1024), b"")
        self.assertEqual(len(lines), 1)
        self.assertIn("timed out", lines[0][0] % lines[0][1:])

    def test_health_endpoint(self):
        status, _hdrs, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["version"], fm.VERSION)
        self.assertFalse(doc["read_only"])

    def test_unknown_paths_and_methods(self):
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("POST", "/api/nope")[0], 404)
        self.assertEqual(self.json_call("POST", "/api/save?path=x", [1, 2])[0], 400)

    def test_invalid_json_body(self):
        self.write("a.txt", "x")
        # A missing body is treated as an empty object, so a rename without a
        # name is still a client error rather than a rename to "".
        self.assertEqual(self.json_call("POST", "/api/rename?path=a.txt", None)[0], 400)
        self.assertEqual(
            self.call(
                "POST",
                "/api/rename?path=a.txt",
                b"nope",
                {"Content-Type": "application/json"},
            )[0],
            400,
        )
        # The name still exists, untouched.
        self.assertEqual(self.get_json("/api/text?path=a.txt")[0], 200)

    # -- feature switches --------------------------------------------------
    def test_read_only_blocks_every_change_but_not_browsing(self):
        self.write("keep.txt", "stays")
        self._swap(fm, "ENABLE_WRITE", False)
        for path in (
            "/api/mkdir?path=new",
            "/api/newfile?path=new.txt",
            "/api/delete?path=keep.txt",
            "/api/save?path=keep.txt",
            "/api/move?path=keep.txt",
            "/api/copy?path=keep.txt",
            "/api/extract?path=keep.txt",
            "/api/upload?path=&filename=x",
            "/api/upload64?path=&filename=x",
            "/api/run?path=keep.txt",
            "/api/make",
            "/api/shell",
        ):
            status, d = self.json_call("POST", path, {})
            self.assertEqual(status, 403, path)
            self.assertIn("read-only", d["error"])
        self.assertEqual((self.root / "keep.txt").read_text(), "stays")
        self.assertEqual(self.get_json("/api/list?path=")[0], 200)
        self.assertEqual(self.call("GET", "/api/raw?path=keep.txt")[0], 200)
        self.assertIn('"read_only": true', self.call("GET", "/health")[2].decode())

    def test_disabled_features_report_why(self):
        self.write("x.bin", "data")
        self._swap(fm, "ENABLE_B64", False)
        status, d = self.get_json("/api/download64?path=x.bin")
        self.assertEqual(status, 403)
        self.assertIn("--no-b64", d["error"])
        status, d = self.json_call(
            "POST", "/api/upload64?path=&filename=y", {"data": "AA=="}
        )
        self.assertEqual(status, 403)

    def test_shell_runs_in_a_folder_and_reports_the_exit_code(self):
        (self.root / "cwdtest").mkdir()
        status, d = self.json_call(
            "POST",
            "/api/shell",
            {"command": "pwd && echo boom >&2; exit 3", "cwd": "cwdtest"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(d["code"], 3)
        self.assertIn(str(self.root / "cwdtest"), d["output"])
        self.assertEqual(
            self.json_call("POST", "/api/shell", {"command": "  "})[0], 400
        )
        self.assertEqual(
            self.json_call("POST", "/api/shell", {"command": "x", "cwd": "ghost"})[0],
            404,
        )

    def test_run_refuses_non_executables(self):
        self.write("plain.sh", "echo hi")
        self.assertEqual(self.json_call("POST", "/api/run?path=plain.sh", {})[0], 403)
        p = self.write("yes.sh", "#!/bin/sh\necho ran\n")
        p.chmod(0o755)
        status, d = self.json_call("POST", "/api/run?path=yes.sh", {})
        self.assertEqual(status, 200)
        self.assertEqual(d["code"], 0)
        self.assertIn("ran", d["output"])

    # -- folder sizes ------------------------------------------------------
    def test_foldersize_deduplicates_paths(self):
        self.write("big/f.txt", "x")
        calls = []
        original = fm.dir_size

        def counting(*a, **kw):
            calls.append(a)
            return original(*a, **kw)

        self._swap(fm, "dir_size", counting)
        fm.DIR_SIZES.invalidate(str(self.root))
        status, d = self.json_call("POST", "/api/foldersize", {"paths": ["big"] * 1000})
        self.assertEqual(status, 200)
        self.assertEqual(d["sizes"], {"big": 1})
        self.assertEqual(len(calls), 1)  # one walk, not one thousand
        # The second request is served from the cache.
        status, d = self.json_call("POST", "/api/foldersize", {"paths": ["big"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 1)

    def test_foldersize_rejects_an_over_grown_request(self):
        self.assertEqual(
            self.json_call("POST", "/api/foldersize", {"paths": ["x"] * 1001})[0], 400
        )
        self.assertEqual(
            self.json_call("POST", "/api/foldersize", {"paths": "nope"})[0], 400
        )


class MakeJobTests(HttpCase):
    def test_job_is_reported_once_done(self):
        calls = []

        def fake(dirp):
            calls.append(dirp)
            return {
                "code": 0,
                "timed_out": False,
                "truncated": False,
                "output": "built",
                "cwd": str(dirp),
            }

        self._swap(fm, "run_make_if_present", fake)
        (self.root / "Makefile").write_text("all:\n\ttrue\n")
        job = fm.MAKE_JOBS.submit(self.root)
        _status, d = self.get_json("/api/make/result?job=" + job)
        deadline = 5.0
        import time

        start = time.time()
        while not d.get("done") and time.time() - start < deadline:
            time.sleep(0.02)
            _status, d = self.get_json("/api/make/result?job=" + job)
        self.assertTrue(d.get("done"), d)
        self.assertEqual(d["result"]["output"], "built")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.get_json("/api/make/result?job=nope")[0], 404)

    def test_manual_make_needs_a_makefile(self):
        status, d = self.json_call("POST", "/api/make", {"path": ""})
        self.assertEqual(status, 400)
        self.assertIn("Makefile", d["error"])

    def test_make_uses_the_configured_program(self):
        # --make-cmd is what gets executed, not a hard-wired "make": `false`
        # stands in for a build tool that is unhappy.
        self._swap(fm, "MAKE_CMD", "false")
        (self.root / "Makefile").write_text("all:\n\ttrue\n")
        status, d = self.json_call("POST", "/api/make", {"path": ""})
        self.assertEqual(status, 200)
        self.assertEqual(d["code"], 1)
        self.assertFalse(d["timed_out"])

    def test_shell_uses_the_configured_program(self):
        self._swap(fm, "SHELL_CMD", "/definitely/not/a/shell")
        status, d = self.json_call("POST", "/api/shell", {"command": "echo hi"})
        self.assertEqual(status, 400)
        self.assertIn("Could not start process", d["error"])


class BodyLimitTests(HttpCase):
    def test_oversized_json_body_is_refused(self):
        self._swap(fm, "MAX_JSON_BODY_SIZE", 64)
        self.write("a.txt", "x")
        status = self.call(
            "POST",
            "/api/rename?path=a.txt",
            b'{"name":"' + b"x" * 200 + b'"}',
            {"Content-Type": "application/json"},
        )[0]
        self.assertEqual(status, 413)
        # The connection is dropped rather than left with unread body bytes.
        self.assertEqual(self.call("GET", "/health", conn=self.conn())[0], 200)

    def test_oversized_base64_body_is_refused_before_it_is_read(self):
        # The point of the body limit is that the server does not swallow 2 GB
        # before saying no, so the answer goes out while the client is writing.
        self._swap(fm, "MAX_B64_BODY_SIZE", 4096)
        status, body = self.post_body(
            "/api/upload64?path=&filename=big.bin",
            json.dumps({"data": "QUJD" * 100_000}).encode(),
        )
        self.assertEqual(status, 413)
        self.assertIn(b"use the regular upload", body)
        self.assertFalse((self.root / "big.bin").exists())

    def test_base64_upload_stops_at_the_decoded_limit(self):
        # A chunked body promises no size, so the limit is caught mid-transfer.
        # The half-written file has to go with the refusal.
        self._swap(fm, "MAX_B64_FILE_SIZE", 1024)
        payload = base64.b64encode(os.urandom(4096)).decode()
        status, body = self.post_body(
            "/api/upload64?path=&filename=grown.bin",
            json.dumps({"data": payload}).encode(),
            chunked=True,
        )
        self.assertEqual(status, 413)
        self.assertIn(b"too large", body)
        self.assertFalse((self.root / "grown.bin").exists())

    def test_stalled_body_does_not_hang_the_server(self):
        # Half a body, then silence: the idle timeout must answer 408 and free
        # the thread, not wait forever.
        old = fm.Handler.timeout
        fm.Handler.timeout = 1
        self.addCleanup(setattr, fm.Handler, "timeout", old)
        request = (
            b"POST /api/save?path=x.txt HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"
        )
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(request)
            s.settimeout(8)
            answer = s.recv(4096)
        self.assertIn(b"408", answer.split(b"\r\n", 1)[0])


# ---------------------------------------------------------------------------
# WebDAV
# ---------------------------------------------------------------------------
DAV = "{DAV:}"


class DavHelpers(unittest.TestCase):
    """The pure parts of the WebDAV layer, without a socket."""

    def test_href_marks_collections_with_a_trailing_slash(self):
        self.assertEqual(fm.dav_href("", True), "/dav/")
        self.assertEqual(fm.dav_href("a b/c.txt", False), "/dav/a%20b/c.txt")
        self.assertEqual(fm.dav_href("sub", True), "/dav/sub/")

    def test_etag_is_strong_and_changes_with_content(self):
        class St:
            st_mtime_ns = 1_700_000_000_000_000_000
            st_size = 10

        tag = fm.dav_etag(St)
        self.assertTrue(tag.startswith('"') and tag.endswith('"'))
        self.assertFalse(tag.startswith('W/"'))
        St.st_size = 11
        self.assertNotEqual(fm.dav_etag(St), tag)

    def test_conditionals_separate_tokens_from_entity_tags(self):
        tokens, etags = fm.dav_conditionals(
            '(<opaquelocktoken:abc>) (["etag"] "other") <http://h/dav/x>'
        )
        self.assertEqual(tokens, {"opaquelocktoken:abc"})
        self.assertEqual(etags, {'"etag"', '"other"'})
        self.assertEqual(fm.dav_conditionals(None), (set(), set()))

    def test_etag_list_ignores_the_weak_indicator(self):
        # davfs2 echoes back the tag it was given, weak or not (drop_weak_etags
        # is on by default, but a proxy in front may not strip it).
        self.assertTrue(fm.dav_etag_listed('W/"abc"', '"abc"'))
        self.assertTrue(fm.dav_etag_listed("*", None))
        self.assertFalse(fm.dav_etag_listed('"abc"', '"abd"'))
        self.assertFalse(fm.dav_etag_listed(None, '"abc"'))

    def test_requested_props_treats_empty_body_as_allprop(self):
        self.assertEqual(fm.dav_requested_props(b""), (True, set()))
        alls = b'<D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>'
        self.assertEqual(fm.dav_requested_props(alls), (True, set()))
        named = (
            b'<D:propfind xmlns:D="DAV:"><D:prop><D:getetag/>'
            b'<x:exec xmlns:x="urn:fm"/></D:prop></D:propfind>'
        )
        self.assertEqual(
            fm.dav_requested_props(named), (False, {"{DAV:}getetag", "{urn:fm}exec"})
        )

    def test_patch_tags_pairs_each_property_with_its_value(self):
        body = (
            b'<D:propertyupdate xmlns:D="DAV:" xmlns:fm="urn:fm"><D:set><D:prop>'
            b"<D:executable/><fm:color>  green  </fm:color></D:prop></D:set>"
            b"<D:remove><D:prop><D:executable>F</D:executable></D:prop></D:remove>"
            b"</D:propertyupdate>"
        )
        sets, removes = fm.dav_patch_tags(body)
        self.assertEqual(sets, [("{DAV:}executable", ""), ("{urn:fm}color", "green")])
        self.assertEqual(removes, [("{DAV:}executable", "f")])
        self.assertEqual(fm.dav_patch_tags(b"   "), ([], []))

    def test_lock_info_reads_scope_type_and_owner(self):
        body = (
            b'<D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
            b"<D:locktype><D:write/></D:locktype><D:owner><div>me@host</div>"
            b"</D:owner></D:lockinfo>"
        )
        self.assertEqual(fm.dav_lock_info(body), ("exclusive", "write", "me@host"))

    def test_lock_table_honours_timeouts_and_expiry(self):
        locks = fm._Locks(ttl=1800, max_entries=10)
        self.assertEqual(locks.timeout("Second-120"), 120)
        self.assertEqual(locks.timeout("Second-99999999"), fm.DAV_LOCK_MAX_TIMEOUT)
        self.assertEqual(locks.timeout("Second-instant"), 1800)  # unparsable
        self.assertEqual(locks.timeout("Infinity"), 1800)
        self.assertEqual(locks.timeout(None), 1800)
        record = locks.add("/r/f", "/dav/f", "0", "me", 1)
        self.assertEqual(locks.count(), 1)
        locks._by_token[record["token"]]["expires"] = time.monotonic() - 1
        self.assertIsNone(locks.holder("/r/f"))
        self.assertEqual(locks.count(), 0)

    def test_infinite_lock_covers_everything_below(self):
        locks = fm._Locks(ttl=60, max_entries=10)
        deep = locks.add("/r/dir", "/dav/dir", "infinity", "me", 60)
        self.assertIsNotNone(locks.blocking("/r/dir/a/b.txt", set()))
        self.assertIsNone(locks.blocking("/r/dir/a/b.txt", {deep["token"]}))
        self.assertIsNone(locks.blocking("/r/other.txt", set()))
        self.assertIsNotNone(locks.blocking("/r/dir", set()))

    def test_unlocking_a_child_frees_an_inherited_tree_lock(self):
        locks = fm._Locks(ttl=60, max_entries=10)
        locks.add("/r/dir", "/dav/dir", "infinity", "me", 60)
        locks.add("/r/other", "/dav/other", "0", "me", 60)
        self.assertEqual(locks.drop_blocking("/r/dir/deep/f.txt"), 1)
        self.assertIsNone(locks.blocking("/r/dir/deep/f.txt", set()))
        self.assertIsNotNone(locks.blocking("/r/other", set()))  # untouched

    def test_flat_lock_covers_only_its_own_path(self):
        locks = fm._Locks(ttl=60, max_entries=10)
        locks.add("/r/dir", "/dav/dir", "0", "me", 60)
        self.assertIsNotNone(locks.blocking("/r/dir", set()))
        self.assertIsNone(locks.blocking("/r/dir/a.txt", set()))

    def test_move_repoints_locks_inside_a_moved_folder(self):
        locks = fm._Locks(ttl=60, max_entries=10)
        locks.add("/r/dir", "/dav/dir", "infinity", "me", 60)
        locks.add("/r/other", "/dav/other", "0", "me", 60)
        locks.move("/r/dir", "/r/new/dir")
        self.assertIsNone(locks.blocking("/r/dir/a.txt", set()))
        self.assertIsNotNone(locks.blocking("/r/new/dir/a.txt", set()))
        self.assertIsNotNone(locks.blocking("/r/other", set()))

    def test_table_is_bounded(self):
        locks = fm._Locks(ttl=600, max_entries=3)
        for i in range(10):
            locks.add(f"/r/f{i}", f"/dav/f{i}", "0", "me", 600)
        self.assertEqual(locks.count(), 3)


class DavTests(HttpCase):
    """The mount on the wire, the way davfs2 talks to it."""

    PROP_ALL = b'<D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>'

    def setUp(self):
        super().setUp()
        # One lock table per test: the module's is shared by the whole run.
        self._swap(fm, "LOCKS", fm._Locks(ttl=fm.DAV_LOCK_TTL, max_entries=64))
        self.write("f.txt", "hello")
        self.write("dir/inner.txt", "inside")
        (self.root / "x").symlink_to("nowhere")

    # -- helpers -----------------------------------------------------------
    def propfind(self, rel="", depth="infinity", body=None, **kw):
        headers = {"Depth": depth}
        headers.update(kw.pop("headers", {}))
        return self.call(
            "PROPFIND", f"/dav/{rel}", b"" if body is None else body, headers
        )

    def hrefs(self, body: bytes) -> list[str]:
        root = ET.fromstring(body)
        return [node.text for node in root.findall(f"{DAV}response/{DAV}href")]

    def props_of(self, body: bytes, href: str) -> dict:
        """{property name: text} for one <D:response>, "" for empty elements."""
        root = ET.fromstring(body)
        for resp in root.findall(f"{DAV}response"):
            if resp.find(f"{DAV}href").text != href:
                continue
            out = {}
            for prop in resp.findall(f"{DAV}propstat/{DAV}prop"):
                for child in prop:
                    out[child.tag.replace(DAV, "")] = (child.text or "").strip()
            return out
        raise AssertionError(f"{href!r} not in the reply")

    def lock(self, rel="", owner="test@host", token=None, timeout=None, depth=None):
        headers = {
            "Content-Type": "application/xml",
            "Timeout": timeout or "Second-600",
        }
        if depth:
            headers["Depth"] = depth
        if token:
            headers["If"] = f"(<{token}>)"
        body = (
            f'<D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
            f"<D:locktype><D:write/></D:locktype>"
            f"<D:owner><D:href>{owner}</D:href></D:owner></D:lockinfo>"
        ).encode()
        return self.call("LOCK", f"/dav/{rel}", body, headers)

    def patch(self, rel, body, **kw):
        headers = {"Content-Type": "application/xml"}
        headers.update(kw.pop("headers", {}))
        return self.call("PROPPATCH", f"/dav/{rel}", body, headers)

    def prop_status(self, body: bytes, href: str) -> dict:
        """{property name: propstat status} for one <D:response>.

        The status is what a PROPPATCH got accepted for and what a PROPFIND
        does not have, so the two together are the answer, not just the part
        of it that fits in a 200.
        """
        root = ET.fromstring(body)
        for resp in root.findall(f"{DAV}response"):
            if resp.find(f"{DAV}href").text != href:
                continue
            out = {}
            for stat_node in resp.findall(f"{DAV}propstat"):
                code = int(stat_node.find(f"{DAV}status").text.split()[1])
                for prop in stat_node.findall(f"{DAV}prop"):
                    for child in prop:
                        out[child.tag.replace(DAV, "")] = code
            return out
        raise AssertionError(f"{href!r} not in the reply")

    # -- discovery ---------------------------------------------------------
    def test_options_advertises_class_2(self):
        status, hdrs, body = self.call("OPTIONS", "/dav/")
        self.assertEqual(status, 204)
        self.assertEqual(hdrs["DAV"], "1, 2")  # class 2: it has locks
        for verb in ("PROPFIND", "MKCOL", "LOCK", "UNLOCK", "MOVE", "COPY"):
            self.assertIn(verb, hdrs["Allow"])
        self.assertEqual(body, b"")

    def test_dav_verbs_outside_the_mount_are_405(self):
        for path in ("/api/list", "/f.txt", "/"):
            status, hdrs, _body = self.call("PROPFIND", path)
            self.assertEqual(status, 405, path)
            self.assertIn("GET", hdrs["Allow"])
        status, _h, body = self.call("PUT", "/api/save")
        self.assertEqual(status, 405)
        self.assertIn("WebDAV", json.loads(body)["error"])

    def test_mount_root_and_named_children_are_reported(self):
        status, hdrs, body = self.propfind("", body=self.PROP_ALL)
        self.assertEqual(status, 207)
        self.assertIn("xml", hdrs["Content-Type"])
        self.assertIn(b"<D:multistatus", body)
        self.assertEqual(
            sorted(self.hrefs(body)),
            sorted(["/dav/", "/dav/dir/", "/dav/f.txt", "/dav/x"]),
        )
        file_props = self.props_of(body, "/dav/f.txt")
        self.assertEqual(file_props["getcontentlength"], "5")
        self.assertEqual(
            file_props["getetag"], fm.dav_etag((self.root / "f.txt").stat())
        )
        self.assertIn("text/plain", file_props["getcontenttype"])
        self.assertTrue(file_props["getlastmodified"].endswith("GMT"))
        # A collection is a collection: without resourcetype davfs2 shows the
        # folder as a zero-length file.
        self.assertEqual(self.props_of(body, "/dav/dir/").get("resourcetype"), "")
        self.assertNotIn("getcontentlength", self.props_of(body, "/dav/dir/"))
        self.assertIn("supportedlock", self.props_of(body, "/dav/"))

    def test_depth_0_and_infinity(self):
        _s, _h, body = self.propfind("", depth="0", body=self.PROP_ALL)
        self.assertEqual(self.hrefs(body), ["/dav/"])
        _s, _h, body = self.propfind("dir", body=self.PROP_ALL)
        self.assertEqual(self.hrefs(body), ["/dav/dir/", "/dav/dir/inner.txt"])

    def test_named_propset_answers_known_and_refuses_unknown(self):
        body = (
            b'<D:propfind xmlns:D="DAV:" xmlns:fm="urn:fm">'
            b"<D:prop><D:getetag/><fm:exec/></D:prop></D:propfind>"
        )
        _s, _h, raw = self.propfind("f.txt", depth="0", body=body)
        doc = ET.fromstring(raw)
        statuses = [
            (next(iter(ps.find(f"{DAV}prop"))).tag, ps.find(f"{DAV}status").text)
            for ps in doc.find(f"{DAV}response").findall(f"{DAV}propstat")
        ]
        self.assertEqual(statuses[0][1], "HTTP/1.1 200 OK")
        self.assertEqual(statuses[1], ("{urn:fm}exec", "HTTP/1.1 404 Not Found"))

    def test_propfind_on_missing_or_weird_paths(self):
        self.assertEqual(self.propfind("nope")[0], 404)
        self.assertEqual(self.propfind("a/../../etc")[0], 404)
        # A broken symlink has to be reported as something, or the whole folder
        # fails to list: davfs2 has no links, so it becomes an odd file.
        _s, _h, body = self.propfind("", body=self.PROP_ALL)
        self.assertIn("/dav/x", self.hrefs(body))
        self.assertEqual(self.props_of(body, "/dav/x").get("resourcetype"), "")

    def test_proppatch_refuses_to_store_a_property(self):
        status, _h, body = self.call(
            "PROPPATCH",
            "/dav/f.txt",
            b'<D:propertyupdate xmlns:D="DAV:" xmlns:fm="urn:fm"><D:set>'
            b"<D:prop><fm:exec>1</fm:exec></D:prop></D:set></D:propertyupdate>",
            {"Content-Type": "application/xml"},
        )
        self.assertEqual(status, 207)
        doc = ET.fromstring(body)
        propstat = doc.find(f".//{DAV}propstat")
        self.assertEqual(propstat.find(f"{DAV}status").text, "HTTP/1.1 403 Forbidden")

    # -- the executable bit -------------------------------------------------
    def test_propfind_reports_the_exec_bit_of_a_file(self):
        # davfs2 has no other way to learn that a file on the mount may be run.
        (self.root / "f.txt").chmod(0o755)
        alls = self.propfind("", depth="1")[2]
        self.assertIn("executable", self.props_of(alls, "/dav/f.txt"))
        # A folder does not carry it, and neither does a plain file.
        self.assertNotIn("executable", self.props_of(alls, "/dav/dir/"))
        inner = self.propfind("dir", depth="1")[2]
        self.assertNotIn("executable", self.props_of(inner, "/dav/dir/inner.txt"))

    def test_a_requested_exec_bit_a_plain_file_lacks_is_reported_as_absent(self):
        # RFC 3253: the property means "true" by being there, so "false" is a
        # property that is not there - which is a 404 propstat, not a 200.
        body = (
            b'<D:propfind xmlns:D="DAV:"><D:prop><D:executable/></D:prop></D:propfind>'
        )
        status, _h, reply = self.propfind("f.txt", depth="0", body=body)
        self.assertEqual(status, 207)
        self.assertEqual(self.prop_status(reply, "/dav/f.txt"), {"executable": 404})
        (self.root / "f.txt").chmod(0o700)
        status, _h, reply = self.propfind("f.txt", depth="0", body=body)
        self.assertEqual(self.prop_status(reply, "/dav/f.txt"), {"executable": 200})

    def test_proppatch_writes_the_exec_bit_back_to_the_file(self):
        sets = (
            b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            b"<D:executable/></D:prop></D:set></D:propertyupdate>"
        )
        status, _h, body = self.patch("f.txt", sets)
        self.assertEqual(status, 207)
        self.assertEqual(self.prop_status(body, "/dav/f.txt"), {"executable": 200})
        self.assertTrue((self.root / "f.txt").stat().st_mode & 0o111)
        # The PROPFIND now says what the mount will show.
        props = self.props_of(
            self.propfind("f.txt", depth="0", body=self.PROP_ALL)[2], "/dav/f.txt"
        )
        self.assertIn("executable", props)
        removes = (
            b'<D:propertyupdate xmlns:D="DAV:"><D:remove><D:prop>'
            b"<D:executable/></D:prop></D:remove></D:propertyupdate>"
        )
        status, _h, body = self.patch("f.txt", removes)
        self.assertEqual(self.prop_status(body, "/dav/f.txt"), {"executable": 200})
        self.assertFalse((self.root / "f.txt").stat().st_mode & 0o111)

    def test_proppatch_reads_a_spelled_out_false(self):
        (self.root / "f.txt").chmod(0o755)
        body = (
            b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            b"<D:executable>F</D:executable></D:prop></D:set></D:propertyupdate>"
        )
        _status, _h, reply = self.patch("f.txt", body)
        self.assertEqual(self.prop_status(reply, "/dav/f.txt"), {"executable": 200})
        self.assertFalse((self.root / "f.txt").stat().st_mode & 0o111)

    def test_proppatch_keeps_the_exec_bit_and_refuses_its_neighbours(self):
        # One request can ask for both: what has somewhere to live is stored,
        # what has not is refused, and neither answer is a lie about the other.
        body = (
            b'<D:propertyupdate xmlns:D="DAV:" xmlns:fm="urn:fm"><D:set><D:prop>'
            b"<D:executable/><fm:color>green</fm:color></D:prop></D:set>"
            b"</D:propertyupdate>"
        )
        status, _h, reply = self.patch("f.txt", body)
        self.assertEqual(status, 207)
        got = self.prop_status(reply, "/dav/f.txt")
        self.assertEqual(got["executable"], 200)
        self.assertEqual(got["{urn:fm}color"], 403)
        self.assertTrue((self.root / "f.txt").stat().st_mode & 0o111)

    def test_proppatch_leaves_the_exec_bit_of_a_folder_alone(self):
        # A folder is always traversable in a mount (dir_mode decides that), so
        # there is nothing to store; saying 403 is the honest answer.
        body = (
            b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            b"<D:executable/></D:prop></D:set></D:propertyupdate>"
        )
        status, _h, reply = self.patch("dir", body)
        self.assertEqual(status, 207)
        self.assertEqual(self.prop_status(reply, "/dav/dir/"), {"executable": 403})

    def test_proppatch_needs_the_lock_covering_the_file_it_writes(self):
        _status, _h, reply = self.lock("f.txt", owner="browser")
        token = re.search(rb"opaquelocktoken:[0-9a-f-]+", reply).group(0).decode()
        body = (
            b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            b"<D:executable/></D:prop></D:set></D:propertyupdate>"
        )
        self.assertEqual(self.patch("f.txt", body)[0], 423)
        # The owner of the lock may change it, and presents it in If:.
        status = self.patch("f.txt", body, headers={"If": f"(<{token}>)"})[0]
        self.assertEqual(status, 207)
        self.assertTrue((self.root / "f.txt").stat().st_mode & 0o111)

    def test_read_only_mode_refuses_the_exec_bit(self):
        self._swap(fm, "ENABLE_WRITE", False)
        body = (
            b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            b"<D:executable/></D:prop></D:set></D:propertyupdate>"
        )
        self.assertEqual(self.patch("f.txt", body)[0], 403)
        self.assertFalse((self.root / "f.txt").stat().st_mode & 0o111)

    # Byte for byte what davfs2/1.7.3 sends, read off the wire: it asks for the
    # Apache spelling of the executable bit and hands back "T". Answering only
    # the RFC 3253 one leaves a mount unable to tell a script from a text file.
    DAVFS_PROPFIND = (
        b'<?xml version="1.0" encoding="utf-8"?>\n<propfind xmlns="DAV:"><prop>\n'
        b'<resourcetype xmlns="DAV:"/>\n<getcontentlength xmlns="DAV:"/>\n'
        b'<getetag xmlns="DAV:"/>\n<getlastmodified xmlns="DAV:"/>\n'
        b'<executable xmlns="http://apache.org/dav/props/"/>\n</prop></propfind>\n'
    )
    DAVFS_CHMOD = (
        b'<?xml version="1.0" encoding="utf-8" ?>\n'
        b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
        b'<executable xmlns="http://apache.org/dav/props/">T</executable>'
        b"</D:prop></D:set>\n</D:propertyupdate>\n"
    )
    APACHE_EXEC = "{http://apache.org/dav/props/}executable"

    def test_davfs2_finds_the_exec_bit_where_davfs2_looks_for_it(self):
        reply = self.propfind("f.txt", depth="0", body=self.DAVFS_PROPFIND)[2]
        self.assertEqual(self.prop_status(reply, "/dav/f.txt")[self.APACHE_EXEC], 404)
        _s, _h, reply = self.patch("f.txt", self.DAVFS_CHMOD)
        self.assertEqual(self.prop_status(reply, "/dav/f.txt")[self.APACHE_EXEC], 200)
        self.assertTrue((self.root / "f.txt").stat().st_mode & 0o111)
        reply = self.propfind("f.txt", depth="0", body=self.DAVFS_PROPFIND)[2]
        # The value is spelled out, because that is what mod_dav taught clients
        # to read; an empty element only means true in the DAV: spelling.
        self.assertEqual(self.props_of(reply, "/dav/f.txt")[self.APACHE_EXEC], "T")
        off = self.DAVFS_CHMOD.replace(b">T<", b">F<")
        _s, _h, reply = self.patch("f.txt", off)
        self.assertEqual(self.prop_status(reply, "/dav/f.txt")[self.APACHE_EXEC], 200)
        self.assertFalse((self.root / "f.txt").stat().st_mode & 0o111)
        reply = self.propfind("f.txt", depth="0", body=self.DAVFS_PROPFIND)[2]
        self.assertEqual(self.prop_status(reply, "/dav/f.txt")[self.APACHE_EXEC], 404)

    def test_the_mount_asking_for_a_folder_never_hears_about_an_exec_bit(self):
        reply = self.propfind("dir", depth="0", body=self.DAVFS_PROPFIND)[2]
        self.assertEqual(self.prop_status(reply, "/dav/dir/")[self.APACHE_EXEC], 404)

    # -- reading -----------------------------------------------------------
    def test_get_reports_etag_and_answers_not_modified(self):
        status, hdrs, body = self.call("GET", "/dav/f.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello")
        self.assertNotIn("Content-Disposition", hdrs)
        self.assertEqual(hdrs["Cache-Control"], "no-cache")
        tag = hdrs["ETag"]
        self.assertEqual(tag, fm.dav_etag((self.root / "f.txt").stat()))
        self.assertIn("Last-Modified", hdrs)
        status, hdrs, body = self.call(
            "GET", "/dav/f.txt", headers={"If-None-Match": tag}
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(hdrs["ETag"], tag)
        # The same file through the browser API carries the same tag.
        self.assertEqual(self.call("GET", "/api/raw?path=f.txt")[1]["ETag"], tag)

    def test_get_of_a_folder_and_of_a_stale_etag(self):
        self.assertEqual(self.call("GET", "/dav/dir")[0], 405)
        self.assertEqual(
            self.call("GET", "/dav/f.txt", headers={"If-Match": '"nope"'})[0], 412
        )

    def test_head_reports_size_without_a_body(self):
        status, hdrs, body = self.call("HEAD", "/dav/f.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(hdrs["Content-Length"], "5")
        self.assertEqual(self.call("HEAD", "/dav/nope")[0], 404)

    # -- writing -----------------------------------------------------------
    def test_put_creates_then_replaces_and_keeps_the_mode(self):
        script = self.write("run.sh", "#!/bin/sh\n")
        os.chmod(script, 0o755)
        status, hdrs, _b = self.call("PUT", "/dav/run.sh", b"#!/bin/sh\necho hi\n")
        self.assertEqual(status, 204)
        self.assertIn("ETag", hdrs)
        self.assertEqual(script.read_text(), "#!/bin/sh\necho hi\n")
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        status, _h, _b = self.call("PUT", "/dav/new.txt", b"fresh")
        self.assertEqual(status, 201)
        self.assertEqual((self.root / "new.txt").read_text(), "fresh")
        # A failed upload must not leave the half file behind.
        self.assertEqual(self.call("PUT", "/dav/dir", b"x")[0], 405)
        self.assertEqual(self.call("PUT", "/dav/no/such/x.txt", b"x")[0], 409)
        self.assertEqual(self.call("PUT", "/dav/f.txt", b"x")[0], 204)

    def test_put_refuses_to_lose_a_newer_version(self):
        tag = fm.dav_etag((self.root / "f.txt").stat())
        # If: with the etag the client read: right tag wins, stale one loses.
        self.assertEqual(
            self.call("PUT", "/dav/f.txt", b"2", headers={"If": f"([{tag}])"})[0], 204
        )
        self.assertEqual(
            self.call("PUT", "/dav/f.txt", b"3", headers={"If": '(["gone"])'})[0], 412
        )
        self.assertEqual(
            self.call("PUT", "/dav/f.txt", b"3", headers={"If-Match": '"0-0"'})[0],
            412,
        )
        # If-None-Match: * is "create only", which is what precheck=1 asks for.
        self.assertEqual(
            self.call("PUT", "/dav/f.txt", b"3", headers={"If-None-Match": "*"})[0], 412
        )
        self.assertEqual(
            self.call("PUT", "/dav/mine.txt", b"1", headers={"If-None-Match": "*"})[0],
            201,
        )
        self.assertEqual(self.root.joinpath("f.txt").read_text(), "2")

    def test_put_accepts_chunked_and_expect_100_bodies(self):
        # davfs2 sends Expect: 100-continue only when told to (use_expect100),
        # and a proxy in front of it may re-send the body as chunked. Both
        # arrive here: the server answers the expectation, then the upload.
        raw = self.raw(
            b"PUT /dav/chunked.txt HTTP/1.1\r\n"
            b"Host: x\r\nTransfer-Encoding: chunked\r\n"
            b"Expect: 100-continue\r\n\r\n"
            b"6\r\na body\r\n0\r\n\r\n"
        )
        self.assertIn(b"HTTP/1.1 100 Continue", raw)
        self.assertIn(b"HTTP/1.1 201 Created", raw)
        self.assertEqual((self.root / "chunked.txt").read_text(), "a body")

    def test_mkdir_is_one_level_only(self):
        self.assertEqual(self.call("MKCOL", "/dav/new/")[0], 201)
        self.assertTrue((self.root / "new").is_dir())
        self.assertEqual(self.call("MKCOL", "/dav/new/")[0], 405)
        self.assertEqual(self.call("MKCOL", "/dav/deep/down/")[0], 409)
        self.assertEqual(self.call("MKCOL", "/dav/")[0], 405)
        # A name already used by a file cannot take a collection as well.
        self.assertEqual(self.call("MKCOL", "/dav/f.txt/")[0], 405)
        self.assertFalse((self.root / "f.txt").is_dir())

    def test_delete_removes_files_and_trees(self):
        self.assertEqual(self.call("DELETE", "/dav/f.txt")[0], 204)
        self.assertFalse((self.root / "f.txt").exists())
        self.assertEqual(self.call("DELETE", "/dav/dir")[0], 204)
        self.assertFalse((self.root / "dir").exists())
        self.assertEqual(self.call("DELETE", "/dav/f.txt")[0], 404)
        self.assertEqual(self.call("DELETE", "/dav/")[0], 400)
        # A symlink goes, the folder behind it stays.
        self.assertEqual(self.call("DELETE", "/dav/x")[0], 204)
        self.assertFalse((self.root / "x").exists())

    def test_move_and_copy(self):
        status, _h, _b = self.call(
            "MOVE", "/dav/f.txt", headers={"Destination": "/dav/moved.txt"}
        )
        self.assertEqual(status, 201)
        self.assertEqual((self.root / "moved.txt").read_text(), "hello")
        self.assertFalse((self.root / "f.txt").exists())
        # MOVE replaces an existing destination unless Overwrite: F says no.
        self.write("other.txt", "other")
        status, _h, _b = self.call(
            "MOVE",
            "/dav/moved.txt",
            headers={"Destination": f"http://127.0.0.1:{self.port}/dav/other.txt"},
        )
        self.assertEqual(status, 204)
        self.assertEqual((self.root / "other.txt").read_text(), "hello")
        self.write("a.txt", "a")
        self.write("b.txt", "b")
        status, _h, _b = self.call(
            "MOVE",
            "/dav/a.txt",
            headers={"Destination": "/dav/b.txt", "Overwrite": "F"},
        )
        self.assertEqual(status, 412)
        self.assertTrue((self.root / "a.txt").exists())
        status, _h, _b = self.call(
            "COPY", "/dav/dir", headers={"Destination": "/dav/copy"}
        )
        self.assertEqual(status, 201)
        self.assertEqual((self.root / "copy" / "inner.txt").read_text(), "inside")
        # Copying a folder into itself is refused, not a silent full copy.
        self.assertEqual(
            self.call("COPY", "/dav/dir", headers={"Destination": "/dav/dir/in"})[0],
            400,
        )
        # Destination outside the mount, or missing.
        self.assertEqual(self.call("MOVE", "/dav/a.txt", headers={})[0], 400)
        self.assertEqual(
            self.call("COPY", "/dav/a.txt", headers={"Destination": "/api/x"})[0],
            400,
        )

    def test_copy_into_a_folder_lands_inside_it(self):
        status, _h, _b = self.call(
            "COPY", "/dav/f.txt", headers={"Destination": "/dav/dir"}
        )
        self.assertEqual(status, 201)
        self.assertEqual((self.root / "dir" / "f.txt").read_text(), "hello")

    def test_names_that_are_not_plain_ascii(self):
        for name in ("two words.txt", "ünïcode.txt", 'quote".txt', "100%.txt"):
            quoted = quote(name)
            self.assertEqual(self.call("PUT", f"/dav/{quoted}", b"x")[0], 201, name)
            self.assertEqual((self.root / name).read_text(), "x")
        _s, _h, body = self.propfind("", body=self.PROP_ALL)
        self.assertIn("/dav/two%20words.txt", self.hrefs(body))
        self.assertIn("/dav/100%25.txt", self.hrefs(body))

    # -- locking -----------------------------------------------------------
    def test_lock_blocks_the_browser_and_unlocking_frees_it(self):
        status, hdrs, body = self.lock("f.txt")
        self.assertEqual(status, 200)
        token = hdrs["Lock-Token"].strip("<>")
        self.assertTrue(token.startswith("opaquelocktoken:"))
        self.assertIn(b"<D:lockdiscovery", body)
        self.assertIn(b"test@host", body)

        # The browser (no token) cannot write it, and says who holds it.
        status, _h, raw = self.call(
            "POST",
            "/api/save?path=f.txt",
            json.dumps({"content": "x"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 423)
        self.assertIn("test@host", json.loads(raw)["error"])
        # Reading is still fine.
        self.assertEqual(self.json_call("GET", "/api/text?path=f.txt")[0], 200)

        # Unlocking from the browser is what makes a dead mount recoverable.
        status, doc = self.json_call("POST", "/api/unlock?path=f.txt")
        self.assertEqual(status, 200, doc)
        self.assertEqual(doc["unlocked"], 1)
        self.assertEqual(
            self.json_call("POST", "/api/save?path=f.txt", {"content": "x"})[0], 200
        )
        self.assertEqual(self.root.joinpath("f.txt").read_text(), "x")

    def test_write_with_the_token_succeeds(self):
        token = self.lock("f.txt")[1]["Lock-Token"].strip("<>")
        self.assertEqual(
            self.call("PUT", "/dav/f.txt", b"mine", headers={"If": f"(<{token}>)"})[0],
            204,
        )
        self.assertEqual(
            self.call("DELETE", "/dav/f.txt", headers={"Lock-Token": f"<{token}>"})[0],
            204,
        )

    def test_write_without_the_token_is_423(self):
        self.lock("f.txt")
        for method, path, kwargs in [
            ("PUT", "/dav/f.txt", {"body": b"x"}),
            ("DELETE", "/dav/f.txt", {}),
            ("MOVE", "/dav/f.txt", {"headers": {"Destination": "/dav/g.txt"}}),
        ]:
            status, _h, raw = self.call(method, path, **kwargs)
            self.assertEqual(status, 423, f"{method} {path}")
            self.assertIn("Locked over WebDAV", json.loads(raw)["error"])
        # Somewhere else is unaffected.
        self.assertEqual(self.call("PUT", "/dav/other.txt", b"x")[0], 201)

    def test_second_locker_is_refused_and_the_owner_may_take_over(self):
        self.lock("f.txt", owner="first")
        status, _h, raw = self.lock("f.txt", owner="second")
        self.assertEqual(status, 423)
        self.assertIn("first", json.loads(raw)["error"])
        # Same owner: a mount that died without unlocking, coming back.
        self.assertEqual(self.lock("f.txt", owner="first")[0], 200)
        self.assertEqual(fm.LOCKS.count(), 1)

    def test_lock_refresh_keeps_the_token(self):
        token = self.lock("f.txt")[1]["Lock-Token"].strip("<>")
        status, hdrs, _b = self.call("LOCK", "/dav/f.txt", b"", {"If": f"(<{token}>)"})
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["Lock-Token"].strip("<>"), token)
        self.assertEqual(fm.LOCKS.count(), 1)
        self.assertEqual(
            self.call("LOCK", "/dav/f.txt", b"", {"If": "(<opaquelocktoken:x>)"})[0],
            409,
        )

    def test_unlock_needs_the_matching_token(self):
        token = self.lock("f.txt")[1]["Lock-Token"].strip("<>")
        self.write("g.txt", "g")
        self.assertEqual(self.call("UNLOCK", "/dav/g.txt", headers={})[0], 400)
        self.assertEqual(
            self.call("UNLOCK", "/dav/g.txt", headers={"Lock-Token": f"<{token}>"})[0],
            400,
        )
        self.assertEqual(
            self.call("UNLOCK", "/dav/f.txt", headers={"Lock-Token": "<nope>"})[0], 409
        )
        status, _h, body = self.call(
            "UNLOCK", "/dav/f.txt", headers={"Lock-Token": f"<{token}>"}
        )
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(fm.LOCKS.count(), 0)
        self.assertEqual(self.call("PUT", "/dav/f.txt", b"free")[0], 204)

    def test_locking_a_folder_blocks_its_children(self):
        status, hdrs, _b = self.lock("dir", depth="infinity")
        self.assertEqual(status, 200)
        token = hdrs["Lock-Token"].strip("<>")
        # Everything inside is busy, whichever door the write comes through.
        self.assertEqual(self.call("PUT", "/dav/dir/new.txt", b"x")[0], 423)
        self.assertEqual(self.call("MKCOL", "/dav/dir/sub/")[0], 423)
        self.assertEqual(
            self.call("POST", "/api/upload?path=dir&filename=n.txt", b"x")[0], 423
        )
        self.assertEqual(self.json_call("POST", "/api/mkdir?path=dir/sub")[0], 423)
        # A folder elsewhere is untouched, and the browser may still write there.
        self.assertEqual(self.json_call("POST", "/api/mkdir?path=sibling")[0], 200)
        # The holder of the token may write.
        self.assertEqual(
            self.call(
                "PUT", "/dav/dir/inner.txt", b"x", headers={"If": f"(<{token}>)"}
            )[0],
            204,
        )

    def test_unlocking_frees_an_inherited_tree_lock(self):
        # The folder is locked for its whole tree, so a row inside it says
        # locked; unlocking that row has to break the lock it can see, which is
        # the one on the folder above.
        self.lock("dir", owner="everywhere", depth="infinity")
        self.assertEqual(self.call("PUT", "/dav/dir/inner.txt", b"x")[0], 423)
        status, doc = self.json_call("POST", "/api/unlock?path=dir/inner.txt")
        self.assertEqual(status, 200, doc)
        self.assertEqual(doc["unlocked"], 1)
        self.assertEqual(self.call("PUT", "/dav/dir/inner.txt", b"x")[0], 204)
        self.assertEqual(fm.LOCKS.count(), 0)

    def test_propfind_reports_the_lock(self):
        token = self.lock("f.txt", owner="editor@box")[1]["Lock-Token"].strip("<>")
        _s, _h, body = self.propfind("", body=self.PROP_ALL)
        self.assertIn(b"<D:lockdiscovery>", body)
        self.assertIn(b"editor@box", body)
        self.assertIn(token.encode(), body)
        self.assertIn(b"<D:exclusive", body)
        self.assertIn(b"Second-600", body)
        # A lock on the file shows up in the browser's listing as well.
        _s, doc = self.json_call("GET", "/api/list?path=")
        self.assertTrue({e["name"]: e for e in doc["entries"]}["f.txt"]["locked"])

    def test_list_marks_locked_entries(self):
        self.lock("f.txt", owner="someone")
        _s, doc = self.json_call("GET", "/api/list?path=")
        by_name = {e["name"]: e for e in doc["entries"]}
        self.assertTrue(by_name["f.txt"]["locked"])
        self.assertEqual(by_name["f.txt"]["lock_owner"], "someone")
        self.assertFalse(by_name["dir"]["locked"])
        self.assertFalse(doc["locked"])
        self.lock("", owner="everywhere")
        _s, doc = self.json_call("GET", "/api/list?path=")
        self.assertTrue(doc["locked"])

    def test_read_only_and_disabled_mounts_refuse_writes(self):
        self._swap(fm, "ENABLE_WRITE", False)
        self.assertEqual(self.call("PUT", "/dav/n.txt", b"x")[0], 403)
        self.assertEqual(self.call("MKCOL", "/dav/n/")[0], 403)
        self.assertEqual(self.lock("f.txt")[0], 403)
        self.assertEqual(self.call("DELETE", "/dav/f.txt")[0], 403)
        self.assertEqual(self.propfind("", body=self.PROP_ALL)[0], 207)
        self._swap(fm, "ENABLE_DAV", False)
        self.assertEqual(self.propfind("", body=self.PROP_ALL)[0], 405)
        self.assertEqual(self.call("GET", "/dav/f.txt")[0], 404)
        status, doc = self.json_call("POST", "/api/unlock?path=f.txt")
        self.assertEqual(status, 403, doc)

    def test_health_and_page_describe_the_mount(self):
        _s, doc = self.json_call("GET", "/health")
        self.assertTrue(doc["dav"])
        self.assertEqual(doc["dav_prefix"], "/dav")
        self.assertEqual(doc["locks"], 0)
        self.lock("f.txt")
        self.assertEqual(self.json_call("GET", "/health")[1]["locks"], 1)
        self.assertTrue(fm.features()["dav"])

    def test_errors_keep_the_connection_usable(self):
        # davfs2 retries hard, so a failed request that left a body unread must
        # not poison the socket: the leftovers would be read as the next
        # request line and answered for a request nobody sent.
        c = self.conn()
        self.assertEqual(self.propfind("nope", body=self.PROP_ALL, conn=c)[0], 404)
        self.assertEqual(self.call("GET", "/health", conn=c)[0], 200)
        self.assertEqual(self.lock("f.txt", owner="a")[0], 200)
        self.assertEqual(self.lock("f.txt", owner="b")[0], 423)
        self.assertEqual(self.call("MKCOL", "/dav/f.txt", b"a body", conn=c)[0], 405)
        self.assertEqual(
            self.call("PROPFIND", "/dav/f.txt", self.PROP_ALL, conn=c)[0], 207
        )
        self.assertEqual(self.propfind("", body=self.PROP_ALL, conn=c)[0], 207)
        self.assertEqual(self.call("GET", "/health", conn=c)[0], 200)

    def test_a_request_body_over_the_xml_cap_is_refused(self):
        self._swap(fm, "DAV_MAX_XML_BODY", 64)
        status, _h, body = self.propfind("", body=b"<D:propfind>" + b"x" * 200)
        self.assertEqual(status, 413)
        self.assertIn("too large", json.loads(body)["error"])

    def test_post_is_not_part_of_webdav_one(self):
        status, _h, body = self.call("POST", "/dav/f.txt", b"x")
        self.assertEqual(status, 501)
        self.assertIn("POST", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
