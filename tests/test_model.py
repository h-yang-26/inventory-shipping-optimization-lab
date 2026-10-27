"""Model tests: hand-derived optima, brute-force cross-check, checker, statuses."""
import itertools
import random

import pulp
import pytest

from shiplab.model import Result, _summarise, check_plan, classify_status, parse_scenario, plan_cost, solve
from helpers import cont, prod, scn


def run(raw):
    return solve(parse_scenario(raw))


# --- tiny cases whose optimum was derived by hand (see comments) ----------

def test_mass_limit_binds():
    # 5 units of 10 kg / 1 m3, penalty 10; container 25 kg / 3 m3, fixed 12.
    # Mass allows floor(25/10)=2 units (volume would allow 3).
    # Ship 2: 3*10 + 12 = 42.  Ship 0: 5*10 = 50.  Unique optimum 42.
    r = run(scn([prod()], [cont()]))
    assert r.status == "OPTIMAL"
    assert r.objective == pytest.approx(42.0)
    assert r.allocation == {"A": {"C1": 2}}
    assert r.shortage == {"A": 3} and r.container_used == {"C1": True}
    assert r.container_load["C1"]["mass_kg"] == pytest.approx(20.0)


def test_volume_limit_binds():
    # Same product; container 100 kg / 3 m3, fixed 12. Volume allows 3 units
    # (mass would allow 10). Ship 3: 2*10 + 12 = 32 (< 50). Unique optimum 32.
    r = run(scn([prod()], [cont(kg=100.0, m3=3.0)]))
    assert r.status == "OPTIMAL"
    assert r.objective == pytest.approx(32.0)
    assert r.allocation == {"A": {"C1": 3}}
    assert r.container_load["C1"]["volume_utilization"] == pytest.approx(1.0)


def test_low_penalty_ships_nothing():
    # Penalty 2: ship 2 -> 3*2 + 12 = 18; ship nothing -> 5*2 = 10. Optimum 10.
    r = run(scn([prod(pen=2.0)], [cont()]))
    assert r.status == "OPTIMAL"
    assert r.objective == pytest.approx(10.0)
    assert r.total_shipped == 0 and r.container_used == {"C1": False}
    assert r.overall_mass_utilization is None  # N/A, no used container


def test_two_products_hand_solution():
    # A: stock/demand 5, 20 kg, 1 m3, penalty 8.  B: 3/3, 1 kg, 4 m3, penalty 6.
    # Container 100 kg / 10 m3, fixed 10. Shipping nothing costs 40+18 = 58.
    # Value avoided = 8a + 6b subject to 20a+b<=100, a+4b<=10, a<=5, b<=3.
    # Best is (a,b)=(5,0) -> 40 (next best (4,1) -> 38). Cost 58-40+10 = 28.
    r = run(scn([prod("A", 5, 5, 20.0, 1.0, 8.0), prod("B", 3, 3, 1.0, 4.0, 6.0)],
                [cont(kg=100.0, m3=10.0, fixed=10.0)]))
    assert r.status == "OPTIMAL"
    assert r.objective == pytest.approx(28.0)
    assert r.allocation == {"A": {"C1": 5}, "B": {"C1": 0}}
    assert r.shortage == {"A": 0, "B": 3}


def test_short_stock_gives_shortage_not_infeasible():
    # Stock 2 < demand 5, ample container. Ship 2: 3*10 + 12 = 42 < 50.
    r = run(scn([prod(stock=2)], [cont(kg=100.0, m3=10.0)]))
    assert r.status == "OPTIMAL"
    assert r.objective == pytest.approx(42.0)
    assert r.allocation == {"A": {"C1": 2}} and r.shortage == {"A": 3}


def test_zero_stock_and_zero_capacity():
    r = run(scn([prod(stock=0)], [cont(kg=0.0, m3=0.0)]))
    assert r.status == "OPTIMAL" and r.objective == pytest.approx(50.0)
    assert r.total_shipped == 0


def test_zero_capacity_free_container_is_reported_unused():
    # fixed_cost 0 and zero capacity: CBC is never given the use variable (all
    # coefficients are zero). It is read as 0 and the plan is still valid.
    r = run(scn([prod(stock=0)], [cont(kg=0.0, m3=0.0, fixed=0.0)]))
    assert r.status == "OPTIMAL" and r.container_used == {"C1": False}
    assert r.objective == pytest.approx(50.0)


def test_utilization_is_na_for_zero_capacity_used_container():
    # A used container with zero capacity has no valid denominator -> None (N/A).
    s = parse_scenario(scn([prod(stock=0)], [cont(kg=0.0, m3=4.0, fixed=0.0)]))
    r = Result(scenario="t", status="OPTIMAL", solver_status="test")
    _summarise(s, r, {("A", "C1"): 0}, {"A": 5}, {"C1": 1}, 50.0)
    load = r.container_load["C1"]
    assert load["mass_utilization"] is None            # capacity_kg == 0
    assert load["volume_utilization"] == pytest.approx(0.0)
    assert r.overall_mass_utilization is None


def test_zero_demand():
    r = run(scn([prod(demand=0)], [cont()]))
    assert r.status == "OPTIMAL" and r.objective == pytest.approx(0.0)
    assert r.total_demand == 0


# --- independent brute-force cross-check on small random instances --------

def brute_force_cost(raw):
    """Enumerate every integer plan; no solver, no shared formulation code."""
    P, C = raw["products"], raw["containers"]
    best = float("inf")
    # For each product, every way to split <= min(stock, demand) units across containers.
    per_product = []
    for p in P:
        opts = [q for q in itertools.product(range(p["stock"] + 1), repeat=len(C))
                if sum(q) <= min(p["stock"], p["demand"])]
        per_product.append(opts)
    for combo in itertools.product(*per_product):
        cost = 0.0
        for p, q in zip(P, combo):
            cost += p["shortage_penalty"] * (p["demand"] - sum(q))
        ok = True
        for j, c in enumerate(C):
            mass = sum(p["unit_mass_kg"] * q[j] for p, q in zip(P, combo))
            vol = sum(p["unit_volume_m3"] * q[j] for p, q in zip(P, combo))
            if mass == 0 and vol == 0:
                continue  # container unused, no fixed charge
            if mass > c["capacity_kg"] + 1e-9 or vol > c["capacity_m3"] + 1e-9:
                ok = False
                break
            cost += c["fixed_cost"]
        if ok:
            best = min(best, cost)
    return best


def random_instance(seed):
    rng = random.Random(seed)
    products = [prod(f"P{i}", rng.randint(0, 3), rng.randint(0, 4), rng.choice([1.0, 2.0, 3.5]),
                     rng.choice([0.5, 1.0, 1.5]), float(rng.randint(1, 9))) for i in range(2)]
    containers = [cont(f"K{j}", float(rng.randint(3, 9)), float(rng.randint(2, 6)),
                       float(rng.randint(1, 8))) for j in range(2)]
    return scn(products, containers, name=f"rand{seed}")


@pytest.mark.parametrize("seed", range(30))
def test_matches_brute_force(seed):
    raw = random_instance(seed)
    r = run(raw)
    assert r.status == "OPTIMAL"
    assert r.objective == pytest.approx(brute_force_cost(raw), abs=1e-6)


# --- feasibility properties on a mid-sized instance -----------------------

def test_decisions_are_nonnegative_integers_and_balanced():
    raw = scn([prod("A", 7, 9, 3.0, 0.5, 5.0), prod("B", 4, 4, 6.0, 1.5, 9.0), prod("C", 10, 6, 1.0, 0.2, 2.0)],
              [cont("K1", 30.0, 6.0, 15.0), cont("K2", 60.0, 9.0, 20.0)])
    s = parse_scenario(raw)
    r = solve(s)
    assert r.status == "OPTIMAL"
    for p in s.products:
        units = [r.allocation[p.id][c.id] for c in s.containers]
        assert all(isinstance(q, int) and q >= 0 for q in units)
        assert isinstance(r.shortage[p.id], int) and r.shortage[p.id] >= 0
        assert sum(units) <= p.stock
        assert sum(units) + r.shortage[p.id] == p.demand
    for c in s.containers:
        load = r.container_load[c.id]
        assert load["mass_kg"] <= c.capacity_kg + 1e-6
        assert load["volume_m3"] <= c.capacity_m3 + 1e-6
        if not r.container_used[c.id]:
            assert load["mass_kg"] == 0 and load["volume_m3"] == 0


def test_repeat_runs_give_same_objective_and_plan():
    raw = random_instance(3)
    a, b = run(raw), run(raw)
    assert (a.status, a.objective, a.allocation, a.shortage) == (b.status, b.objective, b.allocation, b.shortage)


# --- the independent plan checker catches bad plans ------------------------

def good_plan():
    s = parse_scenario(scn([prod()], [cont()]))
    return s, {("A", "C1"): 2}, {"A": 3}, {"C1": 1}


def test_check_plan_accepts_valid_plan():
    s, x, u, y = good_plan()
    assert check_plan(s, x, u, y) == []
    assert plan_cost(s, u, y) == pytest.approx(42.0)


@pytest.mark.parametrize("x,u,y,fragment", [
    ({("A", "C1"): -1}, {"A": 6}, {"C1": 1}, "nonnegative integer"),
    ({("A", "C1"): 1.5}, {"A": 3}, {"C1": 1}, "nonnegative integer"),
    ({("A", "C1"): 2}, {"A": -3}, {"C1": 1}, "nonnegative integer"),
    ({("A", "C1"): 2}, {"A": 2}, {"C1": 1}, "!= demand"),           # balance broken
    ({("A", "C1"): 3}, {"A": 2}, {"C1": 1}, "exceeds capacity"),    # 30 kg > 25 kg
    ({("A", "C1"): 2}, {"A": 3}, {"C1": 0}, "exceeds capacity"),    # unused container loaded
])
def test_check_plan_rejects_bad_plans(x, u, y, fragment):
    s, *_ = good_plan()
    problems = check_plan(s, x, u, y)
    assert problems and any(fragment in p for p in problems)


def test_check_plan_rejects_stock_overuse():
    s = parse_scenario(scn([prod(stock=1)], [cont(kg=100.0, m3=10.0)]))
    assert any("exceeds stock" in p for p in check_plan(s, {("A", "C1"): 2}, {"A": 3}, {"C1": 1}))


@pytest.mark.parametrize("bad", [None, "bad", 1.5, True])
def test_check_plan_reports_invalid_allocations_without_crashing(bad):
    s, x, u, y = good_plan()
    x["A", "C1"] = bad
    assert any("nonnegative integer" in p for p in check_plan(s, x, u, y))


def test_check_plan_reports_missing_allocation_without_crashing():
    s, _, u, y = good_plan()
    assert any("nonnegative integer" in p for p in check_plan(s, {}, u, y))


def test_unused_container_cannot_ship_even_below_float_tolerance():
    s = parse_scenario(scn([prod(stock=1, demand=1, mass=1e-9, vol=1e-9)], [cont()]))
    problems = check_plan(s, {("A", "C1"): 1}, {"A": 0}, {"C1": 0})
    assert any("unused container" in p for p in problems)


def test_check_plan_rejects_volume_overflow():
    s = parse_scenario(scn([prod(mass=1.0, vol=2.0)], [cont(kg=100.0, m3=3.0)]))
    assert any("volume" in p for p in check_plan(s, {("A", "C1"): 2}, {"A": 3}, {"C1": 1}))


@pytest.mark.parametrize("y", [{"C1": 2}, {"C1": -1}, {"C1": 0.5}, {"C1": 1.0}, {"C1": "1"},
                               {"C1": True}, {"C1": None}, {}])
def test_check_plan_rejects_non_binary_or_missing_container_flag(y):
    # The plan itself is fine (2 units, 20 kg in a 25 kg container); only the
    # flag is bad. A truthy value such as 2 must not be read as "used".
    s, x, u, _ = good_plan()
    problems = check_plan(s, x, u, y)
    assert any("C1" in p and "flag" in p for p in problems)


def test_check_plan_accepts_both_binary_flag_values():
    s = parse_scenario(scn([prod(stock=0)], [cont()]))
    assert check_plan(s, {("A", "C1"): 0}, {"A": 5}, {"C1": 0}) == []
    assert check_plan(s, {("A", "C1"): 0}, {"A": 5}, {"C1": 1}) == []


# --- constant-zero objective ---------------------------------------------

def all_zero_cost_scenario():
    # Every penalty and fixed charge is 0, so every feasible plan costs 0 and
    # many allocations tie. Only status, objective and feasibility are asserted.
    return scn([prod("A", 5, 5, 10.0, 1.0, 0.0), prod("B", 3, 4, 2.0, 0.5, 0.0)],
               [cont("C1", 25.0, 3.0, 0.0), cont("C2", 40.0, 5.0, 0.0)], name="all_zero_cost")


def plan_from_result(r):
    x = {(p, c): q for p, row in r.allocation.items() for c, q in row.items()}
    y = {c: int(used) for c, used in r.container_used.items()}
    return x, dict(r.shortage), y


def test_all_zero_costs_give_optimal_zero_objective():
    s = parse_scenario(all_zero_cost_scenario())
    r = solve(s)
    assert r.status == "OPTIMAL"
    assert r.objective == 0.0
    x, u, y = plan_from_result(r)
    assert check_plan(s, x, u, y) == []


def test_missing_objective_value_for_nonconstant_objective_is_invalid(monkeypatch):
    # A real (nonconstant) objective whose value the solver does not supply
    # must not be treated as a zero-cost success.
    monkeypatch.setattr(pulp.LpAffineExpression, "value", lambda self: None)
    r = run(scn([prod()], [cont()]))
    assert r.status == "INVALID_SOLUTION" and not r.usable
    assert r.objective is None and r.allocation == {}
    assert any("objective" in p for p in r.problems)


# --- solver status handling -------------------------------------------------

def test_classify_status_is_conservative():
    assert classify_status(pulp.LpStatusOptimal, pulp.LpSolutionOptimal) == "OPTIMAL"
    assert classify_status(pulp.LpStatusOptimal, pulp.LpSolutionIntegerFeasible) == "FEASIBLE"
    for status, sol in [(pulp.LpStatusInfeasible, pulp.LpSolutionInfeasible),
                        (pulp.LpStatusUnbounded, pulp.LpSolutionUnbounded),
                        (pulp.LpStatusNotSolved, pulp.LpSolutionNoSolutionFound),
                        (pulp.LpStatusUndefined, pulp.LpSolutionNoSolutionFound),
                        (pulp.LpStatusNotSolved, pulp.LpSolutionOptimal),
                        (pulp.LpStatusInfeasible, pulp.LpSolutionIntegerFeasible)]:
        assert classify_status(status, sol) == "NO_USABLE_SOLUTION"


def test_invalid_solver_output_is_not_reported_as_a_plan(monkeypatch):
    # Simulate a solver that claims optimality but returns a plan violating
    # the supply/demand balance; the result must not be labelled usable.
    real = pulp.LpVariable.value

    def corrupt(self):
        v = real(self)
        return v + 1 if self.name.startswith("u_") and v is not None else v

    monkeypatch.setattr(pulp.LpVariable, "value", corrupt)
    r = run(scn([prod()], [cont()]))
    assert r.status == "INVALID_SOLUTION" and not r.usable
    assert r.problems and r.allocation == {} and r.objective is None
