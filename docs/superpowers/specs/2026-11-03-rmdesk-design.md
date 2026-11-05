# rmdesk — Design

**Date:** 2026-11-03
**Status:** Draft for review

## 1. Purpose

A Linux desktop app (Omarchy / Arch / Hyprland) for a reMarkable 2 that brings together the features reMarkable's official apps don't offer on Linux:

1. **Files** — browse, upload, download and trash documents.
2. **Screen** — live view of the tablet, for watching it yourself *and* presenting it in video calls and recordings.
3. **Compose** — add pictures and text to an **existing page** of an existing notebook, as editable ink (or typed text).

### Success criteria
- One app, launched from the app menu, works over USB (10.11.99.1) and over Wi-Fi.
- PDFs/EPUBs can be uploaded to a chosen folder; notebooks can be saved as PDF.
- Live tablet screen in a window that Zoom/Meet/OBS can capture, with zoom/crop and inverted colours.
- A PNG/JPG/SVG or a piece of text can be placed on a chosen spot of an existing notebook page. On the tablet it appears as ink that can be erased and moved with the lasso.
- No notebook edit can lose existing writing: every write is backed up, checked, and can be undone.

### Non-goals (v1)
- Photo-quality pictures on notebook pages (pictures become traced ink lines).
- Rename, move or new folder in Files.
- Using the tablet as a drawing tablet / mouse for the PC.
- reMarkable cloud API access (everything is local, over SSH or the USB web interface).
- reMarkable Paper Pro / rM1 support.

## 2. Tech stack

- **Python 3.14 + PySide6 (Qt6)**. Python because `rmscene` (reads/writes notebook pages in the v6 `.rm` format) and `rmc` (renders `.rm` pages) are Python libraries.
- SSH/SFTP: `paramiko`. Secrets: `keyring` (system Secret Service).
- Image tracing: `opencv` (`python-opencv`).
- Screen streaming: **goMarkableStream** running on the tablet. The app is the client.
- Tests: `pytest`, `pytest-qt` for the UI smoke test.

## 3. Architecture

Only `ui` imports Qt. Every other part is plain Python and can be tested without a window or a tablet.

| Module | Responsibility | Depends on |
|---|---|---|
| `connection` | Find the tablet (USB 10.11.99.1 first, then the saved Wi-Fi IP), open SSH/SFTP, run commands, report state (USB / Wi-Fi / Offline / Connecting). Password kept in the keyring; optional SSH key install. Reads the firmware version. | paramiko, keyring |
| `library` | Read `/home/root/.local/share/remarkable/xochitl/` (`*.metadata`, `*.content`, thumbnails) and build the folder/document tree. Upload, download, move to trash. | connection |
| `notebook` | Load a notebook's pages (`.rm` v6), render previews, add items to a page's scene tree, write the page back. Detects the page format version. | rmscene, rmc |
| `compose` | Convert inputs into page items: image → traced strokes; text → single-line-font strokes; typed text → the page's text block. Scale/position into the box the user chose (page coordinates). | notebook, opencv |
| `stream` | Client for goMarkableStream: connect, decode frames, deliver images to the UI; start/stop on demand. Install/uninstall goMarkableStream on the tablet. | connection |
| `safety` | Transactional notebook edit: back up → build locally → verify → upload under a temp name → swap → restart the interface → confirm, else restore. Backup cleanup. Undo the last send. | connection |
| `ui` | Main window (sidebar: Files, Screen, Compose, Device), present window, status bar, red/black theme. | all above |

### Data locations
- App config: `~/.config/rmdesk/config.toml` (Wi-Fi IP, preferences).
- Backups: `~/.local/share/rmdesk/backups/<notebook-uuid>/<timestamp>/`.
- Logs: `~/.local/state/rmdesk/rmdesk.log`.

## 4. Features

### 4.1 Device (setup)
- First run: connect over USB, ask for the root password (shown on the tablet under *Settings → Help → Copyrights and licenses*), store it in the keyring, and offer to install the user's SSH public key.
- Save the Wi-Fi IP for later Wi-Fi connections.
- Show the firmware version, free storage, and whether goMarkableStream is installed; install or uninstall it after a confirm step.

### 4.2 Files
- Folder tree, plus a document list with thumbnails and a search box.
- **Upload** (drag and drop of PDF/EPUB onto the window or onto a folder):
  - USB: the tablet's USB web interface (select the target folder, then POST to upload). The tablet does the import, so no restart is needed.
  - Wi-Fi: write the document files (`<uuid>.pdf|epub`, `.metadata`, `.content`) over SFTP, then restart `xochitl`.
- **Download:** *Save as PDF* (notebooks and annotated documents) or *Save original*.
  - USB: the PDF export from the USB web interface (the tablet's own renderer).
  - Wi-Fi: render locally with `rmc`; for annotated PDFs, overlay on top of the original PDF.
- **Delete:** set `parent` to `trash` in the metadata (the same as deleting on the tablet). Never delete permanently.
- Requirement: USB web interface uploads/exports need *Settings → Storage → USB web interface* turned on. If it's off, the app says so and falls back to the Wi-Fi method.

### 4.3 Screen
- **View mode:** fit to window, follow rotation, screenshot to PNG.
- **Present mode:** a separate borderless window titled `rmdesk — Present`, which can be selected by screen-capture tools. Options: drag a box to zoom/crop, invert colours, show the pen position as a red dot.
- The stream runs only while a Screen view is visible.

### 4.4 Compose
1. Pick a notebook, then a page (grid of page thumbnails).
2. Preview the page (rendered with `rmc`). Add items and drag/resize them on it:
   - **Image** (PNG/JPG/SVG): SVG paths → polylines directly; raster → grayscale → threshold (a *detail* slider) → contours/centerlines → simplified polylines. Live preview of the traced result.
   - **Ink text**: a single-line (Hershey-style) font → polylines; a size setting.
   - **Typed text**: added to the page's text block (editable on the tablet; position follows the tablet's text layout).
   - For each item: pen type (fineliner, ballpoint, marker), colour (black, grey, white), thickness.
3. **Send to page** → the `safety` flow (§5.3). Existing scene items are kept unchanged; new items are added on a new layer named `rmdesk`.
4. **Undo last send** restores that backup.

## 5. Error handling and safety

### 5.1 Connection
- The connection state is always visible in the status bar. Retries run in the background with backoff.
- Actions that need the tablet are disabled while offline, with a tooltip saying why.
- Authentication or host-key-change failures show a clear message and open Device setup.

### 5.2 Firmware check
- At connect: read the firmware version (`/etc/version` and the os-release info) and the format version of a sample `.rm` page.
- If either is newer than the tested list, **Compose is disabled** ("This firmware isn't tested yet. Writing to notebooks is disabled."). Files and Screen still work.
- If the firmware changed since the last connection: check whether goMarkableStream is still installed and offer to reinstall it.

### 5.3 Notebook write transaction
1. Copy the whole notebook (`<uuid>/`, `.metadata`, `.content`) to the local backup folder and check every file's size and hash.
2. Build the new `.rm` locally; read it back with `rmscene` and check that all original items are present and unchanged and the new items are there. On failure, stop and send nothing.
3. Upload as `<page>.rm.rmdesk-tmp`, then rename it over `<page>.rm` (atomic on the tablet's filesystem).
4. `systemctl restart xochitl` and wait until it's running again (timeout 30 s).
5. If step 3 or 4 fails, restore the backup automatically, restart, and report what happened.
- Backups older than 30 days are deleted, except the most recent 5 per notebook.

### 5.4 Sync conflicts
- If the notebook's `lastModified` changed since the page was loaded: reload it and ask the user to re-check item placement before sending.
- The first send to a cloud-synced notebook shows a one-time warning about possible sync conflicts.

### 5.5 Transfers
- Each file reports its own failures (wrong type, storage full, connection lost); the other files still go through.
- Downloads are written to a temporary file and renamed when complete.

## 6. Testing

- **Unit (pytest, no tablet):**
  - `library`: tree built from sample metadata.
  - `notebook`: round-trip of sample `.rm` files, adding items keeps all original items byte-for-byte equal in the scene tree.
  - `compose`: traced strokes stay inside the target box; text strokes have the expected size and position.
  - `safety`: a fake tablet that fails at each step; checks the backup/restore order and that a failed transaction never leaves a modified page.
  - `stream`: decoding of saved sample frames.
- **Sample files:** public sample pages from the `rmscene` project; later, a read-only copy of one of the user's notebooks to cover their firmware.
- **On the device (manual, with the user):** Compose is only tested on a newly created test notebook. Check the result on the tablet, then test Undo.
- **UI smoke test** (pytest-qt): the app starts and each section opens without errors.

## 7. Installation

- Source: `~/Work/rmdesk` (git).
- pacman: `pyside6`, `python-paramiko`, `python-keyring`, `python-opencv`, `python-pytest`, `python-pytest-qt`.
- Private venv (`--system-site-packages`) in `~/.local/share/rmdesk/venv` for `rmscene` and `rmc`; the app itself is installed there in editable mode.
- Launcher `~/.local/bin/rmdesk`; the `.desktop` entry, icon and any keybind live in `~/dotfiles` (stowed into place) according to the user's dotfiles convention.

## 8. Build order

Each stage works on its own:
1. `connection` + Device setup.
2. Files.
3. Screen (`stream` + view/present windows).
4. Compose (`notebook`, `compose`, `safety`).

## 9. Open items to settle during implementation

- The goMarkableStream version and its frame protocol, pinned to the latest release that supports the user's firmware. Its exact frame format is confirmed from its source before writing `stream`.
- Whether the user's firmware supports more than one text block per page or free-position text. v1 assumes one text block per page.
- The tested firmware/format version list (§5.2), filled in from the user's tablet at stage 1.
