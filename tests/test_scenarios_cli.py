"""Bundled scenarios, differences between them, results example and CLI."""
import json
from pathlib import Path

import pytest

from shiplab.cli import main
from shiplab.model import check_plan, parse_scenario, solve
from shiplab.report import DEFAULT_SCENARIOS, load_scenarios

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def solved():
    scenarios = {s.name: s for s in load_scenarios()}
    return scenarios, {n: solve(s) for n, s in scenarios.items()}


def test_bundled_scenario_shape(solved):
    scenarios, _ = solved
    assert len(scenarios) >= 3
    for s in scenarios.values():
        assert 3 <= len(s.products) <= 5 and 1 <= len(s.containers) <= 2
        assert all(c.fixed_cost > 0 for c in s.containers)  # positive fixed charges by default
    assert {"normal", "tight_capacity", "high_shortage_penalty"} <= set(scenarios)


def test_all_bundled_scenarios_optimal(solved):
    _, results = solved
    assert all(r.status == "OPTIMAL" for r in results.values())


def test_normal_ships_everything_with_both_containers(solved):
    scenarios, results = solved
    r = results["normal"]
    assert r.total_shortage == 0 and all(r.container_used.values())
    # Total demand is 2635 kg / 35.9 m3, more than CNT-L alone holds (2400 kg /
    # 28 m3), so zero shortage needs both containers: 400 + 900 = 1300.
    # Opening only CNT-L would leave >= 7.9 m3 of demand short; the cheapest
    # way to cut that much volume (all filter cartridges plus a few cable
    # spools, penalties 450 + 280) already costs more than the 400 it saves.
    assert r.objective == pytest.approx(1300.0)


def test_tight_capacity_differs_for_model_reasons(solved):
    scenarios, results = solved
    normal, tight = results["normal"], results["tight_capacity"]
    s = scenarios["tight_capacity"]
    assert tight.total_shortage > normal.total_shortage
    # Small container's best contents are worth less than its 400 fixed charge.
    assert tight.container_used == {"CNT-S": False, "CNT-L": True}
    assert tight.objective > normal.objective
    cap_kg = sum(c.capacity_kg for c in s.containers)
    assert sum(l["mass_kg"] for l in tight.container_load.values()) <= cap_kg


def test_high_penalty_opens_the_small_container_and_ships_more(solved):
    _, results = solved
    tight, high = results["tight_capacity"], results["high_shortage_penalty"]
    assert high.container_used == {"CNT-S": True, "CNT-L": True}
    assert high.total_shipped > tight.total_shipped
    assert high.total_shortage < tight.total_shortage


def test_low_penalty_ships_nothing(solved):
    scenarios, results = solved
    r = results["low_shortage_penalty"]
    s = scenarios["low_shortage_penalty"]
    assert sum(p.shortage_penalty * p.demand for p in s.products) < min(c.fixed_cost for c in s.containers)
    assert r.total_shipped == 0 and not any(r.container_used.values())
    assert r.objective == pytest.approx(r.baseline_cost)
    assert r.overall_mass_utilization is None and r.overall_volume_utilization is None


def test_no_inventory_gives_full_shortage_not_infeasibility(solved):
    scenarios, results = solved
    r = results["no_inventory"]
    assert all(p.stock == 0 for p in scenarios["no_inventory"].products)
    assert r.status == "OPTIMAL" and r.total_shipped == 0
    assert r.total_shortage == r.total_demand


def test_example_results_file_matches_fresh_run(solved, tmp_path):
    _, results = solved
    committed = json.loads((ROOT / "results" / "example_results.json").read_text(encoding="utf-8"))
    # Compare status and objective only: equal-cost allocations may differ
    # between CBC builds, which is not a correctness failure.
    fresh = {r.scenario: (r.status, r.objective) for r in results.values()}
    old = {e["scenario"]: (e["status"], e["objective_toy_cost_units"]) for e in committed["results"]}
    assert set(old) == set(fresh)
    for name, (status, obj) in fresh.items():
        assert old[name][0] == status
        assert old[name][1] == pytest.approx(obj)


def test_cli_runs_and_writes_outputs(tmp_path, capsys):
    j, t = tmp_path / "r.json", tmp_path / "r.txt"
    assert main(["--scenario", "tight_capacity", "--json", str(j), "--text", str(t)]) == 0
    out = capsys.readouterr().out
    assert "NOT a validated 3D" in out and "Status: OPTIMAL" in out
    data = json.loads(j.read_text(encoding="utf-8"))
    assert [r["scenario"] for r in data["results"]] == ["tight_capacity"]
    assert t.read_text(encoding="utf-8") == out
    main(["--scenario", "tight_capacity", "--json", str(j)])  # replace, not append
    assert len(json.loads(j.read_text(encoding="utf-8"))["results"]) == 1


def test_cli_output_is_repeatable(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    main(["--scenario", "normal", "--json", str(a)])
    main(["--scenario", "normal", "--json", str(b)])
    assert a.read_bytes() == b.read_bytes()  # no timestamps or timings in the file


def test_cli_rejects_invalid_input_without_traceback(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"scenarios": [{"name": "x", "products": [], "containers": []}]}), encoding="utf-8")
    assert main(["--scenarios", str(bad)]) == 2
    err = capsys.readouterr().err
    assert "input error" in err and "Traceback" not in err


def test_cli_rejects_unknown_scenario_name(capsys):
    assert main(["--scenario", "nope"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_default_scenario_path_exists():
    assert DEFAULT_SCENARIOS.is_file()


def test_cli_all_zero_cost_scenario_exits_zero(tmp_path, capsys):
    from test_model import all_zero_cost_scenario
    f, j = tmp_path / "zero.json", tmp_path / "zero_out.json"
    f.write_text(json.dumps({"scenarios": [all_zero_cost_scenario()]}), encoding="utf-8")
    assert main(["--scenarios", str(f), "--json", str(j)]) == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Status: OPTIMAL" in captured.out and "Objective: 0.00 toy_cost_units" in captured.out
    entry = json.loads(j.read_text(encoding="utf-8"))["results"][0]
    assert entry["status"] == "OPTIMAL" and entry["objective_toy_cost_units"] == 0.0
    s = parse_scenario(all_zero_cost_scenario())
    x = {(p, c): q for p, row in entry["allocation"].items() for c, q in row.items()}
    y = {c: int(used) for c, used in entry["container_used"].items()}
    assert check_plan(s, x, entry["shortage"], y) == []
