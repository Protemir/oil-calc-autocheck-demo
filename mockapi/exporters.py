"""Файлы выгрузки мок-продукта: CSV (Excel-русская локаль), XLSX, JSON.

Округление в выгрузке — как в «документации продукта» (EXPORT_ROUNDING).
Тот же список в suites/demo/suite.toml, чтобы сверщик учитывал округление.
"""
import io
import json
import zipfile
from xml.sax.saxutils import escape

# поле -> знаков после запятой в табличной выгрузке
EXPORT_ROUNDING = {
    "pi_m3d_atm": 4, "q_bubble_m3d": 2, "aof_m3d": 2, "pwf_atm": 1, "q_m3d": 2,
    "eur_kt": 3, "rate_end_t_d": 2, "rate_t_d": 2, "monthly_t": 1, "cum_kt": 3,
    "npv_musd": 3, "irr_pct": 2, "payback_years": 2, "pi": 3,
    "revenue_musd": 3, "met_musd": 3, "rent_tax_musd": 3, "opex_musd": 3,
    "capex_musd": 3, "cit_musd": 3, "net_cf_musd": 3, "disc_cf_musd": 3,
    "pore_volume_mln_m3": 4, "stoiip_mln_m3": 4, "stoiip_mln_t": 4, "recoverable_mln_t": 4,
    "velocity_m_s": 3, "reynolds": 0, "friction_factor": 5,
    "dp_friction_bar": 3, "dp_static_bar": 3, "dp_total_bar": 3,
}

LABELS = {
    "pi_m3d_atm": ("Коэффициент продуктивности", "м3/(сут·атм)"),
    "q_bubble_m3d": ("Дебит при Pнас", "м3/сут"),
    "aof_m3d": ("Потенциальный дебит (AOF)", "м3/сут"),
    "eur_kt": ("Конечная добыча (EUR)", "тыс. т"),
    "rate_end_t_d": ("Дебит на конец прогноза", "т/сут"),
    "npv_musd": ("ЧДД (NPV)", "млн $"),
    "irr_pct": ("ВНД (IRR)", "%"),
    "payback_years": ("Срок окупаемости", "лет"),
    "pi": ("Индекс доходности", ""),
}

TABLE_HEADERS = {
    "curve": [("pwf_atm", "Pзаб, атм"), ("q_m3d", "Дебит, м3/сут")],
    "forecast": [("month", "Месяц"), ("rate_t_d", "Дебит, т/сут"),
                 ("monthly_t", "Добыча за месяц, т"), ("cum_kt", "Накопл. добыча, тыс. т")],
    "cashflow": [("year", "Год"), ("revenue_musd", "Выручка, млн $"), ("met_musd", "НДПИ, млн $"),
                 ("rent_tax_musd", "Рентный налог, млн $"), ("opex_musd", "OPEX, млн $"),
                 ("capex_musd", "CAPEX, млн $"), ("cit_musd", "КПН, млн $"),
                 ("net_cf_musd", "ЧДП, млн $"), ("disc_cf_musd", "ДЧДП, млн $")],
}

SHEET_NAMES = {"cashflow": "Денежный поток", "curve": "ИК", "forecast": "Прогноз"}


def rnd(field, value, override=None):
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    d = (override or {}).get(field, EXPORT_ROUNDING.get(field))
    if d is None:
        return value
    v = round(value, d)
    return int(v) if d == 0 else v


def split(result):
    scalars = {k: v for k, v in result.items() if not isinstance(v, list)}
    tables = {k: v for k, v in result.items() if isinstance(v, list)}
    return scalars, tables


def _num_ru(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return repr(v).replace(".", ",") if isinstance(v, float) else str(v)


def to_csv(result, rc_bugs=False):
    scalars, tables = split(result)
    out = io.StringIO()
    out.write("﻿")
    out.write("## Сводка\n")
    out.write("Код;Параметр;Значение;Ед. изм.\n")
    for k, v in scalars.items():
        label, unit = LABELS.get(k, (k, ""))
        out.write(f"{k};{label};{_num_ru(rnd(k, v))};{unit}\n")
    for name, rows in tables.items():
        cols = TABLE_HEADERS[name]
        out.write("\n")
        out.write(f"## {name}\n")
        out.write(";".join(h for _, h in cols) + "\n")
        page = 36
        written = 0
        start = 0
        while written < len(rows):
            chunk = rows[start:start + page]
            for row in chunk:
                if written >= len(rows):
                    break
                written += 1
                vals = []
                for f, _ in cols:
                    # нумерацию месяцев пишет счётчик, значения — из строки
                    v = written if f == "month" else rnd(f, row[f])
                    vals.append(_num_ru(v))
                out.write(";".join(vals) + "\n")
            # Регрессия RC: следующая «страница» начинается с последней строки предыдущей.
            start += page - 1 if rc_bugs else page
    return out.getvalue().encode("utf-8")


def to_json(result, meta):
    scalars, tables = split(result)
    body = {
        "meta": meta,
        "summary": {k: rnd(k, v) for k, v in scalars.items()},
        "tables": {n: [{f: rnd(f, v) for f, v in row.items()} for row in rows] for n, rows in tables.items()},
    }
    return json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")


# ---------- минимальный XLSX-писатель (inline strings) ----------

def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, v):
    if v is None:
        return f'<c r="{ref}"/>'
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f'<c r="{ref}"><v>{repr(float(v)) if isinstance(v, float) else v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'


def _sheet(rows):
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for r, row in enumerate(rows, start=1):
        xml.append(f'<row r="{r}">' + "".join(_cell(f"{_col(c)}{r}", v) for c, v in enumerate(row)) + "</row>")
    xml.append("</sheetData></worksheet>")
    return "".join(xml)


def write_xlsx(sheets):
    """sheets: list of (name, rows)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
              '<Default Extension="xml" ContentType="application/xml"/>',
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
        for i in range(len(sheets)):
            ct.append(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        ct.append("</Types>")
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
        rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        for i, (name, rows) in enumerate(sheets):
            wb.append(f'<sheet name="{escape(name)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>')
            rels.append(f'<Relationship Id="rId{i + 1}" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                        f'Target="worksheets/sheet{i + 1}.xml"/>')
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet(rows))
        wb.append("</sheets></workbook>")
        rels.append("</Relationships>")
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
    return buf.getvalue()


def to_xlsx(result, rc_bugs=False):
    scalars, tables = split(result)
    summary = [["Код", "Параметр", "Значение", "Ед. изм."]]
    for k, v in scalars.items():
        label, unit = LABELS.get(k, (k, ""))
        summary.append([k, label, rnd(k, v), unit])
    sheets = [("Сводка", summary)]
    # Регрессия RC: рентный налог в Excel округляется до целых.
    override = {"rent_tax_musd": 0} if rc_bugs else None
    for name, rows in tables.items():
        cols = TABLE_HEADERS[name]
        data = [[h for _, h in cols]]
        for row in rows:
            data.append([rnd(f, row[f], override) for f, _ in cols])
        sheets.append((SHEET_NAMES.get(name, name), data))
    return write_xlsx(sheets)
