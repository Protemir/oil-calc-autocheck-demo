"""Мок тестового API расчётного продукта: три слоя результата.

  POST /api/v1/cases                     -> {id}                    создать кейс
  POST /api/v1/cases/{id}/calculate      -> 202 {job_id}            асинхронный расчёт
  GET  /api/v1/jobs/{job_id}             -> {status, result}        слой 1: ответ расчёта
  POST /api/v1/cases/{id}/save           -> {saved_at}              сохранить
  GET  /api/v1/cases/{id}/result         -> {result}                слой 2: сохранённый результат
  GET  /api/v1/cases/{id}/export?format= -> файл csv|xlsx|json      слой 3: выгрузка
  GET  /api/v1/version
  POST/DELETE /admin/faults               внесение расхождений (для проверки KPI)

Релизы: --release 3.7.0 (эталонный) и --release 3.8.0-rc1 (с регрессиями).
Только стандартная библиотека Python 3.11+.
"""
import argparse
import fnmatch
import json
import random
import re
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import calcs, exporters

STATE = {"cases": {}, "jobs": {}, "faults": [], "lock": threading.Lock()}
CFG = {"release": "3.7.0", "token": "demo-token", "latency": 1.0, "flaky": 0.0, "seed": None}

KEY_FIELDS = {"curve": "pwf_atm", "forecast": "month", "cashflow": "year"}


def is_rc():
    return "rc" in CFG["release"]


def f32(x):
    """Сохранение в БД во float32 — шум ~1e-7, не должен давать ложных срабатываний."""
    if isinstance(x, float):
        return struct.unpack("f", struct.pack("f", x))[0]
    if isinstance(x, dict):
        return {k: f32(v) for k, v in x.items()}
    if isinstance(x, list):
        return [f32(v) for v in x]
    return x


def clone(x):
    return json.loads(json.dumps(x))


_PATH = re.compile(r"^(?P<table>[A-Za-z_]\w*)\[(?P<key>\w+)=(?P<val>[^\]]+)\]\.(?P<field>\w+)$")


def apply_faults(case, layer, result):
    for fault in STATE["faults"]:
        if fault["layer"] != layer or not fnmatch.fnmatch(case["name"], fault["case"]):
            continue
        path = fault["path"]
        m = _PATH.match(path)
        targets = []
        if m:
            for row in result.get(m["table"], []):
                if abs(float(row[m["key"]]) - float(m["val"])) < 1e-9:
                    targets.append((row, m["field"]))
        elif path in result:
            targets.append((result, path))
        for obj, field in targets:
            v = obj.get(field)
            if not isinstance(v, (int, float)):
                continue
            if fault.get("op") == "drop":
                obj.pop(field, None)
            elif fault.get("op") == "mul":
                obj[field] = v * fault["value"]
            else:
                obj[field] = v + fault["value"]
    return result


def saved_snapshot(case):
    res = clone(case["result"])
    calc = case["calc"]
    if is_rc():
        res = f32(res)
        if calc == "pipe" and abs(res.get("dp_static_bar", 0)) > 0:
            # Регрессия RC: маппер сохранения пишет в итог только трение.
            res["dp_total_bar"] = res["dp_friction_bar"]
        if calc == "stoiip":
            # Регрессия RC: при сохранении берётся плотность по умолчанию 0,85.
            res["stoiip_mln_t"] = res["stoiip_mln_m3"] * 0.85
    return apply_faults(case, "saved", res)


def export_file(case, fmt):
    res = apply_faults(case, "export", clone(case["saved"]))
    if fmt == "csv":
        return exporters.to_csv(res, rc_bugs=is_rc()), "text/csv; charset=utf-8", "csv"
    if fmt == "xlsx":
        return (exporters.to_xlsx(res, rc_bugs=is_rc()),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")
    meta = {"case": case["name"], "calc": case["calc"], "release": CFG["release"]}
    return exporters.to_json(res, meta), "application/json", "json"


def run_job(job_id):
    job = STATE["jobs"][job_id]
    case = STATE["cases"][job["case_id"]]
    rnd = random.Random(f"{CFG['seed']}:{case['name']}") if CFG["seed"] is not None else random
    job["status"] = "running"
    time.sleep(rnd.uniform(0.2, 1.2) * CFG["latency"])
    try:
        result = calcs.CALCS[case["calc"]](case["input"], rc=is_rc())
        result = apply_faults(case, "response", result)
        case["result"] = result
        job["result"] = result
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(exc)
    job["finished_at"] = datetime.now(timezone.utc).isoformat()


class Handler(BaseHTTPRequestHandler):
    server_version = "ReservoirCalcMock/1.0"

    def log_message(self, fmt, *args):  # тихий режим
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _guard(self):
        if self.headers.get("Authorization") != f"Bearer {CFG['token']}":
            self._send(401, {"error": "unauthorized"})
            return False
        if CFG["flaky"] and random.random() < CFG["flaky"]:
            self._send(503, {"error": "temporarily unavailable"})
            return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if url.path == "/api/v1/version":
            return self._send(200, {"product": "ReservoirCalc (mock)", "release": CFG["release"]})
        if parts[:3] == ["api", "v1", "jobs"] and len(parts) == 4:
            job = STATE["jobs"].get(parts[3])
            if not job:
                return self._send(404, {"error": "job not found"})
            body = {k: job.get(k) for k in ("id", "status", "error", "finished_at")}
            if job["status"] == "done":
                body["result"] = job["result"]
            return self._send(200, body)
        if parts[:3] == ["api", "v1", "cases"] and len(parts) == 5:
            case = STATE["cases"].get(parts[3])
            if not case:
                return self._send(404, {"error": "case not found"})
            if case.get("saved") is None:
                return self._send(409, {"error": "case is not saved"})
            if parts[4] == "result":
                return self._send(200, {"case_id": case["id"], "saved_at": case["saved_at"], "result": case["saved"]})
            if parts[4] == "export":
                fmt = parse_qs(url.query).get("format", ["json"])[0]
                data, ctype, ext = export_file(case, fmt)
                return self._send(200, data, ctype,
                                  {"Content-Disposition": f'attachment; filename="{case["name"]}.{ext}"'})
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/admin/faults"):
            body = self._body()
            with STATE["lock"]:
                STATE["faults"].extend(body.get("faults", []))
            return self._send(200, {"faults": len(STATE["faults"])})
        if not self._guard():
            return
        parts = self.path.strip("/").split("/")
        if self.path == "/api/v1/cases":
            body = self._body()
            if body.get("calc") not in calcs.CALCS:
                return self._send(400, {"error": f"unknown calc {body.get('calc')}"})
            cid = uuid.uuid4().hex[:12]
            STATE["cases"][cid] = {"id": cid, "name": body.get("name", cid), "calc": body["calc"],
                                   "input": body.get("input", {}), "result": None, "saved": None}
            return self._send(201, {"id": cid})
        if parts[:3] == ["api", "v1", "cases"] and len(parts) == 5:
            case = STATE["cases"].get(parts[3])
            if not case:
                return self._send(404, {"error": "case not found"})
            if parts[4] == "calculate":
                jid = uuid.uuid4().hex[:12]
                STATE["jobs"][jid] = {"id": jid, "case_id": case["id"], "status": "queued"}
                threading.Thread(target=run_job, args=(jid,), daemon=True).start()
                return self._send(202, {"job_id": jid})
            if parts[4] == "save":
                if case.get("result") is None:
                    return self._send(409, {"error": "not calculated"})
                case["saved"] = saved_snapshot(case)
                case["saved_at"] = datetime.now(timezone.utc).isoformat()
                return self._send(200, {"saved_at": case["saved_at"]})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        if self.path.startswith("/admin/faults"):
            with STATE["lock"]:
                STATE["faults"].clear()
            return self._send(200, {"faults": 0})
        self._send(404, {"error": "not found"})


def main(argv=None):
    p = argparse.ArgumentParser(description="Мок тестового API расчётного продукта")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--release", default="3.7.0", help="3.7.0 — эталонный, 3.8.0-rc1 — с регрессиями")
    p.add_argument("--token", default="demo-token")
    p.add_argument("--latency", type=float, default=1.0, help="множитель времени расчёта")
    p.add_argument("--flaky", type=float, default=0.0, help="доля ответов 503 (проверка повторов)")
    p.add_argument("--seed", default=None)
    a = p.parse_args(argv)
    CFG.update(release=a.release, token=a.token, latency=a.latency, flaky=a.flaky, seed=a.seed)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"mock API {a.release} on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
