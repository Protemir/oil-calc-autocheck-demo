"""Адаптер к тестовому API: шаги описаны в api.toml, кода под каждый API не нужно.

Шаг: method, path, json (шаблон тела), expect (коды), save (что запомнить из ответа),
poll (ожидание статуса задачи), layer (какое место проверки снимается), file (скачать файл).
Подстановки: {calc}, {case_name}, {scenario_id}, {export_format}, "{input}" (объект целиком)
и всё, что сохранено предыдущими шагами. Если API совсем нестандартный — укажите
python_adapter = "модуль:Класс" с методом run(scenario, case_name) -> RunData.
"""
import importlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import paths

RETRY_CODES = {429, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, step, message):
        super().__init__(f"шаг «{step}»: {message}")
        self.step = step


@dataclass
class RunData:
    layers: dict = field(default_factory=dict)          # response/saved -> dict
    files: dict = field(default_factory=dict)           # export -> (bytes, content_type)
    timings: dict = field(default_factory=dict)         # шаг -> секунды
    retries: int = 0


def _fill(tpl, ctx):
    if isinstance(tpl, str):
        if tpl.startswith("{") and tpl.endswith("}") and tpl[1:-1] in ctx and not isinstance(ctx[tpl[1:-1]], str):
            return ctx[tpl[1:-1]]
        try:
            return tpl.format_map(ctx)
        except KeyError as e:
            raise ApiError("шаблон", f"нет значения для {e} в «{tpl}»") from None
    if isinstance(tpl, dict):
        return {k: _fill(v, ctx) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [_fill(v, ctx) for v in tpl]
    return tpl


class HttpAdapter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        self.timeout = float(cfg.get("timeout_s", 30))
        self.retries = int(cfg.get("retries", 3))
        self.backoff = float(cfg.get("backoff_s", 0.5))
        self.headers = {"Accept": "application/json, */*"}
        auth = cfg.get("auth", {})
        if auth.get("type") == "bearer":
            token = os.environ.get(auth.get("token_env", "OILCHECK_TOKEN")) or auth.get("token")
            if not token:
                raise ApiError("auth", f"не задан токен (переменная {auth.get('token_env')})")
            self.headers["Authorization"] = f"Bearer {token}"
        elif auth.get("type") == "header":
            self.headers[auth["name"]] = os.environ.get(auth.get("value_env", ""), auth.get("value", ""))
        self.headers.update(cfg.get("headers", {}))

    # --- HTTP ---
    def request(self, step, method, path, body=None):
        url = path if path.startswith("http") else self.base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, resp.headers.get("Content-Type", ""), resp.read(), attempt
            except urllib.error.HTTPError as e:
                payload = e.read()
                if e.code in RETRY_CODES and attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff * attempt * (1 + random.random() / 2))
                    continue
                return e.code, e.headers.get("Content-Type", ""), payload, attempt
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff * attempt)
                    continue
                raise ApiError(step, f"{method} {url}: {getattr(e, 'reason', e)}") from None

    def version(self):
        v = self.cfg.get("version")
        if not v:
            return None
        try:
            code, _, body, _ = self.request("version", "GET", v.get("path", "/version"))
            if code == 200:
                return paths.get(json.loads(body), v.get("field", "")) if v.get("field") else body.decode()
        except ApiError:
            return None
        return None

    # --- сценарий ---
    def run(self, scenario, case_name):
        ctx = {"calc": scenario.calc, "case_name": case_name, "scenario_id": scenario.id,
               "export_format": scenario.export_format, "input": scenario.input}
        out = RunData()
        for step in self.cfg["steps"]:
            name = step.get("name", step["path"])
            t0 = time.perf_counter()
            path = _fill(step["path"], ctx)
            body = _fill(step["json"], ctx) if "json" in step else None
            expect = step.get("expect", [200, 201, 202])
            poll = step.get("poll")
            deadline = time.monotonic() + float((poll or {}).get("timeout_s", 0))
            while True:
                code, ctype, payload, retried = self.request(name, step.get("method", "GET"), path, body)
                out.retries += retried
                if code not in expect:
                    raise ApiError(name, f"HTTP {code}: {payload[:300].decode('utf-8', 'replace')}")
                if step.get("file"):
                    out.files[step["file"]] = (payload, ctype)
                    break
                try:
                    doc = json.loads(payload) if payload else {}
                except json.JSONDecodeError:
                    raise ApiError(name, f"ответ не JSON: {payload[:200]!r}") from None
                if poll:
                    status = paths.get(doc, poll.get("field", "status"))
                    if status in poll.get("fail", ["failed", "error"]):
                        raise ApiError(name, f"задача завершилась со статусом {status}: {doc.get('error', '')}")
                    if status not in poll.get("done", ["done"]):
                        if time.monotonic() > deadline:
                            raise ApiError(name, f"задача не завершилась за {poll.get('timeout_s')} с (статус {status})")
                        time.sleep(float(poll.get("interval_s", 0.5)))
                        continue
                for var, src in step.get("save", {}).items():
                    ctx[var] = paths.get(doc, src)
                for layer, src in step.get("layer", {}).items():
                    value = paths.get(doc, src)
                    if not isinstance(value, dict):
                        raise ApiError(name, f"в ответе нет объекта результата по пути «{src}»")
                    out.layers[layer] = value
                break
            out.timings[name] = round(time.perf_counter() - t0, 3)
        return out


def make_adapter(cfg):
    spec = cfg.get("python_adapter")
    if spec:
        mod, cls = spec.split(":")
        return getattr(importlib.import_module(mod), cls)(cfg)
    if not cfg.get("steps"):
        raise ApiError("config", "в api.toml нет шагов [[steps]]")
    return HttpAdapter(cfg)
