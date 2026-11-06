"""Transform: flatten each source, normalize names, merge, resolve conflicts.

Artificial Analysis (AA) is the spine - it has the benchmarks. OpenRouter adds
context length, a second price and (for open models) the Hugging Face repo id.
Hugging Face adds the parameter count. Where parameter counts are missing we fall
back to the size in the model's name ("70B"), and record which source was used.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .config import FUZZY_THRESHOLD
from .normalize import NameIndex, NormalizedName, normalize_name, parse_param_counts
from .schemas import BENCH_COLS, MODEL_COLUMNS

log = logging.getLogger(__name__)

# Where each column lives in an AA record. First path that has a value wins, which
# lets us survive the field being renamed or moved between API versions.
AA_FIELD_PATHS: dict[str, tuple[str, ...]] = {
    "intelligence_index": ("evaluations.artificial_analysis_intelligence_index",),
    "coding_index": ("evaluations.artificial_analysis_coding_index",),
    "math_index": ("evaluations.artificial_analysis_math_index",),
    "mmlu_pro": ("evaluations.mmlu_pro",),
    "gpqa": ("evaluations.gpqa",),
    "hle": ("evaluations.hle",),
    "livecodebench": ("evaluations.livecodebench",),
    "scicode": ("evaluations.scicode",),
    "math_500": ("evaluations.math_500",),
    "aime": ("evaluations.aime",),
    "aa_price_blended": ("pricing.price_1m_blended_3_to_1",),
    "aa_price_input": ("pricing.price_1m_input_tokens",),
    "aa_price_output": ("pricing.price_1m_output_tokens",),
    "tokens_per_sec": ("median_output_tokens_per_second", "performance.median_output_tokens_per_second"),
    "ttft_s": ("median_time_to_first_token_seconds", "performance.median_time_to_first_token_seconds"),
}
# Columns that should never be entirely empty; if they are, the API shape probably changed.
AA_CANARIES = ("intelligence_index", "aa_price_blended", "tokens_per_sec", "release_date")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def records(payload: Any) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _get(rec: dict, *paths: str) -> Any:
    for path in paths:
        cur: Any = rec
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur is not None:
            return cur
    return None


def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(v) or np.isinf(v) else v


def _iso_date(x: Any) -> str | None:
    if x is None or x == "":
        return None
    try:
        if isinstance(x, (int, float)):
            return datetime.fromtimestamp(float(x), tz=timezone.utc).strftime("%Y-%m-%d")
        ts = pd.to_datetime(x, errors="coerce", utc=True)
        return None if pd.isna(ts) else ts.strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return None


def _per_million(x: Any) -> float | None:
    """OpenRouter prices are USD per token as strings; negative means 'varies' (routers)."""
    v = _num(x)
    return None if v is None or v < 0 else round(v * 1e6, 6)


def _blend(p_in: float | None, p_out: float | None) -> float | None:
    """AA's convention: 3 input tokens for every 1 output token."""
    if p_in is None or p_out is None:
        return None
    return round((3 * p_in + p_out) / 4, 6)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# --------------------------------------------------------------------------
# Artificial Analysis
# --------------------------------------------------------------------------
def flatten_aa(payload: Any) -> pd.DataFrame:
    rows = []
    for rec in records(payload):
        name = rec.get("name") or rec.get("slug")
        if not name:
            continue
        creator = _get(rec, "model_creator.name", "creator.name", "model_creator", "creator")
        row: dict[str, Any] = {
            "aa_id": str(rec["id"]) if rec.get("id") is not None else None,
            "aa_slug": rec.get("slug"),
            "name": str(name),
            "creator": creator if isinstance(creator, str) else None,
            "release_date": _iso_date(_get(rec, "release_date", "released_at")),
        }
        for col, paths in AA_FIELD_PATHS.items():
            row[col] = _num(_get(rec, *paths))
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("Artificial Analysis payload contained no usable model records.")

    empty = [c for c in AA_CANARIES if df[c].isna().all()]
    if empty:
        log.warning("AA columns completely empty %s - the API schema may have changed", empty)

    df = _rescale_fractions(df)
    norms = [normalize_name(n) for n in df["name"]]
    df["name_key"] = [n.key for n in norms]
    df["variant"] = [n.variant or None for n in norms]
    df["_norm"] = norms

    base = [(_slugify(s) if s else _slugify(n)) for s, n in zip(df["aa_slug"], df["name"])]
    df["model_key"] = _dedupe_keys(base)
    return df


def _dedupe_keys(keys: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for k in keys:
        seen[k] = seen.get(k, 0) + 1
        out.append(k if seen[k] == 1 else f"{k}-{seen[k]}")
    return out


def _rescale_fractions(df: pd.DataFrame) -> pd.DataFrame:
    """Benchmarks should be 0-1. If a column arrives as 0-100 percentages, convert it."""
    for col in BENCH_COLS:
        top = df[col].max()
        if pd.notna(top) and 1.5 < top <= 100:
            log.warning("%s looks like a percentage (max=%.1f); rescaling to 0-1", col, top)
            df[col] = df[col] / 100
    return df


# --------------------------------------------------------------------------
# OpenRouter
# --------------------------------------------------------------------------
def flatten_openrouter(payload: Any) -> pd.DataFrame:
    rows = []
    for rec in records(payload):
        rid = rec.get("id")
        if not rid:
            continue
        pricing = rec.get("pricing") or {}
        p_in, p_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        rows.append(
            {
                "or_id": rid,
                "or_name": rec.get("name"),
                "or_created": _iso_date(rec.get("created")),
                "context_length": _num(_get(rec, "context_length", "top_provider.context_length")),
                "or_price_input": p_in,
                "or_price_output": p_out,
                "or_price_blended": _blend(p_in, p_out),
                "hf_id": rec.get("hugging_face_id") or None,
                "is_free_variant": str(rid).endswith(":free") or (p_in == 0 and p_out == 0),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("OpenRouter payload contained no usable model records.")

    df["_id_norm"] = [normalize_name(i, is_id=True) for i in df["or_id"]]
    df["_name_norm"] = [normalize_name(n) if isinstance(n, str) else normalize_name("") for n in df["or_name"]]
    df["or_key"] = [n.key for n in df["_id_norm"]]

    # One row per normalized key: prefer paid over ':free', then the newest release.
    df = (
        df.sort_values(["is_free_variant", "or_created"], ascending=[True, False], na_position="last")
        .drop_duplicates("or_key", keep="first")
        .reset_index(drop=True)
    )
    return df


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------
OR_COLUMNS = ["or_id", "context_length", "or_price_input", "or_price_output", "or_price_blended", "hf_id"]


def attach_openrouter(aa: pd.DataFrame, orr: pd.DataFrame, fuzzy_threshold: float = FUZZY_THRESHOLD) -> pd.DataFrame:
    """Left-join OpenRouter onto AA using normalized names; record how each match was made."""
    entries: list[tuple[NormalizedName, int]] = [(n, i) for i, n in enumerate(orr["_id_norm"])]
    entries += [(n, i) for i, n in enumerate(orr["_name_norm"])]  # id-based keys take priority
    index = NameIndex(entries, threshold=fuzzy_threshold)

    matches = [index.match(n) for n in aa["_norm"]]
    out = aa.copy()
    out["match_method"] = [m.method for m in matches]
    out["match_score"] = [m.score for m in matches]

    positions = pd.Series([m.index for m in matches], index=out.index, dtype="object")
    for col in OR_COLUMNS:
        src = orr[col].tolist()
        out[col] = [src[p] if p is not None and not pd.isna(p) else None for p in positions]
    for col in ("context_length", "or_price_input", "or_price_output", "or_price_blended"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    # Price: AA first, OpenRouter as fallback. A row takes ALL its prices from one source.
    use_aa = out["aa_price_blended"].notna()
    use_or = ~use_aa & out["or_price_blended"].notna()
    out["price_source"] = np.where(use_aa, "artificial_analysis", np.where(use_or, "openrouter", None))
    for dest, aa_col, or_col in [
        ("price_blended_usd_per_m", "aa_price_blended", "or_price_blended"),
        ("price_input_usd_per_m", "aa_price_input", "or_price_input"),
        ("price_output_usd_per_m", "aa_price_output", "or_price_output"),
    ]:
        out[dest] = np.where(use_aa, out[aa_col], np.where(use_or, out[or_col], np.nan))
    out["or_price_blended_usd_per_m"] = out["or_price_blended"]
    return out


def attach_params(
    df: pd.DataFrame, hf_results: dict[str, dict], manual_hf: dict[str, str] | None = None
) -> pd.DataFrame:
    """Fill params_b from Hugging Face, falling back to the size written in the name."""
    out = df.copy()
    if manual_hf:  # hand-curated name_key -> repo for open models OpenRouter does not list
        filled = out["name_key"].map(manual_hf)
        out["hf_id"] = out["hf_id"].where(out["hf_id"].notna(), filled)

    hf_params = out["hf_id"].map(lambda r: (hf_results.get(r) or {}).get("params_total") if isinstance(r, str) else None)
    hf_b = pd.to_numeric(hf_params, errors="coerce") / 1e9

    parsed = [parse_param_counts(n, h) for n, h in zip(out["name"], out["hf_id"])]
    name_total = pd.Series([p[0] for p in parsed], index=out.index, dtype="float64")
    out["active_params_b"] = pd.Series([p[1] for p in parsed], index=out.index, dtype="float64")

    out["params_b"] = hf_b.where(hf_b.notna(), name_total)
    out["params_source"] = np.where(hf_b.notna(), "hf", np.where(name_total.notna(), "name", None))
    out.loc[out["params_b"].isna(), "active_params_b"] = np.nan

    both = hf_b.notna() & name_total.notna()
    off = both & ((hf_b / name_total - 1).abs() > 0.25)
    if off.any():
        log.warning(
            "%d models where the HF parameter count differs >25%% from the size in the name, e.g. %s",
            int(off.sum()), out.loc[off, "name"].head(3).tolist(),
        )
    return out


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
@dataclass
class CleanResult:
    models: pd.DataFrame  # merged, one row per AA model, MODEL_COLUMNS only
    aa: pd.DataFrame  # flattened sources, kept for the raw_* staging tables
    openrouter: pd.DataFrame
    hf: pd.DataFrame
    stats: dict[str, Any] = field(default_factory=dict)


def build_models(
    aa_payload: Any,
    or_payload: Any,
    hf_fetcher: Callable[[list[str]], dict[str, dict]],
    manual_hf: dict[str, str] | None = None,
    fuzzy_threshold: float = FUZZY_THRESHOLD,
) -> CleanResult:
    aa = flatten_aa(aa_payload)
    orr = flatten_openrouter(or_payload)
    merged = attach_openrouter(aa, orr, fuzzy_threshold)

    repo_ids = set(merged["hf_id"].dropna())
    if manual_hf:
        repo_ids |= {manual_hf[k] for k in merged["name_key"] if k in manual_hf}
    hf_results = hf_fetcher(sorted(repo_ids))
    merged = attach_params(merged, hf_results, manual_hf)

    models = merged.reindex(columns=MODEL_COLUMNS)
    models["context_length"] = pd.to_numeric(models["context_length"], errors="coerce").round().astype("Int64")

    stats = {
        "aa_models": len(aa),
        "openrouter_models": len(orr),
        "hf_repos": len(hf_results),
        "fuzzy_threshold": fuzzy_threshold,
        "match_methods": merged["match_method"].value_counts().to_dict(),
        "params_sources": merged["params_source"].fillna("missing").value_counts().to_dict(),
        "price_sources": merged["price_source"].fillna("missing").value_counts().to_dict(),
    }
    log.info("merge stats: %s", stats)

    hf_df = pd.DataFrame(list(hf_results.values())) if hf_results else pd.DataFrame(
        columns=["repo_id", "status", "params_total", "pipeline_tag", "fetched_at"]
    )
    return CleanResult(
        models=models,
        aa=aa.drop(columns=["_norm"]),
        openrouter=orr.drop(columns=["_id_norm", "_name_norm"]),
        hf=hf_df,
        stats=stats,
    )
