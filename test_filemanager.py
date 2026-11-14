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

import http.client
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

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
# Page and thumbnails
# ---------------------------------------------------------------------------
class PageTests(TempRoot):
    def setUp(self):
        super().setUp()
        self._swap(fm, "_PAGE_HTML", None)

    def test_placeholders_are_substituted(self):
        page = fm.page_html().decode("utf-8")
        for token in ("__ROOT_NAME__", "__FEATURES__", "__MAKE_POLL_MS__"):
            self.assertNotIn(token, page)
        self.assertIn('"write": true', page.replace("'", '"'))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
