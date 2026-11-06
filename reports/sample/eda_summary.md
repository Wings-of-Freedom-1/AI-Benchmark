# AI model comparison - EDA summary

> **SYNTHETIC SAMPLE DATA.** Generated offline to test the pipeline. Model names and numbers are invented; none of the findings below describe real models. Run `python -m aimodels all` with an Artificial Analysis key for real results.

*Data: Artificial Analysis (artificialanalysis.ai), OpenRouter, Hugging Face Hub.* Pipeline run 1 (2026-11-06T15:53:40+00:00, source: sample).

## Coverage

- **240** models after validation; **3** rows rejected by the Pydantic schema.
- Complete on all four dimensions (score, price, speed, parameter count): **118** (49%).
- Open weights (Hugging Face repo found): **92**; parameter counts known for **133** (55%).

How Artificial Analysis models were matched to OpenRouter after name normalization:

| OpenRouter match | models |
|---|---|
| exact | 147 |
| none | 69 |
| token_set | 25 |
| fuzzy | 2 |

## Score vs model size

- Spearman ρ = **0.65** (strong, n = 133). A log-linear fit gives **+10.6** index points per 10× parameters (R² = 0.45).
- Models on the *smallest-for-their-score* frontier:

| model | params (B) | index |
|---|---|---|
| Halcyon 1.5 3B Instruct | 2.92 | 36.30 |
| Tessera 2.5 3B Instruct | 2.96 | 43.90 |
| Nimbus 3 3B Instruct | 3.00 | 48.00 |
| Kestrel 3 3B Instruct | 3.03 | 51.70 |
| Tessera 3 8B Instruct | 7.94 | 53.80 |
| Marlowe 3 8B Instruct | 8.21 | 55.10 |
| Halcyon 3 32B Instruct | 31.19 | 55.80 |
| Orbis 3 32B Instruct (Reasoning) | 32.00 | 66.90 |
| Nimbus 3 70B Instruct (Reasoning) | 70.00 | 71.50 |
| Tessera 3 405B Instruct (Reasoning) | 398.8 | 76.00 |

## Score vs price

- Spearman ρ (log price vs score) = **0.71** (strong, n = 240).
- Models on the *cheapest-for-their-score* frontier (nothing is both cheaper and better):

| model | index | $/1M | tok/s |
|---|---|---|---|
| Zephra 1.5 3B Instruct | 33.10 | 0.02 | 347.2 |
| Kestrel 3 3B Instruct | 51.70 | 0.02 | 211.9 |
| Marlowe 3 8B Instruct | 55.10 | 0.04 | 159.2 |
| Marlowe 2 32B Instruct (Reasoning) | 56.40 | 0.10 | 70.60 |
| Orbis 3 32B Instruct | 59.90 | 0.17 | – |
| Quillon 2.5 70B Instruct (Reasoning) | 61.60 | 0.27 | – |
| Nimbus 3 70B Instruct | 64.50 | 0.29 | 99.90 |
| Nimbus 3 70B Instruct (Reasoning) | 71.50 | 0.29 | 43.20 |
| Kestrel 3 405B Instruct | 71.70 | 0.96 | 40.60 |
| Marlowe 2.5 Pro (Reasoning) | 72.30 | 1.02 | 36.80 |
| Tessera 3 405B Instruct (Reasoning) | 76.00 | 1.15 | 14.60 |
| Nimbus 3 Ultra | 78.40 | 5.24 | 33.40 |
| Marlowe 3 Pro (Reasoning) | 78.90 | 5.54 | – |
| Orbis 2 Ultra (Reasoning) | 81.00 | 7.89 | 19.60 |
| Nimbus 3 Ultra (Reasoning) | 85.40 | 9.01 | 18.40 |

- Best intelligence per dollar (models at or above the median score):

| model | index | $/1M | pts per $ |
|---|---|---|---|
| Marlowe 3 8B Instruct | 55.10 | 0.04 | 1278 |
| Marlowe 2 32B Instruct (Reasoning) | 56.40 | 0.10 | 548 |
| Halcyon 3 32B Instruct | 55.80 | 0.10 | 533 |
| Orbis 3 32B Instruct | 59.90 | 0.17 | 347 |
| Nimbus 3 70B Instruct (Reasoning) | 71.50 | 0.29 | 245 |
| Quillon 2.5 70B Instruct (Reasoning) | 61.60 | 0.27 | 232 |
| Nimbus 3 70B Instruct | 64.50 | 0.29 | 222 |
| Halcyon 2 70B Instruct | 59.60 | 0.27 | 222 |
| Halcyon 3 70B Instruct | 64.60 | 0.29 | 219 |
| Tessera 3 32B Instruct | 58.10 | 0.27 | 219 |
| Orbis 2.5 180B Instruct | 59.40 | 0.30 | 196 |
| Orbis 3 32B Instruct (Reasoning) | 66.90 | 0.40 | 169 |

## Speed vs price

- Spearman ρ (log price vs tokens/s) = **-0.84** (strong, n = 209).
- Models on the *cheapest-for-their-speed* frontier:

| model | tok/s | $/1M | index |
|---|---|---|---|
| Zephra 1.5 3B Instruct | 347.2 | 0.02 | 33.10 |
| Marlowe 1.5 3B Instruct | 404.3 | 0.03 | 32.90 |

## Benchmark correlations

- Mean pairwise Spearman ρ across 45 benchmark pairs: **0.87** (range 0.77 to 0.96).
- Least-correlated pairs (most distinct signal):

| benchmark A | benchmark B | ρ | shared n |
|---|---|---|---|
| GPQA | SciCode | 0.77 | 194 |
| HLE | SciCode | 0.78 | 138 |
| Math idx | SciCode | 0.80 | 194 |
| LiveCodeBench | SciCode | 0.80 | 194 |
| SciCode | AIME | 0.80 | 144 |

- Most-correlated pairs (largely redundant):

| benchmark A | benchmark B | ρ | shared n |
|---|---|---|---|
| Intelligence idx | MATH-500 | 0.96 | 240 |
| Intelligence idx | MMLU-Pro | 0.95 | 240 |
| Intelligence idx | Coding idx | 0.94 | 240 |

## Trends over time

By release year (models with a release date and score):

| year | models | best | median | best open | median $/1M |
|---|---|---|---|---|---|
| 2023 | 31 | 62.90 | 44.50 | 48.90 | 0.54 |
| 2024 | 75 | 71.90 | 50.30 | 56.40 | 0.62 |
| 2025 | 67 | 81.00 | 58.50 | 64.90 | 0.62 |
| 2026 | 67 | 85.40 | 65.50 | 76.00 | 0.64 |

- Cheapest model with index ≥ 55 so far: **$2.11** at first appearance (2023-08) vs **$0.04** now (48.9× cheaper).

- Best overall index: **85.4**; best open-weights index: **76.0** (gap +9.4).

- `model_snapshots` stores each pipeline run's score/price/speed, so true time-series of *the same model* accumulate as you re-run the ETL on a schedule; the charts above use release dates.

## Missing-data analysis

| column | % missing |
|---|---|
| params_b | 44.60 |
| hle | 27.50 |
| context_length | 27.50 |
| aime | 23.80 |
| scicode | 19.20 |
| tokens_per_sec | 12.90 |
| ttft_s | 11.70 |
| intelligence_index | 0.00 |
| livecodebench | 0.00 |
| gpqa | 0.00 |
| math_index | 0.00 |
| mmlu_pro | 0.00 |
| coding_index | 0.00 |
| math_500 | 0.00 |
| price_blended_usd_per_m | 0.00 |
| release_date | 0.00 |

Missingness by group (% missing). Gaps are not random: they depend on whether a model is open (parameter counts) and on whether OpenRouter lists it (context length):

| group | price_blended_usd_per_m | tokens_per_sec | context_length | params_b | hle |
|---|---|---|---|---|---|
| Closed / not on Hugging Face | 0.00 | 14.90 | 44.60 | 72.30 | 26.40 |
| Open weights (Hugging Face repo found) | 0.00 | 9.80 | 0.00 | 0.00 | 29.30 |

- Earliest model with an HLE score: 2024-06-04; latest model without one: 2024-05-31 - every model without one predates the first model that has one, consistent with the benchmark being added later (structural missingness).

- Price cross-check on 174 models priced by both sources: median OpenRouter/AA ratio **1.00**, 91% within ±25%.

- Parameter-count provenance: {'missing': 110, 'hf': 82, 'name': 51}.

## Figures

- `figures\01_score_vs_size.png`
- `figures\02_score_vs_price.png`
- `figures\03_speed_vs_price.png`
- `figures\04_benchmark_correlations.png`
- `figures\05_trends_over_time.png`
- `figures\06_missing_data.png`
- `figures\07_best_value.png`

## Caveats

- **"Open weights" is inferred** from a Hugging Face repo id on OpenRouter or in `data/manual_hf_ids.csv`; an open model OpenRouter does not list is counted as closed unless added manually.
- **Parameter counts are total, not active.** Mixture-of-experts models look large on the size axis; `active_params_b` is parsed from names like `235B A22B` where available.
- Hugging Face `safetensors.total` can mis-count quantized checkpoints; prefer full-precision repos in the manual map.
- Name matching is conservative by design: ambiguous names stay unmatched rather than risk a wrong join.
- Prices are list prices at fetch time; reasoning models use many more output tokens per task than the price suggests.
- Correlations and fits describe this sample of benchmarked models, not causal effects.
