import sqlite3

import pandas as pd
import pytest

from aimodels.clean import build_models, flatten_aa, flatten_openrouter
from aimodels.eda import pareto_mask, run_eda
from aimodels.extract import fetch_hf_params, summarise_hf
from aimodels.pipeline import run_etl
from aimodels.sample_data import build_sample
from aimodels.schemas import PipelineValidationError, validate_models


@pytest.fixture(scope="module")
def bundle():
    return build_sample(seed=1)


def _hf(bundle):
    return lambda ids: fetch_hf_params(ids, offline_lookup=bundle.hf_lookup)


def test_flatten_aa_extracts_nested_fields(bundle):
    aa = flatten_aa(bundle.aa_payload)
    assert aa["model_key"].is_unique
    assert aa["intelligence_index"].notna().all()
    assert {"coding_index", "aa_price_blended", "tokens_per_sec", "release_date"} <= set(aa.columns)


def test_openrouter_prices_are_per_million_and_dedup_prefers_paid(bundle):
    orr = flatten_openrouter(bundle.or_payload)
    assert orr["or_key"].is_unique
    assert not orr["or_id"].str.endswith(":free").any()  # every free twin in the sample has a paid sibling, which wins
    paid = orr[orr["or_price_input"] > 0]
    assert paid["or_price_input"].between(0.001, 1000).all()  # USD per 1M tokens, not per token
    router = orr[orr["or_id"] == "openrouter/auto"].iloc[0]
    assert pd.isna(router["or_price_input"])  # '-1' pricing means "varies"


def test_merge_matches_and_resolves_sources(bundle):
    res = build_models(bundle.aa_payload, bundle.or_payload, _hf(bundle))
    m = res.models
    assert set(res.stats["match_methods"]) >= {"exact", "token_set", "none"}
    hf_rows = m[m["params_source"] == "hf"]
    assert len(hf_rows) > 0 and hf_rows["hf_id"].notna().all()
    name_rows = m[m["params_source"] == "name"]
    assert len(name_rows) > 0  # open models absent from OpenRouter fall back to the name
    assert m.loc[m["hf_id"].isna() & m["params_source"].isna(), "params_b"].isna().all()


def test_validation_rejects_corrupt_rows_with_reasons(bundle):
    res = build_models(bundle.aa_payload, bundle.or_payload, _hf(bundle))
    v = validate_models(res.models)
    assert len(v.rejects) == 3
    assert v.rejects["errors"].str.contains("intelligence_index|price_blended|tokens_per_sec").all()
    assert v.valid["model_key"].is_unique
    assert not v.valid["model_key"].str.contains("broken").any()


def test_validation_aborts_when_schema_drifts(bundle):
    res = build_models(bundle.aa_payload, bundle.or_payload, _hf(bundle))
    broken = res.models.copy()
    broken["intelligence_index"] = 999.0  # e.g. the API switched scales
    with pytest.raises(PipelineValidationError):
        validate_models(broken)


def test_percentage_benchmarks_are_rescaled(bundle):
    payload = {"data": [dict(r, evaluations={**r["evaluations"], "mmlu_pro": 79.1}) for r in bundle.aa_payload["data"][:20]]}
    assert flatten_aa(payload)["mmlu_pro"].max() <= 1.0


def test_hf_summary_handles_missing_metadata():
    assert summarise_hf("a/b", None)["status"] == "not_found"
    assert summarise_hf("a/b", {"id": "a/b"})["status"] == "no_safetensors"
    ok = summarise_hf("a/b", {"safetensors": {"parameters": {"BF16": 5, "F32": 5}}})
    assert ok["params_total"] == 10


def test_pareto_mask():
    # (price, score): C is dominated by B (cheaper and better); D ties A on price but is worse
    x, y = [1, 2, 3, 1], [10, 20, 15, 5]
    assert pareto_mask(x, y).tolist() == [True, True, False, False]
    # minimise price, maximise speed handled identically
    assert pareto_mask([1, 2], [5, 9]).tolist() == [True, True]


def test_etl_then_eda_end_to_end(tmp_path):
    db = tmp_path / "t.db"
    summary = run_etl(offline=True, db_path=db)
    assert summary.source == "sample" and summary.n_valid > 100 and summary.n_rejected == 3

    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"models", "model_snapshots", "rejects", "pipeline_runs", "v_value", "raw_openrouter"} <= tables
    assert con.execute("SELECT COUNT(*) FROM models").fetchone()[0] == summary.n_valid
    assert con.execute("SELECT COUNT(*) FROM rejects").fetchone()[0] == 3
    # CHECK constraints are a second line of defence
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE models SET intelligence_index = 500")
    con.close()

    run_etl(offline=True, db_path=db)  # idempotent: re-running does not duplicate rows
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM models").fetchone()[0] == summary.n_valid
    assert con.execute("SELECT COUNT(*) FROM model_snapshots").fetchone()[0] == summary.n_valid
    assert con.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 2
    con.close()

    report = run_eda(db, tmp_path / "out")
    text = report.read_text()
    assert "SYNTHETIC SAMPLE DATA" in text
    assert len(list((tmp_path / "out" / "figures").glob("*.png"))) == 7


def test_network_failures_become_readable_extract_errors():
    import requests

    from aimodels.extract import ExtractError, check_connections, explain_request_error, fetch_artificial_analysis, fetch_openrouter

    class Boom(requests.Session):
        def get(self, *a, **k):
            raise requests.exceptions.ProxyError("Unable to connect to proxy")

    with pytest.raises(ExtractError, match="proxy"):
        fetch_artificial_analysis(Boom(), "key", ["https://example.invalid/x"])
    with pytest.raises(ExtractError, match="OpenRouter"):
        fetch_openrouter(Boom())
    assert "certificate" in explain_request_error(requests.exceptions.SSLError("bad certificate"), "X")
    assert callable(check_connections)
