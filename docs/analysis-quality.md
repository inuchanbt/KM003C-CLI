# PD evidence and hysteresis quality

The Web analyzer and `KM003C-CLI/analyze_sweep_csv.py` use `pd-quality/v1`.
Original CSV columns remain; quality and provenance columns are appended.

Every report starts with the actual total/valid/excluded pair counts, both for
the complete sweep and for requested voltages ≥29 V. A turnaround endpoint with
only one leg is not a pair. Duplicate legs form an excluded ambiguous pair.
Different runs, cycles, PDOs and recorded test conditions never mix.

## Independent point decisions

* `protocol_status`: `confirmed` requires Source ACCEPT then Source PS_RDY
  after the matching Sink request, before the next request/reset. A captured
  rejection/WAIT/reset is `failed`; missing replies or unmatched recordings
  are `unconfirmed`. Absence in a capture does not prove a device failed.
* `steady_status`: after PS_RDY, evaluate the final 400 ms of the last contiguous
  native sample segment before the next request/reset. Require ≥6 samples,
  ≥200 ms span and gaps ≤100 ms. Earlier segments and transients remain in the
  raw recording and are not silently joined across gaps.
* Stability limits: sample standard deviation ≤max(20 mV, 0.1% of requested V),
  endpoint drift ≤max(50 mV, 0.2%), peak-to-peak ≤max(100 mV, 0.5%). Drift uses
  first/last 20% windows, at least two samples each. Insufficient evidence is
  `unconfirmed`; measured instability is `failed`.
* `settling_status`: the observed plateau band is ±max(20 mV, 0.5% of measured
  plateau), with ≥80 ms continuous hold. Settlement is bracketed between the
  last out-of-band observation and first in-band observation (or PS_RDY at
  the left capture boundary). No interpolation or millisecond waveform accuracy
  is implied. Target voltage deviation is a separate observation and cannot
  invalidate stable cable/load voltage drop.

These thresholds are analysis policy, not USB compliance limits. Recorded Scope
sampling failures remain exclusion reasons even if the native tail is stable.
`invalid_reasons`, raw sweep row, Scope index range and PD Sno references are
saved with each point. The Web table exposes those details and original rows.

## Pair gating

Both legs must pass all three independent decisions. Requested current and
recorded load setting must match; measured load magnitudes must agree within
max(50 mA, 5%). Missing load evidence is excluded. Only native steady means are
used: return minus outbound. Excluded `hysteresis_v` and `hysteresis_mV` are null
(empty CSV cells). Excluded pairs remain visible as red crosses in an axes
annotation band, and do not affect difference statistics or Y autoscaling.

## Protocol diagnostics and cause attribution

Consecutive identical requests within 50 ms retain each request's Message ID,
own GoodCRC and responses. Equal IDs are retry/duplicate candidates; changed IDs
are distinct same-RDO requests. Neither category alone proves a defect. Changing
the RDO with the same Message ID and no intervening sender message/reset is
classified at the sender. SRC classifications describe an explicit Source
refusal or observed PS_RDY/ACCEPT order. They do not establish hardware fault.
Other causes remain `indeterminate`. Soft Reset sender is always reported
separately and is never used alone to assign blame.

MessageID retry/counter and Soft Reset recovery semantics follow USB PD,
sections 6.7.1 and 6.8.1 of the
[USB-authored PD specification hosted by TI](https://e2e.ti.com/cfs-file/__key/communityserver-discussions-components-files/138/USB_5F00_PD_5F00_R3_5F00_0-V1.1-20170112.pdf).
GoodCRC absence is labelled unobserved; no missing packet is fabricated.

## Load regulation and time resolution

Stable load plateaus within one confirmed request, and adjacent valid steady points at the same requested voltage/PDO/source/run with
≥50 mA load change yield ΔV/Δ|I| and `Rout = -ΔV/Δ|I|`. Current signs remain in
the source data. This is resistance seen at the meter, including cable/path
resistance; a voltage sweep at constant load cannot determine it. Reports state
when no qualifying interval exists. PD timestamps have a 1 ms device tick;
actual Scope median/max sample intervals and settling brackets are reported
separately. PowerPi data supplied on 2026-10-11 have a median interval of 48 ms.

## Recorded regression and verification

The reduced `lenovo29-recorded.json` fixture contains the supplied ZIP's two
29 V point summaries: outbound PS_RDY unconfirmed with Soft Reset flag, and an
apparent ~0.998 V ADC round-trip difference. It must produce null hysteresis.
The separately supplied Lenovo raw PD/Scope are another recording: the ZIP
fingerprint starts at 4346409000 us; the PD CSV starts at 4278139000 us and
contains no Soft Reset. The analyzer refuses to shift or associate those
recordings. That exclusion means insufficient matching evidence, not 33 failed
charger negotiations. Separate timeline tests cover missing replies, actual
Reset interruption, sender direction, GoodCRC and Message ID differences.

The matched PowerPi recording produces **33 pairs: 27 valid, 6 excluded**;
requested ≥29 V produces **19 pairs: 16 valid, 3 excluded**, identically in Web
and CLI. Those are results of this policy, not hardcoded report values.

CLI example:

```text
python analyze_sweep_csv.py sweep.csv --pd-csv recording.pd.csv --scope-csv recording.scope.csv --out analysis
```

Additional outputs: hysteresis, PD checks, protocol diagnostics, load regulation,
analysis settings and the hysteresis plot. Web ZIPs retain the original sweep,
PD and Scope recordings. `km003c_cli.py analyze-sync` and live transition analysis
also preserve header identity, export protocol diagnostics and report actual
Scope time resolution.
