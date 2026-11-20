"""Проверка KPI «ловим ≥ 90 % внесённых отклонений сверх допуска».

Режимы:
  api     — расхождения вносятся в сам продукт (мок: /admin/faults) на нужном слое,
            набор прогоняется через API как обычно. Так проверяется вся цепочка:
            HTTP, разбор CSV/XLSX, округление, допуски.
  etalon  — продукт не трогаем, правим копию эталона и сверяем с уже снятыми
            результатами. Работает на любом стенде, где нельзя вносить ошибки.
Для каждого места вносим отклонения выше допуска (должны быть пойманы) и ниже
допуска (не должны давать ложных срабатываний).
"""
import copy
import json
import random
import time
import urllib.request

from . import compare, paths
from .config import LAYERS, LAYER_TITLES
from .runner import evaluate, run_scenario, ScenarioResult


def _numeric_paths(suite, sc):
    flat = paths.flatten(sc.expected, suite.tables)
    return [p for p, v in flat.items() if isinstance(v, (int, float)) and not isinstance(v, bool)], flat


def _plan(suite, scenarios, count, rng, below_share):
    plan = []
    for i in range(count):
        sc = rng.choice(scenarios)
        nums, flat = _numeric_paths(suite, sc)
        path = rng.choice(nums)
        layer = LAYERS[i % len(LAYERS)]
        tol_abs, tol_rel = compare.tolerance(suite, sc, path, layer)
        e = float(flat[path])
        base = max(tol_abs, tol_rel * abs(e))
        # Отклонение «ниже допуска» должно проходить и правила совпадения слоёв,
        # а они строже эталонного допуска: сохранение вообще не должно менять число.
        rule = suite.rules.get({"saved": "saved_vs_response", "export": "export_vs_saved"}.get(layer, ""))
        if rule:
            base_rule = max(float(rule.get("abs", 0)), float(rule.get("rel", 0)) * abs(e))
            low = min(base, base_rule)
        else:
            low = base
        dec = compare.rounding(suite, path, layer)
        slack = 0.5 * 10 ** (-dec) if dec is not None else 0.0
        above = rng.random() >= below_share
        sign = rng.choice((-1, 1))
        if above:
            # от «чуть выше допуска» до грубой ошибки; учитываем, что выгрузка округлит
            k = rng.choice((1.5, 2, 3, 5, 10, 100))
            delta = sign * max(base * k + 2 * slack, 1e-9 * max(1.0, abs(e)) * k + 2 * slack)
        else:
            delta = sign * low * rng.uniform(0.1, 0.6)
            if delta == 0:
                continue
        plan.append({"n": i, "scenario": sc.id, "layer": layer, "path": path, "expected": e,
                     "delta": delta, "above": above, "allowed": base + slack})
    return plan


def _admin(url, method, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def _detected(res, m):
    hit = any((not c.ok) and c.path == m["path"] and c.layer == m["layer"] for c in res.checks)
    anywhere = any(not c.ok for c in res.checks) or res.status == "error"
    return hit, anywhere


def run_mutation(suite, adapter, api_cfg, count=120, seed=7, mode="api", workers=8, below_share=0.25, admin=None):
    from concurrent.futures import ThreadPoolExecutor
    rng = random.Random(seed)
    t0 = time.perf_counter()
    base_tag = f"base{seed}"
    with ThreadPoolExecutor(max_workers=workers) as pool:
        baseline = list(pool.map(lambda s: run_scenario(suite, adapter, s, base_tag), suite.scenarios))
    clean = {r.id: r for r in baseline if r.status == "pass"}
    scenarios = [s for s in suite.scenarios if s.id in clean]
    skipped = [r.id for r in baseline if r.status != "pass"]
    if not scenarios:
        raise RuntimeError("нет ни одного сценария, прошедшего без расхождений — мутационная проверка невозможна")
    plan = _plan(suite, scenarios, count, rng, below_share)
    by_id = {s.id: s for s in scenarios}
    results = []
    if mode == "api":
        admin = admin or api_cfg.get("admin_url") or api_cfg["base_url"].rstrip("/") + "/admin/faults"
        _admin(admin, "DELETE")
        faults = [{"case": f"{m['scenario']}@mut{seed}-{m['n']}", "layer": m["layer"], "path": m["path"],
                   "op": "add", "value": m["delta"]} for m in plan]
        _admin(admin, "POST", {"faults": faults})
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                runs = list(pool.map(lambda m: run_scenario(suite, adapter, by_id[m["scenario"]],
                                                            f"mut{seed}-{m['n']}"), plan))
        finally:
            _admin(admin, "DELETE")
        for m, res in zip(plan, runs):
            hit, anywhere = _detected(res, m)
            results.append({**m, "detected": hit, "flagged": anywhere})
    else:
        for m in plan:
            sc = copy.deepcopy(by_id[m["scenario"]])
            sc.expected_layers.setdefault(m["layer"], {})[m["path"]] = m["expected"] + m["delta"]
            res = ScenarioResult(sc.id, sc.title, sc.calc, sc.tags, sc.source)
            evaluate(suite, sc, clean[sc.id]._flats, res)
            hit, anywhere = _detected(res, m)
            results.append({**m, "detected": hit, "flagged": anywhere})
    summary = {"mode": mode, "seed": seed, "count": len(results), "skipped_scenarios": skipped,
               "duration_s": round(time.perf_counter() - t0, 2), "layers": {}}
    for layer in LAYERS:
        ab = [r for r in results if r["layer"] == layer and r["above"]]
        be = [r for r in results if r["layer"] == layer and not r["above"]]
        summary["layers"][layer] = {
            "above": len(ab), "detected": sum(r["detected"] for r in ab),
            "below": len(be), "false_alarms": sum(r["flagged"] for r in be),
        }
    ab = [r for r in results if r["above"]]
    be = [r for r in results if not r["above"]]
    summary["above"] = len(ab)
    summary["detected"] = sum(r["detected"] for r in ab)
    summary["rate"] = summary["detected"] / len(ab) if ab else None
    summary["below"] = len(be)
    summary["false_alarms"] = sum(r["flagged"] for r in be)
    summary["missed"] = [r for r in ab if not r["detected"]][:20]
    summary["mutants"] = results
    return summary


def write_mutation_md(s, path):
    rate = f"{s['rate'] * 100:.1f} %" if s["rate"] is not None else "—"
    ok = s["rate"] is not None and s["rate"] >= 0.9 and s["false_alarms"] == 0
    lines = [f"### {'✅' if ok else '❌'} KPI: поймано {s['detected']} из {s['above']} внесённых отклонений ({rate})", "",
             f"Режим `{s['mode']}`, seed {s['seed']}, {s['duration_s']} с. Ниже допуска внесено {s['below']}, "
             f"ложных срабатываний {s['false_alarms']}.", "",
             "| Место | Внесено сверх допуска | Поймано | Ниже допуска | Ложные |", "|---|---|---|---|---|"]
    for layer, v in s["layers"].items():
        lines.append(f"| {LAYER_TITLES[layer]} | {v['above']} | {v['detected']} | {v['below']} | {v['false_alarms']} |")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return ok
