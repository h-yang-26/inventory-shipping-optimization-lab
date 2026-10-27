"""Input validation: missing or invalid parameters are errors, never zeros."""
import copy
import json
import math

import pytest

from shiplab.model import InputError, parse_scenario
from helpers import cont, prod, scn


def base():
    return scn([prod()], [cont()])


def test_valid_input_parses():
    s = parse_scenario(base())
    assert s.products[0].stock == 5 and s.containers[0].capacity_kg == 25.0


def mutate(path, value):
    raw = base()
    kind, field = path
    target = raw[kind][0]
    if value is KeyError:
        del target[field]
    else:
        target[field] = value
    return raw


BAD = [
    (("products", "stock"), KeyError), (("products", "demand"), KeyError),
    (("products", "unit_mass_kg"), KeyError), (("products", "unit_volume_m3"), KeyError),
    (("products", "shortage_penalty"), KeyError), (("products", "id"), KeyError),
    (("containers", "capacity_kg"), KeyError), (("containers", "capacity_m3"), KeyError),
    (("containers", "fixed_cost"), KeyError),
    (("products", "stock"), -1), (("products", "demand"), -3),
    (("products", "stock"), 2.5), (("products", "demand"), 4.0), (("products", "stock"), "5"),
    (("products", "stock"), True), (("products", "stock"), None),
    (("products", "unit_mass_kg"), 0), (("products", "unit_mass_kg"), -1.0),
    (("products", "unit_volume_m3"), 0.0), (("products", "unit_volume_m3"), math.nan),
    (("products", "unit_mass_kg"), math.inf), (("products", "unit_mass_kg"), "10"),
    (("products", "shortage_penalty"), -0.5), (("products", "shortage_penalty"), math.nan),
    (("containers", "capacity_kg"), -1.0), (("containers", "capacity_m3"), math.inf),
    (("containers", "capacity_kg"), None), (("containers", "fixed_cost"), -5.0),
    (("containers", "fixed_cost"), math.nan), (("containers", "fixed_cost"), "12"),
    (("products", "id"), ""), (("containers", "id"), 7),
]


@pytest.mark.parametrize("path,value", BAD, ids=[f"{p[1]}={v!r}" for p, v in BAD])
def test_invalid_or_missing_parameter_is_rejected(path, value):
    with pytest.raises(InputError):
        parse_scenario(mutate(path, value))


def test_error_names_the_offending_field():
    with pytest.raises(InputError, match="capacity_kg"):
        parse_scenario(mutate(("containers", "capacity_kg"), KeyError))


@pytest.mark.parametrize("key", ["name", "products", "containers"])
def test_missing_top_level_key(key):
    raw = base()
    del raw[key]
    with pytest.raises(InputError):
        parse_scenario(raw)


@pytest.mark.parametrize("key", ["products", "containers"])
def test_empty_lists_rejected(key):
    raw = base()
    raw[key] = []
    with pytest.raises(InputError):
        parse_scenario(raw)


def test_duplicate_ids_rejected():
    raw = base()
    raw["products"].append(copy.deepcopy(raw["products"][0]))
    with pytest.raises(InputError, match="duplicate"):
        parse_scenario(raw)


def test_zero_penalty_zero_capacity_and_zero_stock_are_allowed():
    raw = scn([prod(stock=0, demand=0, pen=0.0)], [cont(kg=0.0, m3=0.0, fixed=0.0)])
    parse_scenario(raw)


def test_missing_values_are_not_treated_as_zero_via_json_file(tmp_path):
    from shiplab.report import load_scenarios
    raw = base()
    del raw["containers"][0]["capacity_m3"]
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"scenarios": [raw]}), encoding="utf-8")
    with pytest.raises(InputError, match="capacity_m3"):
        load_scenarios(f)


@pytest.mark.parametrize("content", ["not json", "[]", '{"scenarios": []}', '{"scenarios": {}}'])
def test_bad_scenario_files_rejected(tmp_path, content):
    from shiplab.report import load_scenarios
    f = tmp_path / "s.json"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(InputError):
        load_scenarios(f)


def test_missing_file_rejected(tmp_path):
    from shiplab.report import load_scenarios
    with pytest.raises(InputError):
        load_scenarios(tmp_path / "nope.json")
