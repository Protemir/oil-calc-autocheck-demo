"""Чтение XLSX без внешних библиотек (zipfile + ElementTree).

Поддерживает sharedStrings, inlineStr, числа, булевы и формулы с кешем значения.
"""
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_REF = re.compile(r"([A-Z]+)(\d+)")


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(el):
    return "".join(t.text or "" for t in el.iter(f"{{{NS['m']}}}t"))


def read_xlsx(data):
    """Вернуть {имя листа: [[значения строки], ...]}."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = [_text(si) for si in root.findall("m:si", NS)]
    rels = {}
    rel_root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    for rel in rel_root.findall("pr:Relationship", NS):
        target = rel.get("Target")
        target = target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
        rels[rel.get("Id")] = posixpath.normpath(target)
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = {}
    for sh in wb.find("m:sheets", NS).findall("m:sheet", NS):
        path = rels[sh.get(f"{{{NS['r']}}}id")]
        root = ET.fromstring(z.read(path))
        rows = []
        for row in root.iter(f"{{{NS['m']}}}row"):
            values = {}
            for c in row.findall("m:c", NS):
                col = _col_index(_REF.match(c.get("r")).group(1)) if c.get("r") else len(values)
                t = c.get("t")
                v = c.find("m:v", NS)
                if t == "s":
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = _text(c.find("m:is", NS))
                elif t in ("str", "e"):
                    val = v.text if v is not None else None
                elif t == "b":
                    val = v is not None and v.text == "1"
                elif v is None or v.text is None:
                    val = None
                else:
                    num = float(v.text)
                    val = int(num) if num.is_integer() and re.fullmatch(r"-?\d+", v.text) else num
                values[col] = val
            width = max(values) + 1 if values else 0
            rows.append([values.get(i) for i in range(width)])
        sheets[sh.get("name")] = rows
    return sheets
