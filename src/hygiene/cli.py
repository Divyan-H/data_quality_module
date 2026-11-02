"""Run Phase B locally.

  python -m src.hygiene.cli run <phase-A-output-or-file> -o <run-dir> [-c config/settings.yaml]
                               [--sheet NAME] [--table REF ...] [--all-tables]
  python -m src.hygiene.cli review <run-dir> <edited Hygiene_Review.xlsx> [-c config/settings.yaml]
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .pipeline import run_hygiene
from .review import ReviewError, apply_upload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase B: data hygiene")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="clean the tables of a Phase A output folder (or one csv/xlsx/parquet file)")
    run.add_argument("input")
    run.add_argument("-o", "--output", required=True)
    run.add_argument("--sheet", help="only tables from this sheet name")
    run.add_argument("--table", nargs="+", help="only these table refs (patterns allowed, e.g. 'abc123:0:*')")
    run.add_argument("--all-tables", action="store_true", help="ignore Phase A's table selection")
    rev = sub.add_parser("review", help="apply an edited Hygiene_Review.xlsx to a finished run")
    rev.add_argument("run_dir")
    rev.add_argument("workbook")
    for p in (run, rev):
        p.add_argument("-c", "--config", help="settings.yaml with a hygiene: section")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            result = run_hygiene(a.input, a.output, a.config, None,
                                 a.sheet, a.table, a.all_tables)
        else:
            result = apply_upload(a.run_dir, a.workbook, load_config(a.config))
    except ReviewError as e:
        print("Upload rejected:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
