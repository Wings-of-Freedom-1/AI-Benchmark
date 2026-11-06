import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from aimodels.dashboard import App, Paths, make_server


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    root = tmp_path_factory.mktemp("dash")
    paths = Paths(live_db=root / "live.db", sample_db=root / "sample.db", reports=root / "reports",
                  manual_hf=root / "manual.csv", raw=root / "raw")
    app = App(paths)
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield app, base
    server.shutdown()
    server.server_close()


def call(base, path, body=None, token=None, host=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    if token:
        req.add_header("X-Dashboard-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers["Content-Type"].startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_job(base, job_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        _, j = call(base, f"/api/jobs/{job_id}")
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def test_empty_state_and_security(srv):
    app, base = srv
    code, st = call(base, "/api/status")
    assert code == 200 and not st["datasets"]["sample"]["exists"] and st["stages"][-1] == "eda"
    assert call(base, "/api/models?db=sample")[0] == 404
    assert call(base, "/api/run", {"kind": "all"})[0] == 403  # no token
    assert call(base, "/api/run", {"kind": "all"}, token="wrong")[0] == 403
    assert call(base, "/api/status", host="evil.example.com")[0] == 403  # DNS-rebinding guard
    assert call(base, "/static/../dashboard.py")[0] == 404
    assert call(base, "/reports/sample/../../x.png")[0] == 404


def test_run_sample_pipeline_then_read_everything(srv):
    app, base = srv
    code, job = call(base, "/api/run", {"kind": "all", "dataset": "sample", "fuzzy_threshold": 0.9}, token=app.token)
    assert code == 202
    assert call(base, "/api/run", {"kind": "all"}, token=app.token)[0] in (409, 202)  # one job at a time
    done = wait_job(base, job["id"])
    assert done["status"] == "done", done["error"]
    assert done["done_stages"][:4] == ["extract", "transform", "validate", "load"]
    assert any("rejected" in line["msg"] for line in done["lines"])
    assert done["result"]["n_valid"] > 100 and done["result"]["n_rejected"] == 3

    _, st = call(base, "/api/status")
    assert st["datasets"]["sample"]["models"] == done["result"]["n_valid"]
    assert st["datasets"]["sample"]["last_run"]["stats"]["fuzzy_threshold"] == 0.9
    assert st["datasets"]["sample"]["has_report"] and not st["datasets"]["live"]["exists"]

    _, m = call(base, "/api/models?db=sample")
    assert len(m["models"]) == done["result"]["n_valid"]
    assert {"name", "intelligence_index", "price_blended_usd_per_m", "params_b", "hf_id"} <= set(m["models"][0])

    _, q = call(base, "/api/quality?db=sample")
    assert len(q["rejects"]) == 3 and q["runs"][0]["source"] == "sample" and q["unmatched"]

    _, rep = call(base, "/api/report?db=sample")
    assert rep["exists"] and len(rep["figures"]) == 7 and "SYNTHETIC" in rep["markdown"]
    code, png = call(base, rep["figures"][0]["url"])
    assert code == 200 and png[:4] == b"\x89PNG"


def test_sql_console_is_read_only(srv):
    app, base = srv
    ok = call(base, "/api/sql", {"dataset": "sample", "query": "SELECT COUNT(*) AS n FROM models"}, token=app.token)
    assert ok[0] == 200 and ok[1]["columns"] == ["n"] and ok[1]["rows"][0][0] > 100
    view = call(base, "/api/sql", {"dataset": "sample", "query": "SELECT * FROM v_value LIMIT 3"}, token=app.token)
    assert view[0] == 200 and len(view[1]["rows"]) == 3
    for bad in ["DROP TABLE models", "DELETE FROM models", "PRAGMA table_info(models)",
                "ATTACH DATABASE 'x.db' AS x", "UPDATE models SET name='x'"]:
        code, body = call(base, "/api/sql", {"dataset": "sample", "query": bad}, token=app.token)
        assert code == 400, bad
    assert call(base, "/api/sql", {"dataset": "sample", "query": "SELECT 1"})[0] == 403  # needs token
    _, st = call(base, "/api/status")
    assert st["datasets"]["sample"]["models"] > 100  # nothing was damaged
    big = call(base, "/api/sql", {"dataset": "sample", "query": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 5000) SELECT x FROM c"}, token=app.token)
    assert big[0] == 200 and big[1]["truncated"] and len(big[1]["rows"]) == 1000


def test_manual_hf_editor_validates_and_persists(srv):
    app, base = srv
    ok = call(base, "/api/manual_hf", {"op": "add", "match_key": "llama-3-1-8b", "hf_repo_id": "meta-llama/Llama-3.1-8B-Instruct"}, token=app.token)
    assert ok[0] == 200 and ok[1]["manual_hf"] == [{"match_key": "llama-3-1-8b", "hf_repo_id": "meta-llama/Llama-3.1-8B-Instruct"}]
    for bad in [{"match_key": "../x", "hf_repo_id": "a/b"}, {"match_key": "ok", "hf_repo_id": "no-slash"},
                {"match_key": "ok", "hf_repo_id": "a/b/c"}]:
        assert call(base, "/api/manual_hf", {"op": "add", **bad}, token=app.token)[0] == 400
    rm = call(base, "/api/manual_hf", {"op": "remove", "match_key": "llama-3-1-8b"}, token=app.token)
    assert rm[1]["manual_hf"] == []


def test_keys_live_in_memory_and_live_run_fails_clearly_without_key(srv, monkeypatch):
    app, base = srv
    monkeypatch.delenv("AA_API_KEY", raising=False)
    monkeypatch.delenv("ARTIFICIAL_ANALYSIS_API_KEY", raising=False)
    _, k = call(base, "/api/keys", {"aa_api_key": "  secret  "}, token=app.token)
    assert k["aa"] is True and app.keys["AA_API_KEY"] == "secret"
    call(base, "/api/keys", {"aa_api_key": ""}, token=app.token)
    assert "AA_API_KEY" not in app.keys
    code, job = call(base, "/api/run", {"kind": "etl", "dataset": "live"}, token=app.token)
    done = wait_job(base, job["id"])
    assert done["status"] == "error" and "AA_API_KEY" in done["error"]
    assert not app.paths.live_db.exists()  # a failed run must not leave a database behind


def test_page_and_assets_are_served_with_a_token_and_csp(srv):
    app, base = srv
    code, html = call(base, "/")
    assert code == 200 and app.token.encode() in html and b"__TOKEN__" not in html
    for name in ("app.js", "charts.js", "app.css"):
        assert call(base, f"/static/{name}")[0] == 200
    with urllib.request.urlopen(base + "/") as r:
        assert "script-src 'self'" in r.headers["Content-Security-Policy"] and "'unsafe-eval'" not in r.headers["Content-Security-Policy"]


def test_diagnose_reports_each_source_and_needs_token(srv, monkeypatch):
    app, base = srv
    from aimodels import extract

    seen = {}

    def fake(aa, hf, endpoints=None):
        seen["aa"], seen["hf"] = aa, hf
        return [{"source": "Artificial Analysis", "ok": False, "message": "rejected (HTTP 401)"},
                {"source": "OpenRouter", "ok": True, "message": "300 models returned."}]

    monkeypatch.setattr(extract, "check_connections", fake)
    assert call(base, "/api/diagnose", {})[0] == 403
    call(base, "/api/keys", {"aa_api_key": "abc"}, token=app.token)
    code, body = call(base, "/api/diagnose", {}, token=app.token)
    assert code == 200 and [r["ok"] for r in body["results"]] == [False, True] and seen["aa"] == "abc"
    call(base, "/api/keys", {"aa_api_key": ""}, token=app.token)
