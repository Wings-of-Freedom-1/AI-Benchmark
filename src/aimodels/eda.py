"""EDA: figures and a written summary computed from the SQLite or Supabase database.

Charts follow one visual system: a fixed two-class colour pair (open weights =
blue, everything else = orange), a one-hue blue ramp for magnitude, a blue-gray-red
diverging scale for correlations, hairline grids, thin marks, one y-axis per chart
and an attribution footer (Artificial Analysis requires attribution).

Every number in ``eda_summary.md`` is computed from the data - nothing is hard-coded.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, Normalize  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from .config import ATTRIBUTION  # noqa: E402
from .load import read_last_run, read_models  # noqa: E402
from .schemas import SCORE_COLS  # noqa: E402

log = logging.getLogger(__name__)

# ---- visual tokens (light surface) -----------------------------------------
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, CRITICAL = "#2a78d6", "#eb6834", "#d03b3b"
SEQ = LinearSegmentedColormap.from_list("seq", ["#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"])
DIV = LinearSegmentedColormap.from_list("div", ["#e34948", "#f0efec", "#2a78d6"])

OPEN, OTHER = "Open weights (Hugging Face repo found)", "Closed / not on Hugging Face"
GROUP_COLOR = {OPEN: BLUE, OTHER: ORANGE}
SCORE = "intelligence_index"
PRICE = "price_blended_usd_per_m"
SPEED = "tokens_per_sec"
SIZE = "params_b"

MISS_COLS = [
    "intelligence_index", "coding_index", "math_index", "mmlu_pro", "gpqa", "hle", "livecodebench",
    "scicode", "math_500", "aime", PRICE, SPEED, "ttft_s", "context_length", SIZE, "release_date",
]
LABELS = {
    "intelligence_index": "Intelligence idx", "coding_index": "Coding idx", "math_index": "Math idx",
    "mmlu_pro": "MMLU-Pro", "gpqa": "GPQA", "hle": "HLE", "livecodebench": "LiveCodeBench",
    "scicode": "SciCode", "math_500": "MATH-500", "aime": "AIME", PRICE: "Price", SPEED: "Speed",
    "ttft_s": "Time to 1st token", "context_length": "Context length", SIZE: "Parameters",
    "release_date": "Release date",
}


# ---------------------------------------------------------------------------
# data + helpers
# ---------------------------------------------------------------------------
def load_frame(db_path: Path | None) -> pd.DataFrame:
    df = read_models(db_path)
    df["group"] = np.where(df["hf_id"].notna(), OPEN, OTHER)
    return df


def pareto_mask(x, y, *, x_min: bool = True, y_max: bool = True) -> np.ndarray:
    """True for points no other point beats on both axes (e.g. cheaper AND better)."""
    xs = np.asarray(x, float) * (1 if x_min else -1)
    ys = np.asarray(y, float) * (1 if y_max else -1)
    idx = np.where(np.isfinite(xs) & np.isfinite(ys))[0]
    order = idx[np.lexsort((-ys[idx], xs[idx]))]  # x ascending, ties: best y first
    mask = np.zeros(len(xs), dtype=bool)
    best = -np.inf
    for i in order:
        if ys[i] > best:
            mask[i], best = True, ys[i]
    return mask


def _short(name: str, n: int = 30) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def _style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.family": "sans-serif", "font.size": 9.5, "text.color": INK,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.linewidth": 0.8,
        "xtick.color": MUTED, "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
        "legend.frameon": False, "legend.fontsize": 8.5,
    })


def _frame(title: str, subtitle: str, *, sample: bool, figsize=(9.2, 5.8), **kw):
    fig, ax = plt.subplots(figsize=figsize, **kw)
    _decorate(fig, title, subtitle, sample)
    return fig, ax


def _decorate(fig, title: str, subtitle: str, sample: bool) -> None:
    fig.text(0.012, 0.975, title, fontsize=13, fontweight="bold", va="top", color=INK)
    fig.text(0.012, 0.925, subtitle, fontsize=9, va="top", color=INK2)
    foot = ATTRIBUTION + ("   |   SYNTHETIC SAMPLE DATA - not real models" if sample else "")
    fig.text(0.012, 0.012, foot, fontsize=7.5, color=CRITICAL if sample else MUTED, va="bottom")


def _save(fig, path: Path, top: float = 0.86, bottom: float = 0.11, left: float = 0.085, right: float = 0.965) -> Path:
    fig.subplots_adjust(top=top, bottom=bottom, left=left, right=right)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("wrote %s", path)
    return path


def _scatter_groups(ax, d: pd.DataFrame, x: str, y: str) -> None:
    for g, color in GROUP_COLOR.items():
        s = d[d["group"] == g]
        ax.scatter(s[x], s[y], s=30, color=color, edgecolor=SURFACE, linewidth=1.0, alpha=0.92, zorder=3, label=g)


def _frontier(ax, d: pd.DataFrame, x: str, y: str, **mask_kw) -> pd.DataFrame:
    f = d[pareto_mask(d[x], d[y], **mask_kw)].sort_values(x)
    ax.step(f[x], f[y], where="post", color=INK2, lw=1.3, ls="--", zorder=2)
    return f


def _label_points(ax, rows: pd.DataFrame, x: str, y: str, max_labels: int = 6) -> None:
    """Direct labels on the frontier, skipping any that would collide."""
    if rows.empty:
        return
    pick = rows.iloc[np.unique(np.linspace(0, len(rows) - 1, min(max_labels, len(rows))).round().astype(int))]
    fig = ax.figure
    fig.canvas.draw()
    placed: list[tuple[float, float]] = []
    box = ax.get_window_extent()
    for r in pick.itertuples():
        px, py = ax.transData.transform((getattr(r, x), getattr(r, y)))
        if any(abs(px - qx) < 90 and abs(py - qy) < 14 for qx, qy in placed):
            continue
        placed.append((px, py))
        near_right = px > box.x0 + 0.72 * box.width  # flip the label to the left so it is not clipped
        ax.annotate(_short(r.name, 26), (getattr(r, x), getattr(r, y)),
                    xytext=(-6 if near_right else 6, -11), textcoords="offset points",
                    ha
