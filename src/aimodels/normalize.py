"""Model-name normalization and cross-source matching.

The three sources name the same model differently:

    Artificial Analysis   "Llama 3.1 Instruct 8B"
    OpenRouter name       "Meta: Llama 3.1 8B Instruct"
    OpenRouter id         "meta-llama/llama-3.1-8b-instruct"

``normalize_name`` reduces all of these to the same key (``llama-3-1-8b``).
``NameIndex`` then matches in three passes, from most to least trustworthy:

1. ``exact``     - identical normalized key
2. ``token_set`` - identical bag of tokens (handles ``claude-3.5-sonnet`` vs
                   ``claude-sonnet-3.5``)
3. ``fuzzy``     - difflib ratio >= threshold, *and* identical numeric tokens
                   (so ``-8b`` never matches ``-70b``), *and* a unique winner

A wrong match is worse than a missing one, so every pass refuses ambiguity.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from .config import FUZZY_THRESHOLD

# Tokens that carry no identity information across sources.
NOISE_TOKENS = frozenset(
    {"instruct", "chat", "it", "free", "latest", "hf", "fp8", "fp16", "bf16",
     "int4", "int8", "awq", "gptq", "model"}
)

_PAREN_RE = re.compile(r"[\(\[]([^)\]]*)[\)\]]")
_PREFIX_RE = re.compile(r"^([^:]{1,40}):\s+(.+)$")  # "Provider: Model name"
_ISO_DATE_RE = re.compile(
    r"(?<!\d)20\d{2}[-_]?(?:0[1-9]|1[0-2])[-_]?(?:0[1-9]|[12]\d|3[01])(?!\d)"
)
_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:x\d+)?b?")


def _is_mmdd(token: str) -> bool:
    """4-digit MMDD release stamps such as the '0613' in gpt-4-0613."""
    return (
        len(token) == 4
        and token.isdigit()
        and 1 <= int(token[:2]) <= 12
        and 1 <= int(token[2:]) <= 31
    )


def _is_build_suffix(token: str) -> bool:
    """3+ digit zero-padded build numbers such as the '001' in gemini-2.0-flash-001."""
    return len(token) >= 3 and token.isdigit() and token.startswith("0")


@dataclass(frozen=True)
class NormalizedName:
    raw: str
    key: str
    tokens: tuple[str, ...]
    variant: str  # text found in parentheses, e.g. "Reasoning" or "Aug '24"

    @property
    def sorted_key(self) -> str:
        return "-".join(sorted(self.tokens))

    @property
    def numeric_tokens(self) -> frozenset[str]:
        return frozenset(t for t in self.tokens if _NUMERIC_TOKEN_RE.fullmatch(t))


def normalize_name(raw: str, *, is_id: bool = False) -> NormalizedName:
    """Normalize a display name (``is_id=False``) or an OpenRouter-style id."""
    text = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode().lower()
    text = text.strip()

    variants = [m.strip() for m in _PAREN_RE.findall(text)]
    text = _PAREN_RE.sub(" ", text)

    if is_id:
        text = text.split("/")[-1].split(":")[0]  # drop "org/" and ":free"-style suffixes
    else:
        m = _PREFIX_RE.match(text)
        if m:
            text = m.group(2)

    text = _ISO_DATE_RE.sub(" ", text)
    text = re.sub(r"(?<=\d)\.(?=\d)", "-", text)  # 3.5 -> 3-5

    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    tokens = [
        t for t in tokens
        if t not in NOISE_TOKENS and not _is_mmdd(t) and not _is_build_suffix(t)
    ]
    return NormalizedName(
        raw=str(raw),
        key="-".join(tokens),
        tokens=tuple(tokens),
        variant="; ".join(v for v in variants if v),
    )


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MatchResult:
    index: int | None
    method: str  # exact | token_set | fuzzy | none
    score: float


class NameIndex:
    """Index of candidate names -> row positions in the target table."""

    def __init__(self, entries: Iterable[tuple[NormalizedName, int]], threshold: float = FUZZY_THRESHOLD):
        self.threshold = threshold
        self._exact: dict[str, int] = {}
        self._sorted: dict[str, set[int]] = {}
        self._by_key: dict[str, tuple[NormalizedName, int]] = {}
        for nn, idx in entries:  # earlier entries win exact ties (put id-based keys first)
            if not nn.key:
                continue
            self._exact.setdefault(nn.key, idx)
            self._sorted.setdefault(nn.sorted_key, set()).add(idx)
            self._by_key.setdefault(nn.key, (nn, idx))

    def match(self, nn: NormalizedName) -> MatchResult:
        if not nn.key:
            return MatchResult(None, "none", 0.0)

        if nn.key in self._exact:
            return MatchResult(self._exact[nn.key], "exact", 1.0)

        candidates = self._sorted.get(nn.sorted_key)
        if candidates and len(candidates) == 1:
            return MatchResult(next(iter(candidates)), "token_set", 0.98)

        return self._fuzzy(nn)

    def _fuzzy(self, nn: NormalizedName) -> MatchResult:
        sm = difflib.SequenceMatcher(autojunk=False)
        sm.set_seq2(nn.key)
        scored: list[tuple[float, int]] = []
        for key, (cand, idx) in self._by_key.items():
            if cand.numeric_tokens != nn.numeric_tokens:
                continue  # never let 8b match 70b, or v2 match v3
            sm.set_seq1(key)
            if sm.real_quick_ratio() < self.threshold or sm.quick_ratio() < self.threshold:
                continue
            ratio = sm.ratio()
            if ratio >= self.threshold:
                scored.append((ratio, idx))
        if not scored:
            return MatchResult(None, "none", 0.0)
        scored.sort(reverse=True)
        best_ratio, best_idx = scored[0]
        if len(scored) > 1 and scored[1][1] != best_idx and best_ratio - scored[1][0] < 0.02:
            return MatchResult(None, "none", 0.0)  # ambiguous: refuse to guess
        return MatchResult(best_idx, "fuzzy", round(best_ratio, 3))


# --------------------------------------------------------------------------
# parameter counts parsed from names (fallback when Hugging Face has none)
# --------------------------------------------------------------------------
_TOTAL_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s?(b|m)(?![a-z0-9])")
_ACTIVE_RE = re.compile(r"(?<![a-z0-9])a(\d+(?:\.\d+)?)b(?![a-z0-9])")
_MOE_X_RE = re.compile(r"\d+\s?x\s?\d+(?:\.\d+)?\s?b")


def parse_param_counts(*texts: str | None) -> tuple[float | None, float | None]:
    """Return ``(total_billions, active_billions)`` parsed from the first text that has one.

    ``"Qwen3 235B A22B"`` -> (235.0, 22.0). ``"8x7B"``-style names are skipped because the
    product is not the real parameter count (shared attention layers).
    """
    for text in texts:
        if not text:
            continue
        s = str(text).lower()
        if _MOE_X_RE.search(s):
            continue
        m = _TOTAL_RE.search(s)
        if not m:
            continue
        value = float(m.group(1))
        total = value / 1000 if m.group(2) == "m" else value
        a = _ACTIVE_RE.search(s)
        active = float(a.group(1)) if a else None
        return total, active
    return None, None
