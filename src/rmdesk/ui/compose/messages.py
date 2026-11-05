"""Plain-language wording for composing on the live Screen view.

No Qt: `describe_*` map an exception to (message, details); `details` is the
technical text, shown smaller under the message.
"""
from __future__ import annotations

from rmdesk.device import DeviceError
from rmdesk.firmware import NotTestNotebook
from rmdesk.notebook.opened import NotEditable, OpenedPage
from rmdesk.notebook.page import UnsupportedPage, VerifyError
from rmdesk.safety import (PAGE_CHANGED_MSG, BackupError, Cancelled, NotebookChanged,
                           PageChangedSinceSend, RestartTimeout, RestoreError, SendFailed,
                           StopTimeout, UndoFailed, UndoUnavailable, start_error_of)

NOT_CONNECTED = "The tablet isn't connected."
NOTHING_CHANGED = "Nothing was changed."
NOT_RESPONDING = "The tablet didn't respond. Nothing was changed."
CANT_EDIT = "This page can't be edited safely by rmdesk. Nothing was changed."
CHANGED = "The page changed on the tablet. Check the item positions and send again."
SEND_RESTORED = "Couldn't write the page. Your notebook was put back the way it was."
SEND_NOT_RESTORED = ("Couldn't write the page, and putting it back also failed. "
                     "Your backup is at {backup}.")
SCREEN_RESTART = "The tablet's screen may need a restart (hold the power button)."
BACKUP_FAILED = "Couldn't make a backup of the notebook, so nothing was changed."
NOTHING_TO_SEND = "There is nothing to draw on the page: every item is outside the page or empty."
UNDO_FAILED = "Couldn't undo the last send."
UNDO_SAFETY = "The page as it was before the undo is backed up at {backup}."
UNDO_PARTIAL = "Some files on the tablet were already replaced."
UNDONE = ("The last send to {name} was undone. The notebook as it was just before the undo "
          "is backed up at {backup}.")
UNDO_CONFIRM = ("Remove the last send from {name}? Only the page it was sent to is put back, "
                "and only if that page hasn't changed on the tablet since; other pages and "
                "the notebook's settings are left alone.")
UNDO_TIP = "Put the page of the last send to {name} back the way it was"
UNDO_PAGE_CHANGED = PAGE_CHANGED_MSG
UNDO_UNAVAILABLE = ("This send was made by an older rmdesk that didn't record which page it "
                    "wrote, so it can't be undone safely.")
UNKNOWN_NOTEBOOK = "the notebook of the last send"
UNDO_NO_SCREEN = ("Undo is done, but the tablet's screen didn't come back on. "
                  "Hold the power button to restart it.")
DIALOG_STALE = "Something changed while the dialog was open \u2014 check it and try again."
SENT = "Sent to the tablet. You can undo this with “Undo last send”."
SENT_TO = ("Sent to \u201c{name}\u201d, page {page} of {total}. "
           "\u201cUndo last send\u201d takes it back.")
SENT_HIDDEN = " The rmdesk layer is hidden on the tablet: show it in the layers menu to see it."
INK_SKIPPED = "These characters can't be drawn as ink and were left out: {chars}"
SHADE_WIDER = "This picture is very detailed; rmdesk used wider line spacing ({n} px)."
SHADE_PARTIAL = "This picture is very detailed; only part of it fits."
PICTURE_BLANK = "Nothing to draw \u2014 the picture is blank. Remove it to send the rest."
BLANK_TIP = "Remove the blank picture to send."
NOT_SENT = "Nothing was sent."
LAYOUT_CHECK_FAILED = ("rmdesk couldn't tell whether the tablet is in its keyboard layout, "
                       "so nothing was sent.")
SCROLL_NO_STROKES = ("This page has no handwriting yet, so rmdesk can't tell how far it is "
                     "scrolled with the Type Folio attached. Draw a small mark anywhere on the "
                     "visible part of the page, then send again. Nothing was changed.")
SCROLL_UNKNOWN = ("rmdesk couldn't line up the live picture with this page, so it can't tell "
                  "how far the page is scrolled. Make sure some of your writing is on screen, "
                  "press Refresh, and try again. Nothing was changed.")
# a typed-only page: rmdesk scrolls it to its top to measure where it was (scroll_probe)
CHECKING_PAGE = "Checking the page open on the tablet\u2026"
MEASURING = "Measuring where the page is\u2026"
MEASURED_NOTE = "rmdesk scrolled the page to the top to measure where it was."
# placed items follow the page as it scrolls (compose.scroll_track); this picture changed in a
# way no scroll explains (another page, a menu over most of it): the items stayed put
PAGE_CHANGED = "The tablet page changed — your items may not match it."


def offscreen_label(n: int, where: str) -> str:
    """The edge marker for placed items scrolled off the picture: "2 items above"."""
    return f"{n} item{'s' if n != 1 else ''} {where}"
MEASURE_UNCLEAR = ("This page has no handwriting, and the live picture has too little on it "
                   "(or only a repeating template) for rmdesk to measure how far the page is "
                   "scrolled. Draw a small mark anywhere on the visible part of the page, then "
                   "send again. Nothing was changed.")
MEASURE_MOVED = ("The page changed in a way rmdesk didn't expect while it measured how far the "
                 "page is scrolled (was the tablet touched?). Nothing was changed.")
MEASURE_NO_EFFECT = ("rmdesk's swipes didn't scroll the page, so it couldn't measure where it is. "
                     "Check that Control works (tap something on the picture), then send again. "
                     "Nothing was changed.")
MEASURE_BOUND = ("rmdesk couldn't scroll the page to its top in time to measure where it was. "
                 "Nothing was changed.")
MEASURE_NO_PICTURE = ("The tablet's picture stopped updating while rmdesk measured how far the "
                      "page is scrolled. Press Refresh and send again. Nothing was changed.")
MEASURE_SCROLLED = ("rmdesk scrolled the page toward its top: scroll back to where your items "
                    "go, then send again.")
KEYBOARD_CHECK_FAILED = ("rmdesk couldn't check whether the Type Folio is attached, so nothing "
                         "was sent.")
LANDSCAPE_OFF = ("Sending to landscape pages is turned off while rmdesk re-checks landscape "
                 "positions. Use a portrait page for now.")
CONFIRM = "Add {items} to \u201c{name}\u201d, page {page} of {total}?"
SYNCED = ("This notebook syncs with the reMarkable cloud. If it was edited elsewhere since its "
          "last sync, the cloud copy may conflict with this change.")
HIDDEN_LAYER = ("The rmdesk layer is hidden on this page \u2014 show it in Layers on the tablet "
                "to see the new ink.")
PICTURE_FAILED = "Couldn't read this picture."
SENDING = "Backing up and writing… the tablet screen will go blank for a few seconds."
UNDOING = "Undoing the last send… the tablet screen will go blank for a few seconds."


class NotConnected(Exception):
    """Connection.ensure() failed: nothing was attempted on the tablet."""


class NothingToSend(Exception):
    pass


class PictureFailed(Exception):
    """A picture couldn't be read or shaded for the send (before the tablet was touched)."""


def confirm_text(n: int, opened: OpenedPage, hidden_layer: bool = False) -> str:
    """The question asked while xochitl is stopped, naming the page that would be written;
    warns when the reused rmdesk layer is hidden and (every time) when the notebook syncs."""
    items = "1 item" if n == 1 else f"{n} items"
    parts = [CONFIRM.format(items=items, name=opened.name, page=opened.page_index + 1,
                            total=opened.page_count)]
    if hidden_layer:
        parts.append(HIDDEN_LAYER)
    if opened.synced:
        parts.append(SYNCED)
    return "\n\n".join(parts)


def char_list(chars: list[str]) -> str:
    return ", ".join(_show(c) for c in chars)


def _show(c: str) -> str:
    if c.isprintable():
        return c
    return f"U+{ord(c):04X}"


def _restart_note(e: BaseException | None) -> str:
    return " " + SCREEN_RESTART if e is not None else ""


def describe_send_error(e: BaseException) -> tuple[str, str]:
    """A failed xochitl start (start_error_of) always adds SCREEN_RESTART: never silent."""
    text, detail = _describe_send_error(e)
    start_error = start_error_of(e)
    if start_error is not None and SCREEN_RESTART not in text:
        text += _restart_note(start_error)
        detail = f"{detail} (starting xochitl again failed: {start_error})".lstrip()
    return text, detail


def _describe_send_error(e: BaseException) -> tuple[str, str]:
    detail = str(e)
    if isinstance(e, Cancelled):
        return NOT_SENT, ""
    if isinstance(e, NotEditable):
        return e.reason, ""
    if isinstance(e, NotConnected):
        return f"{NOT_CONNECTED} {NOTHING_CHANGED}", detail
    if isinstance(e, NotTestNotebook):
        return str(e), ""
    if isinstance(e, NothingToSend):
        return NOTHING_TO_SEND, ""
    if isinstance(e, PictureFailed):
        return f"{PICTURE_FAILED} {NOTHING_CHANGED}", detail
    if isinstance(e, NotebookChanged):
        return CHANGED, detail
    if isinstance(e, SendFailed):
        if e.restored:
            return SEND_RESTORED + f" (Backup: {e.backup_dir})" + _restart_note(e.restart_error), detail
        return SEND_NOT_RESTORED.format(backup=e.backup_dir) + _restart_note(e.restart_error), detail
    if isinstance(e, (UnsupportedPage, VerifyError, ValueError)):
        return CANT_EDIT, detail
    if isinstance(e, BackupError):
        return BACKUP_FAILED, detail
    if isinstance(e, (StopTimeout, RestartTimeout, DeviceError)):
        return NOT_RESPONDING, detail
    # send_to_open_page re-raises errors from before the write unchanged; every
    # failure after the write is a SendFailed.
    return f"Something went wrong. {NOTHING_CHANGED}", detail


def undo_completed(e: UndoFailed) -> bool:
    """Every file was restored; only starting xochitl afterwards failed."""
    return e.files_written and e.restart_error is not None and e.cause is e.restart_error


def describe_undo_error(e: BaseException) -> tuple[str, str]:
    """A failed xochitl start (start_error_of) always adds SCREEN_RESTART: never silent."""
    text, detail = _describe_undo_error(e)
    start_error = start_error_of(e)
    if start_error is not None and SCREEN_RESTART not in text and text != UNDO_NO_SCREEN:
        text += _restart_note(start_error)
    return text, detail


def _describe_undo_error(e: BaseException) -> tuple[str, str]:
    detail = str(e)
    if isinstance(e, NotTestNotebook):
        return detail, ""
    if isinstance(e, NotConnected):
        return f"{NOT_CONNECTED} {NOTHING_CHANGED}", detail
    if isinstance(e, UndoFailed) and undo_completed(e):
        return UNDO_NO_SCREEN, f"{detail} (backup of the state before the undo: {e.safety_backup})"
    if isinstance(e, UndoFailed):
        state = NOTHING_CHANGED if not e.files_written else UNDO_PARTIAL
        msg = f"{UNDO_FAILED} {state} {UNDO_SAFETY.format(backup=e.safety_backup)}"
        return msg + _restart_note(e.restart_error), detail
    if isinstance(e, PageChangedSinceSend):
        return f"{UNDO_PAGE_CHANGED} {NOTHING_CHANGED}" + _restart_note(e.restart_error), ""
    if isinstance(e, UndoUnavailable):
        return f"{UNDO_UNAVAILABLE} {NOTHING_CHANGED}", detail
    if isinstance(e, RestoreError):
        return f"{UNDO_FAILED} {NOTHING_CHANGED}" + _restart_note(e.restart_error), detail
    if isinstance(e, DeviceError):
        return NOT_RESPONDING, detail
    return f"{UNDO_FAILED} {NOTHING_CHANGED}", detail
