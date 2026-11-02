"""Run Phase D locally.

  python -m src.trends.cli run <phase-C-run-or-file> -o <run-dir> [--client-config <workbook.xlsx>]
                               [-c config/settings.yaml] [--table REF] [--sheet NAME]
  python -m src.trends.cli init-config <phase-C-run-or-file> -o <client_config.xlsx> [--table REF] [--sheet NAME]
  python -m src.trends.cli review <run-dir> <edited Spend_Support_Trend_Report.xlsx> [-c config/settings.yaml]
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_settings

from src.common.errors import ConfigError
from .pipeline import run_trends
from .review import ReviewError, apply_upload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase D: trend and period-over-period change checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="check the table of a Phase C run folder (or one csv/xlsx/parquet file)")
    run.add_argument("input")
    run.add_argument("-o", "--output", required=True, help="run folder (Phase D writes <run>/phase_d_trends/)")
    run.add_argument("--client-config",
                     help="client config workbook: Data File, Period Mapping, Variable Mapping [, Data Check "
                          "Specifications, Ratio Definitions]. Default: the one the Phase C run used")
    run.add_argument("--table", help="the table ref to check when the input has several (patterns allowed)")
    run.add_argument("--sheet", help="only tables from this sheet name")
    init = sub.add_parser("init-config", help="write a pre-filled client config workbook from the data")
    init.add_argument("input", help="Phase C run folder or one csv/xlsx/parquet file")
    init.add_argument("-o", "--output", required=True, help="the config workbook to write (.xlsx)")
    init.add_argument("--table")
    init.add_argument("--sheet")
    init.add_argument("-c", "--config", help="settings.yaml with a trends: section")
    rev = sub.add_parser("review", help="apply an edited Spend_Support_Trend_Report.xlsx to a finished run and re-check")
    rev.add_argument("run_dir")
    rev.add_argument("workbook")
    for p in (run, rev):
        p.add_argument("-c", "--config", help="settings.yaml with a trends: section")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "init-config":
            from .autoconfig import init_config
            result = init_config(a.input, a.output, a.config, a.table, a.sheet)
            print(f"Wrote {result['path']}: green cells are filled from the data, yellow cells "
                  f"({result['cells_for_you']}) are for you. Nothing is locked.", file=sys.stderr)
        elif a.cmd == "run":
            result = run_trends(a.input, a.output, a.client_config, a.config, table=a.table, sheet=a.sheet)
        else:
            result = apply_upload(a.run_dir, a.workbook, load_settings(a.config))
    except ReviewError as e:
        print("Upload rejected:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in result.items() if k not in ("columns", "raw_header", "rules")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
