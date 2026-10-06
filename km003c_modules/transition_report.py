"""CY4500-compatible AVS report layout (MIT, copyright 2026 inuchanbt).

Adapted for KM003C observations. See LICENSE for attribution and license terms.
"""
from __future__ import annotations
import csv
from pathlib import Path
from typing import Optional
from .transition_analysis import AVSTransitionAnalysis

TRANSITION_CSV_COLUMNS = (
    "request_row_index",
    "request_sno",
    "direction",
    "target_voltage_V",
    "requested_current_A",
    "request_start_us",
    "request_end_us",
    "accept_start_us",
    "accept_latency_us",
    "ps_rdy_start_us",
    "ps_rdy_latency_us",
    "request_scope_timestamp_us",
    "request_scope_vbus_V",
    "ps_rdy_scope_timestamp_us",
    "ps_rdy_scope_vbus_V",
    "baseline_vbus_V",
    "baseline_noise_mad_V",
    "movement_threshold_V",
    "movement_start_us",
    "movement_latency_us",
    "target_crossing_us",
    "target_crossing_latency_us",
    "target_band_first_entry_us",
    "target_band_first_entry_latency_us",
    "settling_us",
    "settling_latency_us",
    "settling_hold_us",
    "target_band_fraction",
    "average_slew_V_per_s",
    "observed_plateau_V",
    "observed_plateau_mad_V",
    "observed_plateau_sample_count",
    "observed_band_fraction",
    "observed_band_first_entry_us",
    "observed_band_first_entry_latency_us",
    "observed_settling_us",
    "observed_settling_latency_us",
    "observed_average_slew_V_per_s",
    "flags",
)

def _transition_row(a: AVSTransitionAnalysis) -> dict[str, object]:
    row = {}
    for name in TRANSITION_CSV_COLUMNS:
        if name == "flags":
            row[name] = ";".join(a.flags)
        else:
            row[name] = getattr(a, name)
    return row

HUMAN_TRANSITION_SUMMARY_COLUMNS = (
    "transition",
    "request_sno",
    "direction",
    "from_V",
    "target_V",
    "observed_plateau_V",
    "target_minus_plateau_mV",
    "accept_ms",
    "movement_start_ms",
    "ps_rdy_ms",
    "vbus_at_ps_rdy_V",
    "ps_rdy_vs_plateau_percent",
    "ps_rdy_error_from_plateau_mV",
    "absolute_settle_status",
    "absolute_settle_ms",
    "ps_rdy_to_absolute_settle_ms",
    "absolute_slew_V_per_s",
    "observed_settle_status",
    "observed_settle_ms",
    "ps_rdy_to_observed_settle_ms",
    "movement_to_observed_settle_ms",
    "observed_slew_V_per_s",
    "flags",
)

def _ms_from_us(value):
    return None if value is None else float(value) / 1000.0

def _human_summary_row(
    transition_no: int,
    a: AVSTransitionAnalysis,
) -> dict[str, object]:
    plateau = a.observed_plateau_V
    ps_v = a.ps_rdy_scope_vbus_V

    target_minus_plateau_mv = (
        None
        if plateau is None
        else (a.target_voltage_V - plateau) * 1000.0
    )

    ps_rdy_vs_plateau_percent = (
        None
        if plateau in (None, 0.0) or ps_v is None
        else ps_v / plateau * 100.0
    )

    ps_rdy_error_from_plateau_mv = (
        None
        if plateau is None or ps_v is None
        else (ps_v - plateau) * 1000.0
    )

    ps_to_abs_ms = (
        None
        if a.ps_rdy_start_us is None or a.settling_us is None
        else (a.settling_us - a.ps_rdy_start_us) / 1000.0
    )
    ps_to_obs_ms = (
        None
        if a.ps_rdy_start_us is None or a.observed_settling_us is None
        else (a.observed_settling_us - a.ps_rdy_start_us) / 1000.0
    )
    move_to_obs_ms = (
        None
        if a.movement_start_us is None or a.observed_settling_us is None
        else (a.observed_settling_us - a.movement_start_us) / 1000.0
    )

    if a.settling_us is not None:
        abs_status = "SETTLED"
    elif "target_band_not_reached" in a.flags:
        abs_status = "TARGET_BAND_NOT_REACHED"
    elif "settling_not_observed" in a.flags:
        abs_status = "NOT_SETTLED"
    else:
        abs_status = "UNAVAILABLE"

    if a.observed_settling_us is not None:
        obs_status = "SETTLED"
    elif "observed_plateau_not_found" in a.flags:
        obs_status = "PLATEAU_NOT_FOUND"
    elif "observed_settling_not_observed" in a.flags:
        obs_status = "NOT_SETTLED"
    else:
        obs_status = "UNAVAILABLE"

    return {
        "transition": transition_no,
        "request_sno": a.request_sno,
        "direction": a.direction,
        "from_V": a.baseline_vbus_V,
        "target_V": a.target_voltage_V,
        "observed_plateau_V": plateau,
        "target_minus_plateau_mV": target_minus_plateau_mv,
        "accept_ms": _ms_from_us(a.accept_latency_us),
        "movement_start_ms": _ms_from_us(a.movement_latency_us),
        "ps_rdy_ms": _ms_from_us(a.ps_rdy_latency_us),
        "vbus_at_ps_rdy_V": ps_v,
        "ps_rdy_vs_plateau_percent": ps_rdy_vs_plateau_percent,
        "ps_rdy_error_from_plateau_mV": ps_rdy_error_from_plateau_mv,
        "absolute_settle_status": abs_status,
        "absolute_settle_ms": _ms_from_us(a.settling_latency_us),
        "ps_rdy_to_absolute_settle_ms": ps_to_abs_ms,
        "absolute_slew_V_per_s": a.average_slew_V_per_s,
        "observed_settle_status": obs_status,
        "observed_settle_ms": _ms_from_us(a.observed_settling_latency_us),
        "ps_rdy_to_observed_settle_ms": ps_to_obs_ms,
        "movement_to_observed_settle_ms": move_to_obs_ms,
        "observed_slew_V_per_s": a.observed_average_slew_V_per_s,
        "flags": ";".join(a.flags),
    }

def _fmt_num(value, digits=3, suffix=""):
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}{suffix}"

def _write_human_transition_summary(
    analyses: list[AVSTransitionAnalysis],
    *,
    csv_path: Path,
    text_path: Path,
) -> None:
    rows = [
        _human_summary_row(i, a)
        for i, a in enumerate(analyses, 1)
    ]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=HUMAN_TRANSITION_SUMMARY_COLUMNS,
        )
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "KM003C AVS per-transition summary",
        "====================================",
        "",
        (
            "T#  SNo  Dir    From -> Target   Plateau   Move     PS_RDY   "
            "V@PS / Plateau   Obs.Set  PS->Obs   Obs.Slew   Abs"
        ),
        (
            "--  ---  ----  ---------------  --------  -------  -------  "
            "---------------  -------  -------  ---------  ----------------------"
        ),
    ]

    for row in rows:
        from_to = (
            f"{_fmt_num(row['from_V'], 2):>5}"
            f" -> {_fmt_num(row['target_V'], 2):>5}V"
        )
        ps_ratio = (
            "-"
            if row["ps_rdy_vs_plateau_percent"] is None
            else f"{row['vbus_at_ps_rdy_V']:.3f}V / "
                 f"{row['ps_rdy_vs_plateau_percent']:.1f}%"
        )
        abs_text = (
            row["absolute_settle_status"]
            if row["absolute_settle_ms"] is None
            else f"{row['absolute_settle_ms']:.1f}ms"
        )
        lines.append(
            f"{row['transition']:>2}  "
            f"{str(row['request_sno']):>3}  "
            f"{row['direction']:<4}  "
            f"{from_to:<15}  "
            f"{_fmt_num(row['observed_plateau_V'], 3, 'V'):>8}  "
            f"{_fmt_num(row['movement_start_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['ps_rdy_ms'], 1, 'ms'):>7}  "
            f"{ps_ratio:>15}  "
            f"{_fmt_num(row['observed_settle_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['ps_rdy_to_observed_settle_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['observed_slew_V_per_s'], 2, 'V/s'):>9}  "
            f"{abs_text}"
        )

    lines.extend(
        [
            "",
            "Column meanings:",
            "  Move      = EPR_REQUEST -> sustained VBUS movement start",
            "  PS_RDY    = EPR_REQUEST -> PS_RDY",
            "  V@PS      = measurement VBUS nearest PS_RDY",
            "  Plateau   = observed final plateau from stable measurement data",
            "  Obs.Set   = EPR_REQUEST -> observed-plateau settle",
            "  PS->Obs   = PS_RDY -> observed-plateau settle "
            "(negative means settled before PS_RDY)",
            "  Abs       = requested-target absolute settling result",
            "",
            "Note: absolute target and observed plateau are intentionally "
            "reported separately.",
        ]
    )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _write_transition_outputs(
    analyses: list[AVSTransitionAnalysis],
    *,
    csv_path: Path,
    summary_path: Path,
    settings: dict[str, object],
    human_csv_path: Optional[Path] = None,
    human_text_path: Optional[Path] = None,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=TRANSITION_CSV_COLUMNS)
        writer.writeheader()
        for a in analyses:
            writer.writerow(_transition_row(a))

    lines = [
        "KM003C synchronized AVS transition analysis",
        "==============================================",
        "Clock handling: native KM003C device milliseconds converted to microseconds;",
        "no host-time offset or fitted clock offset is applied.",
        "",
        "Analysis settings:",
    ]
    for key, value in settings.items():
        lines.append(f"  {key}: {value}")

    lines.extend(["", f"Transitions analyzed: {len(analyses)}", ""])

    for n, a in enumerate(analyses, 1):
        flags = ", ".join(a.flags) if a.flags else "none"
        lines.extend(
            [
                (
                    f"[{n}] SNo={a.request_sno} {a.direction} "
                    f"target={a.target_voltage_V:.3f} V "
                    f"current={a.requested_current_A if a.requested_current_A is not None else 'n/a'} A"
                ),
                f"  Request          : {a.request_start_us} us",
                (
                    f"  ACCEPT           : {a.accept_start_us} us "
                    f"(+{a.accept_latency_us/1000:.3f} ms)"
                    if a.accept_start_us is not None else
                    "  ACCEPT           : not matched"
                ),
                (
                    f"  PS_RDY           : {a.ps_rdy_start_us} us "
                    f"(+{a.ps_rdy_latency_us/1000:.3f} ms)"
                    if a.ps_rdy_start_us is not None else
                    "  PS_RDY           : not matched"
                ),
                (
                    f"  measurement @ Request   : {a.request_scope_vbus_V:.6f} V "
                    f"@ {a.request_scope_timestamp_us} us"
                    if a.request_scope_vbus_V is not None else
                    "  measurement @ Request   : unavailable"
                ),
                (
                    f"  measurement @ PS_RDY    : {a.ps_rdy_scope_vbus_V:.6f} V "
                    f"@ {a.ps_rdy_scope_timestamp_us} us"
                    if a.ps_rdy_scope_vbus_V is not None else
                    "  measurement @ PS_RDY    : unavailable"
                ),
                (
                    f"  Baseline         : {a.baseline_vbus_V:.6f} V; "
                    f"MAD={a.baseline_noise_mad_V:.6f} V; "
                    f"move threshold={a.movement_threshold_V:.6f} V"
                    if (
                        a.baseline_vbus_V is not None
                        and a.baseline_noise_mad_V is not None
                        and a.movement_threshold_V is not None
                    ) else
                    "  Baseline         : incomplete"
                ),
                (
                    f"  Movement start   : {a.movement_start_us} us "
                    f"(+{a.movement_latency_us/1000:.3f} ms)"
                    if a.movement_start_us is not None else
                    "  Movement start   : not detected"
                ),
                (
                    f"  Target crossing  : {a.target_crossing_us} us "
                    f"(+{a.target_crossing_latency_us/1000:.3f} ms)"
                    if a.target_crossing_us is not None else
                    "  Target crossing  : not observed"
                ),
                (
                    f"  ±{a.target_band_fraction*100:.2f}% first entry: "
                    f"{a.target_band_first_entry_us} us "
                    f"(+{a.target_band_first_entry_latency_us/1000:.3f} ms)"
                    if a.target_band_first_entry_us is not None else
                    f"  ±{a.target_band_fraction*100:.2f}% first entry: not observed"
                ),
                (
                    f"  Settled          : {a.settling_us} us "
                    f"(+{a.settling_latency_us/1000:.3f} ms; "
                    f"hold={a.settling_hold_us/1000:.1f} ms)"
                    if a.settling_us is not None else
                    "  Settled          : not observed"
                ),
                (
                    f"  Absolute slew    : {a.average_slew_V_per_s:.3f} V/s"
                    if a.average_slew_V_per_s is not None else
                    "  Absolute slew    : unavailable"
                ),
                (
                    f"  Observed plateau : {a.observed_plateau_V:.6f} V "
                    f"(MAD={a.observed_plateau_mad_V:.6f} V, "
                    f"n={a.observed_plateau_sample_count})"
                    if (
                        a.observed_plateau_V is not None
                        and a.observed_plateau_mad_V is not None
                    ) else
                    "  Observed plateau : unavailable"
                ),
                (
                    f"  Observed ±{a.observed_band_fraction*100:.2f}% entry: "
                    f"{a.observed_band_first_entry_us} us "
                    f"(+{a.observed_band_first_entry_latency_us/1000:.3f} ms)"
                    if a.observed_band_first_entry_us is not None else
                    f"  Observed ±{a.observed_band_fraction*100:.2f}% entry: unavailable"
                ),
                (
                    f"  Observed settled : {a.observed_settling_us} us "
                    f"(+{a.observed_settling_latency_us/1000:.3f} ms)"
                    if a.observed_settling_us is not None else
                    "  Observed settled : unavailable"
                ),
                (
                    f"  Observed slew    : {a.observed_average_slew_V_per_s:.3f} V/s"
                    if a.observed_average_slew_V_per_s is not None else
                    "  Observed slew    : unavailable"
                ),
                f"  Flags            : {flags}",
                "",
            ]
        )

    summary_path.write_text("\n".join(lines), encoding="utf-8")

    if human_csv_path is not None and human_text_path is not None:
        _write_human_transition_summary(
            analyses,
            csv_path=human_csv_path,
            text_path=human_text_path,
        )

def _print_transition_analysis(analyses: list[AVSTransitionAnalysis]) -> None:
    print()
    print("AVS TRANSITION ANALYSIS")
    if not analyses:
        print("No AVS EPR_REQUEST transitions found.")
        return

    for a in analyses:
        ps = (
            f"{a.ps_rdy_latency_us / 1000:.3f} ms"
            if a.ps_rdy_latency_us is not None else "-"
        )
        move = (
            f"{a.movement_latency_us / 1000:.3f} ms"
            if a.movement_latency_us is not None else "-"
        )
        settle = (
            f"{a.settling_latency_us / 1000:.3f} ms"
            if a.settling_latency_us is not None else "-"
        )
        ps_v = (
            f"{a.ps_rdy_scope_vbus_V:.3f} V"
            if a.ps_rdy_scope_vbus_V is not None else "-"
        )
        abs_slew = (
            f"{a.average_slew_V_per_s:.2f} V/s"
            if a.average_slew_V_per_s is not None else "-"
        )
        obs_settle = (
            f"{a.observed_settling_latency_us / 1000:.3f} ms"
            if a.observed_settling_latency_us is not None else "-"
        )
        obs_plateau = (
            f"{a.observed_plateau_V:.3f} V"
            if a.observed_plateau_V is not None else "-"
        )
        obs_slew = (
            f"{a.observed_average_slew_V_per_s:.2f} V/s"
            if a.observed_average_slew_V_per_s is not None else "-"
        )
        print(
            f"SNo={a.request_sno!s:>3} {a.direction:4s} "
            f"target={a.target_voltage_V:6.3f}V "
            f"move={move:>10} PS_RDY={ps:>10} "
            f"V@PS={ps_v:>9} abs_settle={settle:>10} "
            f"abs_slew={abs_slew:>10} | "
            f"plateau={obs_plateau:>9} "
            f"obs_settle={obs_settle:>10} "
            f"obs_slew={obs_slew:>10} "
            f"flags={';'.join(a.flags) if a.flags else '-'}"
        )

TRANSITION_OUTPUT_SUFFIXES = (
    ".transitions.csv",
    ".transitions.txt",
    ".transition_summary.csv",
    ".transition_summary.txt",
)
