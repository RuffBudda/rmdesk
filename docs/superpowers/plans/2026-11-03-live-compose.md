# Live Compose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Compose section with a Compose mode on the live Screen view that sends images and ink text to the page currently open on the tablet.

**Architecture:** A new core function identifies the open page after xochitl is stopped; a single-stop send transaction in `safety` runs stop → identify → confirm (callback) → build → backup/guard/verify/write → start. The UI adds a `LiveComposeView` (QGraphicsView whose background is the latest stream frame, scene in frame coordinates 1404×1872) that reuses the existing placed-item classes; the old Compose section and typed text are removed.

**Tech Stack:** Python 3.14, PySide6, rmscene 0.8.0, existing rmdesk modules.

**Spec:** `docs/superpowers/specs/2026-11-03-live-compose-design.md` (read it first; it lists the device facts).

## Global Constraints

- Only `src/rmdesk/ui/` imports PySide6.
- Frame size 1404×1872; page x range −702..702, y 0..1872.
- Confirm text (exact): `Add {n} items to “{name}”, page {page} of {total}?` (`{n}` = item count, `{page}` 1-based); singular "1 item".
- Confirm timeout: 60 s → treated as No.
- Zoom refusal text (exact): `Zoom the page back to fit (pinch out) and send again.`
- Every path after `stop_xochitl` ends in a start attempt; no write after a refusal or No.
- The R45 job guard (`conn.try_begin_job("sending to the tablet")`) is held for the whole send/undo.
- Testing mode (untested firmware) still restricts writes to the notebook named exactly `rmdesk test`, checked by the existing guard against the notebook identified after the stop.
- Layer label `rmdesk`; reuse the last `rmdesk` layer in root order if present.
- pytest always with `timeout 300`/`600`; bounded waits; modal dialogs patched; never `git stash`.
- Commit messages end with a blank line + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Tablet on the home/library screen (no notebook open) when Send is pressed:** the most recently opened notebook would be chosen even though it isn't on screen. Expected: the confirm dialog names it and its page so the user can say No — pinned by `test_confirm_names_notebook_and_page` (Task 2) and the acceptance step (Task 6).
2. **Two notebooks with the same `lastOpened`:** pick deterministically (greater `lastOpened`, then greater `lastModified`, then id) — `test_tie_breaks_deterministically` (Task 1).
3. **User walks away during confirm:** 60 s timeout → No, xochitl started, nothing written — `test_confirm_timeout_is_no` (Task 2).
4. **Items placed partly outside the page area (landscape margins / edge):** clipped, never written out of bounds — `test_items_outside_page_are_clipped` (Task 5).
5. **Stream frame arrives / view rotates while items are placed:** items keep their frame-coordinate position — `test_rotation_does_not_move_items` (Task 5).

---

### Task 1: Identify the open page

**Files:**
- Create: `src/rmdesk/notebook/opened.py`
- Test: `tests/test_opened.py`

**Interfaces:**
- Consumes: `Device`, `library.model.XOCHITL`.
- Produces: `OpenedPage(notebook_id: str, name: str, page_id: str, page_index: int, page_count: int, orientation: Literal["portrait","landscape"], data: bytes)` (frozen dataclass); `NotEditable(Exception)` with `.reason: str`; `find_opened_page(device: Device, root: str = XOCHITL) -> OpenedPage`; constant `ZOOM_MSG` (exact zoom refusal text).

- [ ] **Step 1: Write failing tests** (FakeDevice with hand-written metadata/content/page files)
  - `test_most_recent_notebook_and_page`: two notebooks, the newer `lastOpened` with `lastOpenedPage: 1` and 2 cPages → page_id is the 2nd non-deleted page, `page_index == 1`, `page_count == 2`, data = that page's bytes.
  - `test_deleted_pages_skipped_in_index`.
  - `test_orientation_landscape`.
  - `test_tie_breaks_deterministically` (same lastOpened → greater lastModified, then greater id).
  - `test_pdf_refused`, `test_index_out_of_range_refused`, `test_missing_page_file_refused` (reason = the existing blank-page message from `ui/compose/messages.py` moved into this module as `BLANK_PAGE_MSG`), `test_bad_json_refused`, `test_no_documents_refused`.
  - `test_zoomed_refused`: `.content` with a non-default zoom → `NotEditable(ZOOM_MSG)`. Default = `zoomMode` absent or `"bestFit"`, and no `customZoomScale` other than 1 (read the real 3.29 `.content` keys from the spec's §2 list; treat unknown zoom modes as zoomed).
  - Trashed / deleted documents are never chosen.
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen timeout 300 ~/.local/share/rmdesk/venv/bin/python -m pytest tests/test_opened.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `find_opened_page`** in `notebook/opened.py` (list `*.metadata`, parse; reuse `library.model` parsing helpers where they fit; read page via `device.read_bytes`).
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: identify the page open on the tablet`.

### Task 2: Single-stop send transaction with confirm

**Files:**
- Modify: `src/rmdesk/safety.py`
- Test: `tests/test_safety_live.py`

**Interfaces:**
- Consumes: `find_opened_page`, `OpenedPage`, `NotEditable` (Task 1); existing `stop_xochitl`, `start_xochitl`, `backup`, restore internals, `page.verify`, `SendFailed`, `SendResult`, `NotebookChanged`, `guard` semantics.
- Produces: `send_to_open_page(device, build: Callable[[OpenedPage], tuple[bytes, int]], confirm: Callable[[OpenedPage], bool], *, root=BACKUP_ROOT, timeout=RESTART_TIMEOUT, sleep=time.sleep, now=None, guard: Callable[[Path, OpenedPage], None] | None = None) -> tuple[SendResult, OpenedPage]`; `Cancelled(Exception)` raised when confirm returns False. `build` returns `(new_page_bytes, added_lines)`.

Order (exact): stop → `find_opened_page` → `confirm(opened)` → `build(opened)` → `backup` (post-stop) → `guard(bdir, opened)` → backup's page bytes == `opened.data` (else `NotebookChanged`) → `page.verify(opened.data, new, added_lines, None)` → tmp + rename → start. Any exception before the rename → `_start_quiet` then re-raise (NotEditable / Cancelled / NotebookChanged / VerifyError unchanged). Failure at/after the rename → restore from that backup while stopped → start → `SendFailed` as today. Share the write/restore code with `send_to_page` (extract a helper; `send_to_page` behaviour and tests unchanged).

- [ ] **Step 1: Write failing tests** (FakeDevice with systemctl state model)
  - `test_stop_before_identify_and_start_after_write` (log order).
  - `test_confirm_no_writes_nothing_and_starts` → `Cancelled`, no write/rename, xochitl active.
  - `test_not_editable_starts_and_raises`.
  - `test_confirm_receives_opened_page` (name, page index).
  - `test_build_failure_starts` ; `test_verify_failure_writes_nothing`.
  - `test_rename_failure_restores` → `SendFailed(restored=True)`.
  - `test_guard_called_with_backup_and_opened_page_before_write`.
- [ ] **Step 2: Run** `... pytest tests/test_safety_live.py tests/test_safety.py -q`. Expected: new tests FAIL, old PASS.
- [ ] **Step 3: Implement** in `safety.py`.
- [ ] **Step 4: Run** `... pytest tests/test_safety_live.py tests/test_safety.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: single-stop send to the open page`.

### Task 3: Frame→page coordinates and calibration

**Files:**
- Create: `src/rmdesk/compose/frame.py`, `scripts/calibrate.py`
- Test: `tests/test_frame.py`

**Interfaces:**
- Consumes: `send_to_open_page` (Task 2), `build._line` / `PenStyle` (existing compose build), `page.load`/`append_layer`.
- Produces: `frame_to_page(x: float, y: float, orientation: str) -> tuple[float, float]`; `polylines_to_page(lines: list[list[tuple[float,float]]], orientation) -> list[...]`.

- [ ] **Step 1: Tests** `test_portrait_formula` (`(0,0)→(-702,0)`, `(1404,1872)→(702,1872)`, `(702,936)→(0,936)`); `test_landscape_is_marked_uncalibrated` asserts `frame_to_page(..., "landscape")` raises `NotImplementedError("landscape not calibrated")` until Step 5.
- [ ] **Step 2: Implement portrait; run tests; commit** `feat: frame to page coordinates (portrait)`.
- [ ] **Step 3: `scripts/calibrate.py`** (run by the user, reads the stored tablet password from the keyring like the app): uses `send_to_open_page` with a confirm that prints the notebook/page and asks y/N on stdin, and a build that adds marks at frame points (60,60), (1344,60), (60,1812), (1344,1812) (10-px crosses) and a centre cross at (702,936) — converted with `frame_to_page` for portrait, and for landscape with BOTH candidate transforms (rotate +90° and −90°, documented in the script) drawn in different pen colours (black / grey) so one send shows which is right. Refuses unless the notebook is named `rmdesk test`.
- [ ] **Step 4: Device step (controller + user):** run the script on a portrait and a landscape `rmdesk test` page; check on the live Screen view which marks sit on the frame points (±3 px). Record the result in the ledger.
- [ ] **Step 5: Implement landscape** with the confirmed transform; replace the NotImplementedError test with `test_landscape_calibration_points` using the recorded points; commit `feat: landscape frame to page coordinates`.

### Task 4: Reuse the rmdesk layer

**Files:**
- Modify: `src/rmdesk/notebook/page.py`
- Test: `tests/test_notebook_page.py`

**Interfaces:**
- Produces: `Page.append_to_rmdesk_layer(lines: list[si.Line]) -> None`; `verify(original, new, added_lines, added_text)` accepts additions under an existing `rmdesk` layer.

- [ ] **Step 1: Failing tests** (all fixtures incl. `user-3.29.1.0-rmdesk-test.rm`, which already has an `rmdesk` layer with xochitl sub-groups):
  - `test_append_to_existing_rmdesk_layer`: on the 3.29 fixture → no new TreeNodeBlock/SceneTreeBlock, N new lines whose parent is the existing rmdesk node, `verify` passes.
  - `test_creates_layer_when_absent` (behaves like `append_layer("rmdesk", ...)`).
  - `test_verify_rejects_line_added_elsewhere`, `test_verify_rejects_relabelled_rmdesk_layer`, `test_verify_rejects_changed_original_line` (on the 3.29 fixture).
  - `test_last_rmdesk_layer_used_when_several`.
- [ ] **Step 2: Run** `... pytest tests/test_notebook_page.py -q`. Expected: new FAIL.
- [ ] **Step 3: Implement.** New lines go directly under the rmdesk layer node, appended after its last child in CRDT order; ids from the existing `_IdAllocator`. Extend verify's new-item rule: new line items must all have the same parent, which is either a newly created `rmdesk` layer (existing rule) or an existing node labelled `rmdesk`; all other checks unchanged (raw-block subsequence, originals equal, labels/visibility unchanged).
- [ ] **Step 4: Run** the page and text test files. Expected: PASS.
- [ ] **Step 5: Commit** `feat: reuse the page's rmdesk layer`.

### Task 5: Compose mode in the Screen view; remove the old Compose section

**Files:**
- Create: `src/rmdesk/ui/live_compose.py`
- Modify: `src/rmdesk/ui/screen_view.py`, `src/rmdesk/ui/app.py`, `src/rmdesk/ui/compose/scene.py` (keep `PlacedItem`, `ImageSceneItem`, `InkSceneItem`, `rasterise_svg`; delete `PageView`, `render_page`), `src/rmdesk/ui/compose/messages.py` (keep send/undo error descriptions; drop typed-text helpers)
- Delete: `src/rmdesk/ui/compose_view.py`, `src/rmdesk/ui/compose/editor.py`, `src/rmdesk/ui/compose/work.py` (move the still-needed `trace` helper into `live_compose.py` or a small `ui/compose/trace_job.py`), `tests/ui/test_compose_view.py` (port the still-relevant tests)
- Test: `tests/ui/test_live_compose.py`

**Interfaces:**
- Consumes: `send_to_open_page`, `Cancelled` (Task 2); `polylines_to_page` (Task 3); `Page.append_to_rmdesk_layer` (Task 4); `safety.undo_last`; `firmware.compose_mode`, `acceptance_guard` (adapt it to the `(bdir, opened)` guard signature); `conn.try_begin_job`/`end_job`; `compose.trace`, `compose.inktext`, `compose.build` (clip + line building, applied after `polylines_to_page`).
- Produces: `LiveComposeView(QGraphicsView)` — scene rect (0,0,1404,1872), `drawBackground` paints the ScreenView's latest frame; view transform = ScreenView rotation + fit; `ScreenView.compose_mode: bool` toggled by a checkable **Compose** toolbar button; compose toolbar (Add image…, Add ink text…, Remove, pen tool, colour, thickness, detail, Send to tablet, Undo last send) visible only in compose mode. `ConfirmBridge` — asks the confirm question on the GUI thread from the worker (Qt signal + `threading.Event`, 60 s `wait` → False).

- [ ] **Step 1: Failing tests** (offscreen; fake stream client delivering frames; FakeDevice)
  - `test_compose_toggle_shows_toolbar_and_hides_present_items` (PresentWindow never draws items).
  - `test_items_stored_in_frame_coordinates`; `test_rotation_does_not_move_items`.
  - `test_items_outside_page_are_clipped` (an item hanging off the frame edge → built points within page bounds).
  - `test_send_flow`: Send → worker → confirm dialog text exactly per Global Constraints (patched) → Yes → `send_to_open_page` called once, job guard held and released, items cleared, `cfg.last_send` saved.
  - `test_confirm_names_notebook_and_page`; `test_confirm_timeout_is_no` (bridge with a short injected timeout) → Cancelled → items kept, status "Nothing was sent."
  - `test_not_editable_shows_reason` (zoom / blank page / pdf).
  - `test_undo` (calls `undo_last`, names the notebook from cfg.last_send).
  - `test_sidebar_has_files_screen_device_only`; `test_no_typed_text_action`.
  - `test_testing_mode_guard_uses_identified_notebook` (untested firmware: guard rejects when the opened notebook isn't `rmdesk test`; nothing written).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen timeout 300 ... pytest tests/ui/test_live_compose.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `live_compose.py` and the ScreenView hooks (keep screen_view.py changes to wiring; logic lives in live_compose.py). Remove the old modules and the sidebar entry. Ensure the stream keeps running while compose mode is on and reconnects after the send's restart (existing reconnect + catch-up).
- [ ] **Step 4: Run** the full suite `QT_QPA_PLATFORM=offscreen timeout 600 ... pytest -q`. Expected: PASS (removed tests gone, no import errors).
- [ ] **Step 5: Commit** `feat: compose on the live Screen view; remove the Compose section`.

### Task 6: Acceptance on the tablet

- [ ] **Step 1:** Controller relaunches rmdesk; user opens `rmdesk test` (portrait page with something drawn), turns on Compose in Screen, places `diagram.png` and ink text, Sends; confirm dialog names `rmdesk test` and the right page; on the tablet the items land where placed (±3 px), on the existing rmdesk layer.
- [ ] **Step 2:** Same on a landscape `rmdesk test` page.
- [ ] **Step 3:** Undo removes the last send only.
- [ ] **Step 4:** On the home screen (no notebook open) press Send → the dialog names the last-opened notebook; answer No → nothing written.
- [ ] **Step 5:** Record results in the ledger; commit any fixture captured from `rmdesk test` (only rmdesk's test ink).
