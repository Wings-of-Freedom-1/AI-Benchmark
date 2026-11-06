"""Orchestrate Extract -> Transform -> Validate -> Load."""

from __future__ import annotations

import csv
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import extract
from .clean import build_models
from .config import DB_PATH, FUZZY_THRESHOLD, MANUAL_HF_PATH, MAX_REJECT_FRACTION, RAW_DIR, SAMPLE_DB_PATH
from .load import load_all
from .sample_data import build_sample
from .schemas import validate_models

log = logging.getLogger(__name__)

STAGES = ("extract", "transform", "validate", "load")
MANUAL_HF_HEADER = [
    "# Hand-curated Hugging Face repos for open-weight models that OpenRouter does not list.",
    "# match_key must equal the normalized name key (the dashboard's Data quality tab can add rows).",
]


@dataclass
class RunSummary:
    run_id: int
    source: str
    db_path: Path
    n_valid: int
    n_rejected: int
    stats: dict


def load_manual_hf(path: Path = MANUAL_HF_PATH) -> dict[str, str]:
    """Hand-curated ``match_key,hf_repo_id`` rows for open models OpenRouter does not list."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        rows = csv.DictReader(line for line in fh if not line.lstrip().startswith("#"))
        return {r["match_key"].strip(): r["hf_repo_id"].strip() for r in rows if r.get("match_key")}


def save_manual_hf(mapping: dict[str, str], path: Path = MANUAL_HF_PATH) -> None:
    """Rewrite the manual map (used by the dashboard's mapping editor)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [*MANUAL_HF_HEADER, "match_key,hf_repo_id"]
    lines += [f"{k},{v}" for k, v in sorted(mapping.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_etl(
    *,
    offline: bool = False,
    use_cache: bool = False,
    skip_hf: bool = False,
    db_path: Path | None = None,
    raw_dir: Path = RAW_DIR,
    manual_hf_path: Path = MANUAL_HF_PATH,
    fuzzy_threshold: float | None = None,
    max_reject_fraction: float | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> RunSummary:
    """Run the whole ETL. ``on_stage`` is told when each stage begins (used by the dashboard)."""
    stage = on_stage or (lambda _name: None)
    started = datetime.now(timezone.utc)
    db_path = db_path or (SAMPLE_DB_PATH if offline else DB_PATH)
    fuzzy = FUZZY_THRESHOLD if fuzzy_threshold is None else fuzzy_threshold
    tolerance = MAX_REJECT_FRACTION if max_reject_fraction is None else max_reject_fraction
    hf_lookup = None

    # ---- Extract -----------------------------------------------------------
    stage("extract")
    if offline:
        log.warning("OFFLINE MODE: using synthetic sample data (not real models)")
        bundle = build_sample()
        aa_payload, or_payload, hf_lookup = bundle.aa_payload, bundle.or_payload, bundle.hf_lookup
        source = "sample"
    elif use_cache:
        aa_payload = extract.load_latest_raw("artificial_analysis", raw_dir)
        or_payload = extract.load_latest_raw("openrouter", raw_dir)
        source = "live"
    else:
        session = extract.make_session()
        key = os.environ.get("AA_API_KEY") or os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
        aa_payload = extract.fetch_artificial_analysis(session, key)
        extract.save_raw("artificial_analysis", aa_payload, raw_dir)
        or_payload = extract.fetch_openrouter(session)
        extract.save_raw("openrouter", or_payload, raw_dir)
        source = "live"

    def hf_fetcher(repo_ids: list[str]) -> dict[str, dict]:
        if skip_hf:
            log.info("skipping Hugging Face (--skip-hf)")
            return {}
        return extract.fetch_hf_params(repo_ids, token=os.environ.get("HF_TOKEN"), offline_lookup=hf_lookup)

    # ---- Transform ---------------------------------------------------------
    stage("transform")
    result = build_models(
        aa_payload, or_payload, hf_fetcher, manual_hf=load_manual_hf(manual_hf_path), fuzzy_threshold=fuzzy
    )

    # ---- Validate ----------------------------------------------------------
    stage("validate")
    validation = validate_models(result.models, max_reject_fraction=tolerance)
    for r in validation.rejects.itertuples():
        log.warning("rejected %s: %s", r.model_key, r.errors)

    # ---- Load --------------------------------------------------------------
    stage("load")
    run_id = load_all(db_path, result, validation.valid, validation.rejects, source=source, started_at=started)
    return RunSummary(run_id, source, db_path, len(validation.valid), len(validation.rejects), result.stats)
