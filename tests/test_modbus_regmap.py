"""Tests for modbus_regmap. Run: python -m unittest discover -s tests -v"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modbus_regmap as mrm

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "registers.csv"

GOOD_CSV = """\
name,address,type,access,unit,description
voltage,0,uint16,ro,V,Phase voltage
power,1,int32,ro,W,Active power
mode,3,uint16,rw,,Operating mode
"""

OVERLAP_CSV = """\
name,address,type,access,unit,description
a,0,int32,ro,,first
b,1,uint16,ro,,second
"""

DUP_CSV = """\
name,address,type,access,unit,description
a,0,uint16,ro,,
a,1,uint16,ro,,
"""

BAD_TYPE_CSV = """\
name,address,type,access,unit,description
a,0,double,ro,,
"""

#: same registers as GOOD_CSV, but power's unit and mode's address changed,
#: plus one extra register — used for diff tests
REVISED_CSV = """\
name,address,type,access,unit,description
voltage,0,uint16,ro,V,Phase voltage
power,1,int32,ro,kW,Active power
mode,4,uint16,rw,,Operating mode
energy,8,uint32,ro,kWh,Total energy
"""


def write_tmp(text: str) -> str:
    fh = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
    )
    fh.write(text)
    fh.close()
    return fh.name


class TestLoad(unittest.TestCase):
    def test_load_example(self):
        regmap = mrm.load_register_map(EXAMPLE)
        self.assertEqual(len(regmap.registers), 8)
        self.assertEqual(regmap.registers[0].name, "grid_voltage")

    def test_missing_column(self):
        path = write_tmp("name,address\na,0\n")
        with self.assertRaises(ValueError):
            mrm.load_register_map(path)

    def test_bad_address(self):
        path = write_tmp("name,address,type,access\na,xyz,uint16,ro\n")
        with self.assertRaises(ValueError):
            mrm.load_register_map(path)


class TestValidate(unittest.TestCase):
    def test_good_map_is_valid(self):
        regmap = mrm.load_register_map(write_tmp(GOOD_CSV))
        self.assertEqual(regmap.validate(), [])

    def test_example_map_is_valid(self):
        regmap = mrm.load_register_map(EXAMPLE)
        self.assertEqual(regmap.validate(), [])

    def test_overlap_detected(self):
        errors = mrm.load_register_map(write_tmp(OVERLAP_CSV)).validate()
        self.assertTrue(any("overlap" in e for e in errors))

    def test_duplicate_name_detected(self):
        errors = mrm.load_register_map(write_tmp(DUP_CSV)).validate()
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_unknown_type_detected(self):
        errors = mrm.load_register_map(write_tmp(BAD_TYPE_CSV)).validate()
        self.assertTrue(any("unknown type" in e for e in errors))


class TestExport(unittest.TestCase):
    def setUp(self):
        self.regmap = mrm.load_register_map(EXAMPLE)

    def test_json_roundtrip(self):
        data = json.loads(self.regmap.to_json())
        self.assertEqual(data["count"], 8)
        by_name = {r["name"]: r for r in data["registers"]}
        self.assertEqual(by_name["active_power"]["width"], 2)
        self.assertEqual(by_name["active_power"]["address"], 2)

    def test_c_header(self):
        header = self.regmap.to_c_header(prefix="METER")
        self.assertIn("#define METER_GRID_VOLTAGE_ADDR   (0u)", header)
        self.assertIn("#define METER_ACTIVE_POWER_WIDTH  (2u)", header)
        self.assertIn("#define METER_COUNT (8u)", header)
        self.assertIn("#ifndef METER_H", header)

    def test_markdown(self):
        doc = self.regmap.to_markdown()
        self.assertIn("| `grid_voltage` | 0 | uint16 | ro | V |", doc)
        self.assertIn("# Modbus Register Map", doc)


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.old = mrm.load_register_map(write_tmp(GOOD_CSV))
        self.new = mrm.load_register_map(write_tmp(REVISED_CSV))

    def test_identical_maps(self):
        diff = mrm.diff_maps(self.old, self.old)
        self.assertTrue(diff.identical)
        self.assertEqual(diff.unchanged, 3)
        self.assertIn("no differences", diff.to_text())

    def test_added_and_changed(self):
        diff = mrm.diff_maps(self.old, self.new)
        self.assertFalse(diff.identical)
        self.assertEqual([r.name for r in diff.added], ["energy"])
        self.assertEqual(diff.removed, [])
        changed = {c.name: c.fields for c in diff.changed}
        self.assertEqual(changed["power"], ["unit: W -> kW"])
        self.assertEqual(changed["mode"], ["address: 3 -> 4"])
        self.assertEqual(diff.unchanged, 1)  # voltage

    def test_removed(self):
        one_reg = mrm.load_register_map(
            write_tmp(
                "name,address,type,access,unit,description\n"
                "voltage,0,uint16,ro,V,Phase voltage\n"
            )
        )
        diff = mrm.diff_maps(self.old, one_reg)
        self.assertEqual(sorted(r.name for r in diff.removed), ["mode", "power"])
        self.assertEqual(diff.added, [])

    def test_text_output(self):
        text = mrm.diff_maps(self.old, self.new).to_text()
        self.assertIn("added (1):", text)
        self.assertIn("+ energy @ 8 (uint32, ro, kWh)", text)
        self.assertIn("~ power @ 1: unit: W -> kW", text)
        self.assertIn("unchanged: 1", text)

    def test_json_output(self):
        data = json.loads(mrm.diff_maps(self.old, self.new).to_json())
        self.assertFalse(data["identical"])
        self.assertEqual(data["added"][0]["name"], "energy")
        self.assertEqual(data["changed"][0]["changes"], ["unit: W -> kW"])
        self.assertEqual(data["unchanged"], 1)


class TestGaps(unittest.TestCase):
    def setUp(self):
        # GOOD_CSV occupies address 0, span 1-2 (int32) and address 3
        self.regmap = mrm.load_register_map(write_tmp(GOOD_CSV))

    def test_free_ranges_default_window(self):
        self.assertEqual(self.regmap.free_ranges(), [(4, mrm.MAX_ADDRESS)])

    def test_free_ranges_narrow_window(self):
        self.assertEqual(self.regmap.free_ranges(0, 10), [(4, 10)])

    def test_free_ranges_window_inside_span(self):
        # window 2-3 starts inside power's int32 span (1-2)
        self.assertEqual(self.regmap.free_ranges(2, 3), [])

    def test_single_register_gap(self):
        regmap = mrm.RegisterMap(
            registers=[
                mrm.Register(name="a", address=0, type="uint16", access="ro"),
                mrm.Register(name="b", address=2, type="uint16", access="ro"),
            ]
        )
        self.assertEqual(regmap.free_ranges(0, 2), [(1, 1)])

    def test_empty_map(self):
        self.assertEqual(mrm.RegisterMap().free_ranges(10, 12), [(10, 12)])

    def test_example_map_gaps(self):
        regmap = mrm.load_register_map(EXAMPLE)
        self.assertEqual(
            regmap.free_ranges(0, 25),
            [(6, 7), (9, 15), (19, 19), (21, 25)],
        )


class TestCli(unittest.TestCase):
    def test_validate_ok(self):
        self.assertEqual(mrm.main(["validate", str(EXAMPLE)]), 0)

    def test_validate_fails_on_overlap(self):
        with self.assertRaises(SystemExit):
            mrm._validate_or_exit(mrm.load_register_map(write_tmp(OVERLAP_CSV)))

    def test_json_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "map.json"
            self.assertEqual(mrm.main(["json", str(EXAMPLE), "-o", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["count"], 8)

    def test_diff_identical_exits_0(self):
        path = write_tmp(GOOD_CSV)
        self.assertEqual(mrm.main(["diff", path, path]), 0)

    def test_diff_different_exits_1(self):
        old_path = write_tmp(GOOD_CSV)
        new_path = write_tmp(REVISED_CSV)
        self.assertEqual(mrm.main(["diff", old_path, new_path]), 1)
        self.assertEqual(mrm.main(["diff", old_path, new_path, "--json"]), 1)

    def test_gaps_exits_0(self):
        self.assertEqual(mrm.main(["gaps", str(EXAMPLE)]), 0)

    def test_gaps_bad_window_exits_2(self):
        self.assertEqual(
            mrm.main(["gaps", str(EXAMPLE), "--from", "10", "--to", "5"]), 2
        )

    def test_gaps_output(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mrm.main(["gaps", str(EXAMPLE), "--from", "0", "--to", "25"])
        out = buf.getvalue()
        self.assertIn("6-7 (2 registers)", out)
        self.assertIn("  19 (1 register)", out)
        self.assertIn("total free: 15 registers", out)

    def test_gaps_full_window_output(self):
        # every address in the window is occupied
        regmap_csv = write_tmp(GOOD_CSV)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mrm.main(["gaps", regmap_csv, "--from", "0", "--to", "3"])
        self.assertIn("no free addresses in range 0-3", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
