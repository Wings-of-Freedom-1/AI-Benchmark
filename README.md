# Which AI models give the best performance for their size, cost and speed?

An ETL + EDA pipeline that joins three public data sources into one SQLite database
and explores it.

| Dimension | Source | What we take |
|---|---|---|
| Performance, price, speed | **Artificial Analysis** API | Intelligence/coding/math indices, 7 benchmarks, blended price, tokens/s, time to first token, release date |
| Price, context length | **OpenRouter** `/api/v1/models` | Per-token prices, context window, Hugging Face repo id |
| Size | **Hugging Face Hub** `/api/models/{repo}` | `safetensors.total` parameter count (open-weight models) |

```
 Artificial Analysis ─┐                                     ┌─► models  (latest, one row/model)
 OpenRouter ──────────┼─► extract ─► clean/merge ─► validate ┼─► model_snapshots (history per run)
 Hugging Face ────────┘  requests    pandas        Pydantic  ├─► rejects (bad rows + reason)
                         + cache     + name         + CHECK  └─► raw_*, pipeline_runs, v_value
                                     normalization                     │
                                                                       ▼
                                                      EDA: 7 figures + eda_summary.md
```

## Dashboard (no command line needed)

**Windows:** double-click `Start-Dashboard.bat`. **macOS:** double-click `Start-Dashboard.command`
(first time: right-click, Open). **Linux:** `./start-dashboard.sh`. The first launch sets up a private
environment (about a minute, needs internet), then your browser opens the console. Afterwards it starts in seconds.
Already have Python set up? `python -m aimodels` does the same.

The page is a control surface in three panels:

| Panel | Power button | Controls |
|---|---|---|
| **Pipeline** | Run: extract, transform, validate, load, then build the report | **Match** knob (name-match strictness), **Tolerance** knob (abort if too many rows fail validation), Demo/Live switch, live stage indicator. **ADV:** reuse last download, skip Hugging Face, API keys (kept in memory only), run ETL only, rebuild report only |
| **Ranker** | Off = rank by quality alone | Four faders (Quality, Cost, Speed, Size), Weighted / Pareto / Per-$ ranking, a live mini-graph. **ADV:** pick any benchmark as "quality", how to treat missing values, how many models to highlight |
| **Filters** | Off = show every model | Min/Max size (linkable), Min quality, Max price, Released since. **ADV:** open vs closed, creator chips, complete-data-only, hide reasoning variants, reset |

Below the panels: the **best picks** for your settings, then tabs for **Explore** (any metric vs any metric with
frontier, tooltips, click to pin), **Leaderboard** (sortable, CSV export), **Compare** (radar + side-by-side),
**Correlations**, **Trends**, **Data quality** (missingness, how names were matched, rejected rows, a form to map open models to
Hugging Face repos by hand, run history), a read-only **SQL** console, the **Report** (all generated charts and the written summary) and the run **Log**.
Drag knobs (Shift = fine, wheel and arrow keys work, double-click resets), save your favourite positions as **scenes**,
and switch light/dark. The console only listens on your own computer.

## Quick start

```bash
pip install -e ".[dev]"            # or: pip install -r requirements.txt  (then prefix commands with PYTHONPATH=src)
make test                          # all tests run offline

# 1) Try it with no keys. Uses SYNTHETIC data and a separate database (data/sample.db)
python -m aimodels all --offline   # -> reports/sample/eda_summary.md + figures/

# 2) Real run
export AA_API_KEY=...              # free key: artificialanalysis.ai -> Insights Platform
export HF_TOKEN=...                # optional, raises the Hugging Face rate limit
python -m aimodels all             # -> data/models.db, reports/eda_summary.md + figures/
python -m aimodels etl --use-cache # re-run transform/load from the last download (no API calls)
python -m aimodels etl --skip-hf   # names only, no Hugging Face lookups
```

Artificial Analysis requires **attribution** wherever its data is shown; every chart footer
and the report header carry it. Its free tier is rate limited per day, so each download is
cached in `data/raw/` and `--use-cache` re-runs without spending quota.

## Pipeline details

**Extract** (`extract.py`) - `requests` with retries/backoff, timeouts and explicit handling of
401/403/404/429. Raw JSON is cached per day. Hugging Face lookups run in a thread pool, are
cached per repo for 7 days, and transient errors are retried on the next run rather than cached.

**Clean and merge** (`clean.py`, `normalize.py`) - Artificial Analysis is the spine (it has the
benchmarks); the others are left-joined on. The hard part is that the sources name the same
model differently:

```
Artificial Analysis   Llama 3.1 Instruct 8B
OpenRouter name       Meta: Llama 3.1 8B Instruct
OpenRouter id         meta-llama/llama-3.1-8b-instruct:free      ->  llama-3-1-8b
```

Normalization lower-cases, strips `Provider:` prefixes, `org/` and `:free`, parenthetical
variants (`(Reasoning)` is kept in its own column), ISO and `MMDD` date stamps, noise words
(`instruct`, `chat`, `fp8`...) and turns `3.5` into `3-5`. Matching then runs three passes and
**records which one succeeded** (`match_method`, `match_score`):

1. `exact` normalized key  
2. `token_set` - same words in any order (`claude-3.5-sonnet` = `claude-sonnet-3.5`)  
3. `fuzzy` - difflib ≥ 0.93, *and* identical numeric tokens (so `8b` never matches `70b`), *and* a unique winner

A wrong join is worse than a missing one, so ambiguity means "unmatched".

Other resolution rules: prices come from Artificial Analysis, else OpenRouter (a row never mixes
sources; `price_source` says which; OpenRouter's price is kept in `or_price_blended_usd_per_m` as a
cross-check). Parameter count comes from Hugging Face, else is parsed from the name
(`235B A22B` → total 235, active 22; `8x7B` is skipped because the product is wrong), and
`params_source` records which. Benchmarks that arrive as 0-100 instead of 0-1 are rescaled with a warning.
Unlisted open models can be mapped by hand in `data/manual_hf_ids.csv`.

**Validate** (`schemas.py`) - every row must satisfy the `ModelRecord` Pydantic model: score
ranges, non-negative prices, plausible release dates, `active_params ≤ params`, no unknown fields.
Failures are not dropped silently: they go to the `rejects` table with the reason. If more than
25 % of rows fail the run aborts, since that almost always means an API changed shape. The same
bounds are repeated as SQLite `CHECK` constraints.

**Load** (`load.py`) - one transaction per run. Re-running is idempotent.

| Table | Contents |
|---|---|
| `models` | latest clean snapshot, one row per model |
| `model_snapshots` | append-only (run date, model, score, price, speed) so repeated runs build a true time series |
| `raw_artificial_analysis`, `raw_openrouter`, `raw_huggingface` | flattened source tables for lineage/debugging |
| `rejects`, `pipeline_runs` | failed rows with reasons; counts and `source` (`live` or `sample`) per run |
| `v_value` (view) | index points per dollar |

## EDA (`eda.py`)

`python -m aimodels eda` writes `eda_summary.md` (every number computed from the data) and:

1. `01_score_vs_size.png` - log-linear fit, smallest-for-score frontier  
2. `02_score_vs_price.png` - cheapest-for-score frontier  
3. `03_speed_vs_price.png`  
4. `04_benchmark_correlations.png` - Spearman  
5. `05_trends_over_time.png` - running best (all vs open weights) and running cheapest model above the median score  
6. `06_missing_data.png` - per-field share plus a model × field matrix sorted by release date  
7. `07_best_value.png` - intelligence per dollar among at-or-above-median models

"Best for the price/size/speed" is answered with **Pareto frontiers** (nothing else is both
cheaper and better) rather than a single ranking, because the right trade-off depends on the use.

## Caveats you should know about

- **The live API calls have not been exercised.** The extractors were written against the
  documented response shapes, but this project was built in a sandbox without access to those
  APIs, so everything was tested with a synthetic stand-in (`sample_data.py`, clearly labelled).
  Expect a first live run to need small fixes. Two places most likely to need attention:
  the Artificial Analysis path (two documented variants are tried in order; override with
  `AA_URL`) and the field names in `AA_FIELD_PATHS` (`clean.py`). The pipeline warns loudly if
  a key column comes back entirely empty.
- The synthetic data exists to prove the plumbing. Its findings say nothing about real models.
- "Open weights" is inferred from a Hugging Face repo id; open models OpenRouter does not list count
  as closed unless added to the manual CSV.
- Parameter counts are *total*, so mixture-of-experts models look big. Quantized checkpoints can be mis-counted by `safetensors.total`.
- Prices are list prices; reasoning models spend many more output tokens per task than the price implies.
- Release dates come from Artificial Analysis; models missing one are excluded from trend charts.

## Layout

```
src/aimodels/  config.py extract.py normalize.py clean.py schemas.py load.py pipeline.py eda.py sample_data.py __main__.py
tests/         name matching, merge rules, validation, SQLite constraints, end-to-end ETL → EDA
data/          manual_hf_ids.csv, raw/ (downloads), models.db (live), sample.db (synthetic)
reports/       eda_summary.md + figures/ (live), sample/ (synthetic demo output)
notebooks/     eda.ipynb - the same analysis, step by step
src/aimodels/web/  the dashboard page (index.html, app.js, charts.js, app.css); dashboard.py is its local server
```

## Troubleshooting Live mode

1. Open **ADV** on the Pipeline panel, paste your Artificial Analysis key and press **Test connections**. It checks the key and all three websites and says which one fails and why (wrong key, plan without access, daily limit, proxy/VPN/firewall, certificate).
2. The red "DEMO DATA" badge only appears while the **Demo** dataset is selected. In **Live** mode with nothing fetched yet, the badge says so instead.
3. Pressing the power button now stores a pasted key automatically; "Use keys" is optional.
4. The full error text is always in the **Log** tab.
