"""Разбор файла выгрузки (CSV / XLSX / JSON) в ту же структуру, что и ответ API.

Формат описывается в suite.toml, секция [export]:
  columns      — заголовок колонки -> код поля («Дебит, м3/сут» = "q_m3d")
  csv          — delimiter, decimal, маркер блока, колонки кода и значения в сводке
  xlsx.sheets  — имя листа -> имя таблицы; summary_sheet — лист сводки
  json         — где лежат сводка и таблицы
"""
import csv
import io
import json
import re

from . import paths
from .xlsx import read_xlsx

_INT = re.compile(r"^-?\d+$")
_NUM = re.compile(r"^-?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?$")


class ExportError(Exception):
    pass


def parse_number(text, decimal=","):
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    s = text.strip().replace(" ", "").replace(" ", "")
    if s == "":
        return None
    if _INT.match(s):
        return int(s)
    if decimal == ",":
        s = s.replace(",", ".")
    if _NUM.match(s):
        return float(s)
    return text.strip()


def _map_header(h, columns):
    h = (h or "").strip()
    return columns.get(h, h)


def _summary_rows(rows, cfg, decimal):
    header = [str(c or "").strip() for c in rows[0]]
    code_col = cfg.get("code_column", "Код")
    val_col = cfg.get("value_column", "Значение")
    if code_col not in header or val_col not in header:
        raise ExportError(f"в сводке нет колонок «{code_col}»/«{val_col}»: {header}")
    ci, vi = header.index(code_col), header.index(val_col)
    out = {}
    for row in rows[1:]:
        if len(row) <= max(ci, vi) or not row[ci]:
            continue
        out[str(row[ci]).strip()] = parse_number(row[vi], decimal)
    return out


def _table_rows(rows, columns, decimal):
    header = [_map_header(str(c or ""), columns) for c in rows[0]]
    table = []
    for row in rows[1:]:
        if not any(c not in (None, "") for c in row):
            continue
        table.append({header[i]: parse_number(row[i], decimal) for i in range(min(len(header), len(row)))})
    return table


def parse_csv(data, cfg):
    c = cfg.get("csv", {})
    text = data.decode(c.get("encoding", "utf-8-sig"))
    delim = c.get("delimiter", ";")
    decimal = c.get("decimal", ",")
    marker = c.get("block_marker", "## ")
    summary_name = c.get("summary_block", "Сводка")
    columns = cfg.get("columns", {})
    blocks, name, cur = {}, None, []
    for line in text.splitlines():
        if line.startswith(marker):
            if name is not None:
                blocks[name] = cur
            name, cur = line[len(marker):].strip(), []
        elif line.strip():
            cur.append(line)
    if name is not None:
        blocks[name] = cur
    if not blocks:  # файл без блоков — одна таблица
        blocks[c.get("table", "rows")] = [ln for ln in text.splitlines() if ln.strip()]
    result = {}
    for bname, lines in blocks.items():
        rows = list(csv.reader(lines, delimiter=delim))
        if not rows:
            continue
        if bname == summary_name:
            result.update(_summary_rows(rows, c, decimal))
        else:
            result[bname] = _table_rows(rows, columns, decimal)
    return result


def parse_xlsx(data, cfg):
    x = cfg.get("xlsx", {})
    columns = cfg.get("columns", {})
    sheets = read_xlsx(data)
    summary_sheet = x.get("summary_sheet", "Сводка")
    mapping = x.get("sheets", {})
    result = {}
    for sname, rows in sheets.items():
        rows = [r for r in rows if any(v not in (None, "") for v in r)]
        if not rows:
            continue
        if sname == summary_sheet:
            result.update(_summary_rows(rows, x, "."))
        else:
            result[mapping.get(sname, sname)] = _table_rows(rows, columns, ".")
    return result


def parse_json(data, cfg):
    j = cfg.get("json", {})
    doc = json.loads(data.decode("utf-8"))
    summary = paths.get(doc, j.get("summary", "summary"))
    tables = paths.get(doc, j.get("tables", "tables"))
    if summary is None and tables is None:
        return doc
    result = dict(summary or {})
    result.update(tables or {})
    return result


def parse_export(fmt, data, cfg):
    try:
        if fmt == "csv":
            return parse_csv(data, cfg)
        if fmt == "xlsx":
            return parse_xlsx(data, cfg)
        return parse_json(data, cfg)
    except ExportError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ExportError(f"не удалось разобрать выгрузку {fmt}: {e}") from e
