# modbus-regmap

A small, dependency-free toolkit for **Modbus register maps**.

Device manuals describe their Modbus registers as tables. `modbus-regmap`
lets you keep that table as a single CSV file under version control, then:

- **validate** it — catch overlapping addresses, duplicate names, bad types
  before they turn into on-site debugging sessions;
- **export JSON** — feed register metadata into gateways, test rigs, or
  MQTT/Modbus bridges;
- **generate a C header** — drop ready-to-use `#define`s into firmware;
- **generate Markdown docs** — keep human-readable documentation in sync
  with the map automatically;
- **diff two maps** — see exactly what changed between two revisions of a
  device manual before touching gateway configs or firmware;
- **find free address ranges** — spot where new registers can go when a
  device manual gets extended.

Pure Python 3.8+ standard library. No dependencies, no build step.

## Install

```bash
git clone https://github.com/nsfxdyj/modbus-regmap.git
cd modbus-regmap
python modbus_regmap.py --help
```

Optionally install it on your PATH by copying or aliasing
`modbus_regmap.py` — it is a single file.

## Register map format

One CSV file, header row required:

```csv
name,address,type,access,unit,description
grid_voltage,0,uint16,ro,V,Grid phase voltage x0.1
active_power,2,int32,ro,W,Instantaneous active power
relay_state,16,uint16,rw,,Relay output bitmask
```

| column        | meaning                                                        |
|---------------|----------------------------------------------------------------|
| `name`        | symbolic name (letters, digits, `_`; must not start with digit)|
| `address`     | register address as in the device manual, 0–65535              |
| `type`        | `int16`, `uint16`, `int32`, `uint32` or `float32`              |
| `access`      | `ro`, `rw` or `wo`                                             |
| `unit`        | optional engineering unit (`V`, `A`, `kWh`, …)                 |
| `description` | optional free text                                             |

16-bit types occupy one register; 32-bit types occupy two consecutive
registers — the validator checks overlaps accordingly.

## Usage

```bash
# check a map for mistakes
python modbus_regmap.py validate examples/registers.csv

# export as JSON (to stdout or a file)
python modbus_regmap.py json examples/registers.csv -o build/registers.json

# generate a C header for firmware
python modbus_regmap.py gen-c examples/registers.csv --prefix METER -o src/registers.h

# generate Markdown documentation
python modbus_regmap.py gen-doc examples/registers.csv -o docs/registers.md

# compare two revisions of a map (exit code 1 when they differ)
python modbus_regmap.py diff old/registers.csv new/registers.csv
python modbus_regmap.py diff old/registers.csv new/registers.csv --json

# list unused address ranges, e.g. before allocating new registers
python modbus_regmap.py gaps examples/registers.csv --from 0 --to 100
```

Example `diff` output:

```text
added (1):
  + energy_total @ 10 (uint32, ro, kWh)
changed (1):
  ~ grid_voltage @ 0: type uint16 -> int16
unchanged: 7
```

`diff` matches registers by name and reports added, removed and changed
entries; unchanged maps exit with code 0, any difference exits with 1, so
it drops straight into CI pipelines or pre-commit checks.

Example `gaps` output:

```text
free address ranges in 0-100 (4):
  6-7 (2 registers)
  9-15 (7 registers)
  19 (1 register)
  21-100 (80 registers)
total free: 90 registers
```

`gaps` accounts for 32-bit types occupying two registers, so a range is
only reported when it is genuinely untouched by any value in the map.

Example C output:

```c
/* grid_voltage: type uint16, access ro | unit: V — Grid phase voltage x0.1 */
#define METER_GRID_VOLTAGE_ADDR   (0u)
#define METER_GRID_VOLTAGE_WIDTH  (1u)
```

## Use as a library

```python
import modbus_regmap as mrm

regmap = mrm.load_register_map("examples/registers.csv")
errors = regmap.validate()
if not errors:
    print(regmap.to_json())
```

## Development

```bash
python -m unittest discover -s tests -v
```

The suite covers both the library API and the CLI end to end; run it
before committing.

## License

MIT — see [LICENSE](LICENSE).
