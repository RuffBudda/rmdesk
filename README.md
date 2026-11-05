# rmdesk

A Linux desktop app for the **reMarkable 2**. It lets you:

- see the tablet's screen live and **control it from your PC** (mouse = touch, keyboard = typing);
- **place pictures, diagrams, text, shapes and sticky notes onto the page open on the tablet**, as real pen strokes you can erase and lasso there;
- **manage files**: upload PDFs and EPUBs, save notebooks as PDF, move things to the trash.

It talks to the tablet over USB or Wi-Fi using SSH. Nothing goes through the reMarkable cloud.

> rmdesk is an independent project. It is not made by or affiliated with reMarkable AS. It changes files on your tablet, carefully and with backups, but use it at your own risk. It was developed and tested on a reMarkable 2 with firmware **3.29.1.0**.

---

## Contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Install](#install)
4. [Prepare the tablet](#prepare-the-tablet)
5. [First start](#first-start)
6. [Using rmdesk](#using-rmdesk)
7. [Keyboard shortcuts](#keyboard-shortcuts)
8. [How it keeps your notes safe](#how-it-keeps-your-notes-safe)
9. [Where rmdesk keeps things](#where-rmdesk-keeps-things)
10. [Troubleshooting](#troubleshooting)
11. [Development](#development)
12. [Credits](#credits)

---

## Features

**Screen**
- A live picture of the tablet, with rotation (remembered), screenshots and an **Expand** mode where the picture fills the window.
- **Control** (on by default): click to tap, drag to swipe, the mouse wheel scrolls, and your PC keyboard types on the tablet. Typing goes through the Type Folio. **Ctrl+V** types the clipboard's text. **1x** resets the tablet's zoom.
- **Keep refreshing**: keeps the picture current while you type on the tablet's keyboard, which otherwise sends no new frames.
- **Refresh** restarts the tablet's picture service, which fixes a frozen or noisy picture.

**Place** (put things on the page)
- **Pictures:**
  - **Shaded** for photos (engraving-style ink).
  - **Line art** for diagrams, charts and logos. Lines are traced into clean strokes. With **Sharp text**, words are recognised (Tesseract, on your PC only) and redrawn in a clean pen font.
- **Ink text** typed straight onto the page: fonts, bold and italic, alignment, size, line and letter spacing, copy and paste, and the usual editing shortcuts.
- **Shapes**: rectangle (with optional hatching and text inside), line, arrow (single or double) and sticky note.
- **Several at once:** drag and drop or **Ctrl+V** pictures and files onto the picture.
- **Long pages:** placed items **follow the page as it scrolls**, so you can lay out several things down a long page and send them together.
- Everything goes on a separate **rmdesk** layer. **Undo last send** takes back the last send if that page hasn't changed since.
- Works with the **Type Folio** attached (the tablet's keyboard layout), on scrolled pages, and on pages with typed text.

**Files**
- Browse the tablet's folders, search, and upload PDFs and EPUBs by drag and drop.
- Save notebooks as PDF, or save any document's original file.
- Move several items to the trash at once.
- **Fast USB uploads** (optional): uses the tablet's USB web interface so uploads don't restart the tablet's screen.

**Device**
- Connection over USB or Wi-Fi, firmware and storage details, battery level.
- Screen-streaming service install and uninstall, and settings.

---

## Requirements

- **Linux** with a desktop (Wayland or X11). Developed on Arch Linux (Omarchy / Hyprland). Other distributions work if you install the same packages yourself.
- **Python 3.12 or newer.** 3.14 is recommended; on 3.12 and 3.13 the `zstandard` package is installed automatically.
- **Qt 6 for Python (PySide6)** and a few Python libraries; see the [Install](#install) step.
- **A reMarkable 2** with:
  - its SSH password, shown on the tablet (see [Prepare the tablet](#prepare-the-tablet));
  - a USB cable, or the same Wi-Fi network as your PC.
- Optional: **Tesseract OCR** with English data, for **Sharp text** in line art.

---

## Install

### 1. Get the code

```bash
git clone https://github.com/RuffBudda/rmdesk.git
cd rmdesk
```

### 2. Install the system packages and the app

**Arch Linux / Omarchy:** one script does it all:

```bash
scripts/setup-venv.sh
```

It:
- installs the needed packages with `pacman` (it asks for your password with `sudo`): `pyside6 python-paramiko python-keyring python-opencv python-numpy python-requests python-websocket-client python-pytest python-pytest-qt`;
- creates a Python environment in `~/.local/share/rmdesk/venv`, which uses the system packages above;
- installs rmdesk into it, together with `rmscene`, `rmc` and `Hershey-Fonts` from PyPI and GitHub.

**Other distributions:** install the equivalents of the packages above (Qt 6 / PySide6, paramiko, keyring, OpenCV for Python, NumPy, requests, websocket-client) with your package manager, then:

```bash
python3 -m venv --system-site-packages ~/.local/share/rmdesk/venv
~/.local/share/rmdesk/venv/bin/pip install -e .
```

If your distribution has no PySide6 package, leave out `--system-site-packages`. Then add PySide6, paramiko, keyring, opencv-python, numpy, requests and websocket-client to the pip install line.

### 3. Optional: Sharp text (recognising words in diagrams)

```bash
sudo pacman -S tesseract tesseract-data-eng      # Arch
# sudo apt install tesseract-ocr tesseract-ocr-eng   # Debian/Ubuntu
```

Without it, line art still works; the **Sharp text** option is just greyed out.

### 4. Optional: a nicer font

rmdesk's design uses the **Inter** font when it is installed (`sudo pacman -S inter-font`); otherwise your system font is used.

### 5. Add it to your app menu

```bash
scripts/install-desktop.sh
```

This puts a `rmdesk` command in `~/.local/bin` and an **rmdesk** entry with an icon in your app menu. You can also start it directly:

```bash
~/.local/share/rmdesk/venv/bin/python -m rmdesk
```

### Updating

```bash
cd rmdesk
git pull
~/.local/share/rmdesk/venv/bin/pip install -e .
```

---

## Prepare the tablet

1. **Find the SSH password.** On the tablet: **Settings → Help → Copyrights and licenses**. The password, and the tablet's IP addresses, are listed in the General information section. rmdesk asks for it once and keeps it in your system keyring (for example GNOME Keyring or KWallet), keyed by the tablet's serial number.
2. **USB:** plug the tablet in. It is reachable at `10.11.99.1`.
3. **Wi-Fi (optional):** recent firmware only allows SSH over USB until you turn Wi-Fi SSH on. With the tablet plugged in by USB, run this once:

   ```bash
   ssh root@10.11.99.1 rm-ssh-over-wlan on
   ```

   Then enter the tablet's Wi-Fi IP address in rmdesk. With this on, anyone on the same network who knows the tablet's password can log in to it, so keep it to trusted networks. Turn it off again with `rm-ssh-over-wlan off`. Pressing **Install my SSH key** in rmdesk's Device page lets rmdesk log in with your SSH key instead of the password.
4. **Screen streaming:** rmdesk's live picture uses [goMarkableStream](https://github.com/owulveryck/goMarkableStream) on the tablet. The first time, open **Device** (the connection button at the top right of the Screen view) and press **Install** under *Screen streaming*. rmdesk downloads the release that matches your firmware, checks its checksum and sets it up.

---

## First start

When rmdesk starts it shows a connection screen until it reaches the tablet:
- **Looking for your reMarkable…** over USB and, if set, Wi-Fi. It retries every 5 seconds.
- A **password field** if no password is saved yet, with a reminder of where to find it.
- Clear messages when something is wrong (wrong password, Wi-Fi SSH off, no route), plus **Try again** and **Open settings**.

Once connected, it opens the **Screen** view.

---

## Using rmdesk

### The Screen view

The header holds, from left to right:

| Group | What it does |
|---|---|
| **Control / Place** | What a click on the picture does: drive the tablet (**Control**) or edit the items you are placing (**Place**). **1x** resets the tablet's zoom (Control only). |
| **Refresh · Keep refreshing · Rotate** | Get a fresh picture (restarts the stream), keep it fresh while typing, turn the view. |
| **Screenshot · Expand** | Save the picture as a PNG; hide everything but the picture (**F11**, **Esc** to go back). |
| **Files · Connection · battery** | Open the Files page, the connection and settings page, and see the tablet's battery. |
| **Undo · Send to tablet** | Take back the last send; write the placed items to the tablet. |

Hover over any button for a short description.

### Placing things on a page

1. Open the notebook page on the tablet.
2. In rmdesk choose **Place**, then add a picture, ink text, shape or sticky note from the floating toolbar. You can also drop or paste (**Ctrl+V**) pictures onto the picture.
3. Move, resize and rotate items with their handles. Double-click text, a note or a rectangle to type in it; the format bar above it sets the font, size and so on.
4. Scroll the tablet's page (with Control or by hand) to place more further down. Items stay attached to their spot.
5. Press **Send to tablet**. The tablet's screen pauses for a few seconds while rmdesk writes the page, then shows your items. The message under the picture says which notebook and page it went to.
6. Not right? **Undo last send** puts the page back.

Notes:
- Pictures are drawn as pen strokes, because reMarkable notebook pages can't hold images. Use **Shaded** for photos and **Line art** for diagrams.
- Only plain notebook pages can be written to. PDFs, EPUBs and zoomed-in pages are refused.
- **With the Type Folio attached**, rmdesk works out how far the page is scrolled by lining up your handwriting on screen. If the page has none, it briefly scrolls the page to its top to measure it, and tells you so.
- **Device → Sending → Ask before writing to the tablet** brings back a confirmation before each send.

### Files

**Files** (Ctrl+1) lists the tablet's folders and documents. From there you can:
- drag in PDFs and EPUBs;
- select items and choose **Save as PDF**, **Save original** or **Move to Trash**.

Changes that need the tablet's screen to restart are collected. A banner offers **Refresh tablet** to apply them all at once.

### Device

**Device** (Ctrl+3, or the connection button) shows:
- the connection, with the password and Wi-Fi address;
- tablet info: firmware, serial, storage and battery;
- screen streaming;
- Fast USB uploads;
- sending options.

---

## Keyboard shortcuts

| Where | Keys |
|---|---|
| Anywhere | **Ctrl+1** Files · **Ctrl+2** Screen · **Ctrl+3** Device · **F11** Expand / back |
| Expand | **Esc** or **F11** to go back |
| Control | Typing goes to the tablet · **Ctrl+V** types the clipboard (pictures on the clipboard are placed instead) · **Ctrl+0** zoom back to 1x |
| Place | **Ctrl+V** pastes a picture, files or text · **[ / ]** rotate the selected item 15° · **Enter** start typing into the selected text item |
| Typing in a text box | **Ctrl+←/→** by word · **Shift+** to select · **Ctrl+A** all · **Ctrl+C / X / V** · **Ctrl+Z / Y** · **Ctrl+Backspace/Delete** a word · **Ctrl+B / I** bold, italic · **Ctrl+L / E / R** align · **Ctrl+Plus / Minus** size · **Esc** stop typing |

---

## How it keeps your notes safe

Writing to a page always follows the same steps:
1. Pause the tablet's interface (`xochitl`) so it saves what it has.
2. **Back up** the page and its notebook's settings to your PC.
3. Check that the page hasn't changed.
4. Build the new page and **verify** it. The original content must be byte-for-byte unchanged, and only new strokes on the rmdesk layer may be added.
5. Write it in one step (temporary file, then rename).
6. Restart the interface and check that it stays up.

If anything fails, the backup is put back before the tablet restarts. **Undo** only touches the one page the last send wrote, and only if that page hasn't changed since (it is still exactly as rmdesk left it).

Other safeguards:
- Uploads, the trash and Control never edit notebook pages.
- Control never sends anything while a send or another tablet job is running.
- Releasing Control always lifts the finger and releases every key on the tablet.

---

## Where rmdesk keeps things

| What | Where |
|---|---|
| Settings | `~/.config/rmdesk/config.toml` |
| Tablet password | your system keyring (service `rmdesk`) |
| Backups made before each write | `~/.local/share/rmdesk/backups/`. The newest are kept, older ones are pruned after 30 days. Only you can read them. |
| Log file | `~/.local/share/rmdesk/rmdesk.log` |
| Diagnostics for failed sends with the folio (pictures of the screen and the page) | `~/.local/share/rmdesk/debug/`: the newest 3, readable only by you, never uploaded |

---

## Troubleshooting

| Problem | What to do |
|---|---|
| **"Unable to connect to port 22"** over Wi-Fi | Turn on SSH over Wi-Fi (see [Prepare the tablet](#prepare-the-tablet)). Check the IP address under Settings → Help → Copyrights and licenses. A sleeping tablet turns its Wi-Fi off. |
| **Screen picture is frozen, or has a band of noise** | Press **Refresh**: it restarts the picture service on the tablet. |
| **"Another viewer is connected"** | Close other stream viewers, or press **Restart stream**. After switching between USB and Wi-Fi, the old connection can hold the stream for a while. |
| **Typing on the tablet stops updating the picture** | Turn on **Keep refreshing**. |
| **The tablet acts as if Shift or Alt is held** | Press and release Shift, Ctrl and Alt on the Type Folio. rmdesk releases all modifiers whenever Control starts or stops. |
| **A send is refused** | The message says why: a PDF, a zoomed page, an empty page, or "can't tell how far the page is scrolled". For the last one, have some of your writing on screen, or let rmdesk measure, and send again. |
| **Something else** | Look at `~/.local/share/rmdesk/rmdesk.log`. |

---

## Development

```bash
QT_QPA_PLATFORM=offscreen timeout 600 ~/.local/share/rmdesk/venv/bin/python -m pytest -q
```

The suite (about 1,700 tests) runs offscreen against fake tablets and never touches a real device.

Project layout:

| Path | Contents |
|---|---|
| `src/rmdesk/` | the app |
| `src/rmdesk/safety.py` | the write transaction |
| `src/rmdesk/notebook/` | reading, extending and verifying `.rm` pages |
| `src/rmdesk/compose/` | turning items into strokes, and the screen-to-page mapping, with no Qt |
| `src/rmdesk/remote/` | Control's input events |
| `src/rmdesk/stream/` | the screen stream client |
| `src/rmdesk/ui/` | the Qt interface |
| `scripts/` | setup and installers, plus device test scripts that write only to a notebook named exactly **rmdesk test** |
| `docs/superpowers/` | design notes and plans |

---

## Credits

- [goMarkableStream](https://github.com/owulveryck/goMarkableStream): the screen stream on the tablet.
- [rmscene](https://github.com/ricklupton/rmscene) and [rmc](https://github.com/ricklupton/rmc): reading and writing reMarkable v6 pages, and PDF export. The test fixtures include rmscene's sample pages (MIT).
- [Hershey-Fonts](https://pypi.org/project/Hershey-Fonts/): the single-stroke fonts used for ink text.
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract): Sharp text.
- reMarkable is a trademark of reMarkable AS.
