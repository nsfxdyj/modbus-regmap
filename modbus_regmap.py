#!/usr/bin/env python3
"""modbus-regmap — a small toolkit for Modbus register maps.

Parse a CSV register map (the way most device manuals describe their
registers), validate it, and export it as JSON, a C header, or a
Markdown document.

CSV columns (header row required):

    name,address,type,access,unit,description

  - name        symbolic register name (letters, digits, underscore)
  - address     register address as written in the device manual (0..65535)
  - type        int16 | uint16 | int32 | uint32 | float32
  - access      ro | rw | wo
  - unit        optional engineering unit, e.g. "V", "A", "kWh"
  - description optional free text

16-bit types occupy one holding register, 32-bit types occupy two.

Standard library only; works with Python 3.8+.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

__version__ = "0.1.0"

#: register width (number of 16-bit registers) per data type
TYPE_WIDTHS = {
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
}

VALID_ACCESS = {"ro", "rw", "wo"}
MAX_ADDRESS = 65535
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

REQUIRED_COLUMNS = ["name", "address", "type", "access"]
OPTIONAL_COLUMNS = ["unit", "description"]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass
class Register:
    """One entry of a register map."""

    name: str
    address: int
    type: str
    access: str
    unit: str = ""
    description: str = ""

    @property
    def width(self) -> int:
        """How many consecutive 16-bit registers this value occupies."""
        return TYPE_WIDTHS[self.type]

    @property
    def end_address(self) -> int:
        return self.address + self.width - 1

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "address": self.address,
            "type": self.type,
            "access": self.access,
            "unit": self.unit,
            "description": self.description,
            "width": self.width,
        }

    def c_name(self, prefix: str = "REGMAP") -> str:
        """SCREAMING_SNAKE name used in generated C headers."""
        return f"{prefix}_{self.name.upper()}"


@dataclass
class RegisterMap:
    """A validated (or to-be-validated) register map."""

    registers: List[Register] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    def validate(self) -> List[str]:
        """Return a list of human-readable problems (empty == valid)."""
        errors: List[str] = []
        seen_names = set()
        spans = []  # (start, end, name)

        for reg in self.registers:
            label = f"register '{reg.name}'"

            if not NAME_RE.match(reg.name):
                errors.append(
                    f"{label}: invalid name; use letters, digits and "
                    "underscore, not starting with a digit"
                )
            if reg.name in seen_names:
                errors.append(f"{label}: duplicate name")
            seen_names.add(reg.name)

            if reg.type not in TYPE_WIDTHS:
                errors.append(
                    f"{label}: unknown type '{reg.type}' "
                    f"(expected one of {', '.join(sorted(TYPE_WIDTHS))})"
                )
                continue  # width unknown, skip span checks

            if reg.access not in VALID_ACCESS:
                errors.append(
                    f"{label}: invalid access '{reg.access}' "
                    "(expected ro, rw or wo)"
                )

            if not 0 <= reg.address <= MAX_ADDRESS:
                errors.append(
                    f"{label}: address {reg.address} out of range 0..{MAX_ADDRESS}"
                )
            elif reg.end_address > MAX_ADDRESS:
                errors.append(
                    f"{label}: {reg.type} at address {reg.address} exceeds "
                    f"max address {MAX_ADDRESS}"
                )

            spans.append((reg.address, reg.end_address, reg.name))

        # overlap detection, O(n log n)
        spans.sort()
        for (s1, e1, n1), (s2, e2, n2) in zip(spans, spans[1:]):
            if s2 <= e1:
                errors.append(
                    f"registers '{n1}' and '{n2}' overlap "
                    f"({s1}-{e1} vs {s2}-{e2})"
                )
        return errors

    # ------------------------------------------------------------------ #
    # exporters
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "count": len(self.registers),
            "registers": [r.to_dict() for r in self.registers],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_c_header(self, prefix: str = "REGMAP") -> str:
        guard = f"{prefix}_H"
        out = io.StringIO()
        out.write("/* Auto-generated by modbus-regmap. Do not edit by hand. */\n")
        out.write(f"#ifndef {guard}\n#define {guard}\n\n")
        out.write("#ifdef __cplusplus\nextern \"C\" {\n#endif\n\n")
        for reg in sorted(self.registers, key=lambda r: r.address):
            unit = f" | unit: {reg.unit}" if reg.unit else ""
            desc = f" — {reg.description}" if reg.description else ""
            out.write(
                f"/* {reg.name}: type {reg.type}, access {reg.access}{unit}{desc} */\n"
            )
            cname = reg.c_name(prefix)
            out.write(f"#define {cname}_ADDR   ({reg.address}u)\n")
            out.write(f"#define {cname}_WIDTH  ({reg.width}u)\n\n")
        out.write(f"#define {prefix}_COUNT ({len(self.registers)}u)\n\n")
        out.write("#ifdef __cplusplus\n}\n#endif\n\n")
        out.write(f"#endif /* {guard} */\n")
        return out.getvalue()

    def to_markdown(self, title: str = "Modbus Register Map") -> str:
        out = io.StringIO()
        out.write(f"# {title}\n\n")
        out.write(f"{len(self.registers)} registers.\n\n")
        out.write("| Name | Address | Type | Access | Unit | Description |\n")
        out.write("|------|--------:|------|--------|------|-------------|\n")
        for reg in sorted(self.registers, key=lambda r: r.address):
            out.write(
                f"| `{reg.name}` | {reg.address} | {reg.type} | {reg.access} "
                f"| {reg.unit} | {reg.description} |\n"
            )
        return out.getvalue()


# ---------------------------------------------------------------------- #
# loading
# ---------------------------------------------------------------------- #
def load_register_map(path: str | Path) -> RegisterMap:
    """Load a CSV register map from *path*.

    Raises ValueError on malformed CSV (missing columns, bad numbers).
    Use RegisterMap.validate() for semantic checks.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty file")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"{path}: missing required column(s): {', '.join(missing)}; "
                f"expected header: {','.join(ALL_COLUMNS)}"
            )
        registers: List[Register] = []
        for lineno, row in enumerate(reader, start=2):  # 1 = header
            name = (row.get("name") or "").strip()
            if not name:
                continue  # skip blank lines
            try:
                address = int((row.get("address") or "").strip(), 0)
            except ValueError:
                raise ValueError(
                    f"{path}:{lineno}: address must be an integer, "
                    f"got '{row.get('address')}'"
                )
            registers.append(
                Register(
                    name=name,
                    address=address,
                    type=(row.get("type") or "").strip().lower(),
                    access=(row.get("access") or "").strip().lower(),
                    unit=(row.get("unit") or "").strip(),
                    description=(row.get("description") or "").strip(),
                )
            )
    return RegisterMap(registers=registers)


def _load_or_exit(csv_path: str) -> RegisterMap:
    try:
        regmap = load_register_map(csv_path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return regmap


def _validate_or_exit(regmap: RegisterMap) -> None:
    errors = regmap.validate()
    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        raise SystemExit(1)


def _write_or_print(text: str, output: Optional[str]) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {output}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modbus-regmap",
        description="Parse, validate and export Modbus CSV register maps.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_csv_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("csv", help="path to the register map CSV file")

    p_validate = sub.add_parser("validate", help="check a register map for problems")
    add_csv_arg(p_validate)

    p_json = sub.add_parser("json", help="export the map as JSON")
    add_csv_arg(p_json)
    p_json.add_argument("-o", "--output", help="write to file instead of stdout")

    p_c = sub.add_parser("gen-c", help="generate a C header with register defines")
    add_csv_arg(p_c)
    p_c.add_argument("-o", "--output", help="write to file instead of stdout")
    p_c.add_argument("--prefix", default="REGMAP", help="C macro prefix (default: REGMAP)")

    p_doc = sub.add_parser("gen-doc", help="generate a Markdown document")
    add_csv_arg(p_doc)
    p_doc.add_argument("-o", "--output", help="write to file instead of stdout")
    p_doc.add_argument("--title", default="Modbus Register Map", help="document title")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    regmap = _load_or_exit(args.csv)

    if args.command == "validate":
        errors = regmap.validate()
        if errors:
            for err in errors:
                print(f"error: {err}", file=sys.stderr)
            return 1
        print(f"ok: {len(regmap.registers)} registers, no problems found")
        return 0

    _validate_or_exit(regmap)
    if args.command == "json":
        _write_or_print(regmap.to_json(), args.output)
    elif args.command == "gen-c":
        _write_or_print(regmap.to_c_header(prefix=args.prefix), args.output)
    elif args.command == "gen-doc":
        _write_or_print(regmap.to_markdown(title=args.title), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
