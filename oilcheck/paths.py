"""Плоские пути к полям результата и шаблоны для допусков.

  npv_musd                        скаляр
  cashflow[year=2029].net_cf_musd строка таблицы по ключевой колонке
  curve[3].q_m3d                  строка по индексу (если ключ не задан)

Шаблоны: «*» — любая часть, «[*]» — любая строка таблицы. Шаблон без точек
и скобок (например, q_m3d) сравнивается с именем поля в любом месте.
"""
import re

_ROW = re.compile(r"\[[^\]]*\]")


def fmt_key(v):
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        f = float(v)
        if f.is_integer():
            return str(int(f))
        return ("%.6f" % f).rstrip("0").rstrip(".")
    return str(v)


def flatten(obj, tables=None, prefix=""):
    tables = tables or {}
    out = {}
    for k, v in obj.items():
        if str(k).startswith("_"):
            continue
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, tables, p + "."))
        elif isinstance(v, list):
            key = tables.get(k)
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    if key and key in item:
                        label = f"{key}={fmt_key(item[key])}"
                    else:
                        label = str(i)
                    for f, fv in flatten(item, tables, "").items():
                        if f == key:
                            continue
                        out[f"{p}[{label}].{f}"] = fv
                else:
                    out[f"{p}[{i}]"] = item
        else:
            out[p] = v
    return out


def leaf(path):
    return path.rsplit(".", 1)[-1] if "." in path else _ROW.sub("", path)


def generic(path):
    return _ROW.sub("[*]", path)


_cache = {}


def _rx(pattern):
    rx = _cache.get(pattern)
    if rx is None:
        s = re.escape(pattern)
        s = s.replace(r"\[\*\]", r"\[[^\]]*\]").replace(r"\*", r".*")
        rx = re.compile("^" + s + "$")
        _cache[pattern] = rx
    return rx


def match(pattern, path):
    if "." not in pattern and "[" not in pattern:
        return bool(_rx(pattern).match(leaf(path)))
    return bool(_rx(pattern).match(path))


def get(obj, dotted):
    """Достать значение по пути вида a.b.0.c (пустой путь — весь объект)."""
    if dotted in ("", "$", None):
        return obj
    cur = obj
    for part in dotted.lstrip("$.").split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur
