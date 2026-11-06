"""Extract: pull raw JSON from the three public APIs with requests.

* Artificial Analysis - benchmark indices, price, speed. Needs a free API key in
  ``AA_API_KEY`` (sent as ``x-api-key``). The free tier is rate limited per day,
  so every successful response is cached to ``data/raw`` and ``--use-cache``
  re-runs the pipeline without spending quota.
* OpenRouter - ``GET /api/v1/models``; public, no key.
* Hugging Face Hub - ``GET /api/models/{repo}``; public. ``HF_TOKEN`` (optional)
  raises the rate limit. The response's ``safetensors.total`` is the parameter count.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    AA_ENDPOINTS,
    HF_CACHE_PATH,
    HF_CACHE_TTL_DAYS,
    HF_MODEL_URL,
    HF_WORKERS,
    OPENROUTER_URL,
    RAW_DIR,
    REQUEST_TIMEOUT,
)

log = logging.getLogger(__name__)


class ExtractError(RuntimeError):
    """Raised when a source cannot be read and the pipeline should stop."""


def explain_request_error(exc: BaseException, source: str) -> str:
    """Turn a requests exception into a sentence a non-programmer can act on."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, requests.exceptions.SSLError) or "certificate" in low:
        why = ("A security certificate check failed. This usually means a company/school network or antivirus "
               "is inspecting HTTPS traffic. Try another network, or install your organisation's root certificate.")
    elif isinstance(exc, requests.exceptions.ProxyError) or "proxy" in low:
        why = "Your computer's proxy settings blocked the connection. Check the proxy/VPN settings or try another network."
    elif isinstance(exc, requests.exceptions.ConnectTimeout) or isinstance(exc, requests.exceptions.ReadTimeout):
        why = "The server did not answer in time. Check your internet connection and try again."
    elif isinstance(exc, requests.exceptions.ConnectionError):
        why = ("Could not connect. Check that you are online, and that a firewall, VPN or antivirus "
               "is not blocking Python from reaching the internet.")
    elif isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        why = f"The server answered HTTP {exc.response.status_code}."
    elif isinstance(exc, ValueError):
        why = "The server answered, but not with JSON. A network login page or the wrong URL may be in the way."
    else:
        why = "Unexpected network error."
    return f"{source}: {why} (technical detail: {exc.__class__.__name__}: {text[:160]})"


def make_session() -> requests.Session:
    """Session with retries on transient errors (429 is handled explicitly instead)."""
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "ai-model-pipeline/0.1 (+research project)"
    return session


# --------------------------------------------------------------------------
# raw-file cache
# --------------------------------------------------------------------------
def save_raw(name: str, payload: Any, raw_dir: Path = RAW_DIR) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{name}_{date.today():%Y%m%d}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    log.info("saved raw payload -> %s", path)
    return path


def load_latest_raw(name: str, raw_dir: Path = RAW_DIR) -> Any:
    files = sorted(raw_dir.glob(f"{name}_*.json"))
    if not files:
        raise ExtractError(f"No cached '{name}' payload in {raw_dir}. Run once without --use-cache.")
    log.info("using cached raw payload %s", files[-1].name)
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _data_list(payload: Any) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# Artificial Analysis
# --------------------------------------------------------------------------
def fetch_artificial_analysis(
    session: requests.Session, api_key: str | None, endpoints: list[str] | None = None
) -> dict:
    if not api_key:
        raise ExtractError(
            "AA_API_KEY is not set. Create a free key at https://artificialanalysis.ai "
            "(Insights Platform), export AA_API_KEY, or run with --offline / --use-cache."
        )
    problems: list[str] = []
    for url in endpoints or AA_ENDPOINTS:
        try:
            resp = session.get(url, headers={"x-api-key": api_key}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise ExtractError(explain_request_error(exc, "Artificial Analysis")) from exc
        if resp.status_code == 401:
            raise ExtractError("Artificial Analysis rejected the API key (HTTP 401). Re-copy the key from "
                               "your Artificial Analysis account; check for a missing character or extra space.")
        if resp.status_code == 429:
            raise ExtractError(
                "Artificial Analysis rate limit hit (429). The free tier is limited per day; "
                "re-run with --use-cache to reuse the last download."
            )
        if resp.status_code in (403, 404, 410):  # wrong path or tier: try the next candidate
            problems.append(f"{url} -> {resp.status_code}")
            continue
        try:
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise ExtractError(explain_request_error(exc, "Artificial Analysis")) from exc
        if not _data_list(payload):
            raise ExtractError(f"Artificial Analysis returned no models from {url}.")
        log.info("Artificial Analysis: %d models from %s", len(_data_list(payload)), url)
        return payload
    raise ExtractError("No Artificial Analysis endpoint accepted your key: " + "; ".join(problems)
                       + ". 403 usually means the key's plan does not include that endpoint; 404 means the "
                       "path changed. Set AA_URL to the endpoint shown in your Artificial Analysis docs.")


# --------------------------------------------------------------------------
# OpenRouter
# --------------------------------------------------------------------------
def fetch_openrouter(session: requests.Session) -> dict:
    try:
        resp = session.get(OPENROUTER_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ExtractError(explain_request_error(exc, "OpenRouter")) from exc
    if not _data_list(payload):
        raise ExtractError("OpenRouter returned no models.")
    log.info("OpenRouter: %d models", len(_data_list(payload)))
    return payload


# --------------------------------------------------------------------------
# Hugging Face Hub
# --------------------------------------------------------------------------
_thread_local = threading.local()


def _thread_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()
    return _thread_local.session


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def summarise_hf(repo_id: str, info: dict | None) -> dict:
    """Reduce a Hub model record to what we keep. ``info=None`` means 404."""
    base = {"repo_id": repo_id, "fetched_at": _now_iso(), "params_total": None, "pipeline_tag": None}
    if info is None:
        return {**base, "status": "not_found"}
    st = info.get("safetensors") or {}
    total = st.get("total")
    if total is None and isinstance(st.get("parameters"), dict):
        total = sum(v for v in st["parameters"].values() if isinstance(v, (int, float))) or None
    return {
        **base,
        "status": "ok" if total else "no_safetensors",
        "params_total": int(total) if total else None,
        "pipeline_tag": info.get("pipeline_tag"),
    }


def _fetch_hf_one(repo_id: str, token: str | None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = _thread_session().get(
            HF_MODEL_URL.format(repo_id=repo_id), headers=headers, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code == 404:
            return summarise_hf(repo_id, None)
        resp.raise_for_status()
        return summarise_hf(repo_id, resp.json())
    except (requests.RequestException, ValueError) as exc:
        # Not cached, so the next run retries it.
        return {"repo_id": repo_id, "status": "error", "error": str(exc)[:200],
                "params_total": None, "pipeline_tag": None, "fetched_at": _now_iso()}


def fetch_hf_params(
    repo_ids: Iterable[str],
    *,
    token: str | None = None,
    cache_path: Path = HF_CACHE_PATH,
    ttl_days: int = HF_CACHE_TTL_DAYS,
    workers: int = HF_WORKERS,
    offline_lookup: dict[str, dict | None] | None = None,
) -> dict[str, dict]:
    """Look up parameter counts. Returns ``{repo_id: summary}``.

    Results (except transient errors) are cached for ``ttl_days`` so repeated runs
    only hit the Hub for new models. ``offline_lookup`` replaces the network for
    the synthetic-data demo and tests.
    """
    repos = sorted({r for r in repo_ids if r})
    if offline_lookup is not None:
        return {r: summarise_hf(r, offline_lookup.get(r)) for r in repos}

    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("ignoring corrupt HF cache %s", cache_path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

    def fresh(entry: dict) -> bool:
        try:
            return entry.get("status") != "error" and datetime.fromisoformat(entry["fetched_at"]) > cutoff
        except (KeyError, ValueError):
            return False

    todo = [r for r in repos if not (r in cache and fresh(cache[r]))]
    log.info("Hugging Face: %d repos (%d cached, %d to fetch)", len(repos), len(repos) - len(todo), len(todo))
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(lambda r: _fetch_hf_one(r, token), todo):
                cache[result["repo_id"]] = result
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache), encoding="utf-8")

    errors = [r for r in repos if cache.get(r, {}).get("status") == "error"]
    if errors:
        log.warning("Hugging Face: %d lookups failed (will retry next run), e.g. %s", len(errors), errors[:3])
    return {r: cache[r] for r in repos if r in cache}


# --------------------------------------------------------------------------
# connection test (used by the dashboard's "Test connections" button)
# --------------------------------------------------------------------------
def check_connections(aa_key: str | None, hf_token: str | None = None, endpoints: list[str] | None = None) -> list[dict]:
    """Probe each source once, cheaply. Returns ``[{source, ok, message}]``.

    The Artificial Analysis probe spends one request of the free daily quota
    (about 10/day), the other two are free and unmetered.
    """
    session = make_session()
    session.adapters["https://"].max_retries = Retry(total=0)  # fail fast when testing
    out: list[dict] = []

    def add(source: str, ok: bool, message: str) -> None:
        out.append({"source": source, "ok": ok, "message": message})

    if not aa_key:
        add("Artificial Analysis", False, "No key entered. Paste it under ADV, then press Use keys.")
    else:
        try:
            payload = fetch_artificial_analysis(session, aa_key, endpoints)
            add("Artificial Analysis", True, f"Key accepted, {len(_data_list(payload))} models returned.")
        except ExtractError as exc:
            add("Artificial Analysis", False, str(exc))
    try:
        payload = fetch_openrouter(session)
        add("OpenRouter", True, f"{len(_data_list(payload))} models returned.")
    except ExtractError as exc:
        add("OpenRouter", False, str(exc))
    try:
        headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
        resp = session.get(HF_MODEL_URL.format(repo_id="Qwen/Qwen2.5-0.5B"), headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 401:
            add("Hugging Face", False, "Hugging Face rejected the token (HTTP 401). Clear it, since the token is optional.")
        else:
            resp.raise_for_status()
            add("Hugging Face", True, "Reachable" + (" with your token." if hf_token else " (no token, lower rate limit)."))
    except (requests.RequestException, ValueError) as exc:
        add("Hugging Face", False, explain_request_error(exc, "Hugging Face"))
    return out
