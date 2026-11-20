"""Сравнение с эталоном: допуски по полям и местам, округление выгрузки, сверка слоёв."""
import math
from dataclasses import dataclass, asdict

from . import paths
from .config import LAYERS

ORDER = {name: i for i, name in enumerate(LAYERS)}


@dataclass
class Check:
    layer: str
    path: str
    expected: object
    actual: object
    ok: bool
    kind: str = "value"        # value | missing | text | type
    delta: float = None
    rel_delta: float = None
    allowed: float = None
    tol_abs: float = None
    tol_rel: float = None
    rounding: int = None

    def to_dict(self):
        return asdict(self)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def tolerance(suite, scenario, path, layer):
    tol = dict(suite.tolerance["default"])
    sources = [
        suite.tolerance["fields"],
        suite.tolerance["layers"].get(layer, {}),
        scenario.tolerance.get("fields", {}),
        scenario.tolerance.get("layers", {}).get(layer, {}),
    ]
    for src in sources:
        for pat, t in src.items():
            if paths.match(pat, path):
                tol.update(t)
    return float(tol.get("abs", 0.0)), float(tol.get("rel", 0.0))


def rounding(suite, path, layer):
    table = suite.rounding.get(layer, {})
    found = None
    for pat, d in table.items():
        if paths.match(pat, path):
            found = int(d)
    return found


def compare_value(layer, path, expected, actual, tol_abs, tol_rel, decimals=None, combine="any"):
    if actual is None and expected is not None:
        return Check(layer, path, expected, None, False, kind="missing")
    if _is_num(expected) and _is_num(actual):
        e, a = float(expected), float(actual)
        if math.isnan(e) or math.isnan(a):
            return Check(layer, path, expected, actual, math.isnan(e) and math.isnan(a), kind="value")
        d = abs(a - e)
        by_abs, by_rel = tol_abs, tol_rel * abs(e)
        if combine == "all":   # должны выполняться оба заданных допуска
            given = [x for x in (by_abs, by_rel) if x > 0]
            allowed = min(given) if given else 0.0
        else:                  # достаточно одного (по умолчанию)
            allowed = max(by_abs, by_rel)
        if decimals is not None:
            allowed += 0.5 * 10 ** (-decimals)
        allowed += 1e-12 * max(1.0, abs(e))   # шум двоичной арифметики
        return Check(layer, path, expected, actual, d <= allowed, delta=a - e,
                     rel_delta=(a - e) / e if e else None, allowed=allowed,
                     tol_abs=tol_abs, tol_rel=tol_rel, rounding=decimals)
    if _is_num(expected) != _is_num(actual) and expected is not None:
        return Check(layer, path, expected, actual, False, kind="type")
    return Check(layer, path, expected, actual, expected == actual, kind="text")


def check_layer(suite, scenario, layer, actual_flat):
    exp = paths.flatten(scenario.expected, suite.tables)
    exp.update(scenario.expected_layers.get(layer, {}))
    combine = suite.raw.get("tolerance", {}).get("combine", "any")
    checks = []
    for path in exp:
        ta, tr = tolerance(suite, scenario, path, layer)
        checks.append(compare_value(layer, path, exp[path], actual_flat.get(path), ta, tr,
                                    rounding(suite, path, layer), combine))
    return checks


def check_rule(suite, scenario, rule_name, left_layer, right_layer, flats):
    """Сверка «место A совпадает с местом B» — когда эталон один или нужен контроль сохранения."""
    rule = suite.rules.get(rule_name)
    if not rule or left_layer not in flats or right_layer not in flats:
        return []
    left, right = flats[left_layer], flats[right_layer]
    name = f"{left_layer}~{right_layer}"
    keys = list(paths.flatten(scenario.expected, suite.tables))
    if rule.get("all_fields"):   # сверять и поля, которых нет в эталоне
        keys += [k for k in right if k not in set(keys)]
    checks = []
    for path in keys:
        if path not in right:
            continue
        dec = rounding(suite, path, left_layer) if rule.get("rounding", False) else None
        checks.append(compare_value(name, path, right[path], left.get(path),
                                    float(rule.get("abs", 0)), float(rule.get("rel", 0)), dec))
    return checks


def origins(checks):
    """Для каждого поля — первое место (по ходу данных), где значение вышло за допуск."""
    first = {}
    for c in checks:
        if c.ok or c.layer not in ORDER:
            continue
        if c.path not in first or ORDER[c.layer] < ORDER[first[c.path]]:
            first[c.path] = c.layer
    return first
