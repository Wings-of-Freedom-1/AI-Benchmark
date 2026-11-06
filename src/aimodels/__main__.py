"""Command line: ``python -m aimodels {etl,eda,all} [options]``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import DB_PATH, REPORTS_DIR, SAMPLE_DB_PATH


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aimodels", description=__doc__)
    sub = p.add_subparsers(dest="cmd")  # no command -> open the dashboard

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--offline", "--sample", dest="offline", action="store_true",
                        help="use synthetic sample data (no network, separate sample.db)")
    common.add_argument("--db", type=Path, help="SQLite path (default: data/models.db, or data/sample.db with --offline)")
    common.add_argument("-v", "--verbose", action="store_true")

    etl_args = argparse.ArgumentParser(add_help=False)
    etl_args.add_argument("--use-cache", action="store_true", help="reuse the latest raw AA/OpenRouter download")
    etl_args.add_argument("--skip-hf", action="store_true", help="skip Hugging Face lookups (names only)")

    eda_args = argparse.ArgumentParser(add_help=False)
    eda_args.add_argument("--out", type=Path, help="output folder (default: reports/, or reports/sample/ with --offline)")

    dash = sub.add_parser("dashboard", help="open the control-surface dashboard in your browser (default)")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    dash.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("etl", parents=[common, etl_args], help="extract, clean, validate, load")
    sub.add_parser("eda", parents=[common, eda_args], help="make figures + summary from the database")
    sub.add_parser("all", parents=[common, etl_args, eda_args], help="etl then eda")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "dashboard"):
        from .dashboard import serve

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
        serve(port=getattr(args, "port", 8765), open_browser=not getattr(args, "no_browser", False))
        return 0
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    db = args.db or (SAMPLE_DB_PATH if args.offline else DB_PATH)

    if args.cmd in ("etl", "all"):
        from .extract import ExtractError
        from .pipeline import run_etl
        from .schemas import PipelineValidationError

        try:
            s = run_etl(offline=args.offline, use_cache=args.use_cache, skip_hf=args.skip_hf, db_path=db)
        except (ExtractError, PipelineValidationError) as exc:
            logging.getLogger("aimodels").error("%s", exc)
            return 1
        print(f"ETL ok [{s.source}]: {s.n_valid} models loaded, {s.n_rejected} rejected -> {s.db_path}")

    if args.cmd in ("eda", "all"):
        from .eda import run_eda

        out = args.out or (REPORTS_DIR / "sample" if args.offline else REPORTS_DIR)
        report = run_eda(db, out)
        print(f"EDA ok: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
