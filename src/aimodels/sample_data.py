"""Synthetic stand-in for the three APIs, so the whole pipeline runs offline.

EVERYTHING HERE IS FAKE. Model names are invented ("Zephra", "Quillon", ...) and
the numbers come from a made-up generative story (bigger and newer -> better,
bigger -> pricier and slower). It exists to test the pipeline end to end, and its
payloads deliberately mimic real-world mess:

* different naming styles per source, dated ids, swapped word order, ':free' twins
* structurally missing values (e.g. no HLE score before mid-2024)
* a few corrupt rows that validation must reject
* open models absent from OpenRouter (parameter counts then come from the name)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

FAMILIES = [  # (family, id prefix, provider display name)
    ("Zephra", "zephra", "Zephra Labs"),
    ("Quillon", "quillon-ai", "Quillon AI"),
    ("Marlowe", "marlowe", "Marlowe Research"),
    ("Tessera", "tessera", "Tessera"),
    ("Orbis", "orbis", "Orbis Systems"),
    ("Kestrel", "kestrel-ai", "Kestrel AI"),
    ("Halcyon", "halcyon", "Halcyon"),
    ("Nimbus", "nimbus", "Nimbus Labs"),
]
GENERATIONS = ["1", "1.5", "2", "2.5", "3"]
OPEN_SIZES = [3, 8, 14, 32, 70, 180, 405]
CLOSED_TIERS = {"Mini": 30, "Pro": 200, "Ultra": 800}  # pseudo-size used only to generate scores
LAST_RELEASE = date(2026, 10, 20)


@dataclass
class SampleBundle:
    aa_payload: dict
    or_payload: dict
    hf_lookup: dict[str, dict | None]


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def build_sample(seed: int = 7) -> SampleBundle:
    rng = np.random.default_rng(seed)
    aa_rows: list[dict] = []
    or_rows: list[dict] = []
    hf_lookup: dict[str, dict | None] = {}
    start = date(2023, 6, 1)

    def clip(x, lo, hi):
        return float(min(max(x, lo), hi))

    def add_model(fam, pslug, pname, gen_i, ver, label, size_eff, is_open, size_b=None, tier=None):
        released = start + timedelta(days=int(gen_i * 245 + rng.integers(0, 70)))
        released = min(released, LAST_RELEASE)
        years = (released - start).days / 365.25
        idx = clip(22 + 9.5 * np.log10(size_eff) + 7.0 * years + (0 if is_open else 4) + rng.normal(0, 3), 2, 95)

        for reasoning in (False, True) if (size_eff >= 30 and rng.random() < 0.3) else (False,):
            q = clip(idx + (7 if reasoning else 0), 2, 96)
            price = 0.05 * (size_eff / 8) ** 0.8 * rng.lognormal(0, 0.3) * (1 if is_open else rng.uniform(3, 6))
            tps = 400 / size_eff**0.35 * rng.lognormal(0, 0.25) * (0.5 if reasoning else 1)
            name = f"{fam} {ver} {label}" + (" (Reasoning)" if reasoning else "")
            ev = {
                "artificial_analysis_intelligence_index": round(q, 1),
                "artificial_analysis_coding_index": round(clip(q * 0.9 + rng.normal(0, 4), 0, 100), 1),
                "artificial_analysis_math_index": round(clip(q * 1.05 + rng.normal(0, 5), 0, 100), 1),
                "mmlu_pro": round(clip(0.2 + 0.0075 * q + rng.normal(0, 0.03), 0, 0.95), 3),
                "gpqa": round(clip(0.1 + 0.0075 * q + rng.normal(0, 0.04), 0, 0.95), 3),
                "hle": round(clip((q - 20) / 400 + rng.normal(0, 0.01), 0, 0.3), 3),
                "livecodebench": round(clip(0.05 + 0.009 * q + rng.normal(0, 0.04), 0, 0.95), 3),
                "scicode": round(clip(0.05 + 0.0045 * q + rng.normal(0, 0.03), 0, 0.6), 3),
                "math_500": round(clip(0.3 + 0.0085 * q + rng.normal(0, 0.03), 0, 0.99), 3),
                "aime": round(clip(-0.1 + 0.011 * q + rng.normal(0, 0.05), 0, 0.95), 3),
            }
            # structural + random missingness
            if released < date(2024, 6, 1):
                ev["hle"] = None  # benchmark did not exist yet
            if rng.random() < 0.2:
                ev["scicode"] = None
            if not reasoning and rng.random() < 0.3:
                ev["aime"] = None
            blended = round(price, 4)
            aa_rows.append({
                "id": f"synth-{len(aa_rows):04d}",
                "name": name,
                "slug": _slug(name),
                "release_date": released.isoformat(),
                "model_creator": {"id": pslug, "name": pname, "slug": pslug},
                "evaluations": ev,
                "pricing": {
                    "price_1m_blended_3_to_1": blended,
                    "price_1m_input_tokens": round(blended / 1.5, 4),
                    "price_1m_output_tokens": round(blended / 1.5 * 3, 4),
                },
                "median_output_tokens_per_second": None if rng.random() < 0.12 else round(tps, 1),
                "median_time_to_first_token_seconds": None if rng.random() < 0.1 else round(0.25 + 0.12 * np.log10(size_eff) + rng.random() * 0.3, 2),
            })
            if reasoning:
                continue  # variants share one OpenRouter listing with the base model

            # ~75% of models also appear on OpenRouter, under a differently styled name
            if rng.random() < 0.75:
                inp = blended / 1.5
                ctx = int(rng.choice([8192, 32768, 131072, 200000]))
                if is_open:
                    oid = f"{pslug}/{fam.lower()}-{ver}-{size_b}b-instruct"
                    oname = f"{pname}: {fam} {ver} {size_b}B Instruct"
                    hf = f"{pslug}/{fam}-{ver}-{size_b}B-Instruct"
                    n_params = int(size_b * 1e9 * rng.uniform(0.97, 1.03))
                    r = rng.random()
                    hf_lookup[hf] = (
                        None if r < 0.04
                        else {"id": hf} if r < 0.12  # no safetensors metadata
                        else {"id": hf, "safetensors": {"parameters": {"BF16": n_params}, "total": n_params}}
                    )
                else:
                    tag = label.lower().replace(" ", "-")
                    oid = (f"{pslug}/{fam.lower()}-{ver}-{tag}" if rng.random() < 0.7
                           else f"{pslug}/{fam.lower()}-{tag}-{ver}")  # swapped order -> token_set match
                    if rng.random() < 0.4:
                        oid += f"-{(released.year)}-{released.month:02d}-{released.day:02d}"
                    oname = f"{pname}: {fam} {label} {ver}"
                    hf = ""
                or_rows.append({
                    "id": oid, "name": oname, "hugging_face_id": hf,
                    "created": int(np.datetime64(released.isoformat(), "s").astype(int)),
                    "context_length": ctx,
                    "pricing": {"prompt": f"{inp / 1e6:.10f}", "completion": f"{inp * 3 / 1e6:.10f}"},
                })
                if is_open and rng.random() < 0.15:  # a free twin of the same model
                    twin = dict(or_rows[-1])
                    twin.update(id=oid + ":free", name=oname + " (free)", pricing={"prompt": "0", "completion": "0"})
                    or_rows.append(twin)

    for fi, (fam, pslug, pname) in enumerate(FAMILIES):
        for gi, ver in enumerate(GENERATIONS):
            sizes = sorted(rng.choice(OPEN_SIZES, size=3, replace=False))
            for s in sizes:
                add_model(fam, pslug, pname, gi + fi * 0.15, ver, f"{s}B Instruct", s, True, size_b=int(s))
            for tier in rng.choice(list(CLOSED_TIERS), size=2, replace=False):
                add_model(fam, pslug, pname, gi + fi * 0.15, ver, tier, CLOSED_TIERS[tier], False, tier=tier)

    # Corrupt rows that validation must catch
    template = aa_rows[10]
    for i, (path, bad) in enumerate([
        (("evaluations", "artificial_analysis_intelligence_index"), 140.0),
        (("pricing", "price_1m_blended_3_to_1"), -5.0),
        (("median_output_tokens_per_second",), 99999.0),
    ]):
        row = {k: (dict(v) if isinstance(v, dict) else v) for k, v in template.items()}
        row["id"], row["name"], row["slug"] = f"synth-bad-{i}", f"Zephra Broken {i}", f"zephra-broken-{i}"
        target = row
        for p in path[:-1]:
            target = target[p]
        target[path[-1]] = bad
        aa_rows.append(row)

    # OpenRouter-only listings and a router with variable ('-1') pricing
    or_rows.append({"id": "openrouter/auto", "name": "Auto Router", "hugging_face_id": "", "created": 1_700_000_000,
                    "context_length": 2_000_000, "pricing": {"prompt": "-1", "completion": "-1"}})
    for k in range(6):
        or_rows.append({"id": f"other/image-model-{k}", "name": f"Other: Image Model {k}", "hugging_face_id": "",
                        "created": 1_720_000_000 + k, "context_length": 4096,
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"}})

    aa_payload = {"status": 200, "synthetic": True, "prompt_options": {"prompt_length": "medium"}, "data": aa_rows}
    return SampleBundle(aa_payload, {"data": or_rows}, hf_lookup)
