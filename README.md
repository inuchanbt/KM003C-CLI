# KM003C CLI

[日本語](README.ja.md) | English

A Python CLI for POWER-Z KM003C measurements, USB-PD capture, and fast-charge triggering. Shared command-line options and CSV column names/order follow [cy4500-cli](https://github.com/inuchanbt/cy4500-cli). Features unavailable on KM003C are omitted; KM003C-specific commands are added.

## Installation

Requires Python 3.10 or later. Windows / PowerShell examples:

```powershell
git clone https://github.com/inuchanbt/KM003C-CLI.git
cd KM003C-CLI
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe km003c_cli.py --help
.\.venv\Scripts\python.exe km003c_cli.py usb-info
```

Keep `km003c_cli.py` and `km003c_modules/` together. Offline conversion, tests, and `--dry-run` need only the Python standard library. Hardware access uses `hidapi`, `libusb1`, or `pyserial`, depending on the transport.

## Quick start: capture PD

Connect the KM003C USB data/control interface to the PC and place the meter in the USB-C connection being measured. Capture ten seconds:

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --seconds 10 --out-prefix captures/pd01
```

Capture continuously until Ctrl+C (the default):

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --until-ctrl-c --out-prefix captures/pd02
```

Omitting `--seconds` captures until Ctrl+C. Omitting `--out-prefix` creates `captures/km003c_<local YYYYMMDD_HHMMSS_microseconds>`. CSV, `.ccgx3`, native logs, and measurements are enabled by default. Scope and ccgx3 are enabled, GoodCRC console lines are hidden, and analysis/inference, raw scope transfers, quiet mode, and overwrite are disabled. Periodic status is printed every second (`--status-interval 1`); `--quiet` suppresses event/status lines. Standalone `scope` retains its five-second default. `--scope` / `--ccgx3` explicitly select the defaults; add `--scope-raw` to preserve measurement input transfers:

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --seconds 10 --scope --scope-raw --out-prefix captures/session01
```

Capture defaults to USB vendor interface 0. On Windows this interface must be accessible through WinUSB. Capture does not automatically issue voltage requests or enter a charging protocol. Output directories are created automatically. Existing output files are refused before hardware access; use `--force` to overwrite. Ctrl+C saves data collected so far. Use a prefix without an extension: output suffixes replace any existing extension.

## EZ-PD Protocol Analyzer Utility 4.2.0 output

Open the PD `.csv` with **File > Import**, or open the `.ccgx3` session with **File > Open**. Select a PD row to display its details and the session's voltage/current/CC waveforms when measurements are enabled. CSV imports contain the PD table only; use ccgx3 for waveforms. No Java runtime or manufacturer files are needed to generate either format.

```powershell
# CY4500-style explicit options; CSV and ccgx3 are enabled by default.
python km003c_cli.py capture --until-ctrl-c --scope --ccgx3 --out-prefix captures/ezpd01

# Disable waveform and session output, keeping CSV/native logs.
python km003c_cli.py capture --seconds 10 --no-scope --no-ccgx3 --out-prefix captures/pd_only

# TI-style format selection.
python km003c_cli.py capture --seconds 10 --formats all --out-prefix captures/all01
python km003c_cli.py capture --seconds 10 --formats csv ccgx3 --out-prefix captures/gui_only

# Offline conversion of an existing KM003C capture, including its measurements.
python km003c_cli.py export-gui --records captures/session01.records.bin --out-prefix captures/ezpd_converted
python km003c_cli.py convert --input captures/session01.records.bin --out-prefix captures/ezpd_converted --force
```

`capture` and `export-gui` / `convert` / `decode` accept `--scope` / `--no-scope`, `--ccgx3` / `--no-ccgx3`, `--formats all|original|csv|ccgx3` (one or more values), `--force`, and the compatibility alias `--gui-csv`. Defaults are all formats and measurements enabled. Explicit `--[no-]ccgx3` overrides `--formats` for that format. `--formats original` selects KM003C JSONL/native files and scope CSV; CSV/ccgx3 are selected independently. Metadata and summary are always written. Offline inputs accept `--records` or `--input` and must be KM003C length-prefixed frames, not CY4500 records or TI `.pda`.

GUI CSV/ccgx3 contain observed PD messages with `SOP`, `SOP_PRIME`, `SOP_DPRIME` and `v1`/`v2`/`v3` values. Headerless status/unknown events and unverified SOP kinds are omitted from GUI files and counted in metadata; **default original-format JSONL/native logs retain them**, including raw bytes. GoodCRC messages are retained. Selecting only GUI formats intentionally omits those original logs.

For observed PD messages, KM003C supplies no CRC/EOP result or wire duration: `Ok`, `Duration`, and `Delta` remain blank, and start/end are the same observed point timestamp. The session's `pktData` is a synthetic GUI decoder adapter, with no OK/CRC/EOP bits asserted; it is not a CY4500 capture. Waveform `GraphData` stores physical mV/mA rounded to integers, including signed current; scope CSV/JSONL retain the full original precision. VBUS is stored as an unsigned 16-bit mV value (up to 65.535 V); CC/current use signed 16-bit fields. Out-of-range values are bounded and counted in `ccgx3_graph_clipped`. The stock 4.2.0 GUI interprets VBUS above 32.767 V as negative; the EPR-modified Build 155 GUI reads it correctly. GUI axis limits may hide negative current. No samples are interpolated or added, and KM003C's 1 ms timestamp resolution / polling rate are unchanged.

### GoodCRC console display and inferred VBUS events

`capture --hide-goodcrc` hides decoded `GOODCRC` console lines and is the default, matching CY4500/TI capture options. `--show-goodcrc` restores them; `--quiet` suppresses all event lines. Every selected output still retains GoodCRC, and unknown events and framing errors are not filtered. KM003C has no CRC/EOP/idle-error results, so it cannot reproduce CY4500's distinction between valid and error-bearing GoodCRC.

```powershell
python km003c_cli.py capture --until-ctrl-c --hide-goodcrc --infer-vbus-events --out-prefix captures/vbus01
python km003c_cli.py capture --seconds 10 --show-goodcrc --out-prefix captures/goodcrc01
python km003c_cli.py convert --input captures/session01.records.bin --infer-vbus-events --out-prefix captures/vbus_converted
```

`--infer-vbus-events` is **off by default** and works with capture and offline conversion. It requires measurements (`--scope`, enabled by default) and uses device-timestamped PD status preambles only. The TI-compatible hysteresis emits `VBUS_UP` at 4,000 mV or above after a known low state, and `VBUS_DN` at 800 mV or below after a known high state. Intermediate voltages retain the state. The first sample emits no event; gaps over 100,000 µs, duplicate timestamps, and backward timestamps reset the baseline without emitting an event.

The event time is the first sample after the threshold, with 1 ms device resolution and the polling interval's uncertainty (default 40 ms). It is a software estimate, not a KM003C hardware event; transitions between samples may be missed. Console lines are labeled `[VBUS inferred]`. Selected GUI outputs receive voltage-event rows (`Ok=VBUS_UP/DN`, Utility `VOLT_PKT` adapter), not fabricated PD messages. GUI rows alone cannot distinguish these estimates from hardware VBUS events: share `.vbus_events.jsonl` and metadata with them. The sidecar is always written when inference is enabled, even with `--formats original`, and records `estimated`, source, threshold, sample interval, and GUI row association. `vbus_event_inference` metadata records thresholds, counts, and gap resets; `gui_pd_messages` excludes estimates, while `gui_rows` includes them. Native frames, original JSONL, scope samples, and native PD counts are unchanged.

### AVS transition analysis

```powershell
# Capture until Ctrl+C with an automatic captures/km003c_<date/time> prefix.
python km003c_cli.py capture --analyze-transitions

# Fixed-duration capture; VBUS UP/DN inference is independent and optional.
python km003c_cli.py capture --seconds 10 --analyze-transitions --infer-vbus-events --out-prefix captures/avs01

# Analyze saved native records, without hardware or changing the input.
python km003c_cli.py convert --input captures/avs01.records.bin --analyze-transitions --out-prefix captures/avs_reanalyzed

# CY4500-style analysis of the PD and scope CSV from the same KM003C capture.
python km003c_cli.py analyze-sync --pd-csv captures/avs01.csv --scope-csv captures/avs01.scope.csv --out-prefix captures/avs_csv
```

`--analyze-transitions` is off by default, requires scope, and runs after capture ends (including Ctrl+C) or after offline conversion. It decodes only `EPR_REQUEST` with an RDO and selected EPR AVS PDO, matches subsequent SOP ACCEPT/PS_RDY before the next request, and estimates baseline, movement direction/start, target crossing, target-band settling, observed plateau, and average slew. Fixed/PPS/SPR-AVS requests are outside this analysis. An empty capture or one without AVS requests still produces reports with headers and a no-transitions message. Analysis works with every `--formats` selection, using temporary input files independent of saved GUI/native outputs. Original records and measurements are preserved. `analyze-sync` needs device-timestamped PD/scope files from the same capture; a standalone host-timed `scope` CSV cannot be correlated.

Reports use CY4500's column names/order: `.transitions.csv`, `.transitions.txt`, `.transition_summary.csv`, and `.transition_summary.txt`. Capture/conversion metadata records the analysis settings, status, counts and request-index policy; standalone analyze-sync records settings in the text report. The human summary compares the requested target and measured plateau separately. CSV flags identify KM003C point timestamps, sampled waveforms, missing data, and interrupted/failed captures. The device resolution is 1 ms and default polling is 40 ms: reported latency/slew is an estimate, fast ramps may be missed, and the reports are not a physical USB-PD timing compliance test. No samples or clock offsets are invented. Long gaps/duplicate timestamps break sustained movement and settling; insufficient stable data leaves the plateau or settling fields empty. A nearest measurement more than the allowed sample gap from a request/PS_RDY is marked unavailable.

Common thresholds and option names match CY4500. Defaults tied to native sampling differ:

| Option | KM003C default | CY4500 default |
| --- | --- | --- |
| `--movement-sustain-samples` | 2 | 5 |
| `--settle-hold-ms` / `--observed-settle-hold-ms` | 80 | 20 |
| `--settle-max-gap-ms` | 100 | 5 |
| `--plateau-lookback-ms` | 400 | 150 |
| `--plateau-min-samples` | 6 | 50 |

Other defaults: baseline window/guard 100/5 ms, movement threshold 0.05 V with MAD multiplier 6, target band ±1%, observed band ±0.5%, plateau span limit max(0.10 V, 0.25%), and target guard 10%. All can be overridden using the corresponding CY4500 options shown by `--help`. Changing analysis settings does not change native polling or device configuration.

## Measurement and offline conversion

```powershell
# Live voltage/current; volt-amp is an alias.
.\.venv\Scripts\python.exe km003c_cli.py live-status --count 20 --interval 0.1 --median 5 --csv captures/live.csv

# Five seconds of ADC measurements.
.\.venv\Scripts\python.exe km003c_cli.py scope --seconds 5 --csv captures/scope.csv --raw captures/scope.xfers.bin

# Detailed ADC, including D+/D-/VDD and raw temperature.
.\.venv\Scripts\python.exe km003c_cli.py adc --count 10 --csv captures/adc.csv --jsonl captures/adc.jsonl

# Convert saved KM003C records without hardware; decode is an alias.
.\.venv\Scripts\python.exe km003c_cli.py export-gui --records captures/session01.records.bin --out-prefix captures/converted01 --scope
```

| Command | Main options |
| --- | --- |
| `usb-info` | `--json`, device enumeration |
| `live-status` / `volt-amp` | `--count`, `--interval`, `--median`, `--csv`, `--instant` |
| `scope` | `--seconds` / `--until-ctrl-c`, `--csv`, `--raw`, `--quiet`, `--interval`, `--instant`, `--stream`, `--rate` |
| `capture` | `--seconds` / `--until-ctrl-c`, `--out-prefix`, `--scope` / `--no-scope`, `--scope-raw`, `--ccgx3` / `--no-ccgx3`, `--formats`, `--force`, `--quiet`, `--hide-goodcrc` / `--show-goodcrc`, `--infer-vbus-events`, `--analyze-transitions`, `--status-interval`, `--interval`, `--allow-framing-errors`, `--gui-csv` |
| `export-gui` / `convert` / `decode` | `--records` / `--input`, `--out-prefix`, `--scope` / `--no-scope`, `--ccgx3` / `--no-ccgx3`, `--formats`, `--force`, `--infer-vbus-events`, `--analyze-transitions`, `--allow-framing-errors` |
| `analyze-sync` | `--pd-csv`, `--scope-csv`, `--out-prefix`, `--force`, transition analysis options |
| `adc` | `--count`, `--interval`, `--csv`, `--jsonl`, `--instant` |

Use `<command> --help` for all options. Utility CSV is generated by default by `capture`; `--gui-csv` is accepted for compatibility. `--scope-raw` requires `--scope`. ADC uses device-averaged VBUS/IBUS by default; `--instant` selects instantaneous values.

## Connections

Options follow the command: `--transport auto|hid|usb|cdc`, `--serial`, `--vid`, `--pid`, `--timeout`, `--port` / `-p` / `--com`, and `--baud`. HID also accepts `--hid-path`. Defaults: VID `0x5FC9`, PID `0x0063`, two-second I/O timeout, 115200 baud.

- `auto` chooses USB interface 0 for capture and HID interface 3 for ADC measurements. Specifying `--port` chooses CDC.
- USB uses `libusb1`. On Windows, configure WinUSB for **vendor interface 0**, keeping HID and virtual COM available.
- HID uses `hidapi`. Use `usb-info`, `--serial`, or `--hid-path` to select among matching devices/interfaces.
- ASCII trigger commands and the new CDC stream use virtual COM. If exactly one matching KM003C is found, the port can be omitted. `COM3` below is a placeholder.

## Output files and CY4500 compatibility

`capture --out-prefix captures/session01 --scope --scope-raw` creates:

| Suffix | Content |
| --- | --- |
| `.csv` | PD messages, CY4500 Utility's 15 columns, UTF-8 |
| `.ccgx3` | EZ-PD 4.2 session ZIP with Java-serialized PD and waveform lists |
| `.records.jsonl` | Native frames, decoded events/measurements, unknown data, decode errors |
| `.records.bin` | Length-prefixed KM003C response frames |
| `.records.hex.txt` | Hexadecimal frame listing |
| `.xfers.bin` | Length-prefixed received transfers |
| `.scope.csv` | Measurements, CY4500's 19 columns, UTF-8 with BOM; with `--scope` |
| `.scope.xfers.bin` | Same input transfers as `.xfers.bin`, including measurement preambles; with `--scope-raw` |
| `.summary.txt` | Counts, message statistics, completion status |
| `.metadata.json` | Connection settings, formats, time sources, resolution, unobserved fields |
| `.transitions.csv` / `.transitions.txt` | AVS analysis in the CY4500 report layout; only with `--analyze-transitions` |
| `.transition_summary.csv` / `.transition_summary.txt` | Per-transition human summary; only with analysis |
| `.vbus_events.jsonl` | Software VBUS estimates and supporting sample intervals; only with `--infer-vbus-events` |

Live-status CSV has CY4500's 18 columns. Standalone `scope` also writes `<CSV filename>.metadata.json`. Offline export produces the selected CSV/ccgx3/records JSONL/scope CSV plus metadata and summary; it does not rewrite native binary input. Waveforms for offline sessions are reconstructed from saved measurement preambles.

Unknown PD event flags (including `0x05`) do not stop capture or offline export. They appear in the console/JSONL as `UNKNOWN_PD_EVENT_0xNN`; the remaining logical payload is preserved in `raw_hex`, and decoding resumes at the next logical packet/response without guessing boundaries. They are omitted from GUI CSV/ccgx3 because they lack an interpretable PD header. Their JSONL timestamp source is `pd_status_preamble_ms`. Summary/metadata include unknown and GUI-omitted counts. Truncated known formats remain framing errors; `--allow-framing-errors` optionally skips them while retaining raw evidence in original-format output.

### Units and timestamps

- **PD CSV's `Vbus(V)` contains integer mV**, matching the current CY4500 Utility layout. Scope CSV's `Vbus(V)` and live CSV's `vbus_V` contain V.
- Raw scope values use KM003C units: VBUS in µV, IBUS in µA; CC is 0.1 mV/count for ADC and 1 mV/count for PD status/new CDC. Do not apply CY4500 ADC scaling. Current polarity is preserved as received.
- `capture --scope` reads the 12-byte PD status preamble. Device timestamps have **1 ms resolution**, are converted to µs, and handle 32-bit/24-bit wraparound. Default `--interval 0.04` polls about 25 times per second; it is not a high-speed CY4500 waveform capture.
- PD events are points: `Start Time` and `End Time` contain the same observed timestamp. `Duration`, `Delta`, and `Ok` are blank because wire start/end, CRC, and EOP status are unavailable. Existing CY4500 CSV loaders can read this representation.
- Standalone `scope` timestamps are host receipt times relative to the run's start. ADC has no device timestamp, so `Timestamp Raw` is blank. Separate scope and capture runs do not share a time origin.
- Raw binaries repeat **4-byte little-endian length + native KM003C bytes**. They are not CY4500 64-byte records. Convert them with this CLI's `export-gui` / `decode`.
- Temperature is preserved as `temp_raw`; its unit is unspecified in the supplied manufacturer material.

## KM003C-specific controls

### New CDC ADC stream

```powershell
.\.venv\Scripts\python.exe km003c_cli.py scope --port COM3 --stream --rate 50 --seconds 5 --csv captures/cdc.csv --raw captures/cdc.xfers.bin
```

`--rate` accepts 4, 10, 50, or 1000; omitting it uses the meter's setting. The CLI starts with `02` / `02 rate`, stops with `03`, and sends `01` every 30 minutes to prevent the documented one-hour stop. It decodes batches of 20-byte ADC samples.

The manufacturer material does not define the `Time` unit or CRC8 algorithm. CRC is retained without verification, and scope uses host receipt time; samples in one batch share that time. A documented example has an inconsistent declared length. Unsupported frames stop decoding; specify `--raw` to retain their bytes. **New CDC streaming has not been verified on hardware.**

### Fast-charge triggers

These commands send manufacturer ASCII commands through CDC and can change voltage or protocol state. `--dry-run` shows the command without sending it. `--wait` sets the response read window; `--response-file` saves exact response bytes. Successful sending alone does not establish successful negotiation. Triggers have been checked with dry runs, not live voltage changes.

```powershell
.\.venv\Scripts\python.exe km003c_cli.py pdm open --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pdm set --type 2 --em 2 --sink 1 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py entry pd --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --pdo --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --req 2 --cur 3000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --req 5 --volt 12000 --cur 3000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --cmd 18 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --data 008F1201A800FF --dry-run
.\.venv\Scripts\python.exe km003c_cli.py pd --drp --port COM3
.\.venv\Scripts\python.exe km003c_cli.py qc --voltage 9 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py qc3 --volt 5000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py qc3 --inc 8 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py ufcs --pdo --port COM3
.\.venv\Scripts\python.exe km003c_cli.py ufcs --req 1 --volt 11000 --cur 4000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py reset --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pdm close --port COM3
```

| Command | Options / values |
| --- | --- |
| `pdm` | `open` / `close` / `set`; `set`: `--type` 0–3, `--em` 0–2, `--sink` 0/1 |
| `entry` | `pd`, `ufcs`, `qc`, `fcp`, `scp`, `afc`, `vfcp`, `sfcp`, `bc`, `apple`, `list`, `list+` |
| `pd` | One of `--pdo`, `--req`, `--cmd`, `--data`, `--drp`; request parameters `--volt`, `--cur` |
| `ufcs` | One of `--pdo`, `--req`, `--cmd`; requests require `--volt` and `--cur` |
| `qc` | `--voltage` 5/9/12/20 |
| `fcp`, `afc`, `sfcp` | `--voltage` 5/9/12 |
| `qc3` | One of `--volt` (3600–20000, step 200), `--inc`, `--dec` |
| `scp`, `vfcp` | `--volt`, `--cur` |
| `reset` | Reset the trigger module |

`--voltage` is V, `--volt` is mV, and `--cur` is the manufacturer's integer current parameter. Fixed PD PDO requests may omit `--volt`. Supported voltages/currents depend on the source and firmware. `pd --data` takes SOP + a two-byte little-endian header + four-byte objects, without CRC; the device may rewrite MessageID and roles. Commands run separately; opening PDM, entering a protocol, and requesting voltage are not chained automatically.

## Development and verification

```text
km003c_cli.py          CLI entry point and exporters
km003c_modules/       Protocol decoding, USB/HID/CDC backends, GUI session writer
tests/                Offline protocol, transport, and compatibility tests
README.md             English documentation
README.ja.md          Japanese documentation
requirements.txt      Hardware-access dependencies
LICENSE               MIT license
```

Manufacturer documents/demo binaries and measurement files are **not distributed**. Local-only `local/`, `data/`, and `captures/` directories and captured-output extensions are ignored by Git.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Optional: directory containing your separate CY4500 checkout's
# cy4500_cli.py and ezpd_protocol.py; enables three reference compatibility tests.
$env:CY4500_CLI_ROOT = 'C:\path\to\cy4500-cli\CLI'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover synthetic PD packets, ADC example bytes, fragmented reads, timestamp wraparound, signs/units, offline export, raw evidence on decode errors, ASCII command generation, capture defaults, and AVS transition analysis. Reference tests compare column layouts, read-only CSV loading, and analysis results with CY4500; they skip if no reference checkout is supplied.

On 2026-10-06, all 65 tests passed with CY4500 reference checks enabled. Local EZ-PD 4.2.0 Build 155 (EPR mod v1.0p) GUI checks confirmed CSV Import, ccgx3 Open, PD details, and waveforms. The stock 4.2.0 CSV parser accepted all 1,881 PD messages in an existing capture; its Java classes deserialized the session's 1,881 packets and 5,674 waveform samples. All graph values matched scope CSV within integer mV/mA rounding. Additional synthetic-data checks confirmed inferred UP/DN rows in CSV Import and ccgx3 Open, row selection and waveform alignment; the stock parser accepted all four rows and its Java classes deserialized four rows and six samples. Those local measurements and manufacturer classes are excluded from Git.

Hardware checks on 2026-10-02 confirmed ADC reading over USB/HID/CDC, USB PD capture, standalone HID scope, and offline replay. HID PD-only requests did not respond on the tested unit. New CDC streaming, electronic loads, and live trigger/voltage changes remain unverified.

Implementation references include manufacturer interface/CDC/PDM documentation and public [KM003C protocol research](https://github.com/okhsunrog/km003c-protocol-research/blob/main/docs/protocol_reference.md) / [PD event format](https://github.com/okhsunrog/km003c-protocol-research/blob/main/docs/features/pd_analysis.md). Public research mainly describes firmware V1.9.9; unknown data is retained as raw bytes. Library references: [HIDAPI](https://trezor.github.io/cython-hidapi/api.html), [pySerial](https://pyserial.readthedocs.io/en/latest/pyserial_api.html).

## License

[MIT](LICENSE). Manufacturer material is excluded from this repository.

The Java serialization schema is shared with the MIT-licensed [CY4500 CLI](https://github.com/inuchanbt/cy4500-cli) and sibling TI CLI. Manufacturer classes are used only for local compatibility verification and are not distributed.

AVS transition analysis and report layouts are adapted from the MIT-licensed CY4500 CLI (copyright 2026 inuchanbt), with KM003C clock/sampling adaptations and conservative gap handling.
