"""Command-line entry point: python -m shiplab"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .model import InputError, solve, solver_versions
from .report import DEFAULT_SCENARIOS, format_result, load_scenarios, result_to_dict

BANNER = ("Fictional toy model: volume/weight totals only. This is NOT a validated 3D "
          "container-loading or load-restraint plan. All costs are toy_cost_units.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="shiplab", description="Inventory-to-container allocation lab")
    ap.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS), help="scenarios JSON file")
    ap.add_argument("--scenario", action="append", metavar="NAME", help="run only this scenario (repeatable)")
    ap.add_argument("--time-limit", type=float, default=30.0, help="solver time limit per scenario, seconds")
    ap.add_argument("--json", metavar="PATH", help="write results as JSON (file is replaced)")
    ap.add_argument("--text", metavar="PATH", help="write the text report as UTF-8 (file is replaced)")
    args = ap.parse_args(argv)

    try:
        scenarios = load_scenarios(args.scenarios)
        if args.scenario:
            known = {s.name for s in scenarios}
            missing = [n for n in args.scenario if n not in known]
            if missing:
                raise InputError(f"unknown scenario(s) {missing}; available: {sorted(known)}")
            scenarios = [s for s in scenarios if s.name in args.scenario]
    except InputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2

    versions = solver_versions()
    blocks = [BANNER, f"Solver: PuLP {versions['pulp']} with CBC {versions['cbc']}"]
    results = []
    for scn in scenarios:
        r = solve(scn, time_limit=args.time_limit)
        results.append(r)
        blocks.append(format_result(scn, r))
    text = "\n\n".join(blocks) + "\n"
    print(text, end="")

    if args.text:
        Path(args.text).write_text(text, encoding="utf-8", newline="\n")
    if args.json:
        payload = {"disclaimer": BANNER, "solver": versions,
                   "results": [result_to_dict(r) for r in results]}
        Path(args.json).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8", newline="\n")
    return 0 if all(r.status in ("OPTIMAL", "FEASIBLE") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
