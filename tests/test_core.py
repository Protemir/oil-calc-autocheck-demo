"""Автотесты инструмента: python -m unittest discover -s tests -v"""
import io
import os
import sys
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mockapi import exporters, server  # noqa: E402
from oilcheck import compare, paths  # noqa: E402
from oilcheck.adapter import make_adapter  # noqa: E402
from oilcheck.config import load_api, load_suite  # noqa: E402
from oilcheck.exports import parse_csv, parse_number, parse_xlsx  # noqa: E402
from oilcheck.runner import run_suite  # noqa: E402
from oilcheck.xlsx import read_xlsx  # noqa: E402

SUITE = os.path.join(ROOT, "suites", "demo")
SUITE_CFG = load_suite(SUITE)


class PathsTest(unittest.TestCase):
    def test_flatten_by_key_column(self):
        flat = paths.flatten({"npv": 1.5, "cashflow": [{"year": 2027, "net": -3.0}, {"year": 2028, "net": 4.0}]},
                             {"cashflow": "year"})
        self.assertEqual(flat, {"npv": 1.5, "cashflow[year=2027].net": -3.0, "cashflow[year=2028].net": 4.0})

    def test_flatten_by_index_without_key(self):
        flat = paths.flatten({"rows": [{"a": 1}, {"a": 2}]})
        self.assertEqual(flat, {"rows[0].a": 1, "rows[1].a": 2})

    def test_float_key_formatting(self):
        flat = paths.flatten({"curve": [{"pwf_atm": 120.0, "q": 1}, {"pwf_atm": 87.5, "q": 2}]}, {"curve": "pwf_atm"})
        self.assertIn("curve[pwf_atm=120].q", flat)
        self.assertIn("curve[pwf_atm=87.5].q", flat)

    def test_patterns(self):
        self.assertTrue(paths.match("q_m3d", "curve[pwf_atm=30].q_m3d"))
        self.assertTrue(paths.match("*_musd", "cashflow[year=2030].opex_musd"))
        self.assertTrue(paths.match("forecast[*].monthly_t", "forecast[month=7].monthly_t"))
        self.assertFalse(paths.match("forecast[*].monthly_t", "forecast[month=7].cum_kt"))
        self.assertFalse(paths.match("npv_musd", "irr_pct"))


class CompareTest(unittest.TestCase):
    def test_abs_or_rel(self):
        self.assertTrue(compare.compare_value("response", "x", 100.0, 100.4, 0.5, 0).ok)
        self.assertFalse(compare.compare_value("response", "x", 100.0, 100.6, 0.5, 0).ok)
        self.assertTrue(compare.compare_value("response", "x", 100.0, 100.9, 0.5, 0.01).ok)   # rel 1 % = 1.0

    def test_combine_all_takes_stricter(self):
        self.assertFalse(compare.compare_value("response", "x", 100.0, 100.9, 0.5, 0.01, combine="all").ok)

    def test_rounding_adds_half_unit(self):
        # эталон 1.23456, в выгрузке 1.23 (2 знака) — это не ошибка
        self.assertTrue(compare.compare_value("export", "x", 1.23456, 1.23, 1e-6, 1e-6, decimals=2).ok)
        # а 1.24 — уже ошибка
        self.assertFalse(compare.compare_value("export", "x", 1.23456, 1.24, 1e-6, 1e-6, decimals=2).ok)

    def test_missing_and_text(self):
        self.assertEqual(compare.compare_value("saved", "x", 1.0, None, 0, 0).kind, "missing")
        self.assertTrue(compare.compare_value("saved", "r", "turbulent", "turbulent", 0, 0).ok)
        self.assertFalse(compare.compare_value("saved", "r", "turbulent", "laminar", 0, 0).ok)

    def test_delta_sign_and_relative(self):
        c = compare.compare_value("response", "x", 200.0, 210.0, 0, 0)
        self.assertEqual(c.delta, 10.0)
        self.assertAlmostEqual(c.rel_delta, 0.05)

    def test_origin_is_first_layer_in_data_flow(self):
        checks = [compare.Check("export", "a", 1, 2, False), compare.Check("saved", "a", 1, 2, False),
                  compare.Check("response", "a", 1, 1, True), compare.Check("export", "b", 1, 2, False)]
        self.assertEqual(compare.origins(checks), {"a": "saved", "b": "export"})

    def test_field_tolerance_overrides(self):
        sc = SUITE_CFG.scenarios[0]
        self.assertEqual(compare.tolerance(SUITE_CFG, sc, "cashflow[year=2030].opex_musd", "export"), (0.0005, 1e-6))
        self.assertEqual(compare.tolerance(SUITE_CFG, sc, "irr_pct", "response"), (0.001, 1e-6))
        self.assertEqual(compare.rounding(SUITE_CFG, "curve[pwf_atm=30].q_m3d", "export"), 2)
        self.assertIsNone(compare.rounding(SUITE_CFG, "curve[pwf_atm=30].q_m3d", "saved"))


class ExportParseTest(unittest.TestCase):
    def test_number_locale(self):
        self.assertEqual(parse_number("1 234,5"), 1234.5)
        self.assertEqual(parse_number("42"), 42)
        self.assertEqual(parse_number("-1,5e-3"), -0.0015)
        self.assertEqual(parse_number("turbulent"), "turbulent")
        self.assertIsNone(parse_number(""))

    def test_csv_blocks(self):
        res = {"aof_m3d": 238.8095238, "curve": [{"pwf_atm": 0.0, "q_m3d": 238.8095238},
                                                 {"pwf_atm": 30.0, "q_m3d": 230.7142857}]}
        parsed = parse_csv(exporters.to_csv(res), SUITE_CFG.export)
        self.assertEqual(parsed["aof_m3d"], 238.81)
        self.assertEqual(parsed["curve"][1], {"pwf_atm": 30, "q_m3d": 230.71})

    def test_xlsx_roundtrip(self):
        data = exporters.write_xlsx([("Сводка", [["Код", "Значение"], ["npv_musd", 12.345]]),
                                     ("Лист 2", [["Год", "ЧДП, млн $"], [2027, -1.5], [2028, None]])])
        sheets = read_xlsx(data)
        self.assertEqual(sheets["Сводка"][1], ["npv_musd", 12.345])
        self.assertEqual(sheets["Лист 2"][1], [2027, -1.5])

    def test_xlsx_shared_strings_from_excel(self):
        """Файлы из настоящего Excel хранят строки в sharedStrings.xml."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                       '<sheet name="Сводка" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
                       '2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
                       '</Relationships>')
            z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<si><t>Код</t></si><si><t>Значение</t></si><si><t>pi</t></si></sst>')
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
                       '2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c>'
                       '</row><row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>1.25</v></c></row>'
                       '</sheetData></worksheet>')
        self.assertEqual(parse_xlsx(buf.getvalue(), SUITE_CFG.export), {"pi": 1.25})


class SuiteTest(unittest.TestCase):
    def test_demo_suite_is_valid(self):
        self.assertEqual(len(SUITE_CFG.scenarios), 20)
        self.assertEqual({s.export_format for s in SUITE_CFG.scenarios}, {"csv", "json", "xlsx"})


class EndToEndTest(unittest.TestCase):
    """Полный прогон набора через мок API: эталонный релиз и релиз с регрессиями."""

    @classmethod
    def setUpClass(cls):
        server.CFG.update(latency=0.05, token="demo-token")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["OILCHECK_BASE_URL"] = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.adapter = make_adapter(load_api(os.path.join(ROOT, "api.toml")))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        os.environ.pop("OILCHECK_BASE_URL", None)

    def run_release(self, release):
        server.CFG["release"] = release
        return run_suite(load_suite(SUITE), self.adapter, workers=8)

    def test_stable_release_passes(self):
        rep = self.run_release("3.7.0")
        self.assertEqual(rep.counts, {"pass": 20, "fail": 0, "error": 0})

    def test_rc_regressions_found_on_right_layer(self):
        rep = self.run_release("3.8.0-rc1")
        got = {r.id: sorted(set(r.origin.values())) for r in rep.results if r.status != "pass"}
        self.assertEqual(got, {
            "IPR-03": ["response"],                 # ошибка в формуле — видна сразу в ответе
            "VOL-03": ["saved"], "PIPE-02": ["saved"], "PIPE-04": ["saved"],   # маппер сохранения
            "DCA-03": ["export"], "DCA-04": ["export"], "ECO-02": ["export"],  # выгрузка
        })

    def test_retries_survive_flaky_api(self):
        server.CFG["flaky"] = 0.1
        try:
            rep = self.run_release("3.7.0")
        finally:
            server.CFG["flaky"] = 0.0
        self.assertEqual(rep.counts["pass"], 20)
        self.assertGreater(sum(r.retries for r in rep.results), 0)


if __name__ == "__main__":
    unittest.main()
