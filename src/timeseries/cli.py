"""Run Phase C locally.

  python -m src.timeseries.cli run <phase-B-run-or-file> -o <run-dir> --client-config <workbook.xlsx>
                                   [-c config/settings.yaml] [--table REF] [--sheet NAME]
  python -m src.timeseries.cli review <run-dir> <edited Data_Validation_Report.xlsx> [-c config/settings.yaml]
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_settings

from src.common.errors import ConfigError
from .pipeline import run_timeseries
from .review import ReviewError, apply_upload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase C: time-series / execution checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="check the weekly table of a Phase B run folder (or one csv/xlsx/parquet file)")
    run.add_argument("input")
    run.add_argument("-o", "--output", required=True, help="run folder (Phase C writes <run>/phase_c_timeseries/)")
    run.add_argument("--client-config", required=True,
                     help="client config workbook: Data File, Variable Mapping, Period Mapping [, Data Check Specifications]")
    run.add_argument("--table", help="the table ref to check when the input has several (patterns allowed)")
    run.add_argument("--sheet", help="only tables from this sheet name")
    init = sub.add_parser("init-config", help="write a pre-filled client config workbook from the data")
    init.add_argument("input", help="Phase B run folder or one csv/xlsx/parquet file")
    init.add_argument("-o", "--output", required=True, help="the config workbook to write (.xlsx)")
    init.add_argument("--table")
    init.add_argument("--sheet")
    init.add_argument("-c", "--config", help="settings.yaml with a timeseries: section")
    rev = sub.add_parser("review", help="apply an edited Data_Validation_Report.xlsx to a finished run and re-check")
    rev.add_argument("run_dir")
    rev.add_argument("workbook")
    for p in (run, rev):
        p.add_argument("-c", "--config", help="settings.yaml with a timeseries: section")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "init-config":
            from .autoconfig import init_config
            result = init_config(a.input, a.output, a.config, a.table, a.sheet)
            print(f"Wrote {result['path']}: green cells are filled from the data, yellow cells "
                  f"({result['cells_for_you']}) are for you. Nothing is locked.", file=sys.stderr)
        elif a.cmd == "run":
            result = run_timeseries(a.input, a.output, a.client_config, a.config, table=a.table, sheet=a.sheet)
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
    print(json.dumps({k: v for k, v in result.items() if k not in ("columns", "raw_header")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
