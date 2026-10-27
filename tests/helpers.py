"""Small raw-scenario builders shared by the tests."""
import copy


def prod(pid="A", stock=5, demand=5, mass=10.0, vol=1.0, pen=10.0):
    return {"id": pid, "stock": stock, "demand": demand, "unit_mass_kg": mass,
            "unit_volume_m3": vol, "shortage_penalty": pen}


def cont(cid="C1", kg=25.0, m3=3.0, fixed=12.0):
    return {"id": cid, "capacity_kg": kg, "capacity_m3": m3, "fixed_cost": fixed}


def scn(products, containers, name="t"):
    return {"name": name, "products": copy.deepcopy(products), "containers": copy.deepcopy(containers)}
