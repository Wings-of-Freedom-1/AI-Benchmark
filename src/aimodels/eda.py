"""EDA: figures and a written summary computed from the SQLite database.

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
def load_frame(db_path: Path) -> pd.DataFrame:
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
                    ha="right" if near_right else "left", fontsize=7.5, color=INK2, zorder=5)


def _money(v, _=None) -> str:
    return f"${v:,.2f}" if v < 1 else f"${v:,.0f}" if v >= 10 else f"${v:g}"


def _fmt(v: Any) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v is pd.NaT:
        return "–"
    if isinstance(v, (float, np.floating)):
        return f"{v:,.1f}" if abs(v) >= 100 else f"{v:,.2f}"
    return str(v).replace("|", "\\|")


def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    lines += ["| " + " | ".join(_fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def _strength(rho: float) -> str:
    a = abs(rho)
    return "weak" if a < 0.3 else "moderate" if a < 0.6 else "strong"


def _spearman(a: pd.Series, b: pd.Series) -> tuple[float, int]:
    d = pd.concat([a, b], axis=1).dropna()
    return (float(d.corr(method="spearman").iloc[0, 1]) if len(d) >= 5 else float("nan")), len(d)


# ---------------------------------------------------------------------------
# figures - each returns (path, facts) or None when there is too little data
# ---------------------------------------------------------------------------
def fig_score_vs_size(df, out: Path, sample: bool):
    d = df[(df[SIZE] > 0) & df[SCORE].notna()].copy()
    if len(d) < 8:
        return None
    lx = np.log10(d[SIZE])
    slope, intercept = np.polyfit(lx, d[SCORE], 1)
    r2 = float(np.corrcoef(lx, d[SCORE])[0, 1] ** 2)
    rho, n = _spearman(d[SIZE], d[SCORE])

    fig, ax = _frame("Intelligence vs model size",
                     "Models with a known parameter count (total parameters; MoE models count all experts)", sample=sample)
    ax.set_xscale("log")
    xs = np.linspace(lx.min(), lx.max(), 50)
    ax.plot(10**xs, intercept + slope * xs, color=MUTED, lw=1.2, zorder=1)
    for src, filled in (("hf", True), ("name", False)):
        s = d[d["params_source"] == src]
        ax.scatter(s[SIZE], s[SCORE], s=32, linewidth=1.2, zorder=3,
                   facecolor=BLUE if filled else SURFACE, edgecolor=BLUE)
    f = _frontier(ax, d, SIZE, SCORE)
    _label_points(ax, f, SIZE, SCORE)
    ax.set_xlabel("Parameters (billions, log scale)")
    ax.set_ylabel("Artificial Analysis Intelligence Index")
    ticks = [t for t in (1, 3, 10, 30, 100, 300, 1000) if d[SIZE].min() / 1.5 <= t <= d[SIZE].max() * 1.5]
    ax.set_xticks(ticks)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}B"))
    ax.xaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
    ax.legend(handles=[
        Line2D([], [], marker="o", ls="", color=BLUE, label="size from Hugging Face"),
        Line2D([], [], marker="o", ls="", markerfacecolor=SURFACE, markeredgecolor=BLUE, label="size parsed from name"),
        Line2D([], [], color=MUTED, lw=1.2, label=f"log-linear fit (R² = {r2:.2f})"),
        Line2D([], [], color=INK2, lw=1.3, ls="--", label="smallest-for-score frontier"),
    ], loc="lower right")
    path = _save(fig, out / "01_score_vs_size.png")
    return path, {"n": n, "rho": rho, "slope_per_10x": float(slope), "r2": r2, "frontier": f}


def fig_score_vs_price(df, out: Path, sample: bool):
    d = df[(df[PRICE] > 0) & df[SCORE].notna()].copy()
    if len(d) < 8:
        return None
    rho, n = _spearman(np.log10(d[PRICE]), d[SCORE])
    fig, ax = _frame("Intelligence vs price",
                     "Blended price per 1M tokens (3:1 input:output). Up and to the left is better", sample=sample)
    ax.set_xscale("log")
    _scatter_groups(ax, d, PRICE, SCORE)
    f = _frontier(ax, d, PRICE, SCORE)
    _label_points(ax, f, PRICE, SCORE)
    ax.xaxis.set_major_formatter(FuncFormatter(_money))
    ax.set_xlabel("Price, USD per 1M tokens (log scale)")
    ax.set_ylabel("Artificial Analysis Intelligence Index")
    h, _ = ax.get_legend_handles_labels()
    ax.legend(handles=h + [Line2D([], [], color=INK2, lw=1.3, ls="--", label="cheapest-for-score frontier")], loc="lower right")
    path = _save(fig, out / "02_score_vs_price.png")
    return path, {"n": n, "rho": rho, "frontier": f}


def fig_speed_vs_price(df, out: Path, sample: bool):
    d = df[(df[PRICE] > 0) & df[SPEED].notna() & df[SCORE].notna()].copy()
    if len(d) < 8:
        return None
    rho, n = _spearman(np.log10(d[PRICE]), d[SPEED])
    fig, ax = _frame("Speed vs price", "Median output tokens per second. Up and to the left is better; colour shows intelligence",
                     sample=sample)
    ax.set_xscale("log")
    norm = Normalize(d[SCORE].min(), d[SCORE].max())
    sc = ax.scatter(d[PRICE], d[SPEED], c=d[SCORE], cmap=SEQ, norm=norm, s=34, edgecolor=SURFACE, linewidth=1.0, zorder=3)
    f = _frontier(ax, d, PRICE, SPEED)
    _label_points(ax, f, PRICE, SPEED, max_labels=5)
    ax.xaxis.set_major_formatter(FuncFormatter(_money))
    ax.set_xlabel("Price, USD per 1M tokens (log scale)")
    ax.set_ylabel("Output speed (tokens / second)")
    cb = fig.colorbar(sc, ax=ax, pad=0.015, fraction=0.035)
    cb.set_label("Intelligence Index", color=INK2)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, length=0)
    ax.legend(handles=[Line2D([], [], color=INK2, lw=1.3, ls="--", label="cheapest-for-speed frontier")], loc="upper right")
    path = _save(fig, out / "03_speed_vs_price.png")
    return path, {"n": n, "rho": rho, "frontier": f}


def correlation_table(df) -> tuple[pd.DataFrame, pd.DataFrame]:
    cols = [c for c in SCORE_COLS if df[c].notna().sum() >= 15]
    corr = df[cols].corr(method="spearman", min_periods=15)
    m = df[cols].notna().astype(int)
    return corr, m.T @ m


def fig_correlations(df, out: Path, sample: bool):
    corr, n = correlation_table(df)
    if len(corr) < 3:
        return None
    k = len(corr)
    fig, ax = _frame("Benchmark correlations", "Spearman rank correlation across models; pairs need at least 15 shared models",
                     sample=sample, figsize=(8.2, 6.6))
    ax.grid(False)
    off = corr.to_numpy()[~np.eye(k, dtype=bool)]
    if np.nanmin(off) >= 0:  # all positive: a one-hue ramp stretched over the observed range shows the differences
        vmin, cmap = max(0.0, np.floor((np.nanmin(off) - 0.05) * 10) / 10), SEQ
    else:  # mixed signs: diverging scale with a neutral midpoint
        vmin, cmap = -1.0, DIV
    im = ax.imshow(corr.to_numpy(), cmap=cmap, vmin=vmin, vmax=1)
    cb = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.02)
    cb.set_label("Spearman ρ", color=INK2)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, length=0)
    names = [LABELS[c] for c in corr.columns]
    ax.set_xticks(range(k), names, rotation=40, ha="right")
    ax.set_yticks(range(k), names)
    ax.tick_params(length=0, colors=INK2)
    for s in ax.spines.values():
        s.set_visible(False)
    for i in range(k):
        for j in range(k):
            v = corr.iat[i, j]
            if np.isfinite(v):
                dark = (v - vmin) / (1 - vmin) > 0.55
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7.5, color="#ffffff" if dark else INK)
    path = _save(fig, out / "04_benchmark_correlations.png", top=0.88, bottom=0.2, right=0.92)
    tri = [(corr.index[i], corr.columns[j], corr.iat[i, j], int(n.iat[i, j])) for i in range(k) for j in range(i + 1, k)
           if np.isfinite(corr.iat[i, j])]
    tri.sort(key=lambda t: t[2])
    return path, {"corr": corr, "pairs": tri}


def trend_tables(df) -> dict[str, Any]:
    d = df[df["release_date"].notna() & df[SCORE].notna()].sort_values("release_date").copy()
    if len(d) < 8:
        return {}
    d["running_best"] = d[SCORE].cummax()
    d["running_best_open"] = d[SCORE].where(d["group"] == OPEN).cummax()
    thr = float(round(d[SCORE].median()))
    p = d[(d[PRICE] > 0) & (d[SCORE] >= thr)].copy()
    p["cheapest_so_far"] = p[PRICE].cummin()
    return {"d": d, "threshold": thr, "p": p}


def fig_trends(df, out: Path, sample: bool):
    t = trend_tables(df)
    if not t:
        return None
    d, p, thr = t["d"], t["p"], t["threshold"]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9.2, 7.6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    _decorate(fig, "Trends over release date", "Each dot is a model. Lines are running best (top) and running cheapest (bottom)", sample)
    _scatter_groups(a1, d, "release_date", SCORE)
    a1.step(d["release_date"], d["running_best"], where="post", color=INK2, lw=1.5, zorder=2)
    od = d[d["running_best_open"].notna()]
    a1.step(od["release_date"], od["running_best_open"], where="post", color=BLUE, lw=1.5, zorder=2)
    a1.set_ylabel("Intelligence Index")
    h, _ = a1.get_legend_handles_labels()
    a1.legend(handles=h + [Line2D([], [], color=INK2, lw=1.5, label="best model so far"),
                           Line2D([], [], color=BLUE, lw=1.5, label="best open-weights so far")], loc="upper left")
    if len(p):
        a2.set_yscale("log")
        a2.scatter(p["release_date"], p[PRICE], s=22, color=MUTED, edgecolor=SURFACE, linewidth=0.8, zorder=3)
        a2.step(p["release_date"], p["cheapest_so_far"], where="post", color=INK2, lw=1.5, zorder=2)
        a2.yaxis.set_major_formatter(FuncFormatter(_money))
    a2.set_ylabel("Price per 1M tokens")
    a2.set_title(f"Only models scoring at least {thr:g} on the index (the median); line = cheapest released so far",
                 fontsize=8.5, color=INK2, loc="left")
    a2.set_xlabel("Release date")
    fig.subplots_adjust(top=0.87, bottom=0.09, left=0.1, right=0.965, hspace=0.12)
    path = out / "05_trends_over_time.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path, t


def missing_table(df) -> pd.DataFrame:
    return (df[MISS_COLS].isna().mean().mul(100).rename("pct_missing").reset_index()
            .rename(columns={"index": "column"}).sort_values("pct_missing", ascending=False))


def fig_missing(df, out: Path, sample: bool):
    miss = missing_table(df)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 6.4), gridspec_kw={"width_ratios": [1, 1.25]})
    _decorate(fig, "Where the data is missing", "Left: share of models missing each field. Right: one row per model, oldest at the top", sample)
    m = miss.iloc[::-1]
    a1.barh([LABELS[c] for c in m["column"]], m["pct_missing"], color=BLUE, height=0.55)
    for y, v in enumerate(m["pct_missing"]):
        a1.text(v + 1, y, f"{v:.0f}%", va="center", fontsize=8, color=INK2)
    a1.set_xlim(0, 105)
    a1.set_xlabel("% of models missing")
    a1.grid(axis="y", visible=False)
    order = df.sort_values("release_date", na_position="last")
    cols = list(miss["column"])
    a2.imshow(order[cols].isna().to_numpy(), aspect="auto", interpolation="nearest",
              cmap=ListedColormap(["#cde2fb", ORANGE]))
    a2.set_xticks(range(len(cols)), [LABELS[c] for c in cols], rotation=60, ha="right", fontsize=8)
    a2.set_yticks([])
    a2.grid(False)
    for s in a2.spines.values():
        s.set_visible(False)
    a2.legend(handles=[Patch(color="#cde2fb", label="present"), Patch(color=ORANGE, label="missing")],
              loc="upper center", bbox_to_anchor=(0.5, 1.07), ncol=2)
    fig.subplots_adjust(top=0.85, bottom=0.2, left=0.13, right=0.98, wspace=0.12)
    path = out / "06_missing_data.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path, {"miss": miss}


def value_table(df, top: int = 12) -> pd.DataFrame:
    d = df[(df[PRICE] > 0) & df[SCORE].notna()].copy()
    if d.empty:
        return d
    d = d[d[SCORE] >= d[SCORE].median()]  # ratio alone rewards cheap-and-weak models
    d["points_per_usd"] = d[SCORE] / d[PRICE]
    return d.sort_values("points_per_usd", ascending=False).head(top)


def fig_value(df, out: Path, sample: bool):
    v = value_table(df)
    if len(v) < 3:
        return None
    fig, ax = _frame("Best value: intelligence per dollar",
                     "Index points per USD per 1M tokens, among models at or above the median intelligence", sample=sample)
    v = v.iloc[::-1]
    ax.barh([_short(n, 34) for n in v["name"]], v["points_per_usd"], height=0.55, color=[GROUP_COLOR[g] for g in v["group"]])
    for y, val in enumerate(v["points_per_usd"]):
        ax.text(val * 1.01, y, f"{val:,.0f}", va="center", fontsize=8, color=INK2)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Intelligence index points per USD / 1M tokens")
    ax.legend(handles=[Patch(color=c, label=g) for g, c in GROUP_COLOR.items()], loc="lower right")
    path = _save(fig, out / "07_best_value.png", left=0.3)
    return path, {"table": v.iloc[::-1]}


# ---------------------------------------------------------------------------
# written summary
# ---------------------------------------------------------------------------
def _pareto_rows(f: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return f[cols].rename(columns={"name": "model", SCORE: "index", PRICE: "$/1M", SPEED: "tok/s", SIZE: "params (B)"})


def build_summary(df: pd.DataFrame, run: dict, facts: dict[str, dict], figs: dict[str, Path], out: Path) -> str:
    sample = run.get("source") == "sample"
    L: list[str] = ["# AI model comparison - EDA summary", ""]
    if sample:
        L += ["> **SYNTHETIC SAMPLE DATA.** Generated offline to test the pipeline. Model names and numbers are invented; "
              "none of the findings below describe real models. Run `python -m aimodels all` with an Artificial Analysis key "
              "for real results.", ""]
    L += [f"*{ATTRIBUTION}.* Pipeline run {run.get('run_id', '?')} ({run.get('finished_at', '')}, source: {run.get('source', '?')}).", ""]

    # --- coverage
    n = len(df)
    complete = df[[SCORE, PRICE, SPEED, SIZE]].notna().all(axis=1).sum()
    L += ["## Coverage", "",
          f"- **{n}** models after validation; **{run.get('n_rejected', 0)}** rows rejected by the Pydantic schema.",
          f"- Complete on all four dimensions (score, price, speed, parameter count): **{complete}** ({complete / n:.0%}).",
          f"- Open weights (Hugging Face repo found): **{(df['group'] == OPEN).sum()}**; parameter counts known for "
          f"**{df[SIZE].notna().sum()}** ({df[SIZE].notna().mean():.0%}).", ""]
    stats = run.get("stats", {})
    if stats.get("match_methods"):
        mm = pd.Series(stats["match_methods"]).rename("models").rename_axis("OpenRouter match").reset_index()
        L += ["How Artificial Analysis models were matched to OpenRouter after name normalization:", "", _md(mm), ""]

    # --- size
    if "size" in facts:
        s = facts["size"]
        L += ["## Score vs model size", "",
              f"- Spearman ρ = **{s['rho']:.2f}** ({_strength(s['rho'])}, n = {s['n']}). A log-linear fit gives "
              f"**{s['slope_per_10x']:+.1f}** index points per 10× parameters (R² = {s['r2']:.2f}).",
              "- Models on the *smallest-for-their-score* frontier:", "",
              _md(_pareto_rows(s["frontier"], ["name", SIZE, SCORE])), ""]

    # --- price
    if "price" in facts:
        s = facts["price"]
        L += ["## Score vs price", "",
              f"- Spearman ρ (log price vs score) = **{s['rho']:.2f}** ({_strength(s['rho'])}, n = {s['n']}).",
              "- Models on the *cheapest-for-their-score* frontier (nothing is both cheaper and better):", "",
              _md(_pareto_rows(s["frontier"], ["name", SCORE, PRICE, SPEED])), ""]
    if "value" in facts:
        L += ["- Best intelligence per dollar (models at or above the median score):", "",
              _md(_pareto_rows(facts["value"]["table"].assign(points_per_usd=lambda x: x["points_per_usd"].round().astype(int)),
                               ["name", SCORE, PRICE, "points_per_usd"]).rename(columns={"points_per_usd": "pts per $"})), ""]

    # --- speed
    if "speed" in facts:
        s = facts["speed"]
        L += ["## Speed vs price", "",
              f"- Spearman ρ (log price vs tokens/s) = **{s['rho']:.2f}** ({_strength(s['rho'])}, n = {s['n']}).",
              "- Models on the *cheapest-for-their-speed* frontier:", "",
              _md(_pareto_rows(s["frontier"], ["name", SPEED, PRICE, SCORE])), ""]

    # --- correlations
    if "corr" in facts:
        pairs = facts["corr"]["pairs"]
        vals = [p[2] for p in pairs]
        L += ["## Benchmark correlations", "",
              f"- Mean pairwise Spearman ρ across {len(pairs)} benchmark pairs: **{np.mean(vals):.2f}** "
              f"(range {min(vals):.2f} to {max(vals):.2f}).", "- Least-correlated pairs (most distinct signal):", ""]
        low = pd.DataFrame([(LABELS[a], LABELS[b], r, k) for a, b, r, k in pairs[:5]], columns=["benchmark A", "benchmark B", "ρ", "shared n"])
        high = pd.DataFrame([(LABELS[a], LABELS[b], r, k) for a, b, r, k in pairs[-3:][::-1]], columns=["benchmark A", "benchmark B", "ρ", "shared n"])
        L += [_md(low), "", "- Most-correlated pairs (largely redundant):", "", _md(high), ""]

    # --- trends
    if "trend" in facts:
        t = facts["trend"]
        d = t["d"].copy()
        d["year"] = d["release_date"].dt.year
        by = d.groupby("year").agg(models=(SCORE, "size"), best=(SCORE, "max"), median=(SCORE, "median")).reset_index()
        op = d[d["group"] == OPEN].groupby("year")[SCORE].max().rename("best open").reset_index()
        by = by.merge(op, on="year", how="left")
        pr = d[d[PRICE] > 0].groupby("year")[PRICE].median().rename("median $/1M").reset_index()
        by = by.merge(pr, on="year", how="left")
        L += ["## Trends over time", "", "By release year (models with a release date and score):", "", _md(by), ""]
        p = t["p"]
        if len(p) >= 2:
            L += [f"- Cheapest model with index ≥ {t['threshold']:g} so far: **${p['cheapest_so_far'].iloc[0]:,.2f}** at first appearance "
                  f"({p['release_date'].iloc[0]:%Y-%m}) vs **${p['cheapest_so_far'].iloc[-1]:,.2f}** now "
                  f"({p['cheapest_so_far'].iloc[0] / p['cheapest_so_far'].iloc[-1]:.1f}× cheaper).", ""]
        best_all, best_open = d[SCORE].max(), d.loc[d["group"] == OPEN, SCORE].max()
        if pd.notna(best_open):
            L += [f"- Best overall index: **{best_all:.1f}**; best open-weights index: **{best_open:.1f}** (gap {best_all - best_open:+.1f}).", ""]
        L += ["- `model_snapshots` stores each pipeline run's score/price/speed, so true time-series of *the same model* "
              "accumulate as you re-run the ETL on a schedule; the charts above use release dates.", ""]

    # --- missing
    if "missing" in facts:
        miss = facts["missing"]["miss"].copy()
        miss["pct_missing"] = miss["pct_missing"].round(1)
        L += ["## Missing-data analysis", "", _md(miss.rename(columns={"pct_missing": "% missing"})), ""]
        key = [PRICE, SPEED, "context_length", SIZE, "hle"]
        g = df.groupby("group")[key].agg(lambda s: s.isna().mean() * 100).round(1).reset_index()
        L += ["Missingness by group (% missing). Gaps are not random: they depend on whether a model is open "
              "(parameter counts) and on whether OpenRouter lists it (context length):", "", _md(g), ""]
        if df["hle"].isna().any() and df["hle"].notna().any() and df["release_date"].notna().any():
            has, no = df[df["hle"].notna()]["release_date"].min(), df[df["hle"].isna()]["release_date"].max()
            verdict = ("every model without one predates the first model that has one, consistent with the benchmark "
                       "being added later (structural missingness)" if no < has else
                       "the date ranges overlap, so some gaps are not explained by the benchmark's start date alone")
            L += [f"- Earliest model with an HLE score: {has:%Y-%m-%d}; latest model without one: {no:%Y-%m-%d} - {verdict}.", ""]
        both = df[(df[PRICE] > 0) & (df["or_price_blended_usd_per_m"] > 0)]
        if len(both) >= 5:
            ratio = both["or_price_blended_usd_per_m"] / both[PRICE]
            L += [f"- Price cross-check on {len(both)} models priced by both sources: median OpenRouter/AA ratio "
                  f"**{ratio.median():.2f}**, {((ratio - 1).abs() <= 0.25).mean():.0%} within ±25%.", ""]
        if stats.get("params_sources"):
            L += [f"- Parameter-count provenance: {stats['params_sources']}.", ""]

    L += ["## Figures", ""] + [f"- `{p.relative_to(out) if p.is_relative_to(out) else p}`" for p in figs.values()] + [""]
    L += ["## Caveats", "",
          "- **\"Open weights\" is inferred** from a Hugging Face repo id on OpenRouter or in `data/manual_hf_ids.csv`; "
          "an open model OpenRouter does not list is counted as closed unless added manually.",
          "- **Parameter counts are total, not active.** Mixture-of-experts models look large on the size axis; "
          "`active_params_b` is parsed from names like `235B A22B` where available.",
          "- Hugging Face `safetensors.total` can mis-count quantized checkpoints; prefer full-precision repos in the manual map.",
          "- Name matching is conservative by design: ambiguous names stay unmatched rather than risk a wrong join.",
          "- Prices are list prices at fetch time; reasoning models use many more output tokens per task than the price suggests.",
          "- Correlations and fits describe this sample of benchmarked models, not causal effects."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
def run_eda(db_path: Path, out_dir: Path) -> Path:
    _style()
    df = load_frame(db_path)
    run = read_last_run(db_path)
    sample = run.get("source") == "sample"
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    figs: dict[str, Path] = {}
    facts: dict[str, dict] = {}
    for key, fn in [("size", fig_score_vs_size), ("price", fig_score_vs_price), ("speed", fig_speed_vs_price),
                    ("corr", fig_correlations), ("trend", fig_trends), ("missing", fig_missing), ("value", fig_value)]:
        res = fn(df, fig_dir, sample)
        if res is None:
            log.warning("skipped '%s' figure: not enough data", key)
            continue
        figs[key], facts[key] = res

    report = out_dir / "eda_summary.md"
    report.write_text(build_summary(df, run, facts, figs, out_dir), encoding="utf-8")
    return report
