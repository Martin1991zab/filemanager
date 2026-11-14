# filemanager.py

> **Disclaimer:** This project is vibe coded — built quickly with AI
> assistance and without formal review or testing for production use. It is
> a toy project and should **not** be used for any production purposes.

A single-file web file manager. Start it in any directory, open the URL in
a browser, and browse, edit, upload, download and run files — no database,
no configuration file, no build step. Everything (backend, HTML, CSS and
JavaScript) lives in one Python file and only needs the standard library.
(`test_filemanager.py` holds the tests; the app never imports it.) The same
folder doubles as a WebDAV share, so it can be mounted with davfs2 and worked
on with ordinary programs instead of a browser.

The screenshots below show the app serving a demo folder with sample
project data.

![Light theme: paper listing over a summer desk scene](screenshots/light.png)

*Light theme — a summer desk by the window.*

![Dark theme: neon listing over a view of Earth from a space station](screenshots/dark.png)

*Dark theme — a space-station window looking down at Earth.*

## Features

### Browsing

- Directory listing with name, size, created time and last-updated time.
- Click a sortable column header to sort; click again to reverse the order.
- Breadcrumb navigation back up the tree, plus a filter box that narrows the
  current folder as you type.
- The current folder lives in the URL, so the back button, reloads and pasted
  links all work.
- Recursive folder sizes, computed in the background.
- Image thumbnails in the listing (requires Pillow; without it you get a
  generic icon). SVG previews work either way — the browser draws those.
- Symbolic links are marked with what they point at, and a link that points at
  nothing is labelled `broken`.
- Hidden files are shown like any other entry — nothing is filtered out.
- Very large folders are listed up to a cap (20 000 entries by default) and
  the header says so instead of pretending that is everything.
- The header keeps the two everyday actions in plain sight (**+ New** and
  **Upload**); everything used less often — zip download of the current folder,
  base64 upload, make and the shell — is grouped in a **Tools** menu, and the
  theme and help buttons sit apart from both. A menu closes with `Esc` or a
  click anywhere else, and its items are walked with the arrow keys.
- The **?** button in the header (or the `?` key) opens a help popup that
  documents every button, the keyboard shortcuts and the WebDAV mount; the
  parts the server was started without are left out of it.

### Creating, editing and organising

- Create folders and empty text files (`n` opens that menu, `u` picks files to
  upload).
- Rename, move and copy files or folders (both with a folder-tree picker), and
  delete them.
- Tick a few entries to move, copy or delete the whole selection at once.
- View images directly in the browser.
- Edit plain-text files in a built-in editor, with selectable line endings
  (`as-is`, `LF`, `CRLF`, `CR`) and `Ctrl`/`Cmd`+`S` to save.
- Saving refuses to overwrite a file that changed on disk since you opened it
  (a second tab, a make, an outside editor) and offers to reload it instead;
  closing the editor with unsaved changes asks first.
- Other file types can be opened in the browser for preview or download.
- Nothing is ever silently overwritten: a name that exists becomes
  `report (1).pdf`, `report (2).pdf`, and so on.

### Uploading and downloading

- Multi-file upload with a progress bar; three files go up at a time and the
  bar counts bytes, not files.
- Files up to 1 GB go through, on every path listed below. Nothing here
  buffers a whole file: an upload is written to disk as it arrives and a
  download is read a chunk at a time, so a large transfer uses the same memory
  as a small one.
- Drag files onto the window to upload them to the folder you are looking at
  (drop a folder instead and it tells you to zip it first).
- Uploads keep going to the folder that was open when they started, even if
  you browse elsewhere while they run.
- Base64 upload/download as a workaround for flaky networks (the upload is the
  **Tools ▸ Upload as base64** item, the download a 🔣 action in each row); can
  be disabled entirely with `--no-b64` (the actions disappear and the endpoints
  refuse requests with a 403 error). The payload is decoded (and, in the
  browser, encoded) a piece at a time, so this path carries the same 1 GB as
  the plain one; it is still the slower of the two, since base64 is a third
  bigger than the file.
- Download single files, or the current folder as a ZIP archive
  (**Tools ▸ Download folder as zip**; empty folders survive the round trip, and
  the archive holds one folder rather than a spill of files).
- Extract ZIP archives into a folder, with an optional delete-after-extract.
- Downloads are streamed and support `Range` requests and `HEAD`, so a video
  or audio file can be seeked in the browser instead of downloaded first.

### Running commands

- **Run** executable files with optional arguments and a timeout, and see
  the combined stdout/stderr and exit code.
- **Tools ▸ Shell** opens a command box that runs arbitrary commands through
  `bash -c` in a chosen working directory, with history and a timeout.
- **Tools ▸ Run make here** runs `make` in the current folder on demand.
- **Automatic make**: when a folder contains a `Makefile`, `make` is run
  automatically after files are edited, uploaded or deleted, and the output
  is shown in a modal. **Tools ▸ Auto-make after edits** disables this (the
  choice is stored in the browser and survives restarts, and the Tools button
  carries a red dot while it is off); **Run make here** always runs regardless
  of that toggle. A build that finishes while another dialog is open waits in a
  queue instead of replacing it.
- Any of the three can be disabled entirely at startup with `--no-shell`,
  `--no-run` or `--no-make` (the last one turns off both the automatic
  rebuilds and the manual make), and the programs they use can be changed with
  `--shell-cmd` and `--make-cmd`.
- `--read-only` turns off every change, including Run, Shell and Make, since
  all three can write anywhere.

### WebDAV share

The same folder is also a WebDAV share, at `/dav/` by default, so it can be
mounted and worked on with ordinary programs rather than a browser. The client
it is built for is **davfs2** on Linux; mounting it is documented in the Help
popup and in [Mounting the folder over WebDAV](#mounting-the-folder-over-webdav).

- Compliance class 2: `OPTIONS`, `PROPFIND`, `PROPPATCH`, `MKCOL`, `PUT`,
  `DELETE`, `MOVE`, `COPY`, `LOCK`, `UNLOCK`, `GET` and `HEAD` are implemented,
  with `Depth: 0` and `Depth: 1` listings and strong `ETag`s.
- **Locks, and they are enforced both ways.** A file open in an editor through
  the mount is marked 🔒 in the browser listing, and saving it there answers 423
  with the name of whoever holds it, next to a 🔓 action to break the lock if
  that client died or walked away. Writes arriving over the mount are refused
  the same way while the browser holds one. An exclusive write lock never stops
  anyone from reading.
- **A lost update is refused, not won.** Conditional requests
  (`If-Match`, `If-None-Match`, `If-Unmodified-Since`, and entity tags inside
  `If:`) are checked on every write, so an upload of a stale version comes back
  as 412 instead of quietly replacing what is there now. What the client does
  then is the client's choice; under davfs2 it is [described below](#mounting-the-folder-over-webdav).
- Uploads are written to a temporary name and swapped in, so a client that
  gives up half way through an upload (davfs2 retries often) leaves no half
  file for the browser to open.
- What is deliberately left out: shared and read locks, `Depth: infinite`
  recursion (answered as `Depth: 1`), stored custom properties (PROPPATCH refuses
  them honestly), the quota properties davfs2 also asks for (answered as "not
  here", so `df` on the mount says nothing about the real disk), partial
  `Content-Range` uploads and the Microsoft extensions.
  None of them is used by davfs2, and each one would need a client that misses
  it.
- **The executable bit survives the round trip.** PROPFIND reports it and
  PROPPATCH writes it back with a `chmod`, so a script made on the mount is
  runnable in the browser and one made in the browser is runnable on the mount.
  WebDAV offers exactly one boolean for this, so it carries no distinction
  between owner, group and other.
- Symbolic links, owners and groups do not survive a WebDAV round trip because
  davfs2 cannot express them: a symlink looks like a plain file (its contents
  are the target's name), and a link to a folder looks like the folder itself.
- `--no-dav` turns the share off, `--dav-path` moves it to another URL segment.

### Themes

- **Light** — a summer day: a translucent paper listing on a desk with a
  pencil, looking out over a grassy field and forest.
- **Dark** — cyan neon cyberpunk aboard a space station: a view over Earth's
  horizon, a distant sun, stars and a subtle station-window frame.
- The initial theme follows the operating system (`prefers-color-scheme`).
- The header toggle switches themes and remembers the choice per browser.
- Shift-click the theme toggle to forget the choice and return to automatic
  system detection.

## Requirements

- Python 3.10 or newer.
- `Pillow` is optional; install it to get image thumbnails.
- `make` is optional; it is only used for automatic or manual make runs.
- `bash` is optional; it is only used by the Shell feature.
- `davfs2` is optional and client-side: it is what mounts the WebDAV share.
  Nothing on the server depends on it being installed.

No third-party Python packages are required.

## Quick start

```bash
# Serve the current directory on http://127.0.0.1:8000/
python3 filemanager.py

# Serve a specific directory
python3 filemanager.py /path/to/folder

# Bind to all interfaces on a custom port
python3 filemanager.py /path/to/folder --host 0.0.0.0 --port 8080

# Browse only: nothing can be created, edited, uploaded, moved, deleted,
# extracted or run
python3 filemanager.py /path/to/folder --read-only

# Disable shell access, executable execution, make (auto and manual)
# and the base64 upload/download workaround
python3 filemanager.py /path/to/folder --no-shell --no-run --no-make --no-b64

# No WebDAV share, or share it under a different URL segment
python3 filemanager.py /path/to/folder --no-dav
python3 filemanager.py /path/to/folder --dav-path files
```

Then open the printed URL in your browser. Startup also prints the active
modes (read-only or read-write, which shell and make program are used, and
whether thumbnails are available) and, when WebDAV is on, the URL to mount and
the `mount -t davfs` line that does it.

## Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `root` | current directory | Directory to serve. |
| `--host` | `127.0.0.1` | Address to bind. Use `0.0.0.0` only behind a reverse proxy or firewall. |
| `--port` | `8000` | Port to listen on. |
| `--read-only` | off | Refuse every change: no creating, editing, uploading, moving, deleting, extracting or running. Browsing, viewing and downloading keep working. |
| `--no-shell` | off | Disable shell access (Tools ▸ Shell and `/api/shell`). |
| `--no-run` | off | Disable execution of executable files (Run button and `/api/run`). |
| `--no-make` | off | Disable make entirely: no automatic rebuilds and no manual make, i.e. both items of the Tools menu (`/api/make`). |
| `--no-b64` | off | Disable the base64 upload/download workaround (`/api/upload64`, `/api/download64`). |
| `--no-dav` | off | Disable the WebDAV share (its verbs answer 405 and `/api/unlock` answers 403). |
| `--dav-path` | `dav` | First URL segment of the WebDAV share; one path component, and not `api` or `health`. |
| `--shell-cmd` | `bash` | Program the Shell action passes commands to. |
| `--make-cmd` | `make` | Program used for make (e.g. `gmake`, `ninja`). |
| `--version` | — | Print the version and exit. |

## HTTP API

Everything speaks JSON except the download endpoints, which stream files.
`GET /health` is a small status document for monitors and container health
checks: it reports the version, uptime, served root, whether the server is
read-only, whether Pillow is available, and whether WebDAV is on (with its
prefix and how many locks are currently held).

Under `/dav/` (or whatever `--dav-path` says) the same port speaks WebDAV
instead: those requests get XML bodies and WebDAV status codes, and the verbs
do not work anywhere else on the port. The rest of this section is the browser
API, and the two never overlap.

| GET | Description |
| --- | --- |
| `/` | The single-page app. |
| `/health` | Status document (silent in the access log). |
| `/api/list?path=` | One folder: entries, totals, symlink targets, WebDAV locks. |
| `/api/tree?path=` | Folder names only, for the move/copy picker. |
| `/api/text?path=` | File contents, detected line endings, `mtime_us`. |
| `/api/raw?path=` | File inline (supports `Range` and `HEAD`). |
| `/api/download?path=` | File as an attachment. |
| `/api/zip?path=` | Folder streamed as a ZIP archive. |
| `/api/thumbnail/…` | Rendered thumbnail, cached and ETagged. |
| `/api/download64?path=` | File as base64 in JSON. |
| `/api/make/result?job=` | Status of a background make job. |

| POST | Description |
| --- | --- |
| `/api/mkdir?path=`, `/api/newfile?path=` | Create a folder or an empty file. |
| `/api/save?path=` | Write a text file (atomic, optional `if_mtime_us` guard). |
| `/api/delete?path=` | Delete one entry (a symlink is unlinked, not followed). |
| `/api/rename?path=` | Rename, body `{"name": …}`. |
| `/api/move?path=`, `/api/copy?path=` | Move or copy into a folder, body `{"dest": …}`. |
| `/api/unlock?path=` | Break any WebDAV locks on an entry (recovery from a dead client). |
| `/api/foldersize` | Recursive sizes for a list of folders, body `{"paths": […]}`. |
| `/api/upload?path=&filename=` | Raw request body stored as a file. |
| `/api/upload64?path=&filename=` | Base64 in JSON, body `{"data": …}`. |
| `/api/extract?path=` | Extract a ZIP, body `{"dest": …, "delete": …}`. |
| `/api/run?path=` | Execute a file, body `{"args": […], "timeout": …}`. |
| `/api/make?path=` | Run make and wait for the result. |
| `/api/shell` | Run a command, body `{"command": …, "cwd": …, "timeout": …}`. |

## Mounting the folder over WebDAV

The share is written for **davfs2** on Linux, which needs no configuration to
work against it. Mount it once to try it:

```bash
sudo mkdir -p /mnt/this
sudo mount -t davfs http://127.0.0.1:8000/dav/ /mnt/this
```

Or keep it in `/etc/fstab` so your own user can mount and unmount it without a
password (`noauto` keeps it off the boot path):

```
http://127.0.0.1:8000/dav/  /mnt/this  davfs  noauto,user,_netdev  0  0
```

```bash
mount /mnt/this
umount /mnt/this        # or: fusermount -u /mnt/this
```

Details that differ between distributions or bite once a mount is in daily use:

- Let your user mount WebDAV at all: `sudo usermod -aG davfs2 $USER`, then log
  out and in again. Some distributions instead rely on `/usr/sbin/mount.davfs`
  being setuid root, which lets it mount any URL:
  `sudo mount -t davfs -o uid=$(id -u),gid=$(id -g) <url> <dir>`.
- **davfs2 asks for credentials anyway.** This server never sends an
  authentication challenge, yet `mount.davfs` prompts for a username and a
  password the first time it mounts a URL it has nothing stored for. Two empty
  answers (just Return) are right here, and a `mount.davfs` that is not allowed
  to ask - no terminal, output redirected - gives up without sending a single
  request. It reads `~/.davfs2/secrets` before asking, so one line
  `http://host:8000/dav/ guest guest` in that file (mode `600`) silences the
  prompt; mind whose home that is, because a mount through `sudo` uses root's.
  Put a proxy with authentication in front of the server and the credentials it
  needs go in the same place rather than in fstab.
- Reached through a proxy under a sub-path? Mount that same URL with `/dav` on
  the end. The Help popup prints the address it computed from the page you are
  looking at, which is usually the right one.
- **Writes are late by design.** davfs2 keeps a changed file locally and
  re-uploads the whole file about ten seconds after the program closes it
  (`delay_upload 10`), so the browser may show the previous version for a
  moment, and a file created on the mount does not exist for the browser until
  then. `delay_upload 0` in `~/.davfs2/davfs2.conf` makes writes immediate.
- **Listings are cached too** (`dir_refresh 60`), so a file created in the
  browser appears on the mount up to a minute later; `dir_refresh 0` makes it
  live. Both defaults are the price of a network mount feeling local. That cache
  outlives an unmount (`~/.cache/davfs2`, or `/var/cache/davfs2` for a mount done
  as root) and with it the modes and contents davfs2 last saw, so after changing
  a file outside the mount it is the mount that is out of date - or delete that
  directory.
- **A lock on the other side is an error here.** While the browser holds a lock,
  writing that file on the mount fails with `Permission denied`, renaming it
  fails as an I/O error, and deleting it the same way; reading is never refused.
  The other way round, a file an editor holds open through the mount shows 🔒 in
  the browser and its saves come back as 423 until it is closed (or the lock is
  broken with 🔓).
- **Edit the same file from both sides and davfs2 has the last word.** The server
  refuses its stale upload with 412, davfs2 then re-reads the file, sends its own
  version and leaves the copy it started from in `lost+found` on the mount — so
  the browser's edit is overwritten, but nothing is thrown away and you can copy
  it back. Breaking the lock with 🔓 before saving is what invites this, because
  the mount is still holding its own idea of the file. The browser guards its own
  side too: saving a file that changed on disk since it was opened is refused with
  "the file changed on disk…", not silently overwritten.
- `--read-only` on the server makes the mount refuse writes; `-o ro` on the
  mount point refuses them on the client side.
- `lost+found`, and on some versions `.DAVFS` too, exist in the **mount** only:
  davfs2's own bookkeeping, and its place for writes that failed while the
  connection was down. This server never hears about either one, so the shared
  folder stays free of them and the browser listing never shows them.

## Docker

```bash
docker build -t filemanager .
docker run --rm -p 8000:8000 -v "$PWD:/data" filemanager
# arguments replace the image's defaults, so repeat --host/--port:
docker run --rm -p 8000:8000 -v "$PWD:/data" \
    filemanager /data --host 0.0.0.0 --port 8000 --read-only
```

The image is the Python 3.12-alpine base plus `bash` and `make` (only needed
by the Shell and Make features); Pillow is left out, so thumbnails fall back
to a generic icon — add `py3-pillow` to the `apk add` line to get them. It
runs as an unprivileged user and serves `/data`. Its health check polls
`/health`. A published port is a complete WebDAV share too, so the container's
folder can be mounted with davfs2 from the host machine.

## Security

This is a single-user tool intended for a trusted machine or a protected
network:

- There is **no authentication and no HTTPS**. Anyone who can reach the
  port can read, write, rename and delete files under the served root.
- The **Run** and **Shell** features execute arbitrary commands with the
  permissions of the user running the server. Treat access to this app as
  equivalent to a shell account. To reduce the exposure, start the server
  with `--no-run`, `--no-shell`, `--no-make` or `--read-only`.
- The **WebDAV share is open to the same audience**: whoever can reach the port
  can mount the folder and use it as a disk, and `--read-only` applies to it
  too. Locks decide *who may write a file right now*, not *who is allowed to* —
  the Unlock action is recovery, not permission, and anyone reaching the page
  can break someone else's lock.
- The server refuses paths outside the served root, but that is not a
  substitute for access control.
- Requests are not authenticated, so the app deliberately does not try to be
  a browser-security boundary either: there are no CSRF or `Origin` checks and
  `/api/raw` serves files with their own content type. That is the reverse
  proxy's job.
- For remote access, bind to localhost and put the app behind an
  authenticating reverse proxy, or forward the port over SSH.

## Design notes

- **Stateless**: the app serves the directory given on the command line and
  stores no database, config or metadata. UI preferences (theme, auto-make)
  and the current folder (URL hash) live in the browser. Three things are kept
  in memory to avoid repeating expensive work or duplicating it badly —
  rendered thumbnails and folder sizes, both invalidated on change and
  size/TTL-bounded, and the WebDAV lock table, which is TTL-bounded and loses
  every lock when the process restarts. Nothing has to be migrated, and the
  served folder stays the only description of what there is.
- **Single file**: `filemanager.py` embeds the HTML template, CSS and
  JavaScript. The template is read once at startup, so restart the process
  after editing the file.
- **Symlinks** inside the served tree are listed and browsed, and operations
  act on the entry the listing shows: deleting a link to a folder removes the
  link and leaves the linked folder alone. A link may point outside the served
  directory; its parent path is still confined.
- **Limits**, in one block at the top of the file:

  | Limit | Default |
  | --- | --- |
  | Run output captured | 1 MB |
  | Run / Shell timeout | 60 s default, 1 h maximum |
  | Idle socket timeout | 30 s (stalled clients get a 408) |
  | Upload or download | 1 GB, on every path |
  | File opened in the editor | 20 MB |
  | JSON request body | 64 MB |
  | Base64 transfer | 1 GB (its JSON body: 1.3 GB) |
  | ZIP extraction | 4 GB per entry, 8 GB total |
  | Entries per `/api/list` | 20 000 |
  | Entries per WebDAV `PROPFIND` | 5 000 |
  | WebDAV XML request body | 1 MB |
  | WebDAV locks | 30 min default TTL (`Timeout:` honoured up to 24 h), 4 096 in memory |
  | Folder-size scans | 2 000 000 directory entries per request, 30 s cache |
  | Thumbnails in memory | 512 images / 64 MB |
  | Concurrent make jobs | 4 |

  Everything that moves file data is streamed, so a 1 GB transfer costs the
  server a fixed chunk of memory rather than the file: a base64 upload is
  decoded to disk as it arrives and a download is encoded on the way out. The
  limit is about what the endpoints promise to carry, not about RAM, and the
  thing that runs out first is usually free disk (which answers 413 too).

  A reverse proxy in front of the server applies its own request-size limit
  first, and that one tends to bite far below these numbers: nginx refuses a
  body larger than 1 MB unless `client_max_body_size` says otherwise.

- Uses `ThreadingHTTPServer`, so multiple requests can be handled at once.
  Connections are kept alive, chunked request bodies (an upload with no
  `Content-Length`) are read properly, and a body an endpoint never needed is
  discarded afterwards so the next request on the same connection cannot be
  misread as its leftovers.
- File type detection is extension-based (falling back to the system MIME
  database), with UTF-8 assumed for text files.

## Tests

```bash
python3 -m unittest test_filemanager.py        # 110+ tests, about three seconds
python3 -m unittest -v test_filemanager.py     # per test
FM_TEST_LOG=1 python3 -m unittest test_filemanager.py   # with the access log
```

The suite is part of the standard library only, needs no network, and creates
a throwaway root directory plus a real server on a random port for each case.
Thumbnail tests are skipped when Pillow is missing. The WebDAV half is tested
over a real socket, the way a client talks to it: verb by verb, plus the lock
rules from both the mount's side and the browser's.
