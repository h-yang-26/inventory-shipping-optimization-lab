"""Scenario loading and result formatting (no solver logic here)."""
from __future__ import annotations

import json
from pathlib import Path

from .model import InputError, Result, Scenario, parse_scenario

DEFAULT_SCENARIOS = Path(__file__).resolve().parent.parent / "data" / "scenarios.json"

STATUS_NOTE = {
    "OPTIMAL": "proven optimal by the solver and re-validated",
    "FEASIBLE": "valid plan, NOT proven optimal (solver stopped early)",
    "NO_USABLE_SOLUTION": "no usable solution; nothing is reported as a plan",
    "INVALID_SOLUTION": "solver output failed independent validation; not reported as a plan",
}


def load_scenarios(path=DEFAULT_SCENARIOS) -> list[Scenario]:
    """Read and validate a scenarios JSON file ({"scenarios": [...]})."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not valid JSON: {exc}") from exc
    raw = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise InputError(f"{path}: expected a non-empty 'scenarios' list")
    scenarios = [parse_scenario(s) for s in raw]
    names = [s.name for s in scenarios]
    if len(set(names)) != len(names):
        raise InputError(f"{path}: duplicate scenario names {names}")
    return scenarios


def _pct(value) -> str:
    return "N/A" if value is None else f"{100 * value:.1f}%"


def _num(value) -> str:
    return f"{value:,.2f}"


def format_result(scn: Scenario, r: Result) -> str:
    lines = [f"=== {scn.name} ===", scn.description] if scn.description else [f"=== {scn.name} ==="]
    lines.append(f"Status: {r.status} - {STATUS_NOTE[r.status]}")
    lines.append(f"Solver status: {r.solver_status}")
    if not r.usable:
        lines += [f"  problem: {p}" for p in r.problems]
        return "\n".join(lines)

    cids = [c.id for c in scn.containers]
    head = f"  {'product':<20}{'demand':>7}{'stock':>7}" + "".join(f"{c:>9}" for c in cids) + f"{'shortage':>10}"
    lines += ["Allocation (units):", head]
    for p in scn.products:
        row = f"  {p.id:<20}{p.demand:>7}{p.stock:>7}"
        row += "".join(f"{r.allocation[p.id][c]:>9}" for c in cids)
        lines.append(row + f"{r.shortage[p.id]:>10}")
    lines.append("Containers (utilization = load / capacity of a used container):")
    for c in scn.containers:
        load = r.container_load[c.id]
        if r.container_used[c.id]:
            lines.append(
                f"  {c.id}: used, fixed cost {_num(c.fixed_cost)}, "
                f"mass {load['mass_kg']:.1f}/{c.capacity_kg:.1f} kg ({_pct(load['mass_utilization'])}), "
                f"volume {load['volume_m3']:.2f}/{c.capacity_m3:.2f} m3 ({_pct(load['volume_utilization'])})")
        else:
            lines.append(f"  {c.id}: not used (no fixed charge incurred, nothing loaded)")
    lines.append(f"  overall (used containers): mass {_pct(r.overall_mass_utilization)}, "
                 f"volume {_pct(r.overall_volume_utilization)}")
    lines.append(f"Units: shipped {r.total_shipped} of {r.total_demand} demanded; shortage {r.total_shortage}")
    lines.append(f"Objective: {_num(r.objective)} toy_cost_units "
                 f"(shortage penalties + fixed container charges)")
    lines.append(f"Baseline (ship nothing, all demand short): {_num(r.baseline_cost)} toy_cost_units; "
                 f"difference vs baseline: {_num(r.baseline_cost - r.objective)} toy_cost_units (fictional)")
    return "\n".join(lines)


def result_to_dict(r: Result) -> dict:
    """JSON-friendly result. Contains no timestamps or timings."""
    def clean(v):
        if isinstance(v, float):
            return round(v, 6)
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        return v
    return clean({
        "scenario": r.scenario, "status": r.status, "solver_status": r.solver_status,
        "problems": r.problems, "allocation": r.allocation, "shortage": r.shortage,
        "container_used": r.container_used, "container_load": r.container_load,
        "total_demand": r.total_demand, "total_shipped": r.total_shipped,
        "total_shortage": r.total_shortage,
        "overall_mass_utilization": r.overall_mass_utilization,
        "overall_volume_utilization": r.overall_volume_utilization,
        "objective_toy_cost_units": r.objective,
        "baseline_ship_nothing_toy_cost_units": r.baseline_cost,
    })
