.PHONY: install test demo etl eda all clean

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q

# Offline demo on SYNTHETIC data (no API keys, writes data/sample.db + reports/sample/)
demo:
	python -m aimodels all --offline

# Real run: needs AA_API_KEY (free key from artificialanalysis.ai)
etl:
	python -m aimodels etl

eda:
	python -m aimodels eda

all:
	python -m aimodels all

clean:
	rm -rf data/sample.db reports/sample .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
