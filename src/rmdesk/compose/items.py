"""Plain data describing what the user wants to put on a page."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .shapes import ShapeOptions

Tool = Literal["fineliner", "ballpoint", "marker", "pencil"]  # pencil: shaded pictures
Color = Literal["black", "grey", "white"]


@dataclass(frozen=True)
class PenStyle:
    tool: Tool = "fineliner"
    color: Color = "black"
    thickness: float = 1.0


@dataclass(frozen=True)
class Box:
    """A rectangle in page pixels (0..1404, 0..1872)."""
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class ImageItem:
    path: Path
    box: Box
    pen: PenStyle = field(default_factory=PenStyle)
    rotation: float = 0.0  # degrees clockwise (y-down), around the box centre


@dataclass(frozen=True)
class InkTextItem:
    text: str
    box: Box
    size: float = 40
    pen: PenStyle = field(default_factory=PenStyle)
    rotation: float = 0.0  # degrees clockwise (y-down), around the box centre
    font: str = "sans"  # compose.inktext.FONTS; one style for the whole text
    bold: bool = False
    italic: bool = False
    align: str = "left"  # per line, within the box width
    line_spacing: float = 1.0  # x inktext.LINE_HEIGHT (1.4 em): 1.0 is the original pitch
    letter_spacing: float = 0.0  # extra em after each character


@dataclass(frozen=True)
class TypedTextItem:
    text: str


@dataclass(frozen=True)
class ShapeItem:
    """A rectangle, line, arrow or sticky note (compose.shapes) drawn in `box`.

    Lines and arrows run along the middle of the box from its left to its right edge (the
    box width is the length; turn it with `rotation`). A sticky note's `text` is drawn
    inside it (its strokes come with the outline's: see ui.compose.scene.NoteSceneItem)."""
    kind: str
    box: Box
    pen: PenStyle = field(default_factory=PenStyle)
    rotation: float = 0.0  # degrees clockwise (y-down), around the box centre
    fill: bool = False  # rectangle: hatched inside
    double: bool = False  # arrow: a head at both ends
    text: str = ""  # sticky note

    def options(self) -> ShapeOptions:
        return ShapeOptions(fill=self.fill, double=self.double, thickness=self.pen.thickness)
