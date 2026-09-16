"""Shared matplotlib styling so every figure in results/ looks like one system."""

from __future__ import annotations

import matplotlib as mpl

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e6e5e1"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")  # fixed order


def apply() -> None:
    mpl.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK_2,
            "axes.titlecolor": INK,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.color": INK_2,
            "ytick.color": INK_2,
            "text.color": INK,
            "lines.linewidth": 2.0,
            "lines.markersize": 6,
            "legend.frameon": False,
            "font.size": 10,
            "figure.dpi": 130,
            "axes.prop_cycle": mpl.cycler(color=list(SERIES)),
        }
    )
