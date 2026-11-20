"""Командная строка: python -m oilcheck run | validate | capture | mutate."""
import argparse
import json
import os
import sys

from . import __version__, report
from .adapter import ApiError, make_adapter
from .config import ConfigError, LAYER_TITLES, load_api, load_suite


def _progress(quiet):
    mark = {"pass": "ok  ", "fail": "FAIL", "error": "ERR "}

    def cb(r, rnd=1, total=1):
        if quiet:
            return
        extra = ""
        if r.status == "fail":
            layers = ", ".join(LAYER_TITLES[k] for k, v in r.layer_status.items() if v == "fail")
            extra = f"  [{layers}; полей вне допуска: {len(r.failed_checks)}]"
        elif r.status == "error":
            extra = f"  [{r.error}]"
        prefix = f"[{rnd}/{total}] " if total > 1 else ""
        print(f"  {prefix}{mark[r.status]} {r.id:<10} {r.duration_s:6.2f} с  {r.title}{extra}", flush=True)
    return cb


def cmd_run(a):
    suite = load_suite(a.suite, only=a.only, tags=a.tags)
    api = load_api(a.api)
    adapter = make_adapter(api)
    print(f"oilcheck {__version__}: набор «{suite.name}», сценариев {len(suite.scenarios)}, "
          f"параллельно {a.workers}, API {api['base_url']}", flush=True)
    from .runner import run_suite
    rep = run_suite(suite, adapter, workers=a.workers, repeat=a.repeat, api_name=api["base_url"],
                    progress=_progress(a.quiet))
    os.makedirs(a.out, exist_ok=True)
    command = "python -m oilcheck " + " ".join(sys.argv[1:])
    report.write_html(rep, os.path.join(a.out, "report.html"), command)
    report.write_json(rep, os.path.join(a.out, "report.json"))
    report.write_junit(rep, os.path.join(a.out, "junit.xml"))
    report.write_markdown(rep, os.path.join(a.out, "summary.md"))
    c = rep.counts
    print(f"\nИтог: прошли {c['pass']}, расхождения {c['fail']}, ошибки {c['error']}"
          + (f", нестабильные {len(rep.unstable)}" if rep.unstable else "")
          + f" · {rep.duration_s} с · отчёт {os.path.join(a.out, 'report.html')}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh, \
                open(os.path.join(a.out, "summary.md"), encoding="utf-8") as src:
            fh.write(src.read())
    if c["error"]:
        return 2
    return 0 if rep.ok else 1


def cmd_validate(a):
    suite = load_suite(a.suite)
    print(f"Набор «{suite.name}»: {len(suite.scenarios)} сценариев, формат в порядке.")
    for s in suite.scenarios:
        from .paths import flatten
        n = len(flatten(s.expected, suite.tables))
        print(f"  {s.id:<10} {s.calc:<10} выгрузка {s.export_format:<5} контрольных полей {n:>4}  {s.title}")
    return 0


def _round_sig(x, digits):
    if isinstance(x, float):
        return float(f"{x:.{digits}g}")
    if isinstance(x, dict):
        return {k: _round_sig(v, digits) for k, v in x.items()}
    if isinstance(x, list):
        return [_round_sig(v, digits) for v in x]
    return x


def cmd_capture(a):
    """Снять черновик эталона с принятого релиза. Эталон обязательно проверяет инженер."""
    suite = load_suite(a.suite, only=a.only, need_expected=False)
    adapter = make_adapter(load_api(a.api))
    for sc in suite.scenarios:
        path = os.path.join(suite.root, os.path.dirname(sc.source), f"expected/{sc.id}.json")
        if os.path.exists(path) and not a.force:
            print(f"  skip {sc.id}: {path} уже есть (--force чтобы перезаписать)")
            continue
        data = adapter.run(sc, f"{sc.id}@capture")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_round_sig(data.layers[a.layer], a.digits), fh, ensure_ascii=False, indent=1)
        print(f"  ok   {sc.id}: {os.path.relpath(path)}")
    return 0


def cmd_mutate(a):
    from .mutate import run_mutation, write_mutation_md
    suite = load_suite(a.suite, only=a.only)
    api = load_api(a.api)
    adapter = make_adapter(api)
    print(f"Проверка KPI: {a.count} внесённых отклонений, режим {a.mode}, seed {a.seed}", flush=True)
    s = run_mutation(suite, adapter, api, count=a.count, seed=a.seed, mode=a.mode, workers=a.workers, admin=a.admin)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "mutation.json"), "w", encoding="utf-8") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=1)
    ok = write_mutation_md(s, os.path.join(a.out, "mutation.md"))
    with open(os.path.join(a.out, "mutation.md"), encoding="utf-8") as fh:
        md = fh.read()
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(md)
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="oilcheck", description="Автопроверка расчётов по эталонам на трёх слоях API")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, api=True):
        sp.add_argument("--suite", default="suites/demo", help="папка набора (suite.toml + scenarios/)")
        if api:
            sp.add_argument("--api", default="api.toml", help="описание тестового API")
        sp.add_argument("--only", nargs="*", help="id сценариев или шаблоны, например ECO-*")

    r = sub.add_parser("run", help="прогнать набор и построить отчёты")
    common(r)
    r.add_argument("--tags", nargs="*")
    r.add_argument("--workers", type=int, default=int(os.environ.get("OILCHECK_WORKERS", 4)))
    r.add_argument("--repeat", type=int, default=1, help="прогнать N раз и сверить, что результаты одинаковы")
    r.add_argument("--out", default="reports")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    v = sub.add_parser("validate", help="проверить формат набора без запуска")
    common(v, api=False)
    v.set_defaults(fn=cmd_validate)

    c = sub.add_parser("capture", help="снять черновик эталона с принятого релиза")
    common(c)
    c.add_argument("--layer", default="response")
    c.add_argument("--digits", type=int, default=10)
    c.add_argument("--force", action="store_true")
    c.set_defaults(fn=cmd_capture)

    m = sub.add_parser("mutate", help="проверить, что внесённые отклонения ловятся (KPI ≥ 90 %%)")
    common(m)
    m.add_argument("--count", type=int, default=120)
    m.add_argument("--seed", type=int, default=7)
    m.add_argument("--mode", choices=("api", "etalon"), default="api")
    m.add_argument("--admin", help="адрес /admin/faults (режим api)")
    m.add_argument("--workers", type=int, default=8)
    m.add_argument("--out", default="reports")
    m.set_defaults(fn=cmd_mutate)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (ConfigError, ApiError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
