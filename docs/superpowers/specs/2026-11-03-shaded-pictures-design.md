# rmdesk — Shaded pictures design

**Date:** 2026-11-03
**Status:** Draft for review
**Builds on:** `2026-11-03-live-compose-design.md`

## 1. Purpose

Let the user put real pictures (photos, screenshots, charts, logos) on notebook pages. Notebook pages (`.rm` v6) can hold only strokes and text, so pictures become **shaded ink**: engraving-style lines whose width follows the picture's darkness. This **replaces** outline tracing.

### User decisions
- Approach A (shaded ink), not page templates.
- Shaded only — tracing and its Detail slider are removed.
- Picture types: all (photos, screenshots/UI, charts/diagrams, logos/icons).

### Success criteria
- A photo, a screenshot and a chart placed over the live view arrive on the page as recognisable shaded images on the rmdesk layer, at the placed position and size, and can be erased/lassoed like ink.
- A picture never produces more than `MAX_POINTS` points; the page check (`verify`) accepts every output.
- Undo removes it like any send.

### Non-goals
Colour; pixel-exact photos; page templates/backgrounds; text-perfect screenshots.

## 2. Shading algorithm (`compose/shade.py`, no Qt)

Input: image path, target box size in page pixels, `spacing` (px between lines, 3–12, default 5), `contrast` (0.5–2.0, default 1.0), `crosshatch` (bool, default False).

1. Load, convert to greyscale, resize to the box size (keep aspect, same fit-and-centre as today), apply contrast around mid-grey, darkness `d = 1 − value/255` per pixel.
2. **Primary pass:** horizontal lines every `spacing` px. Walk each line; split it into runs where `d > d_min` (0.06). Each run is one stroke; each point (every 2 px along x) gets `width = d × max_width(spacing)` and `pressure = d × 255`, clamped to the tablet's valid ranges. Runs shorter than 2 points are dropped.
3. **Cross-hatch pass** (optional): lines at 45° every `spacing` px, only where `d > 0.6`, with width scaled by `(d − 0.6)/0.4`.
4. **Cap:** if the total point count exceeds `MAX_POINTS` (20000, same as today), increase `spacing` by 1 and redo, up to 12; if still over, take the strokes in reading order up to the cap and report `capped=True`.
5. Output: `ShadeResult(strokes: list[Stroke], size: (w, h), capped: bool, spacing_used: int)` where `Stroke = list[(x, y, width, pressure)]` in box pixels.

`max_width(spacing)` = `spacing × 0.9` in tablet width units, converted with the same factor `build._line` uses for pen width (pinned in tests).

## 3. Turning strokes into lines

`compose/build.py` gains `shade_to_lines(item, result, pen) -> list[si.Line]`: maps box → page coordinates (identity + X_MIN, as today), clips to the page with per-point width/pressure carried through the clip, builds `si.Line` with per-point `width`/`pressure`, `speed=0`, `direction=0`. Valid ranges enforced to pass `page._check_line`.

## 4. Pen look — device test strip

Per-point width/pressure rendering differs per pen and is undocumented. Before choosing defaults, `scripts/shade_strip.py` (same safety path and "rmdesk test" guard as the calibration script) sends a 600×120 px black→white gradient three times — fineliner, ballpoint, pencil — labelled with small ink text. The user picks the best-looking one; it becomes `DEFAULT_SHADE_PEN`. The user can still choose the pen per picture.

## 5. UI (live Compose)

- **Add image…** creates a shaded picture item; the red preview draws the shading strokes (width scaled) over the live view and updates (debounced 300 ms, in a worker) when **Spacing**, **Contrast** or **Cross-hatch** change.
- Remove: tracing, `compose/trace.py`, the Detail slider, and their tests.
- `capped` → status "This picture is very detailed; rmdesk used wider line spacing (N px)." / "…only part of it fits."

## 6. Testing

- Gradient → stroke width increases monotonically left→right darkness; white → no strokes; black → full width.
- Cross-hatch only where `d > 0.6`.
- Cap: a noisy 1024×768 image ≤ 20000 points, `spacing_used` increased, `capped` set when needed.
- Output within the box and page bounds after clipping; per-point width/pressure within valid ranges.
- `shade_to_lines` + `Page.append_to_rmdesk_layer` + `verify` pass on every fixture including the real 3.29 page.
- UI: preview strokes match the strokes sent (same function); controls re-shade; tracing UI gone.
- Device: test strip (pen choice), then acceptance with a photo, a screenshot and a chart on "rmdesk test", then Undo.

## 7. Build order

1. `compose/shade.py` + `shade_to_lines` + `scripts/shade_strip.py`.
2. Device test strip → `DEFAULT_SHADE_PEN`.
3. UI: replace tracing with shading controls.
4. Acceptance on the tablet.
