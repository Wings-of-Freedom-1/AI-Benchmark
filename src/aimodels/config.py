"""Paths, endpoints and tunables in one place."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- paths -----------------------------------------------------------------
ROOT = Path(os.environ.get("AIMODELS_HOME", Path(__file__).resolve().parents[2]))
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("AIMODELS_DB", DATA_DIR / "models.db"))
SAMPLE_DB_PATH = DATA_DIR / "sample.db"  # offline/synthetic runs never touch the live DB
REPORTS_DIR = ROOT / "reports"
MANUAL_HF_PATH = DATA_DIR / "manual_hf_ids.csv"
HF_CACHE_PATH = RAW_DIR / "hf_cache.json"

# --- endpoints -------------------------------------------------------------
# Artificial Analysis has documented two paths for its free LLM endpoint; we try
# them in order and fall back on 403/404/410. Override with AA_URL if it changes.
AA_ENDPOINTS: list[str] = (
    [os.environ["AA_URL"]]
    if os.environ.get("AA_URL")
    else [
        "https://artificialanalysis.ai/api/v2/data/llms/models",
        "https://artificialanalysis.ai/api/v2/language/models/free",
    ]
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
HF_MODEL_URL = "https://huggingface.co/api/models/{repo_id}"

# --- tunables --------------------------------------------------------------
REQUEST_TIMEOUT = (10, 60)  # (connect, read) seconds
HF_CACHE_TTL_DAYS = 7
HF_WORKERS = 8
FUZZY_THRESHOLD = 0.93  # difflib ratio needed for a fuzzy name match
MAX_REJECT_FRACTION = 0.25  # fail the run if more rows than this fail validation

# Artificial Analysis requires attribution wherever its data is shown.
ATTRIBUTION = "Data: Artificial Analysis (artificialanalysis.ai), OpenRouter, Hugging Face Hub"
