# Shaded Pictures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace outline tracing with engraving-style shaded ink so photos, screenshots, charts and logos can be placed on notebook pages.

**Architecture:** A pure `compose/shade.py` turns a picture into variable-width strokes in box pixels; `build.shade_to_lines` turns them into `si.Line`s (per-point width/pressure) through the existing page mapping and clipping; the live Compose image item uses it for both preview and send; tracing is removed.

**Tech Stack:** Python 3.14, numpy, OpenCV (already installed), rmscene 0.8.0, PySide6.

**Spec:** `docs/superpowers/specs/2026-11-03-shaded-pictures-design.md`

## Global Constraints

- No Qt in `src/rmdesk/compose/`.
- `MAX_POINTS = 20000` (reuse the existing constant); spacing range 3–12 px, default 5; contrast 0.5–2.0, default 1.0; cross-hatch default off; `d_min = 0.06`; cross-hatch threshold `0.6`; point step 2 px along a line.
- Every `si.Point.width`/`pressure` within the ranges `page._check_line` accepts.
- Status texts (exact): `This picture is very detailed; rmdesk used wider line spacing ({n} px).` and `This picture is very detailed; only part of it fits.`
- The device scripts reuse `safety.send_to_open_page` and refuse unless the open notebook is named exactly `rmdesk test`.
- pytest always with `timeout 300`/`600`; bounded waits; never `git stash`.
- Commit messages end with a blank line + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Fully white or transparent PNG** (logo on transparency): no strokes, a friendly "Nothing to draw — the picture is blank." instead of a send — `test_blank_picture_gives_no_strokes` (Task 1) and `test_blank_picture_not_sendable` (Task 3).
2. **Huge photo (e.g. 6000×4000)**: downscaled before shading; shading stays under ~1 s for the default box — `test_large_image_downscaled_fast` (Task 1).
3. **Tiny box (e.g. 20×20 px)**: still valid strokes or none, never a crash — `test_tiny_box` (Task 1).
4. **Picture hanging off the page edge**: clipped with per-point width/pressure preserved — `test_clip_keeps_width_pressure` (Task 1).
5. **Re-shade while a send is running**: the sent strokes are the ones confirmed, not a later preview — `test_send_uses_snapshot_strokes` (Task 3).

---

### Task 1: Shading engine and line building

**Files:** Create `src/rmdesk/compose/shade.py`; modify `src/rmdesk/compose/build.py`; test `tests/test_shade.py`.

**Interfaces:** Produces `Stroke = list[tuple[float, float, float, float]]` (x, y, width, pressure in box px / tablet units); `ShadeResult(strokes: list[Stroke], size: tuple[int, int], capped: bool, spacing_used: int)`; `shade_image(path: Path, box_w: int, box_h: int, spacing: int = 5, contrast: float = 1.0, crosshatch: bool = False) -> ShadeResult`; `build.shade_to_lines(box: Box, result: ShadeResult, pen: PenStyle) -> list[si.Line]`; `MAX_SHADE_WIDTH(spacing) -> float` documented in the module.

- [ ] **Step 1: Failing tests:** `test_gradient_width_monotonic` (horizontal black→white gradient: mean width per x-bucket strictly decreasing left→right), `test_blank_picture_gives_no_strokes` (white and fully transparent), `test_black_gives_full_width`, `test_crosshatch_only_dark`, `test_cap_widens_spacing` (noisy 1024×768 → ≤ 20000 points, `spacing_used > 5`), `test_large_image_downscaled_fast` (6000×4000 → < 1.5 s), `test_tiny_box`, `test_strokes_inside_box`, `test_clip_keeps_width_pressure`, `test_lines_pass_check_line_and_verify` (append via `Page.append_to_rmdesk_layer` on every fixture incl. the 3.29 page, `verify` passes).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen timeout 300 ~/.local/share/rmdesk/venv/bin/python -m pytest tests/test_shade.py -q` → FAIL.
- [ ] **Step 3: Implement** per spec §2–§3 (OpenCV for load/resize/alpha → white; numpy for runs; reuse `build._clip` generalised to carry width/pressure; reuse the existing box→page mapping — frame/box x plus X_MIN — exactly once).
- [ ] **Step 4: Run** the test file, then the full suite → PASS.
- [ ] **Step 5: Commit** `feat: shaded picture engine`.

### Task 2: Pen test-strip script

**Files:** Create `src/rmdesk/compose/shade_strip.py` (pure build), `scripts/shade_strip.py` (thin runner reusing `compose/calibration.py`'s connect/guard/confirm helpers); test `tests/test_shade_strip.py`.

- [ ] **Step 1: Tests:** the strip build produces three gradient blocks (600×120 box px each, stacked with 40 px gaps, centred) for fineliner, ballpoint, pencil, each with an ink-text label; the page passes `verify`.
- [ ] **Step 2–4:** RED, implement, GREEN; full suite.
- [ ] **Step 5: Commit** `feat: pen test strip for shading`.
- [ ] **Step 6 (device, controller + user):** user runs `~/.local/share/rmdesk/venv/bin/python scripts/shade_strip.py` on "rmdesk test", picks the best block; record in the ledger.

### Task 3: Shading in live Compose; remove tracing

**Files:** Modify `src/rmdesk/ui/live_compose.py` (and `ui/compose/scene.py` image item), `src/rmdesk/compose/shade.py` (`DEFAULT_SHADE_PEN` from Task 2 Step 6); delete `src/rmdesk/compose/trace.py` and its tests; test `tests/ui/test_live_compose.py`.

- [ ] **Step 1: Tests:** image item preview strokes == `shade_image` output for its current box/settings; Spacing/Contrast/Cross-hatch controls re-shade (debounced, worker, stale results discarded); `test_send_uses_snapshot_strokes`; `test_blank_picture_not_sendable`; capped status texts exact; no Detail slider / trace references remain (grep test on the package).
- [ ] **Step 2–4:** RED, implement, GREEN; full suite.
- [ ] **Step 5: Commit** `feat: shaded pictures in live Compose; remove tracing`.

### Task 4: Acceptance on the tablet

- [ ] Photo, screenshot and chart placed over the live view on "rmdesk test" → shaded images at the placed position/size on the rmdesk layer; erase/lasso work with the rmdesk layer selected; Undo removes the send. Record in the ledger.
