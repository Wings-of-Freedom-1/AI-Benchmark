# AI model comparison - EDA summary

*Data: Artificial Analysis (artificialanalysis.ai), OpenRouter, Hugging Face Hub.* Pipeline run 3 (2026-11-06T15:55:35+00:00, source: live).

## Coverage

- **199** models after validation; **1** rows rejected by the Pydantic schema.
- Complete on all four dimensions (score, price, speed, parameter count): **12** (6%).
- Open weights (Hugging Face repo found): **52**; parameter counts known for **89** (45%).

How Artificial Analysis models were matched to OpenRouter after name normalization:

| OpenRouter match | models |
|---|---|
| exact | 111 |
| none | 80 |
| fuzzy | 9 |

## Score vs model size

- Spearman ρ = **0.75** (strong, n = 13). A log-linear fit gives **+10.4** index points per 10× parameters (R² = 0.56).
- Models on the *smallest-for-their-score* frontier:

| model | params (B) | index |
|---|---|---|
| Granite 4.2 3B | 3.00 | 4.70 |
| Qwen3.8 27B (Non-reasoning) | 27.78 | 10.00 |
| MiMo-V2.6-Flash | 310.8 | 26.70 |
| GLM-5.3-Flash | 321.3 | 27.80 |
| Step 5 Preview | 604.3 | 28.50 |
| MiMo-V2.6-Pro | 1,024.2 | 29.90 |

## Score vs price

- Spearman ρ (log price vs score) = **0.67** (strong, n = 33).
- Models on the *cheapest-for-their-score* frontier (nothing is both cheaper and better):

| model | index | $/1M | tok/s |
|---|---|---|---|
| Nemotron 3.5 Lightning | 5.50 | 0.10 | 286.9 |
| MiMo-V2.6-Flash | 26.70 | 0.17 | 34.46 |
| Claude Haiku 5.5 (Xhigh) | 34.60 | 0.20 | 178.2 |
| Grok 4.7 (High) | 35.20 | 3.00 | 59.49 |
| Gemini 4 Carbon (Medium) | 58.30 | 4.00 | 93.07 |

- Best intelligence per dollar (models at or above the median score):

| model | index | $/1M | pts per $ |
|---|---|---|---|
| Claude Haiku 5.5 (Xhigh) | 34.60 | 0.20 | 173 |
| Claude Haiku 5.5 (High) | 29.90 | 0.20 | 149 |
| MiMo-V2.6-Pro | 29.90 | 0.54 | 55 |
| GPT-6.1 Terra (Non-reasoning) | 30.50 | 2.00 | 15 |
| Gemini 4 Carbon (Medium) | 58.30 | 4.00 | 15 |
| GPT-6.1 Sol (Xhigh) | 52.50 | 4.00 | 13 |
| GPT-6.1 Sol (High) | 50.30 | 4.00 | 13 |
| Grok 4.7 (High) | 35.20 | 3.00 | 12 |
| Claude Sonnet 5.5 (High, Default Fallback) | 41.50 | 4.00 | 10 |
| Grok 4.7 (Low) | 31.10 | 3.00 | 10 |
| Claude Sonnet 5.5 (Medium, Default Fallback) | 30.80 | 4.00 | 8 |
| Muse Wonder (High) | 40.50 | 8.00 | 5 |

## Speed vs price

- Spearman ρ (log price vs tokens/s) = **-0.34** (moderate, n = 33).
- Models on the *cheapest-for-their-speed* frontier:

| model | tok/s | $/1M | index |
|---|---|---|---|
| Nemotron 3.5 Lightning | 286.9 | 0.10 | 5.50 |
| Gemini 3.5 Flash-Lite | 381.6 | 0.85 | 11.70 |

## Trends over time

By release year (models with a release date and score):

| year | models | best | median | best open | median $/1M |
|---|---|---|---|---|---|
| 2026 | 36 | 58.30 | 28.15 | 29.90 | 1.76 |

- Cheapest model with index ≥ 28 so far: **$20.00** at first appearance (2026-09) vs **$0.20** now (100.0× cheaper).

- Best overall index: **58.3**; best open-weights index: **29.9** (gap +28.4).

- `model_snapshots` stores each pipeline run's score/price/speed, so true time-series of *the same model* accumulate as you re-run the ETL on a schedule; the charts above use release dates.

## Missing-data analysis

| column | % missing |
|---|---|
| coding_index | 100.0 |
| math_index | 100.0 |
| mmlu_pro | 100.0 |
| gpqa | 100.0 |
| livecodebench | 100.0 |
| hle | 100.0 |
| scicode | 100.0 |
| math_500 | 100.0 |
| aime | 100.0 |
| intelligence_index | 81.90 |
| params_b | 55.30 |
| tokens_per_sec | 44.70 |
| ttft_s | 44.70 |
| price_blended_usd_per_m | 40.20 |
| context_length | 40.20 |
| release_date | 0.00 |

Missingness by group (% missing). Gaps are not random: they depend on whether a model is open (parameter counts) and on whether OpenRouter lists it (context length):

| group | price_blended_usd_per_m | tokens_per_sec | context_length | params_b | hle |
|---|---|---|---|---|---|
| Closed / not on Hugging Face | 54.40 | 53.70 | 54.40 | 74.10 | 100.0 |
| Open weights (Hugging Face repo found) | 0.00 | 19.20 | 0.00 | 1.90 | 100.0 |

- Price cross-check on 118 models priced by both sources: median OpenRouter/AA ratio **1.00**, 100% within ±25%.

- Parameter-count provenance: {'missing': 111, 'hf': 51, 'name': 38}.

## Figures

- `figures\01_score_vs_size.png`
- `figures\02_score_vs_price.png`
- `figures\03_speed_vs_price.png`
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
