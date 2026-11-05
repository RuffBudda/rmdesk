"""Shared widgets of the redesigned UI."""
from __future__ import annotations

from PySide6.QtWidgets import QWidget


def repolish(w: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    st = w.style()
    st.unpolish(w)
    st.polish(w)
    w.update()


from rmdesk.ui.widgets.banner import Banner  # noqa: E402
from rmdesk.ui.widgets.card import Card  # noqa: E402
from rmdesk.ui.widgets.chip import Chip  # noqa: E402
from rmdesk.ui.widgets.empty import EmptyState  # noqa: E402
from rmdesk.ui.widgets.pill import DevicePill  # noqa: E402
from rmdesk.ui.widgets.segmented import Segmented  # noqa: E402
from rmdesk.ui.widgets.toggle import ToggleSwitch  # noqa: E402

__all__ = ["repolish", "Banner", "Card", "Chip", "DevicePill", "EmptyState", "Segmented",
           "ToggleSwitch"]
