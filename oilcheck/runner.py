"""Прогон набора: параллельно по сценариям, три места на каждый, сверка, итог."""
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import compare, paths
from .adapter import ApiError
from .config import LAYERS
from .exports import ExportError, parse_export


@dataclass
class ScenarioResult:
    id: str
    title: str
    calc: str
    tags: list
    source: str
    status: str = "pass"             # pass | fail | error
    error: str = None
    layer_status: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    origin: dict = field(default_factory=dict)
    duration_s: float = 0.0
    timings: dict = field(default_factory=dict)
    retries: int = 0
    fingerprint: str = None
    case_name: str = None
    export_format: str = None

    @property
    def failed_checks(self):
        return [c for c in self.checks if not c.ok]

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != "checks" and not k.startswith("_")}
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


def fingerprint(flats):
    blob = json.dumps(flats, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def collect(suite, scenario, data):
    """Привести три места к плоскому виду {путь: значение}."""
    flats = {}
    for layer in ("response", "saved"):
        if layer in data.layers:
            flats[layer] = paths.flatten(data.layers[layer], suite.tables)
    if "export" in data.files:
        payload, _ = data.files["export"]
        parsed = parse_export(scenario.export_format, payload, suite.export)
        flats["export"] = paths.flatten(parsed, suite.tables)
    return flats


def evaluate(suite, scenario, flats, result):
    checks = []
    for layer in scenario.layers:
        if layer not in flats:
            result.layer_status[layer] = "error"
            result.status = "error"
            result.error = (result.error or "") + f"нет данных места «{layer}»; "
            continue
        lc = compare.check_layer(suite, scenario, layer, flats[layer])
        checks.extend(lc)
        result.layer_status[layer] = "pass" if all(c.ok for c in lc) else "fail"
    rule_checks = (compare.check_rule(suite, scenario, "saved_vs_response", "saved", "response", flats)
                   + compare.check_rule(suite, scenario, "export_vs_saved", "export", "saved", flats))
    for c in rule_checks:
        result.layer_status.setdefault(c.layer, "pass")
        if not c.ok:
            result.layer_status[c.layer] = "fail"
    checks.extend(rule_checks)
    result.checks = checks
    result.origin = compare.origins(checks)
    if result.status != "error" and any(not c.ok for c in checks):
        result.status = "fail"
    return result


def run_scenario(suite, adapter, scenario, run_tag):
    res = ScenarioResult(scenario.id, scenario.title, scenario.calc, scenario.tags, scenario.source,
                         export_format=scenario.export_format)
    res.case_name = f"{scenario.id}@{run_tag}"
    t0 = time.perf_counter()
    try:
        data = adapter.run(scenario, res.case_name)
        res.timings, res.retries = data.timings, data.retries
        flats = collect(suite, scenario, data)
        res.fingerprint = fingerprint(flats)
        evaluate(suite, scenario, flats, res)
        res._flats = flats  # для mutate/повторов; в отчёт не попадает
    except (ApiError, ExportError) as e:
        res.status, res.error = "error", str(e)
    except Exception as e:  # noqa: BLE001
        res.status, res.error = "error", f"{type(e).__name__}: {e}"
    res.duration_s = round(time.perf_counter() - t0, 3)
    return res


@dataclass
class RunReport:
    suite: str
    run_id: str
    started_at: str
    finished_at: str = None
    duration_s: float = 0.0
    workers: int = 1
    repeat: int = 1
    api: str = None
    api_version: object = None
    results: list = field(default_factory=list)
    unstable: list = field(default_factory=list)

    @property
    def counts(self):
        c = {"pass": 0, "fail": 0, "error": 0}
        for r in self.results:
            c[r.status] += 1
        return c

    @property
    def ok(self):
        return all(r.status == "pass" for r in self.results) and not self.unstable

    def layer_counts(self):
        out = {}
        for layer in list(LAYERS) + ["saved~response", "export~saved"]:
            vals = [r.layer_status.get(layer) for r in self.results if layer in r.layer_status]
            if vals:
                out[layer] = {"pass": vals.count("pass"), "fail": vals.count("fail"), "error": vals.count("error")}
        return out

    def to_dict(self):
        return {
            "suite": self.suite, "run_id": self.run_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "duration_s": self.duration_s, "workers": self.workers,
            "repeat": self.repeat, "api": self.api, "api_version": self.api_version,
            "ok": self.ok, "counts": self.counts, "layers": self.layer_counts(),
            "unstable": self.unstable,
            "scenarios": [r.to_dict() for r in self.results],
        }


def run_suite(suite, adapter, workers=4, repeat=1, api_name=None, progress=None):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    rep = RunReport(suite=suite.name, run_id=run_id, workers=workers, repeat=repeat, api=api_name,
                    started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rep.api_version = adapter.version() if hasattr(adapter, "version") else None
    t0 = time.perf_counter()
    rounds = []
    for r in range(repeat):
        tag = f"{run_id}-{r + 1}"
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(run_scenario, suite, adapter, sc, tag) for sc in suite.scenarios]
            results = []
            for f in futures:
                res = f.result()
                results.append(res)
                if progress:
                    progress(res, r + 1, repeat)
        rounds.append(sorted(results, key=lambda x: x.id))
    rep.results = rounds[0]
    if repeat > 1:
        # Повторяемость: одинаковый вход должен давать одинаковые три места.
        for i, base in enumerate(rep.results):
            prints = {rnd[i].fingerprint for rnd in rounds}
            statuses = {rnd[i].status for rnd in rounds}
            if len(prints) > 1 or len(statuses) > 1:
                rep.unstable.append({"id": base.id, "fingerprints": sorted(p or "-" for p in prints),
                                     "statuses": sorted(statuses)})
    rep.duration_s = round(time.perf_counter() - t0, 2)
    rep.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return rep
