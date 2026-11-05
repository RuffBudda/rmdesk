# rmdesk — Live Compose design

**Date:** 2026-11-03
**Status:** Draft for review
**Builds on:** `2026-11-03-rmdesk-design.md` (rmdesk v1)

## 1. Purpose

Replace the separate Compose section (notebook/page picker + rendered preview) with a **Compose mode on the live Screen view**: the user places images and ink text directly over the live picture of the tablet, and sends them to **whatever notebook page is open on the tablet**.

### Why
- The rendered preview is blank on pages the tablet has re-saved (rmc does not place xochitl's anchored sub-groups) — found on firmware 3.29.1.0.
- The user wants to see exactly where items land on the real page.
- The user wants text they can move; typed text (fixed to the page's text area) is not needed.

### User decisions (this session)
- Target page: **follow the tablet** (the page open on the tablet).
- Typed text: **removed**; ink text only.
- Layer: keep a **separate "rmdesk" layer**, reused if it already exists on the page.
- Approach A: Compose is a mode of the Screen view.
- Flow: identify the open page **after** stopping xochitl, confirm while stopped.

### Success criteria
- With the tablet showing a notebook page at normal zoom, the user can place an image and ink text over the live view, press Send, confirm "Add N items to <notebook>, page <n> of <total>?", and the items appear on that page exactly where they were placed (within ±3 px at screen scale), in portrait and landscape notebooks.
- No write ever happens to a page other than the one confirmed; every failure leaves the page unchanged or restored, and the tablet UI running.

### Non-goals
- Zoomed/panned pages (refused with a message in this version).
- Real pictures (separate project).
- Typed text.
- PDFs/EPUBs as targets (refused).

## 2. Device facts this design relies on (verified 2026-11-03, fw 3.29.1.0)

- `<id>.metadata` has `lastOpened` (ms string) and `lastOpenedPage` (0-based index); `<id>.content` has `cPages.pages[]`, `orientation` (`portrait`/`landscape`), `pageCount`, and `customZoom*` fields.
- These files are **not** updated while the notebook is open: after turning to a new page 2, the files still showed page 0 of 1 and no page-2 file. xochitl writes its state when the notebook closes or xochitl stops.
- goMarkableStream frames are 1404×1872 framebuffer pixels; on a landscape notebook the content appears rotated in the frame.
- reMarkable tools (eraser, lasso) act only on the selected layer.

To be verified in build step 2 (calibration): that xochitl writes `lastOpenedPage`/page list on `systemctl stop`; the exact framebuffer→page transform for portrait and landscape; how zoom is recorded.

## 3. UI

- **Sidebar:** Files, Screen, Device (Compose section removed).
- **Screen view → Compose toggle.** When on, a compose toolbar appears: Add image…, Add ink text…, Remove, pen tool, colour, thickness, detail slider (images), Send to tablet, Undo last send.
- Items are drawn in the accent red over the live picture; drag to move, corner handle to resize. Items are stored in **frame coordinates** (1404×1872, unrotated); the view's Rotate setting only changes how they are drawn, never their stored position.
- Rotate, ⟳ Refresh, Keep refreshing and Present keep working. **Present never shows compose items.**
- Items survive a failed or cancelled send; they are cleared after a successful send.
- Testing mode (untested firmware) still applies, checked against the notebook identified at send time.

## 4. Send flow

1. **Pre-checks (no device I/O):** ≥1 item, all image traces finished, no other tablet job (R45 job guard), compose allowed for this firmware (full or test-only).
2. **Stop xochitl** (`safety.stop_xochitl`).
3. **Identify the open page** (new pure function, see §6): the DocumentType with the greatest `lastOpened`; its `lastOpenedPage` index into the non-deleted `cPages.pages`; its `orientation` and zoom fields; read `<id>/<page>.rm`.
   Refuse — start xochitl, write nothing, show the reason — when: no document found; it isn't a notebook (`fileType` pdf/epub); the page index is out of range; the page file doesn't exist (never drawn on — existing blank-page message); zoom isn't the default fit; any file unreadable/unparseable.
4. **Confirm while stopped:** "Add N items to “<name>”, page <n> of <total>?" — Yes / No. **No, or no answer in 60 s → start xochitl, write nothing.**
5. **Build the page:** convert item polylines from frame coordinates to page coordinates (§5), `append_to_rmdesk_layer` (§7) the lines.
6. **Safe write** (existing `safety` transaction, reusing the already-stopped state): post-stop backup → testing-mode guard (test-only firmware) → page bytes in the backup must equal the bytes read in step 3 → `verify` → tmp + rename → start xochitl. Failure after stop → restore while stopped → start → error naming the backup. `cfg.last_send` stored on success; Undo unchanged.
7. Live stream reconnects by itself after the restart (existing reconnect); a catch-up full frame shows the result.

`safety.send_to_page` gains the ability to run with xochitl already stopped by the caller (a `stopped=True` path) so steps 2–6 happen in one stop/start cycle.

## 5. Coordinates

- Portrait notebook, default zoom: `x_page = x_frame − 702`, `y_page = y_frame`.
- Landscape notebook, default zoom: the page is shown rotated 90° in the frame; the transform (rotation direction and offsets) is **pinned by the calibration step** and encoded as a pure function `frame_to_page(x, y, orientation) -> (x, y)` with tests built from the calibration results.
- Zoom ≠ default → refused (§4).
- Clipping to the page rectangle stays (existing `build._clip`).

## 6. New core units (no Qt)

- `notebook/opened.py`: `OpenedPage(notebook_id, name, page_id, page_index, page_count, orientation, data: bytes)`; `find_opened_page(device) -> OpenedPage`, raising `NotEditable(reason: str)` for every refusal in §4.3.
- `compose/frame.py`: `frame_to_page(x, y, orientation)`, plus applying it to polylines.

## 7. Reusing the rmdesk layer

- `Page.append_to_rmdesk_layer(lines)`: if the page already has a visible-or-hidden layer under root labelled `rmdesk` (the last one in root order if several), append lines to it; otherwise create one as today.
- `verify(original, new, added_lines, added_text=None)` accepts lines added under an existing `rmdesk` layer: every original block still byte-identical and in order; exactly `added_lines` new line items, all under that layer; no other new items; no layer label/visibility change. The new-layer-last rule applies only when a layer is created.
- Lines inside xochitl-created sub-groups of the rmdesk layer stay untouched; new lines go directly under the rmdesk layer node.

## 8. Removed

The Compose section (`ui/compose_view.py`, `ui/compose/*` scene/preview/picker), page preview rendering for Compose, and typed text in the UI. `Page.append_text` stays in the core (tested) but is unused by the UI.

## 9. Testing

- `find_opened_page`: most-recent notebook chosen; page index → page id; orientation; refusals (pdf, out of range, missing page file, zoom, bad JSON) each raise `NotEditable` with a clear reason.
- Send flow (FakeDevice): stop before reads; confirm No / timeout → no write, xochitl started; Yes → write and start; failure after stop → restore + start; job guard held throughout.
- `frame_to_page`: portrait formula; landscape from calibration points.
- `append_to_rmdesk_layer` + `verify` on every fixture including `user-3.29.1.0-rmdesk-test.rm` (which already has an rmdesk layer with xochitl sub-groups); verify rejects a line added elsewhere, a changed original, a relabelled layer.
- UI (offscreen): Compose toggle, items over the live canvas, move/resize/rotation-independent storage, Present shows no items, items kept on failure and cleared on success.
- On device: calibration (step 2) and acceptance (step 5) on the "rmdesk test" notebook only.

## 10. Build order

1. `find_opened_page` + single-stop send flow with confirm-while-stopped (core + minimal dialog).
2. `frame_to_page` + **calibration on the tablet** (portrait and landscape "rmdesk test" pages: corner marks + centre cross, checked on the live view).
3. Compose mode in the Screen view; remove the old Compose section and typed text.
4. Reuse of the rmdesk layer + verify update.
5. Acceptance on the tablet ("rmdesk test"), then Undo.
