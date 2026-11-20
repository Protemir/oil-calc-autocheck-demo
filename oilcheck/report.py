"""Отчёты: HTML (для людей), JUnit XML (для CI), JSON (для машин), Markdown (сводка CI)."""
import html
import json
import math
from xml.sax.saxutils import quoteattr, escape

from . import __version__
from .config import LAYER_TITLES, LAYERS

CROSS = ("saved~response", "export~saved")


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if v != 0 and (abs(v) >= 1e7 or abs(v) < 1e-4):
            return f"{v:.6e}"
        s = f"{v:,.6f}".rstrip("0").rstrip(".")
        return s.replace(",", " ").replace(".", ",")
    return str(v)


def _delta(c):
    if c.kind == "missing":
        return "нет поля"
    if c.kind == "type":
        return "другой тип"
    if c.kind == "text":
        return "текст отличается"
    if c.delta is None:
        return "—"
    s = ("+" if c.delta > 0 else "") + _fmt(c.delta)
    if c.rel_delta is not None:
        s += f" ({c.rel_delta * 100:+.4g} %)".replace(".", ",")
    return s


def _short(v):
    s = f"{v:g}"
    if "e" in s:
        m, ex = s.split("e")
        s = f"{m}e{int(ex)}"
    return s.replace(".", ",")


def _tol(c):
    if c.kind != "value" or c.allowed is None:
        return "точное совпадение" if c.kind == "text" else "—"
    parts = []
    if c.tol_abs:
        parts.append(f"±{_short(c.tol_abs)}")
    if c.tol_rel:
        parts.append(f"±{_short(c.tol_rel * 100)} %")
    s = " или ".join(parts) or "точно"
    if c.rounding is not None:
        s += f" · округл. {c.rounding} зн."
    return s


def _plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def _short_msg(checks, limit=5):
    lines = []
    for c in checks[:limit]:
        lines.append(f"{c.path}: ожидалось {_fmt(c.expected)}, получено {_fmt(c.actual)}, Δ {_delta(c)}")
    if len(checks) > limit:
        lines.append(f"… ещё {len(checks) - limit}")
    return "; ".join(lines)


# ---------------- JSON ----------------

def write_json(rep, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep.to_dict(), f, ensure_ascii=False, indent=2, default=str)


# ---------------- JUnit ----------------

def write_junit(rep, path):
    places = list(LAYERS) + list(CROSS)
    suites = []
    total = fails = errs = 0
    for layer in places:
        cases = []
        n = f = e = 0
        for r in rep.results:
            if layer not in r.layer_status and r.status != "error":
                continue
            if layer not in r.layer_status and layer in CROSS:
                continue
            n += 1
            name = f"{r.id} — {r.title}"
            attrs = f'classname={quoteattr("oilcheck." + layer)} name={quoteattr(name)} ' \
                    f'time="{r.duration_s:.3f}"'
            bad = [c for c in r.checks if c.layer == layer and not c.ok]
            if r.status == "error" and r.layer_status.get(layer) in (None, "error"):
                e += 1
                cases.append(f"<testcase {attrs}><error message={quoteattr(r.error or 'ошибка')}/></testcase>")
            elif bad:
                f += 1
                body = "\n".join(
                    f"{c.path}: ожидалось {_fmt(c.expected)}, получено {_fmt(c.actual)}, Δ {_delta(c)}, допуск {_tol(c)}"
                    for c in bad)
                cases.append(f"<testcase {attrs}><failure message={quoteattr(LAYER_TITLES[layer] + ': ' + _short_msg(bad, 3))} "
                             f'type="ToleranceExceeded">{escape(body)}</failure></testcase>')
            else:
                cases.append(f"<testcase {attrs}/>")
        if n:
            suites.append(f'<testsuite name={quoteattr(LAYER_TITLES[layer])} tests="{n}" failures="{f}" '
                          f'errors="{e}" time="{rep.duration_s:.2f}">' + "".join(cases) + "</testsuite>")
            total, fails, errs = total + n, fails + f, errs + e
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<testsuites name={quoteattr("oilcheck: " + rep.suite)} tests="{total}" failures="{fails}" '
           f'errors="{errs}" time="{rep.duration_s:.2f}">' + "".join(suites) + "</testsuites>\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)


# ---------------- Markdown ----------------

def write_markdown(rep, path):
    c = rep.counts
    icon = "✅" if rep.ok else "❌"
    lines = [f"### {icon} Автопроверка расчётов: {rep.suite}", "",
             f"API: `{rep.api_version or rep.api or '—'}` · сценариев {len(rep.results)} · "
             f"прошли {c['pass']} · упали {c['fail']} · ошибки {c['error']} · {rep.duration_s} с", "",
             "| Сценарий | Ответ | Сохранён | Выгрузка | Где появилось |", "|---|---|---|---|---|"]
    mark = {"pass": "✅", "fail": "❌", "error": "⚠️", None: "—"}
    for r in rep.results:
        if r.status == "pass":
            continue
        where = ", ".join(sorted({LAYER_TITLES[v] for v in r.origin.values()})) or (r.error or "")
        lines.append(f"| {r.id} | {mark[r.layer_status.get('response')]} | {mark[r.layer_status.get('saved')]} | "
                     f"{mark[r.layer_status.get('export')]} | {where} |")
    if c["fail"] == 0 and c["error"] == 0:
        lines.append("| все сценарии прошли | ✅ | ✅ | ✅ | — |")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------- HTML ----------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16202b;--mut:#5c6b7a;--line:#e3e7ec;--ok:#137a4b;--okbg:#e5f5ec;
--bad:#b42318;--badbg:#fdecea;--warn:#a15c00;--warnbg:#fff4e0;--acc:#0b5cad;--chip:#eef2f6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1419;--card:#171d24;--ink:#e6edf3;--mut:#93a1b0;--line:#2a333d;
--ok:#4ac38a;--okbg:#12301f;--bad:#ff7b72;--badbg:#3a1714;--warn:#f0b35a;--warnbg:#3a2a10;--acc:#58a6ff;--chip:#222a33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.mut{color:var(--mut)}.meta{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;color:var(--mut);margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:24px;line-height:1.2}.card span{font-size:12px;color:var(--mut)}
.card.ok b{color:var(--ok)}.card.bad b{color:var(--bad)}.card.warn b{color:var(--warn)}
.banner{border-radius:10px;padding:12px 16px;margin:14px 0;font-weight:600}
.banner.ok{background:var(--okbg);color:var(--ok)}.banner.bad{background:var(--badbg);color:var(--bad)}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600;background:var(--card);position:sticky;top:0}
tr:last-child td{border-bottom:0}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.st{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.st.pass{background:var(--okbg);color:var(--ok)}.st.fail{background:var(--badbg);color:var(--bad)}
.st.error{background:var(--warnbg);color:var(--warn)}.st.na{background:var(--chip);color:var(--mut)}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:0 6px;font-size:12px;margin-right:4px}
code{font:12px/1.4 ui-monospace,Consolas,monospace}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:10px 0}
summary{cursor:pointer;padding:12px 14px;font-weight:600}summary .mut{font-weight:400}
details .tablewrap{border:0;border-top:1px solid var(--line);border-radius:0 0 10px 10px}
td.badv{background:var(--badbg)}td.badv small{color:var(--bad)}small{display:block;color:var(--mut);font-size:11px}
.origin{color:var(--bad);font-weight:600}
.bar{display:flex;gap:8px;align-items:center;margin:10px 0;flex-wrap:wrap}
.bar button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 12px;cursor:pointer}
.bar button.on{border-color:var(--acc);color:var(--acc)}.bar input{border:1px solid var(--line);background:var(--card);
color:var(--ink);border-radius:8px;padding:5px 10px;min-width:220px}
.flow{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 0}.flow div{flex:1;min-width:180px}
footer{margin-top:32px;font-size:12px;color:var(--mut)}
#matrix td:first-child{white-space:nowrap}td code{white-space:nowrap}
"""

JS = """
const rows=[...document.querySelectorAll('#matrix tbody tr')];let mode='all';
function apply(){const q=document.getElementById('q').value.toLowerCase();
rows.forEach(r=>{const okMode=mode==='all'||r.dataset.status!=='pass';
const okQ=!q||r.textContent.toLowerCase().includes(q);r.style.display=okMode&&okQ?'':'none'});}
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;
document.querySelectorAll('.bar button').forEach(x=>x.classList.toggle('on',x===b));apply();});
document.getElementById('q').oninput=apply;
"""

ST_TEXT = {"pass": "✓ ок", "fail": "✗ расхождение", "error": "⚠ ошибка"}


def _st(status, extra=""):
    if status is None:
        return '<span class="st na">—</span>'
    return f'<span class="st {status}">{ST_TEXT[status]}{extra}</span>'


def _layer_cell(r, layer):
    st = r.layer_status.get(layer)
    bad = [c for c in r.checks if c.layer == layer and not c.ok]
    extra = f" · {len(bad)}" if bad else ""
    return _st(st, extra)


def _detail(r):
    e = html.escape
    by_path = {}
    for c in r.checks:
        by_path.setdefault(c.path, {})[c.layer] = c
    bad_paths = [p for p, d in by_path.items() if any(not c.ok for c in d.values())]
    head = [f'<details open id="d-{e(r.id)}"><summary>{e(r.id)} · {e(r.title)} ']
    if r.status == "error":
        head.append(f'<span class="mut">— {e(r.error or "")}</span></summary>')
        if not bad_paths:
            return "".join(head) + "</details>"
    else:
        where = {}
        for p, layer in r.origin.items():
            where.setdefault(layer, 0)
            where[layer] += 1
        txt = "; ".join(f"появилось на слое «{LAYER_TITLES[k]}»: {v} {_plural(v, 'поле', 'поля', 'полей')}" for k, v in
                        sorted(where.items(), key=lambda kv: LAYERS.index(kv[0])))
        cross_only = [p for p in bad_paths if p not in r.origin]
        if cross_only:
            txt += ("; " if txt else "") + f"нарушено правило совпадения слоёв: {len(cross_only)} {_plural(len(cross_only), 'поле', 'поля', 'полей')}"
        head.append(f'<span class="mut">— {e(txt)}</span></summary>')
    out = ["".join(head), '<div class="tablewrap"><table><thead><tr><th>Поле</th><th>Эталон</th>']
    for layer in LAYERS:
        out.append(f"<th>{LAYER_TITLES[layer]}</th>")
    out.append("<th>Допуск</th><th>Появилось</th></tr></thead><tbody>")
    limit = 60
    for p in bad_paths[:limit]:
        d = by_path[p]
        exp = next((c.expected for lay, c in d.items() if lay in LAYERS), None)
        out.append(f"<tr><td><code>{e(p)}</code></td><td class='num'>{e(_fmt(exp))}</td>")
        for layer in LAYERS:
            c = d.get(layer)
            if not c:
                out.append("<td class='num mut'>—</td>")
            elif c.ok:
                out.append(f"<td class='num'>{e(_fmt(c.actual))}</td>")
            else:
                out.append(f"<td class='num badv'>{e(_fmt(c.actual))}<small>Δ {e(_delta(c))}</small></td>")
        cross = [c for lay, c in d.items() if lay in CROSS and not c.ok]
        origin = r.origin.get(p)
        if origin:
            org, tol = LAYER_TITLES[origin], _tol(d[origin])
        else:   # эталон совпал везде, но места разошлись между собой
            org = ", ".join(f"правило «{LAYER_TITLES[c.layer]}» Δ {_delta(c)}" for c in cross)
            tol = _tol(cross[0]) if cross else ""
        out.append(f"<td>{e(tol)}</td><td class='origin'>{e(org)}</td></tr>")
    if len(bad_paths) > limit:
        out.append(f"<tr><td colspan='7' class='mut'>… и ещё {len(bad_paths) - limit} полей — см. JSON-отчёт</td></tr>")
    out.append("</tbody></table></div></details>")
    return "".join(out)


def write_html(rep, path, command=None):
    e = html.escape
    c = rep.counts
    checks = sum(len(r.checks) for r in rep.results)
    bad_checks = sum(len(r.failed_checks) for r in rep.results)
    lc = rep.layer_counts()
    parts = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'>",
        f"<title>Автопроверка расчётов — {e(rep.suite)}</title><style>{CSS}</style></head><body><div class='wrap'>",
        f"<h1>Автопроверка расчётов: {e(rep.suite)}</h1>",
        "<div class='meta'>",
        f"<span>API: <b>{e(str(rep.api_version or rep.api or '—'))}</b></span>",
        f"<span>Прогон: <code>{e(rep.run_id)}</code></span>",
        f"<span>Начало: {e(rep.started_at)}</span>",
        f"<span>Длительность: {rep.duration_s} с</span>",
        f"<span>Параллельно: {rep.workers}</span>",
        f"<span>Повторов: {rep.repeat}</span></div>",
    ]
    if rep.ok:
        parts.append("<div class='banner ok'>Все сценарии прошли: три места совпадают с эталонами в пределах допусков.</div>")
    else:
        parts.append(f"<div class='banner bad'>Найдены расхождения: {c['fail']} {_plural(c['fail'], 'сценарий', 'сценария', 'сценариев')} с отклонениями, "
                     f"{c['error']} с ошибками выполнения"
                     + (f", нестабильных результатов: {len(rep.unstable)}" if rep.unstable else "") + ".</div>")
    parts.append("<div class='cards'>")
    for cls, val, label in [("", len(rep.results), "сценариев"), ("ok", c["pass"], "прошли"),
                            ("bad" if c["fail"] else "", c["fail"], "с расхождениями"),
                            ("warn" if c["error"] else "", c["error"], "ошибки выполнения"),
                            ("", checks, "проверок полей"), ("bad" if bad_checks else "", bad_checks, "вне допуска"),
                            ("", f"{rep.duration_s} с", "время прогона")]:
        parts.append(f"<div class='card {cls}'><b>{e(str(val))}</b><span>{label}</span></div>")
    parts.append("</div><h2>Места проверки</h2><div class='cards'>")
    for layer in list(LAYERS) + list(CROSS):
        if layer in lc:
            v = lc[layer]
            cls = "bad" if v["fail"] else ("warn" if v["error"] else "ok")
            parts.append(f"<div class='card {cls}'><b>{v['pass']}/{sum(v.values())}</b>"
                         f"<span>{LAYER_TITLES[layer]}: без расхождений</span></div>")
    parts.append("</div>")
    if rep.unstable:
        parts.append("<h2>Нестабильные результаты</h2><div class='tablewrap'><table><tr><th>Сценарий</th>"
                     "<th>Отпечатки трёх мест по повторам</th><th>Статусы</th></tr>")
        for u in rep.unstable:
            parts.append(f"<tr><td>{e(u['id'])}</td><td><code>{e(', '.join(u['fingerprints']))}</code></td>"
                         f"<td>{e(', '.join(u['statuses']))}</td></tr>")
        parts.append("</table></div>")
    parts.append("<h2>Сценарии</h2><div class='bar'><button class='on' data-mode='all'>Все</button>"
                 "<button data-mode='bad'>Только с расхождениями</button>"
                 "<input id='q' placeholder='Поиск: id, название, тег'></div>")
    parts.append("<div class='tablewrap'><table id='matrix'><thead><tr><th>ID</th><th>Сценарий</th><th>Итог</th>")
    for layer in LAYERS:
        parts.append(f"<th>{LAYER_TITLES[layer]}</th>")
    parts.append("<th>Совпадение слоёв</th><th>Где появилось</th><th>Время</th></tr></thead><tbody>")
    for r in rep.results:
        cross = [r.layer_status.get(x) for x in CROSS if x in r.layer_status]
        cross_st = None if not cross else ("fail" if "fail" in cross else "pass")
        where = sorted({r.origin[p] for p in r.origin}, key=LAYERS.index)
        where_txt = ", ".join(LAYER_TITLES[w] for w in where) if where else (
            "—" if r.status == "pass" else ("ошибка выполнения" if r.status == "error" else "правило слоёв"))
        link = f"<a href='#d-{e(r.id)}'>{e(r.id)}</a>" if r.status != "pass" else e(r.id)
        tags = "".join(f"<span class='chip'>{e(t)}</span>" for t in r.tags)
        parts.append(f"<tr data-status='{r.status}'><td><b>{link}</b></td><td>{e(r.title)}<small>{tags}"
                     f" <code>{e(r.export_format or '')}</code></small></td><td>{_st(r.status)}</td>")
        for layer in LAYERS:
            parts.append(f"<td>{_layer_cell(r, layer)}</td>")
        parts.append(f"<td>{_st(cross_st)}</td><td>{e(where_txt)}</td><td class='num'>{r.duration_s:.2f} с</td></tr>")
    parts.append("</tbody></table></div>")
    bad = [r for r in rep.results if r.status != "pass"]
    if bad:
        parts.append("<h2>Расхождения по полям</h2><p class='mut'>Значение в каждом месте; красным — вне допуска. "
                     "«Появилось» — первое место по ходу данных (ответ → сохранение → выгрузка), где поле разошлось "
                     "с эталоном.</p>")
        for r in bad:
            parts.append(_detail(r))
    parts.append(f"<footer>oilcheck {__version__} · TEMIRLAB · "
                 + (f"команда: <code>{e(command)}</code>" if command else "") + "</footer>")
    parts.append(f"</div><script>{JS}</script></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
