"""Shading a picture for the live Compose mode. Runs in a worker thread (no widgets).

A picture has a style: SHADED (engraving-style hatching, `compose.shade`) or
LINEART (its lines as clean constant-width strokes, `compose.lineart`). Both give a
ShadeResult and go through the same preview and send path. AUTO (a newly added
picture) is decided here from the picture's content, once, in the first job.

A ShadeResult is only valid for the box, rotation and settings it was made
for (only the part on the page is shaded), so both the preview and the send
shade at a given geometry: the preview job for the item as it was when the
job started, the send for the GUI-thread snapshot (`ShadeSpec`), never a
cached preview result.

`wide` (the keyboard layout): the parts of the box past the frame's ends along x are on the
scrolled page too, so they are shaded and previewed (`shade.KEYBOARD_REGION`); the send clips
them to the page in page coordinates.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer
from rmscene import scene_items as si

from rmdesk.compose import build, lineart, shade as shademod, sharptext
from rmdesk.compose.items import ImageItem
from rmdesk.compose.lineart import DEFAULT_LINEART_PEN, DETAIL_DEFAULT, lineart_item
from rmdesk.compose.shade import (DEFAULT_SHADE_PEN, SPACING_DEFAULT, WIDTH_MAX, WIDTH_MIN,
                                  ShadeResult, shade_image, shade_item)
from rmdesk.notebook.page import PAGE_H, PAGE_W, X_MIN

SHADED, LINEART, AUTO = "shaded", "lineart", "auto"
STYLE_PEN = {SHADED: DEFAULT_SHADE_PEN, LINEART: DEFAULT_LINEART_PEN}  # each style's pen
SVG_RASTER = 1024  # long side of the PNG an SVG picture is shaded from
# item-local (x, y, width in tablet units exactly as sent: pen thickness applied, clamped)
PreviewStroke = list[tuple[float, float, int]]


@dataclass(frozen=True)
class ShadeSettings:
    spacing: int = SPACING_DEFAULT
    contrast: float = 1.0
    crosshatch: bool = False
    invert: bool = False  # swap light and dark (a light logo on a dark background)
    style: str = SHADED  # SHADED, LINEART, or AUTO (decided by the first job)
    detail: int = DETAIL_DEFAULT  # line art: how small a line still counts (1..5)
    threshold: float = 1.0  # line art: x Otsu's ink level (lower takes fainter lines)
    # line art: recognised words redrawn as clean text (`sharptext`); on when Tesseract is there
    sharp_text: bool = field(default_factory=sharptext.available)


@dataclass(frozen=True)
class ShadeSpec:
    """What one picture item is, taken on the GUI thread: the raster to shade
    (`model.path`), its box, rotation and pen, and the shading settings."""
    model: ImageItem
    settings: ShadeSettings


@dataclass
class Shaded:
    raster: Path  # the file shaded (an SVG's rasterised PNG); reuse it next time
    result: ShadeResult
    blank: bool  # the picture itself draws nothing at these settings
    preview: list[PreviewStroke]  # exactly the strokes the send builds for this geometry and pen
    paths: list[tuple[int, QPainterPath]]  # preview segments grouped by width (tablet units)
    spec: ShadeSpec | None = None  # what was drawn: AUTO decided, with its pen


def rasterise_svg(src: Path, dest: Path, long_side: int = SVG_RASTER) -> Path:
    """Render an SVG file to a PNG (white background, `long_side` px). Worker-safe."""
    r = QSvgRenderer(str(src))
    if not r.isValid():
        raise ValueError(f"cannot read SVG: {src}")
    size = r.defaultSize()
    w, h = max(size.width(), 1), max(size.height(), 1)
    k = long_side / max(w, h)
    img = QImage(max(1, round(w * k)), max(1, round(h * k)), QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, img.width(), img.height()))
    p.end()
    if not img.save(str(dest), "PNG"):
        raise ValueError(f"cannot write {dest}")
    return dest


def resolve(spec: ShadeSpec) -> ShadeSpec:
    """An AUTO spec decided by the picture's content (`lineart.looks_like_diagram`): line
    art (with the line-art pen, if the pen is still the shading default) or shaded."""
    if spec.settings.style != AUTO:
        return spec
    style = LINEART if lineart.looks_like_diagram(spec.model.path) else SHADED
    model = spec.model
    if model.pen.tool == STYLE_PEN[SHADED]:
        model = dataclasses.replace(model, pen=dataclasses.replace(model.pen,
                                                                   tool=STYLE_PEN[style]))
    return ShadeSpec(model, dataclasses.replace(spec.settings, style=style))


def shade(spec: ShadeSpec, wide: bool = False) -> ShadeResult:
    spec = resolve(spec)
    s = spec.settings
    rect = shademod.KEYBOARD_REGION if wide else shademod.FRAME_REGION
    with shademod.region(rect):
        if s.style == LINEART:
            return lineart_item(spec.model, s.detail, s.threshold, s.invert, s.sharp_text)
        return shade_item(spec.model, s.spacing, s.contrast, s.crosshatch, s.invert)


def page_lines(spec: ShadeSpec, orientation: str) -> list[si.Line]:
    """The send: shade at the snapshot's geometry and build its page lines."""
    return build.frame_shade_to_lines(spec.model, shade(spec), spec.model.pen, orientation)


def is_blank(path: Path, settings: ShadeSettings, model: ImageItem | None = None) -> bool:
    """No strokes even with the whole picture on the page (fitted to the page, unrotated)."""
    if settings.style == LINEART and model is not None:
        whole = dataclasses.replace(model, box=dataclasses.replace(
            model.box, x=0.0, y=0.0, w=float(PAGE_W), h=float(PAGE_H)), rotation=0.0)
        return not lineart_item(whole, settings.detail, settings.threshold,
                                settings.invert, settings.sharp_text).strokes
    res = shade_image(path, PAGE_W, PAGE_H, settings.spacing, settings.contrast,
                      settings.crosshatch, invert=settings.invert)
    return not res.strokes


def preview_strokes(model: ImageItem, result: ShadeResult,
                    wide: bool = False) -> list[PreviewStroke]:
    """The page lines of `result` with the item's pen, back in item-local frame pixels: the
    widths are the sent ones, round(w * k * thickness) clamped to WIDTH_MIN..WIDTH_MAX.
    `wide`: not clipped to the frame (the keyboard layout)."""
    bx, by = model.box.x, model.box.y
    return [[(p.x - X_MIN - bx, p.y - by, p.width) for p in ln.points]
            for ln in build.shade_to_lines(model, result, model.pen, clip=not wide)]


def preview_paths(strokes: list[PreviewStroke]) -> list[tuple[int, QPainterPath]]:
    """One path per segment width (the mean of its two points), built once per result."""
    paths: dict[int, QPainterPath] = {}
    for st in strokes:
        for (x0, y0, w0), (x1, y1, w1) in zip(st, st[1:]):
            w = min(max(int(round((w0 + w1) / 2)), WIDTH_MIN), WIDTH_MAX)
            path = paths.get(w)
            if path is None:
                path = paths[w] = QPainterPath()
            path.moveTo(QPointF(x0, y0))
            path.lineTo(QPointF(x1, y1))
    return sorted(paths.items())


def run(source: Path, raster: Path | None, tmp: Path, spec: ShadeSpec,
        wide: bool = False) -> Shaded:
    """The preview job. An SVG is first rasterised to `tmp` (once: pass the
    returned `raster` back next time). `wide`: as `shade`."""
    if raster is None:
        raster = rasterise_svg(source, tmp) if source.suffix.lower() == ".svg" else source
    spec = dataclasses.replace(spec, model=dataclasses.replace(spec.model, path=raster))
    spec = resolve(spec)
    result = shade(spec, wide)
    blank = not result.strokes and is_blank(raster, spec.settings, spec.model)
    strokes = preview_strokes(spec.model, result, wide)
    return Shaded(raster, result, blank, strokes, preview_paths(strokes), spec)
