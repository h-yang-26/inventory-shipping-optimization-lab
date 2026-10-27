"""Inventory-to-container allocation model (PuLP + CBC).

Everything here is a toy: all quantities are fictional and the model checks
only total mass and total volume per container. It is NOT a 3D load plan.
"""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field

import pulp

# Absolute/relative slack used when comparing float mass, volume and cost sums.
# It matches the scale of CBC's own feasibility tolerance.
TOL = 1e-6


class InputError(ValueError):
    """Raised for missing or invalid scenario parameters."""


@dataclass(frozen=True)
class Product:
    id: str
    stock: int  # units on hand
    demand: int  # units requested
    unit_mass_kg: float
    unit_volume_m3: float
    shortage_penalty: float  # toy_cost_units per unfulfilled unit


@dataclass(frozen=True)
class Container:
    id: str
    capacity_kg: float
    capacity_m3: float
    fixed_cost: float  # toy_cost_units if the container is used


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    products: tuple[Product, ...]
    containers: tuple[Container, ...]


@dataclass
class Result:
    scenario: str
    # OPTIMAL | FEASIBLE | NO_USABLE_SOLUTION | INVALID_SOLUTION
    status: str
    solver_status: str  # raw PuLP status / solution-status text
    problems: list[str] = field(default_factory=list)
    allocation: dict[str, dict[str, int]] = field(default_factory=dict)  # product -> container -> units
    shortage: dict[str, int] = field(default_factory=dict)
    container_used: dict[str, bool] = field(default_factory=dict)
    objective: float | None = None  # recomputed from the plan, not read from the solver
    baseline_cost: float | None = None  # ship-nothing plan: sum(penalty * demand)
    total_demand: int = 0
    total_shipped: int = 0
    total_shortage: int = 0
    container_load: dict[str, dict[str, float | None]] = field(default_factory=dict)
    overall_mass_utilization: float | None = None
    overall_volume_utilization: float | None = None

    @property
    def usable(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------

def _require(obj, key, where):
    if not isinstance(obj, dict) or key not in obj:
        raise InputError(f"{where}: missing required field '{key}'")
    return obj[key]


def _nonneg_int(value, name, where):
    # bool is a subclass of int; JSON true/false must not pass as a quantity.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{where}: '{name}' must be a nonnegative integer, got {value!r}")
    if value < 0:
        raise InputError(f"{where}: '{name}' must be >= 0, got {value!r}")
    return value


def _finite(value, name, where, positive):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{where}: '{name}' must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise InputError(f"{where}: '{name}' must be finite, got {value!r}")
    if positive and value <= 0:
        raise InputError(f"{where}: '{name}' must be > 0, got {value!r}")
    if not positive and value < 0:
        raise InputError(f"{where}: '{name}' must be >= 0, got {value!r}")
    return value


def _ident(value, where):
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{where}: 'id' must be a non-empty string, got {value!r}")
    return value


def parse_scenario(raw) -> Scenario:
    """Validate one raw scenario dict and return an immutable Scenario.

    Nothing is defaulted: a missing stock, demand, capacity or cost is an
    error, never an implicit zero.
    """
    name = _ident(_require(raw, "name", "scenario"), "scenario")
    where = f"scenario '{name}'"
    raw_products = _require(raw, "products", where)
    raw_containers = _require(raw, "containers", where)
    if not isinstance(raw_products, list) or not raw_products:
        raise InputError(f"{where}: 'products' must be a non-empty list")
    if not isinstance(raw_containers, list) or not raw_containers:
        raise InputError(f"{where}: 'containers' must be a non-empty list")

    products, containers = [], []
    for k, p in enumerate(raw_products):
        w = f"{where}, products[{k}]"
        pid = _ident(_require(p, "id", w), w)
        w = f"{where}, product '{pid}'"
        products.append(Product(
            id=pid,
            stock=_nonneg_int(_require(p, "stock", w), "stock", w),
            demand=_nonneg_int(_require(p, "demand", w), "demand", w),
            unit_mass_kg=_finite(_require(p, "unit_mass_kg", w), "unit_mass_kg", w, positive=True),
            unit_volume_m3=_finite(_require(p, "unit_volume_m3", w), "unit_volume_m3", w, positive=True),
            shortage_penalty=_finite(_require(p, "shortage_penalty", w), "shortage_penalty", w, positive=False),
        ))
    for k, c in enumerate(raw_containers):
        w = f"{where}, containers[{k}]"
        cid = _ident(_require(c, "id", w), w)
        w = f"{where}, container '{cid}'"
        containers.append(Container(
            id=cid,
            capacity_kg=_finite(_require(c, "capacity_kg", w), "capacity_kg", w, positive=False),
            capacity_m3=_finite(_require(c, "capacity_m3", w), "capacity_m3", w, positive=False),
            fixed_cost=_finite(_require(c, "fixed_cost", w), "fixed_cost", w, positive=False),
        ))

    for kind, items in (("product", products), ("container", containers)):
        ids = [i.id for i in items]
        if len(set(ids)) != len(ids):
            raise InputError(f"{where}: duplicate {kind} ids in {ids}")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise InputError(f"{where}: 'description' must be a string")
    return Scenario(name, description, tuple(products), tuple(containers))


# --------------------------------------------------------------------------
# Independent plan checker (does not use PuLP)
# --------------------------------------------------------------------------

def check_plan(scn: Scenario, x, u, y) -> list[str]:
    """Return a list of violated conditions for a candidate plan.

    x[(product_id, container_id)] units shipped, u[product_id] shortage units,
    y[container_id] container-use flag (int 0 or 1). An empty list means the
    plan satisfies every constraint of the model.
    """
    problems = []
    for p in scn.products:
        shipped = 0
        for c in scn.containers:
            q = x.get((p.id, c.id))
            if not isinstance(q, int) or isinstance(q, bool) or q < 0:
                problems.append(f"x[{p.id},{c.id}] is not a nonnegative integer: {q!r}")
            else:
                shipped += q
        s = u.get(p.id)
        if not isinstance(s, int) or isinstance(s, bool) or s < 0:
            problems.append(f"shortage[{p.id}] is not a nonnegative integer: {s!r}")
            continue
        if shipped > p.stock:
            problems.append(f"{p.id}: shipped {shipped} exceeds stock {p.stock}")
        if shipped + s != p.demand:
            problems.append(f"{p.id}: shipped {shipped} + shortage {s} != demand {p.demand}")
    for c in scn.containers:
        flag = y.get(c.id)
        # Exactly int 0 or 1; a truthy 2, a float, a bool or a missing flag is an error.
        if isinstance(flag, bool) or not isinstance(flag, int) or flag not in (0, 1):
            problems.append(f"{c.id}: container-use flag must be 0 or 1, got {flag!r}")
            continue
        quantities = [x.get((p.id, c.id)) for p in scn.products]
        if any(isinstance(q, bool) or not isinstance(q, int) or q < 0 for q in quantities):
            # The product loop already records these domain errors. Do not do
            # arithmetic on missing/text decisions while collecting diagnostics.
            continue
        if flag == 0 and any(quantities):
            problems.append(f"{c.id}: unused container has allocated units")
        mass = sum(p.unit_mass_kg * q for p, q in zip(scn.products, quantities))
        vol = sum(p.unit_volume_m3 * q for p, q in zip(scn.products, quantities))
        if mass > c.capacity_kg * flag + TOL * max(1.0, c.capacity_kg):
            problems.append(f"{c.id}: mass {mass:.6g} kg exceeds capacity {c.capacity_kg * flag:.6g} kg")
        if vol > c.capacity_m3 * flag + TOL * max(1.0, c.capacity_m3):
            problems.append(f"{c.id}: volume {vol:.6g} m3 exceeds capacity {c.capacity_m3 * flag:.6g} m3")
    return problems


def plan_cost(scn: Scenario, u, y) -> float:
    """Objective: sum(penalty * shortage) + sum(fixed_cost * use flag)."""
    return (sum(p.shortage_penalty * u[p.id] for p in scn.products)
            + sum(c.fixed_cost * (1 if y[c.id] else 0) for c in scn.containers))


def classify_status(status: int, sol_status: int) -> str:
    """Map PuLP (status, sol_status) to a conservative label.

    OPTIMAL only when the solver reports a proven optimum. A usable incumbent
    that is not proven optimal (e.g. a time limit) is FEASIBLE. Anything else
    is NO_USABLE_SOLUTION. The caller must still validate the plan.
    """
    if status == pulp.LpStatusOptimal and sol_status == pulp.LpSolutionOptimal:
        return "OPTIMAL"
    if status == pulp.LpStatusOptimal and sol_status == pulp.LpSolutionIntegerFeasible:
        return "FEASIBLE"
    return "NO_USABLE_SOLUTION"


# --------------------------------------------------------------------------
# Solving
# --------------------------------------------------------------------------

def _as_int(value):
    """Round a solver value to int; None if it is missing or not integral."""
    if value is None or not math.isfinite(value):
        return None
    r = round(value)
    return int(r) if abs(value - r) <= 1e-6 else None


def solve(scn: Scenario, time_limit: float = 30.0) -> Result:
    """Build the MILP, solve it with CBC, validate and summarise the plan."""
    P, C = scn.products, scn.containers
    prob = pulp.LpProblem(f"allocation_{scn.name}", pulp.LpMinimize)
    x = {(i, c): pulp.LpVariable(f"x_{i}_{c}", lowBound=0, cat="Integer")
         for i in range(len(P)) for c in range(len(C))}
    u = {i: pulp.LpVariable(f"u_{i}", lowBound=0, cat="Integer") for i in range(len(P))}
    y = {c: pulp.LpVariable(f"y_{c}", cat="Binary") for c in range(len(C))}

    prob += (pulp.lpSum(P[i].shortage_penalty * u[i] for i in u)
             + pulp.lpSum(C[c].fixed_cost * y[c] for c in y)), "total_cost"
    for i, p in enumerate(P):
        shipped = pulp.lpSum(x[i, c] for c in range(len(C)))
        prob += shipped <= p.stock, f"supply_{i}"
        prob += shipped + u[i] == p.demand, f"balance_{i}"
    for c, k in enumerate(C):
        prob += (pulp.lpSum(P[i].unit_mass_kg * x[i, c] for i in range(len(P)))
                 <= k.capacity_kg * y[c]), f"mass_{c}"
        prob += (pulp.lpSum(P[i].unit_volume_m3 * x[i, c] for i in range(len(P)))
                 <= k.capacity_m3 * y[c]), f"volume_{c}"

    # CBC's native serial mode is threads=0. Its worker-thread mode (threads=1)
    # can hang in the bundled Windows CBC 2.10.3 despite a solver time limit.
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, threads=0))
    status = classify_status(prob.status, prob.sol_status)
    raw = f"{pulp.LpStatus[prob.status]} (solution status {prob.sol_status})"
    baseline = float(sum(p.shortage_penalty * p.demand for p in P))
    result = Result(scenario=scn.name, status=status, solver_status=raw, baseline_cost=baseline)
    if status == "NO_USABLE_SOLUTION":
        return result

    # PuLP drops variables whose coefficients are all zero (only a container
    # with zero fixed cost AND zero capacity); CBC then returns no value for
    # them. Such a variable cannot affect cost or feasibility, so it is read as
    # 0 (unused). A missing value for any variable actually in the model is an
    # error (checked below).
    in_model = {v.name for v in prob.variables()}

    def read(v):
        return _as_int(v.value()) if v.name in in_model else 0

    xi = {(P[i].id, C[c].id): read(v) for (i, c), v in x.items()}
    ui = {P[i].id: read(v) for i, v in u.items()}
    yi = {C[c].id: read(v) for c, v in y.items()}
    if None in xi.values() or None in ui.values() or None in yi.values():
        result.status = "INVALID_SOLUTION"
        result.problems = ["solver returned a missing or non-integral variable value"]
        return result
    problems = check_plan(scn, xi, ui, yi)
    cost = plan_cost(scn, ui, yi) if not problems else None
    if cost is not None:
        # lpSum drops zero-coefficient terms. If no variable is left (all
        # penalties and fixed charges are 0) the objective is the constant term
        # and PuLP reports no value for it. A nonconstant objective without a
        # value is an error, never an implicit 0.
        if any(coef != 0 for coef in prob.objective.values()):
            solver_obj = pulp.value(prob.objective)
        else:
            solver_obj = float(prob.objective.constant)
        if solver_obj is None:
            problems.append("solver returned no objective value")
        elif abs(cost - solver_obj) > TOL * max(1.0, abs(cost)):
            problems.append(f"recomputed cost {cost} != solver objective {solver_obj}")
    if problems:
        result.status = "INVALID_SOLUTION"
        result.problems = problems
        return result

    _summarise(scn, result, xi, ui, yi, cost)
    return result


def _ratio(num: float, den: float):
    return None if den <= 0 else num / den


def _summarise(scn, result, xi, ui, yi, cost):
    result.objective = cost
    result.allocation = {p.id: {c.id: xi[p.id, c.id] for c in scn.containers} for p in scn.products}
    result.shortage = dict(ui)
    result.container_used = {c.id: bool(yi[c.id]) for c in scn.containers}
    result.total_demand = sum(p.demand for p in scn.products)
    result.total_shortage = sum(ui.values())
    result.total_shipped = sum(xi.values())

    used_mass = used_vol = used_cap_kg = used_cap_m3 = 0.0
    for c in scn.containers:
        mass = sum(p.unit_mass_kg * xi[p.id, c.id] for p in scn.products)
        vol = sum(p.unit_volume_m3 * xi[p.id, c.id] for p in scn.products)
        used = yi[c.id] == 1
        # Utilization = load / capacity of a USED container; N/A (None) if the
        # container is unused or its capacity is zero.
        result.container_load[c.id] = {
            "mass_kg": mass, "volume_m3": vol,
            "mass_utilization": _ratio(mass, c.capacity_kg) if used else None,
            "volume_utilization": _ratio(vol, c.capacity_m3) if used else None,
        }
        if used:
            used_mass += mass
            used_vol += vol
            used_cap_kg += c.capacity_kg
            used_cap_m3 += c.capacity_m3
    result.overall_mass_utilization = _ratio(used_mass, used_cap_kg)
    result.overall_volume_utilization = _ratio(used_vol, used_cap_m3)


def solver_versions() -> dict[str, str]:
    """Report the PuLP version and the bundled CBC version (best effort)."""
    info = {"pulp": pulp.__version__, "cbc": "unknown"}
    try:
        out = subprocess.run([pulp.PULP_CBC_CMD(msg=False).path, "-quit"],
                             capture_output=True, text=True, timeout=20).stdout
        for line in out.splitlines():
            if line.strip().startswith("Version:"):
                info["cbc"] = line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return info
