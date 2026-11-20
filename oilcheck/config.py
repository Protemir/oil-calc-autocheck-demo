"""Загрузка набора сценариев (suite.toml + scenarios/*.toml) и проверка формата."""
import fnmatch
import glob
import json
import os
import tomllib
from dataclasses import dataclass, field

LAYERS = ("response", "saved", "export")
LAYER_TITLES = {
    "response": "Ответ расчёта",
    "saved": "Сохранённый результат",
    "export": "Файл выгрузки",
    "saved~response": "Сохранённый ↔ ответ",
    "export~saved": "Выгрузка ↔ сохранённый",
}


class ConfigError(Exception):
    pass


@dataclass
class Scenario:
    id: str
    title: str
    calc: str
    input: dict
    expected: dict
    expected_layers: dict = field(default_factory=dict)
    export_format: str = "json"
    tags: list = field(default_factory=list)
    tolerance: dict = field(default_factory=dict)
    layers: list = field(default_factory=lambda: list(LAYERS))
    source: str = ""


@dataclass
class Suite:
    name: str
    root: str
    scenarios: list
    tables: dict
    tolerance: dict
    rounding: dict
    rules: dict
    export: dict
    layers: list
    raw: dict


def _load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"нет файла {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: ошибка TOML: {e}") from None


def _check_tol(where, tol):
    for pat, t in (tol or {}).items():
        if not isinstance(t, dict) or not set(t) <= {"abs", "rel"}:
            raise ConfigError(f"{where}: допуск для «{pat}» должен быть {{abs=..., rel=...}}")


def load_suite(path, only=None, tags=None, need_expected=True):
    suite_file = os.path.join(path, "suite.toml") if os.path.isdir(path) else path
    root = os.path.dirname(os.path.abspath(suite_file))
    raw = _load_toml(suite_file)
    tol = raw.get("tolerance", {})
    default = tol.get("default", {"abs": 1e-9, "rel": 1e-9})
    _check_tol("suite.toml [tolerance.fields]", tol.get("fields"))
    suite = Suite(
        name=raw.get("name", os.path.basename(root)),
        root=root,
        scenarios=[],
        tables=raw.get("tables", {}),
        tolerance={"default": default, "fields": tol.get("fields", {}), "layers": tol.get("layers", {})},
        rounding=raw.get("rounding", {}),
        rules=raw.get("rules", {}),
        export=raw.get("export", {}),
        layers=raw.get("layers", list(LAYERS)),
        raw=raw,
    )
    patterns = raw.get("scenarios", ["scenarios/*.toml"])
    files = sorted({f for p in patterns for f in glob.glob(os.path.join(root, p))})
    if not files:
        raise ConfigError(f"в наборе {root} не найдено сценариев по шаблонам {patterns}")
    seen = set()
    for fpath in files:
        sc = load_scenario(fpath, suite, need_expected)
        if sc.id in seen:
            raise ConfigError(f"повторяется id сценария {sc.id} ({fpath})")
        seen.add(sc.id)
        if only and not any(fnmatch.fnmatch(sc.id, o) for o in only):
            continue
        if tags and not set(tags) & set(sc.tags):
            continue
        suite.scenarios.append(sc)
    return suite


def load_scenario(fpath, suite, need_expected=True):
    raw = _load_toml(fpath)
    for key in ("id", "calc", "input"):
        if key not in raw:
            raise ConfigError(f"{fpath}: нет обязательного поля «{key}»")
    expected = raw.get("expected")
    if "expected_file" in raw:
        epath = os.path.join(os.path.dirname(fpath), raw["expected_file"])
        try:
            with open(epath, encoding="utf-8") as f:
                expected = json.load(f)
        except FileNotFoundError:
            if need_expected:
                raise ConfigError(f"{fpath}: нет файла эталона {epath}") from None
            expected = {}
    if expected is None and not need_expected:
        expected = {}
    if expected is None:
        raise ConfigError(f"{fpath}: нет эталона ([expected] или expected_file)")
    expected = dict(expected)
    layer_over = expected.pop("_layers", {})
    layer_over.update(raw.get("expected_layers", {}))
    for name in layer_over:
        if name not in LAYERS:
            raise ConfigError(f"{fpath}: неизвестное место проверки «{name}» (есть {', '.join(LAYERS)})")
    tol = raw.get("tolerance", {})
    _check_tol(f"{fpath} [tolerance.fields]", tol.get("fields"))
    fmt = raw.get("export_format", suite.export.get("default_format", "json"))
    if fmt not in ("csv", "xlsx", "json"):
        raise ConfigError(f"{fpath}: формат выгрузки {fmt} не поддерживается (csv, xlsx, json)")
    return Scenario(
        id=str(raw["id"]),
        title=raw.get("title", raw["id"]),
        calc=raw["calc"],
        input=raw["input"],
        expected=expected,
        expected_layers=layer_over,
        export_format=fmt,
        tags=list(raw.get("tags", [])),
        tolerance=tol,
        layers=raw.get("layers", suite.layers),
        source=os.path.relpath(fpath, suite.root),
    )


def load_api(path):
    raw = _load_toml(path)
    if "base_url" not in raw:
        raise ConfigError(f"{path}: нет base_url")
    env_url = os.environ.get("OILCHECK_BASE_URL")
    if env_url:
        raw["base_url"] = env_url
    return raw
