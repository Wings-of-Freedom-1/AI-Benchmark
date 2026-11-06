"""Validate: the Pydantic contract every row must meet before it reaches SQLite.

Rows that fail are not dropped silently - they are returned in a ``rejects``
frame (and stored in the ``rejects`` table) with a human-readable reason. If too
many rows fail, the run aborts: that almost always means an API changed shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import MAX_REJECT_FRACTION

log = logging.getLogger(__name__)

INDEX_COLS = ["intelligence_index", "coding_index", "math_index"]  # 0-100 composite indices
BENCH_COLS = ["mmlu_pro", "gpqa", "hle", "livecodebench", "scicode", "math_500", "aime"]  # 0-1 accuracy
SCORE_COLS = INDEX_COLS + BENCH_COLS


class PipelineValidationError(RuntimeError):
    """Too many rows failed validation, or none were left."""


class ModelRecord(BaseModel):
    """One model: the unit of analysis. Field order is the SQLite column order."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # identity
    model_key: str = Field(min_length=1)
    aa_id: str | None = None
    name: str = Field(min_length=1)
    name_key: str
    variant: str | None = None
    creator: str | None = None
    release_date: date | None = None

    # performance
    intelligence_index: float | None = Field(None, ge=0, le=100)
    coding_index: float | None = Field(None, ge=0, le=100)
    math_index: float | None = Field(None, ge=0, le=100)
    mmlu_pro: float | None = Field(None, ge=0, le=1)
    gpqa: float | None = Field(None, ge=0, le=1)
    hle: float | None = Field(None, ge=0, le=1)
    livecodebench: float | None = Field(None, ge=0, le=1)
    scicode: float | None = Field(None, ge=0, le=1)
    math_500: float | None = Field(None, ge=0, le=1)
    aime: float | None = Field(None, ge=0, le=1)

    # cost (USD per 1M tokens)
    price_blended_usd_per_m: float | None = Field(None, ge=0, le=1000)
    price_input_usd_per_m: float | None = Field(None, ge=0, le=1000)
    price_output_usd_per_m: float | None = Field(None, ge=0, le=1000)
    price_source: Literal["artificial_analysis", "openrouter"] | None = None
    or_price_blended_usd_per_m: float | None = Field(None, ge=0, le=1000)

    # speed
    tokens_per_sec: float | None = Field(None, ge=0, le=5000)
    ttft_s: float | None = Field(None, ge=0, le=600)

    # size / context
    context_length: int | None = Field(None, gt=0)
    or_id: str | None = None
    hf_id: str | None = None
    params_b: float | None = Field(None, gt=0, le=20_000)
    active_params_b: float | None = Field(None, gt=0, le=20_000)
    params_source: Literal["hf", "name"] | None = None

    # lineage
    match_method: Literal["exact", "token_set", "fuzzy", "none"] = "none"
    match_score: float | None = Field(None, ge=0, le=1)

    @field_validator("release_date")
    @classmethod
    def _plausible_date(cls, v: date | None) -> date | None:
        if v is not None and not (date(2015, 1, 1) <= v <= date.today() + timedelta(days=60)):
            raise ValueError("release_date outside 2015-01-01 .. today+60d")
        return v

    @model_validator(mode="after")
    def _active_not_above_total(self) -> "ModelRecord":
        if self.active_params_b and self.params_b and self.active_params_b > self.params_b * 1.001:
            raise ValueError("active_params_b exceeds params_b")
        return self


MODEL_COLUMNS: list[str] = list(ModelRecord.model_fields)
INTEGER_COLS = {"context_length"}
TEXT_COLS = {
    "model_key", "aa_id", "name", "name_key", "variant", "creator", "release_date",
    "price_source", "or_id", "hf_id", "params_source", "match_method",
}


@dataclass
class ValidationResult:
    valid: pd.DataFrame
    rejects: pd.DataFrame


def _fmt_errors(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "record"
        got = repr(e.get("input"))
        parts.append(f"{loc}: {e['msg']} (got {got[:60]})" if loc != "record" else f"{loc}: {e['msg']}")
    return "; ".join(parts)


def validate_models(df: pd.DataFrame, *, max_reject_fraction: float = MAX_REJECT_FRACTION) -> ValidationResult:
    """Validate every row against :class:`ModelRecord`."""
    if df.empty:
        raise PipelineValidationError("No rows to validate - an upstream source returned nothing.")

    records = df.astype(object).where(df.notna(), None).to_dict("records")
    good: list[dict] = []
    bad: list[dict] = []
    seen: set[str] = set()

    for rec in records:
        key = rec.get("model_key")
        if key in seen:
            bad.append({"model_key": key, "name": rec.get("name"), "errors": "duplicate model_key"})
            continue
        try:
            obj = ModelRecord.model_validate(rec)
        except ValidationError as exc:
            bad.append({"model_key": key, "name": rec.get("name"), "errors": _fmt_errors(exc)})
            continue
        seen.add(key)
        good.append(obj.model_dump(mode="json"))

    rejects = pd.DataFrame(bad, columns=["model_key", "name", "errors"])
    valid = pd.DataFrame(good, columns=MODEL_COLUMNS)
    log.info("validation: %d valid, %d rejected", len(valid), len(rejects))

    if len(rejects) / len(df) > max_reject_fraction:
        sample = "; ".join(f"{r.model_key}: {r.errors}" for r in rejects.head(3).itertuples())
        raise PipelineValidationError(
            f"{len(rejects)}/{len(df)} rows failed validation (> {max_reject_fraction:.0%}). "
            f"An API schema has probably changed. Examples: {sample}"
        )
    if valid.empty:
        raise PipelineValidationError("Every row failed validation.")
    return ValidationResult(valid=valid, rejects=rejects)
