import pytest

from aimodels.normalize import NameIndex, normalize_name, parse_param_counts


@pytest.mark.parametrize(
    "raw, is_id, key",
    [
        ("Llama 3.1 Instruct 8B", False, "llama-3-1-8b"),
        ("Meta: Llama 3.1 8B Instruct", False, "llama-3-1-8b"),
        ("meta-llama/llama-3.1-8b-instruct", True, "llama-3-1-8b"),
        ("meta-llama/llama-3.1-8b-instruct:free", True, "llama-3-1-8b"),
        ("openai/gpt-4o-2024-08-06", True, "gpt-4o"),
        ("GPT-4o (Aug '24)", False, "gpt-4o"),
        ("openai/gpt-4-0613", True, "gpt-4"),
        ("google/gemini-2.0-flash-001", True, "gemini-2-0-flash"),
        ("Claude 3.5 Sonnet (Oct '24)", False, "claude-3-5-sonnet"),
    ],
)
def test_normalize_key(raw, is_id, key):
    assert normalize_name(raw, is_id=is_id).key == key


def test_variant_is_kept_separately():
    nn = normalize_name("Qwen3 32B (Reasoning)")
    assert nn.key == "qwen3-32b"
    assert nn.variant == "reasoning"  # lower-cased with the rest of the name


def test_sizes_are_never_merged():
    assert normalize_name("Llama 3.1 8B").key != normalize_name("Llama 3.1 70B").key


def _index(*names, ids=True):
    return NameIndex([(normalize_name(n, is_id=ids), i) for i, n in enumerate(names)])


def test_exact_and_token_set_matching():
    idx = _index("anthropic/claude-3.5-sonnet", "anthropic/claude-3-opus")
    assert idx.match(normalize_name("Claude 3.5 Sonnet")).method == "exact"
    swapped = idx.match(normalize_name("Claude Sonnet 3.5"))
    assert (swapped.index, swapped.method) == (0, "token_set")


def test_fuzzy_needs_identical_numbers_and_unique_winner():
    idx = _index("acme/frobnicator-ultra-8b")
    typo = idx.match(normalize_name("Frobnicatr Ultra 8B"))
    assert (typo.index, typo.method) == (0, "fuzzy")
    # same words, different size: must NOT match
    assert idx.match(normalize_name("Frobnicatr Ultra 70B")).index is None
    # two equally good candidates: refuse to guess
    amb = _index("acme/frobnicator-ultra-8b", "acme/frobnicatar-ultra-8b")
    assert amb.match(normalize_name("Frobnicatur Ultra 8B")).index is None


def test_unmatched_returns_none():
    res = _index("a/b-c").match(normalize_name("Totally Different Model"))
    assert res.index is None and res.method == "none"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Llama 3.1 8B Instruct", (8.0, None)),
        ("Qwen3 235B A22B", (235.0, 22.0)),
        ("SmolLM 135M", (0.135, None)),
        ("Mixtral 8x7B", (None, None)),  # product of experts is not the real total
        ("GPT-4o mini", (None, None)),
        ("Gemini 1.5 Flash-8B", (8.0, None)),
    ],
)
def test_parse_param_counts(text, expected):
    assert parse_param_counts(text) == expected
