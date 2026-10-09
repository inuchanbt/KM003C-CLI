#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_sweep_csv.py

Analyze KM003C PPS/AVS sweep CSV logs without an ASD-PD31 checkout.

Adapted from ASD-PD31 analyze_sweep_csv_v4.5.py (analyzer v4.6.2),
MIT License, copyright (c) 2026 inuchanbt. See LICENSE.
KM003C signed current/power and missing load/ripple data are preserved.

Reports default to Japanese and English; select with --lang ja|en|both.
Appended runs, PDO/current settings and outbound/return legs stay separate.
Failed/cancelled requests and nonfinite/missing readings are excluded.
Legacy ASD columns remain supported, including optional ripple/temperature.

Outputs:
    <out>_normalized.csv
    <out>_summary.csv
    <out>_human_report.txt
    <out>_human_report.en.txt
    <out>_voltage_error.png
    <out>_voltage_actual.png
    <out>_current.png
    <out>_power.png
    <out>_ripple.png
    <out>_ripple_jump.png
    <out>_module_temp.png              (if temperature exists)
    <out>_ripple_vs_temp.png           (if temperature exists)
    <out>_voltage_error_pct.png
    <out>_ripple_mv_per_v.png
    <out>_ripple_pct_of_voltage.png
    <out>_ripple_ppm_of_voltage.png
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Optional

try:
    import pandas as pd
except ModuleNotFoundError as exc:
    raise SystemExit('CSV analysis requires pandas. Install requirements-analysis.txt.') from exc
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
except ModuleNotFoundError:  # --no-plots analysis remains useful on lean runtimes.
    plt = None

__version__ = '1.0.0'


NEW_COLUMN_ALIASES = {
    "target_voltage_v": ["target_voltage_v", "set_voltage_v", "set_voltage", "voltage_set_v"],
    "target_load_current_a": ["target_load_current_a", "set_load_current_a", "load_current_a", "set_current_a"],
    "request_current_a": ["request_current_a", "set_request_current_a"],
    "actual_voltage_v": ["actual_voltage_v", "voltage_v", "measured_voltage_v"],
    "actual_current_a": ["actual_current_a", "current_a", "measured_current_a"],
    "actual_power_w": ["actual_power_w", "power_w", "measured_power_w"],
    "ripple_mv": ["ripple_mv", "ripple"],
    "timestamp": ["timestamp", "time", "datetime"],
    "phase_elapsed_s": ["phase_elapsed_s", "elapsed_s"],
    "condition_module_temp_c": ["condition_module_temp_c", "module_temp_c", "temp_c", "temperature_c"],
}

META_COLS = [
    "controller_version",
    "run_started_at",
    "request_status",
    "_device",
    "source_name",
    "source_manufacturer",
    "source_model",
    "source_port",
    "source_type",
    "cable_name",
    "cable_manufacturer",
    "cable_model",
    "cable_length_m",
    "cable_type",
    "emarker_claim",
    "input_ac_voltage_v",
    "input_ac_frequency_hz",
    "asd_type_version",
    "asd_power_firmware_version",
    "asd_power_hardware_version",
    "asd_protocol_firmware_version",
    "asd_protocol_hardware_version",
    "asd_display_firmware_version",
    "asd_display_hardware_version",
    "asd_version_query_status",
    "meter_chain",
    "test_note",
    "mode",
    "pdo_kind",
    "pdo_object_number",
    "pdo_raw_u32",
    "epr_capable",
    "has_epr",
    "has_avs",
    "epr_missing_suspected",
    "precondition_enabled",
    "precondition_voltage_v",
    "precondition_current_a",
    "precondition_seconds",
    "precondition_power_limit_w",
    "precondition_keep_load_on",
    "measurement_settle_enabled",
    "measurement_settle_seconds",
    "measurement_settle_measure_interval",
    "condition_log_enabled",
    "condition_selected_frame",
    # v5.7.36+: preserve round-trip sweep identity so forward and return
    # measurements remain independent in summaries and PNG plots.
    "sweep_leg",
    "sweep_pass",
    "sweep_direction",
    "sequence_step",
    "sequence_step_count",
    "sequence_step_duration_s",
    "sequence_step_elapsed_s",
]

CONDITION_COLS = [
    "condition_frame_sent",
    "condition_frame_crc_ok",
    "condition_raw_response",
    "condition_response_len",
    "condition_response_crc_ok",
    "condition_is_ack_only",
    "condition_ack_code",
    "condition_ack_echo_crc",
    "condition_selected_score",
    "condition_payload_hex",
    "condition_payload_len",
    "condition_module_temp_raw",
    "condition_module_input_voltage_mv",
    "condition_module_status_flags_hex",
    "condition_module_output_ocp",
    "condition_module_output_opp",
    "condition_module_input_ocp",
    "condition_module_input_opp",
    "condition_module_temp_protection",
    "condition_module_no_calibration",
    "condition_u16le_csv",
    "condition_u16be_csv",
    "condition_i16le_csv",
    "condition_temp_candidates_c",
    "condition_note",
]

EXTERNAL_TEMPERATURE_COLS = [
    "temp_ch1_c",
    "temp_ch2_c",
    "temp_ch3_c",
    "temp_ch4_c",
    "temp_ch1_label",
    "temp_ch2_label",
    "temp_ch3_label",
    "temp_ch4_label",
    "temp_source",
    "temp_delta_s",
]

DEFAULT_MEASUREMENT_MODES = [
    "avs-sweep",
    "pps-sweep",
    "avs",
    "avs-continuous",
    "pps",
    "pps-continuous",
    "fixed",
    "pdo-index",
    "var",
    "measurement",
    "legacy",
    "fixed-current-sweep",
    "pps-current-sweep",
    "avs-current-sweep",
    "hold-fixed",
    "hold-pps",
    "hold-avs",
    "avs-sequence",
]

PRIMARY_SWEEP_MODE_PRIORITY = [
    ["avs-sequence"],
    ["fixed-current-sweep", "pps-current-sweep", "avs-current-sweep"],
    # A combined APDO handoff sweep intentionally has both modes in one CSV.
    # Keep its PPS and AVS segments together instead of treating AVS as the
    # winner and silently discarding the lower-voltage PPS section.
    ["avs-continuous", "pps-continuous", "avs-sweep", "pps-sweep"],
    ["avs", "pps"],
]

# Friendly aliases for --modes.  The CSV records a continuous AVS run as
# "avs-continuous", but an operator naturally asks for the AVS family with
# "--modes avs".  Keep the raw, more specific spelling available too.
MODE_FAMILY_ALIASES = {
    "avs": {"avs", "avs-sweep", "avs-continuous", "avs-sequence"},
    "pps": {"pps", "pps-sweep", "pps-continuous"},
    "hold": {"hold-fixed", "hold-pps", "hold-avs"},
}

HELPER_MODES = [
    "precondition",
    "measurement-settle",
    "avs-continuous-precheck",
    "pps-continuous-precheck",
    "avs-precheck",
    "fixed-precheck",
    "fixed-current-precheck",
    "pps-current-precheck",
    "avs-current-precheck",
    "hold-fixed-precheck",
    "hold-pps-precheck",
    "hold-avs-precheck",
    "avs-sequence-precheck",
    "avs-sequence-ramp",
    "post-run-observe",
]


def _norm_mode(value: object) -> str:
    return str(value).strip().lower()


def sweep_axis_for(df: pd.DataFrame) -> dict[str, str]:
    """Return the requested sweep coordinate while preserving old CSV support."""
    declared_axes = df.get("sweep_axis", pd.Series(dtype=object)).map(_norm_mode)
    modes = df.get("mode", pd.Series(dtype=object)).map(_norm_mode)
    elapsed_col = "phase_elapsed_s"
    timed_hold = (
        modes.str.match(r"^hold-(?:fixed|pps|avs)(?:$|-)").any()
        or modes.eq("avs-sequence").any()
    )
    if (
        elapsed_col in df.columns
        and df[elapsed_col].notna().any()
        and (declared_axes.eq("time").any() or timed_hold)
    ):
        return {
            "kind": "time",
            "target_col": elapsed_col,
            "actual_col": elapsed_col,
            "label": "Elapsed Time",
            "unit": "s",
        }

    current_target = "target_load_current_a"
    if current_target in df.columns and df[current_target].notna().any():
        explicit_current_sweep = modes.str.contains(r"(?:^|-)current-sweep$", regex=True).any()
        current_points = df[current_target].nunique(dropna=True)
        voltage_points = df["target_voltage_v"].nunique(dropna=True) if "target_voltage_v" in df.columns else 0
        if explicit_current_sweep or (current_points > 1 and voltage_points <= 1):
            return {
                "kind": "current",
                "target_col": current_target,
                "actual_col": "actual_current_a",
                "label": "Set Current",
                "unit": "A",
            }
    return {
        "kind": "voltage",
        "target_col": "target_voltage_v",
        "actual_col": "actual_voltage_v",
        "label": "Set Voltage",
        "unit": "V",
    }

LEGACY_HEADER = [
    "timestamp", "set_voltage_v", "set_load_current_a", "set_request_current_a",
    "crc_ok", "voltage_uv", "current_ua", "voltage_v", "current_a", "power_w", "ripple_mv",
    "raw_w0", "raw_w1", "raw_w2", "raw_w3", "raw_w4", "raw_w5", "raw_w6", "raw_w7", "raw_w8",
    "raw_hex",
    "source_name", "source_type", "cable_name", "cable_type", "emarker_claim", "meter_chain", "test_note"
]


def pick_col(df: pd.DataFrame, canonical: str) -> Optional[str]:
    lower_to_real = {str(c).strip().lower(): c for c in df.columns}
    for alias in NEW_COLUMN_ALIASES[canonical]:
        hit = lower_to_real.get(alias.lower())
        if hit is not None:
            return hit
    return None


def normalize_header_csv(path: Path) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return None

    if df.empty:
        return None

    normalized = pd.DataFrame()

    for canonical in ["target_voltage_v", "actual_voltage_v"]:
        if pick_col(df, canonical) is None:
            return None

    for canonical in NEW_COLUMN_ALIASES:
        col = pick_col(df, canonical)
        if col is not None:
            normalized[canonical] = df[col]

    # Preserve metadata, condition, and merged thermocouple columns.
    for col in META_COLS + CONDITION_COLS + EXTERNAL_TEMPERATURE_COLS:
        if col in df.columns and col not in normalized.columns:
            normalized[col] = df[col]

    for col in ["raw_measure_response", "raw_hex", "command", "raw_command_response",
                "measurement_index", "error", "request_sent_at"]:
        if col in df.columns and col not in normalized.columns:
            normalized[col] = df[col]

    normalized["_format"] = "header"
    if {"controller_version", "request_status", "command"} <= set(df.columns):
        normalized["_device"] = "KM003C"
    normalized["_source_file"] = path.name
    normalized["_row_number"] = range(1, len(normalized) + 1)
    return normalized


def normalize_legacy_rows(path: Path) -> Optional[pd.DataFrame]:
    data_rows: list[list[str]] = []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for parts in reader:
            if not parts:
                continue
            parts = [str(p).strip() for p in parts]
            if len(parts) < 20:
                continue
            try:
                float(parts[0])
            except ValueError:
                continue

            row_data = parts[:21]
            meta_data = parts[21:28]
            while len(meta_data) < 7:
                meta_data.append("unknown")
            data_rows.append(row_data + meta_data)

    if not data_rows:
        return None

    df = pd.DataFrame(data_rows, columns=LEGACY_HEADER)

    normalized = pd.DataFrame()
    normalized["timestamp"] = df["timestamp"]
    normalized["target_voltage_v"] = df["set_voltage_v"]
    normalized["target_load_current_a"] = df["set_load_current_a"]
    normalized["request_current_a"] = df["set_request_current_a"]
    normalized["actual_voltage_v"] = df["voltage_v"]
    normalized["actual_current_a"] = df["current_a"]
    normalized["actual_power_w"] = df["power_w"]
    normalized["ripple_mv"] = df["ripple_mv"]
    normalized["raw_hex"] = df["raw_hex"]

    for col in ["source_name", "source_type", "cable_name", "cable_type", "emarker_claim", "meter_chain", "test_note"]:
        normalized[col] = df[col]

    normalized["mode"] = "legacy"
    normalized["_format"] = "legacy"
    normalized["_source_file"] = path.name
    normalized["_row_number"] = range(1, len(normalized) + 1)
    return normalized


def load_and_normalize(path: Path) -> pd.DataFrame:
    df = normalize_header_csv(path)
    if df is None:
        df = normalize_legacy_rows(path)

    if df is None or df.empty:
        raise ValueError(
            "有効な測定データが見つかりませんでした。"
            "KM003C sweep CSVなら target_voltage_v / actual_voltage_v 列、"
            "旧CSVなら set_voltage_v / voltage_v 列が必要です。"
            "測定には --continuous-sweep、--measure または --measure-loop を指定してください。"
        )

    input_rows = len(df)
    numeric_cols = [
        "target_voltage_v",
        "target_load_current_a",
        "request_current_a",
        "actual_voltage_v",
        "actual_current_a",
        "actual_power_w",
        "ripple_mv",
        "phase_elapsed_s",
        "condition_module_temp_c",
        "condition_module_temp_raw",
        "condition_module_input_voltage_mv",
        "condition_response_len",
        "condition_payload_len",
        "precondition_voltage_v",
        "precondition_current_a",
        "precondition_seconds",
        "precondition_power_limit_w",
        "measurement_settle_seconds",
        "measurement_settle_measure_interval",
        "temp_ch1_c",
        "temp_ch2_c",
        "temp_ch3_c",
        "temp_ch4_c",
        "temp_delta_s",
    ]

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "actual_power_w" not in df.columns or df["actual_power_w"].isna().all():
        if "actual_voltage_v" in df.columns and "actual_current_a" in df.columns:
            df["actual_power_w"] = df["actual_voltage_v"] * df["actual_current_a"]

    if "timestamp" in df.columns:
        df["timestamp_parsed"] = pd.to_datetime(df["timestamp"], errors="coerce")

    if "mode" not in df.columns:
        df["mode"] = "measurement"

    # A failed/cancelled row can retain a previous ADC sample. It must not
    # influence averages even when its voltage/current cells are populated.
    if "request_status" in df.columns:
        status_ok = df["request_status"].map(_norm_mode).eq("sent_unverified")
        error_empty = df.get("error", pd.Series("", index=df.index)).fillna("").astype(str).str.strip().eq("")
        valid = status_ok & error_empty
        removed = int((~valid).sum())
        if removed:
            print(f'Excluded {removed} failed/interrupted request row(s).')
        df = df[valid].copy()
    measured_cols = ["target_voltage_v", "actual_voltage_v", "actual_current_a", "actual_power_w"]
    missing = [col for col in measured_cols if col not in df.columns]
    if missing:
        raise ValueError('測定列がありません: ' + ', '.join(missing))
    finite = pd.Series(True, index=df.index, dtype=bool)
    for col in measured_cols:
        finite &= df[col].map(lambda value: pd.notna(value) and math.isfinite(float(value))).astype(bool)
    df = df.loc[finite].copy()
    if df.empty:
        raise ValueError("集計可能な実測データがありません。KM003C の取得時に "
                         "--continuous-sweep、--measure または --measure-loop を指定してください。")

    axis = sweep_axis_for(df)
    df["sweep_axis"] = axis["kind"]
    df["sweep_target"] = df[axis["target_col"]]

    df = df.reset_index(drop=True)
    df.attrs['input_rows'] = input_rows
    return df


def filter_modes(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    """Select which phase/mode rows are analyzed.

    v4.5 is deliberately conservative: the default is measurement-only.
    Helper rows such as precondition and measurement-settle are excluded even
    if their target voltage looks like a real sweep point. This prevents the
    48V precondition row from contaminating a 23.95-24.15V boundary scan.
    """
    if "mode" not in df.columns:
        return df.copy()

    work = df.copy()
    work["_mode_norm"] = work["mode"].map(_norm_mode)

    # Backward compatible alias: --include-all-modes means --phase all.
    phase = getattr(args, "phase", "measurement")
    if getattr(args, "include_all_modes", False):
        phase = "all"

    auto_primary = False
    if args.modes:
        modes = [_norm_mode(m) for m in args.modes.split(",") if m.strip()]
        expanded_modes = set().union(*(MODE_FAMILY_ALIASES.get(mode, {mode}) for mode in modes))
        filtered = work[work["_mode_norm"].isin(expanded_modes)].copy()
        if expanded_modes == set(modes):
            filter_label = f"custom modes: {', '.join(modes)}"
        else:
            filter_label = f"custom mode family: {', '.join(modes)} -> {', '.join(sorted(expanded_modes))}"
    elif phase == "all":
        filtered = work.copy()
        filter_label = "all modes"
    elif phase == "helper":
        filtered = work[work["_mode_norm"].isin(HELPER_MODES)].copy()
        filter_label = "helper modes only"
    elif phase == "precondition":
        filtered = work[work["_mode_norm"].eq("precondition")].copy()
        filter_label = "precondition only"
    elif phase in ("settle", "measurement-settle"):
        filtered = work[work["_mode_norm"].eq("measurement-settle")].copy()
        filter_label = "measurement-settle only"
    else:
        # Default: measurement only. Include known measurement modes and also
        # any unknown non-helper modes, but exclude known helper modes.
        measurement = set(_norm_mode(m) for m in DEFAULT_MEASUREMENT_MODES)
        helpers = set(_norm_mode(m) for m in HELPER_MODES)
        mask = work["_mode_norm"].isin(measurement) | ~work["_mode_norm"].isin(helpers)
        mask &= ~work["_mode_norm"].isin(helpers)
        filtered = work[mask].copy()
        filter_label = "measurement modes only"
        if getattr(args, "primary_sweep", "auto") == "auto":
            for candidates in PRIMARY_SWEEP_MODE_PRIORITY:
                primary = filtered[filtered["_mode_norm"].isin(candidates)].copy()
                if not primary.empty:
                    filtered = primary
                    filter_label = f"primary sweep auto: {', '.join(candidates)}"
                    auto_primary = True
                    break

    if filtered.empty:
        print("⚠️ 指定されたphase/modeに一致する行がありません。全行を使います。")
        filtered = work.copy()

    removed = len(work) - len(filtered)
    if removed > 0:
        print(f"Mode filter ({filter_label}): kept {len(filtered)} row(s), excluded {removed} helper/other row(s).")
    else:
        print(f"Mode filter ({filter_label}): kept all {len(filtered)} row(s).")

    kept = ", ".join(sorted(filtered["mode"].astype(str).unique()))
    excluded = ", ".join(sorted(set(work["mode"].astype(str).unique()) - set(filtered["mode"].astype(str).unique())))
    print(f"  kept modes: {kept if kept else '-'}")
    if excluded:
        print(f"  excluded modes: {excluded}")
    if auto_primary:
        print("  note: primary sweep auto excludes other measurement modes such as fixed summary rows.")

    helper_left = sorted(set(filtered["_mode_norm"]).intersection(set(HELPER_MODES)))
    if helper_left:
        print("⚠️ WARNING: helper modes are included in analysis: " + ", ".join(helper_left))
        print("   Boundary/ripple reports may include precondition/settle points.")

    return filtered.drop(columns=["_mode_norm"], errors="ignore").reset_index(drop=True)

def discard_first_per_target(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if n <= 0:
        return df.copy()

    work = df.copy()
    work["_original_row_order"] = range(len(work))
    target_col = sweep_axis_for(work)["target_col"]
    group_cols = sweep_group_columns(work, target_col)
    work["_sample_index_per_target"] = (
        work.sort_values(group_cols + ["_original_row_order"])
        .groupby(group_cols, dropna=False)
        .cumcount()
    )

    before = len(work)
    work = work[work["_sample_index_per_target"] >= n].copy()
    after = len(work)

    work = work.sort_values("_original_row_order").drop(
        columns=["_sample_index_per_target", "_original_row_order"],
        errors="ignore",
    )

    print(f"Discarded first {n} sample(s) per target: {before - after} row(s) removed.")
    return work.reset_index(drop=True)


def first_existing_cols(df: pd.DataFrame, candidates: list[str]) -> list[str]:
    return [c for c in candidates if c in df.columns]


def sweep_group_columns(df: pd.DataFrame, target_col: str) -> list[str]:
    """Keep appended runs, PDOs and outbound/return points independent."""
    columns = first_existing_cols(df, [target_col, "mode", "pdo_kind"])
    for col in ["run_started_at", "pdo_object_number", "request_current_a",
                "sweep_leg", "sweep_pass", "sweep_direction"]:
        if col in df.columns and df[col].notna().any() and col not in columns:
            columns.append(col)
    return columns


def build_summary(df: pd.DataFrame) -> pd.DataFrame:
    axis = sweep_axis_for(df)
    target_col = axis["target_col"]
    group_cols = sweep_group_columns(df, target_col)
    if not group_cols:
        group_cols = [target_col]
    # A round trip revisits the same voltage.  Keeping the leg/pass in the
    # group key prevents those independent points from being averaged together.
    sweep_identity_cols = [
        col for col in ["sweep_leg", "sweep_pass", "sweep_direction"]
        if col in df.columns and df[col].notna().any()
    ]
    group_cols += [col for col in sweep_identity_cols if col not in group_cols]

    agg_spec = {
        "samples": ("actual_voltage_v", "count"),
        "voltage_mean": ("actual_voltage_v", "mean"),
        "voltage_min": ("actual_voltage_v", "min"),
        "voltage_max": ("actual_voltage_v", "max"),
        "voltage_std": ("actual_voltage_v", "std"),
        "current_mean": ("actual_current_a", "mean"),
        "current_min": ("actual_current_a", "min"),
        "current_max": ("actual_current_a", "max"),
        "power_mean": ("actual_power_w", "mean"),
        "power_min": ("actual_power_w", "min"),
        "power_max": ("actual_power_w", "max"),
    }

    if "ripple_mv" in df.columns:
        agg_spec.update({
            "ripple_mean": ("ripple_mv", "mean"),
            "ripple_min": ("ripple_mv", "min"),
            "ripple_max": ("ripple_mv", "max"),
            "ripple_std": ("ripple_mv", "std"),
        })

    if "condition_module_temp_c" in df.columns and not df["condition_module_temp_c"].isna().all():
        agg_spec.update({
            "module_temp_mean_c": ("condition_module_temp_c", "mean"),
            "module_temp_min_c": ("condition_module_temp_c", "min"),
            "module_temp_max_c": ("condition_module_temp_c", "max"),
        })

    for col in META_COLS:
        if col in df.columns:
            if col in group_cols:
                continue
            agg_spec[col] = (col, "first")

    if "target_load_current_a" in df.columns:
        if "target_load_current_a" not in group_cols:
            agg_spec["target_load_current_a"] = ("target_load_current_a", "first")
    if "target_voltage_v" in df.columns and "target_voltage_v" not in group_cols:
        agg_spec["target_voltage_v"] = ("target_voltage_v", "first")
    if "request_current_a" in df.columns and "request_current_a" not in group_cols:
        agg_spec["request_current_a"] = ("request_current_a", "first")
    if "phase_elapsed_s" in df.columns:
        agg_spec["phase_elapsed_first_s"] = ("phase_elapsed_s", "first")
        agg_spec["phase_elapsed_last_s"] = ("phase_elapsed_s", "last")
    if "_row_number" in df.columns:
        agg_spec["first_row_number"] = ("_row_number", "min")

    summary = (
        df.groupby(group_cols, dropna=False)
        .agg(**agg_spec)
        .reset_index()
    )
    # Preserve acquisition order within every leg.  Sorting purely by target
    # voltage makes a return leg appear to run in the wrong direction.
    chronology_cols = [col for col in ["first_row_number"] if col in summary.columns]
    summary = summary.sort_values(chronology_cols or group_cols).reset_index(drop=True)
    summary["sweep_axis"] = axis["kind"]
    summary["sweep_target"] = summary[target_col]

    summary["voltage_error_v"] = summary["voltage_mean"] - summary["target_voltage_v"]
    summary["voltage_error_pct"] = (summary["voltage_error_v"] / summary["target_voltage_v"]) * 100.0
    delta_group_cols = [col for col in ["run_started_at", "mode", "pdo_kind", "pdo_object_number",
                                       "request_current_a", "sweep_pass", "sweep_leg"] if col in summary.columns]

    if "ripple_mean" in summary.columns:
        summary["ripple_mv_per_v"] = summary["ripple_mean"] / summary["voltage_mean"]
        summary["ripple_pct_of_voltage"] = (summary["ripple_mean"] / 1000.0) / summary["voltage_mean"] * 100.0
        summary["ripple_ppm_of_voltage"] = (summary["ripple_mean"] / 1000.0) / summary["voltage_mean"] * 1_000_000.0
        if delta_group_cols:
            summary["ripple_delta_from_prev_mv"] = summary.groupby(delta_group_cols, dropna=False)["ripple_mean"].diff()
        else:
            summary["ripple_delta_from_prev_mv"] = summary["ripple_mean"].diff()
        summary["ripple_abs_delta_from_prev_mv"] = summary["ripple_delta_from_prev_mv"].abs()

    if delta_group_cols:
        summary["actual_voltage_step_from_prev_v"] = summary.groupby(delta_group_cols, dropna=False)["voltage_mean"].diff()
        summary["target_voltage_step_from_prev_v"] = summary.groupby(delta_group_cols, dropna=False)["target_voltage_v"].diff()
    else:
        summary["actual_voltage_step_from_prev_v"] = summary["voltage_mean"].diff()
        summary["target_voltage_step_from_prev_v"] = summary["target_voltage_v"].diff()

    if "target_load_current_a" in summary.columns and summary["target_load_current_a"].notna().any():
        summary["current_error_a"] = summary["current_mean"] - summary["target_load_current_a"]
        summary["current_error_pct"] = (
            summary["current_error_a"] / summary["target_load_current_a"].replace(0, math.nan)
        ) * 100.0

    if axis["kind"] == "current":
        if delta_group_cols:
            summary["actual_current_step_from_prev_a"] = summary.groupby(delta_group_cols, dropna=False)["current_mean"].diff()
            summary["target_current_step_from_prev_a"] = summary.groupby(delta_group_cols, dropna=False)["target_load_current_a"].diff()
        else:
            summary["actual_current_step_from_prev_a"] = summary["current_mean"].diff()
            summary["target_current_step_from_prev_a"] = summary["target_load_current_a"].diff()

    if "module_temp_mean_c" in summary.columns:
        if delta_group_cols:
            summary["module_temp_delta_from_prev_c"] = summary.groupby(delta_group_cols, dropna=False)["module_temp_mean_c"].diff()
        else:
            summary["module_temp_delta_from_prev_c"] = summary["module_temp_mean_c"].diff()

    return summary


def save_plot(x, y, xlabel: str, ylabel: str, title: str, path: Path) -> None:
    plt.figure()
    plt.plot(x, y, marker="o")
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def save_sweep_plot(summary: pd.DataFrame, value_col: str, xlabel: str, ylabel: str, title: str, path: Path) -> None:
    """Plot independent runs and legs as separate lines in acquisition order."""
    target_col = sweep_axis_for(summary)["target_col"]
    identity = [col for col in ["run_started_at", "mode", "pdo_object_number", "request_current_a",
                               "sweep_pass", "sweep_leg"]
                if col in summary.columns and summary[col].nunique(dropna=False) > 1]
    if not identity:
        save_plot(summary[target_col], summary[value_col], xlabel, ylabel, title, path)
        return

    colors = {"outbound": "#1E5AA8", "return": "#D1493D"}
    labels = {"outbound": "Outbound", "return": "Return"}
    plt.figure()
    for _, group in summary.groupby(identity, sort=False, dropna=False):
        leg = group["sweep_leg"].iloc[0] if "sweep_leg" in group.columns else "Sweep"
        name = str(leg).strip().lower()
        label = labels.get(name, str(leg) or "Sweep")
        details = [str(group[col].iloc[0]) for col in identity if col not in ("sweep_leg", "sweep_pass")]
        if details:
            label += ' / ' + ' / '.join(details)
        plt.plot(
            group[target_col],
            group[value_col],
            marker="o",
            color=colors.get(name),
            label=label,
        )
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def save_scatter(x, y, xlabel: str, ylabel: str, title: str, path: Path) -> None:
    plt.figure()
    plt.scatter(x, y)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def generate_plots(summary: pd.DataFrame, out_prefix: Path) -> None:
    if plt is None:
        raise RuntimeError(
            "Plot generation requires matplotlib. Install it or run with --no-plots."
        )
    axis = sweep_axis_for(summary)
    xlabel = f"{axis['label']} ({axis['unit']})"
    save_sweep_plot(summary, "voltage_error_v", xlabel, "Voltage Error (V)", "Voltage Error", Path(f"{out_prefix}_voltage_error.png"))

    if "voltage_error_pct" in summary.columns:
        save_sweep_plot(summary, "voltage_error_pct", xlabel, "Voltage Error (%)", "Voltage Error (%)", Path(f"{out_prefix}_voltage_error_pct.png"))

    save_sweep_plot(summary, "voltage_mean", xlabel, "Actual Voltage Mean (V)", "Actual Voltage", Path(f"{out_prefix}_voltage_actual.png"))

    if "current_mean" in summary.columns:
        save_sweep_plot(summary, "current_mean", xlabel, "Current Mean (A)", "Average Current", Path(f"{out_prefix}_current.png"))
        if axis["kind"] == "current" and "current_error_a" in summary.columns:
            save_sweep_plot(summary, "current_error_a", xlabel, "Current Error (A)", "Current Tracking Error", Path(f"{out_prefix}_current_error.png"))
        if axis["kind"] == "current" and "current_error_pct" in summary.columns:
            save_sweep_plot(summary, "current_error_pct", xlabel, "Current Error (%)", "Current Tracking Error (%)", Path(f"{out_prefix}_current_error_pct.png"))

    if "power_mean" in summary.columns:
        save_sweep_plot(summary, "power_mean", xlabel, "Power Mean (W)", "Average Power", Path(f"{out_prefix}_power.png"))

    if "ripple_mean" in summary.columns:
        save_sweep_plot(summary, "ripple_mean", xlabel, "Ripple Mean (mV)", "Average Ripple", Path(f"{out_prefix}_ripple.png"))
        if "ripple_delta_from_prev_mv" in summary.columns:
            save_sweep_plot(summary, "ripple_delta_from_prev_mv", xlabel, "Ripple jump from previous point (mV)", "Ripple Step-to-Step Jump", Path(f"{out_prefix}_ripple_jump.png"))

    if "module_temp_mean_c" in summary.columns:
        save_sweep_plot(summary, "module_temp_mean_c", xlabel, "ASD-PD31 Module Temp (C)", "ASD-PD31 Module Temperature", Path(f"{out_prefix}_module_temp.png"))
        if "ripple_mean" in summary.columns:
            save_scatter(summary["module_temp_mean_c"], summary["ripple_mean"], "ASD-PD31 Module Temp (C)", "Ripple Mean (mV)", "Ripple vs ASD Temperature", Path(f"{out_prefix}_ripple_vs_temp.png"))

    if "ripple_mv_per_v" in summary.columns:
        save_sweep_plot(summary, "ripple_mv_per_v", xlabel, "Ripple / Voltage (mV/V)", "Ripple Normalized by Voltage", Path(f"{out_prefix}_ripple_mv_per_v.png"))

    if "ripple_pct_of_voltage" in summary.columns:
        save_sweep_plot(summary, "ripple_pct_of_voltage", xlabel, "Ripple / Voltage (%)", "Ripple as % of Output Voltage", Path(f"{out_prefix}_ripple_pct_of_voltage.png"))

    if "ripple_ppm_of_voltage" in summary.columns:
        save_sweep_plot(summary, "ripple_ppm_of_voltage", xlabel, "Ripple / Voltage (ppm)", "Ripple as ppm of Output Voltage", Path(f"{out_prefix}_ripple_ppm_of_voltage.png"))


def detect_ripple_boundary(summary: pd.DataFrame) -> dict[str, object]:
    result: dict[str, object] = {
        "found": False,
        "message": "ripple_mean列がないため、境界検出はできません。",
    }
    if "ripple_mean" not in summary.columns or len(summary) < 2:
        return result

    axis = sweep_axis_for(summary)
    target_col = axis["target_col"]
    actual_col = "current_mean" if axis["kind"] == "current" else "voltage_mean"
    actual_unit = "A" if axis["kind"] == "current" else "V"
    work = summary.copy()
    if "sweep_leg" in work.columns and work["sweep_leg"].nunique(dropna=True) > 1:
        outbound = work[work["sweep_leg"].astype(str).str.lower().eq("outbound")]
        work = outbound if not outbound.empty else work[work["sweep_leg"].eq(work["sweep_leg"].dropna().iloc[0])]
    work = work.sort_values(target_col).reset_index(drop=True)
    work["next_target"] = work[target_col].shift(-1)
    work["next_actual"] = work[actual_col].shift(-1)
    work["next_ripple_mean"] = work["ripple_mean"].shift(-1)
    work["ripple_jump_to_next_mv"] = work["next_ripple_mean"] - work["ripple_mean"]
    work["ripple_abs_jump_to_next_mv"] = work["ripple_jump_to_next_mv"].abs()

    candidates = work.dropna(subset=["ripple_abs_jump_to_next_mv"]).copy()
    if candidates.empty:
        return result

    idx = candidates["ripple_abs_jump_to_next_mv"].idxmax()
    row = candidates.loc[idx]

    result.update({
        "found": True,
        "axis": axis["kind"],
        "unit": axis["unit"],
        "lower_target": float(row[target_col]),
        "upper_target": float(row["next_target"]),
        "lower_actual": float(row[actual_col]),
        "upper_actual": float(row["next_actual"]),
        "actual_unit": actual_unit,
        "lower_ripple_mv": float(row["ripple_mean"]),
        "upper_ripple_mv": float(row["next_ripple_mean"]),
        "jump_mv": float(row["ripple_jump_to_next_mv"]),
        "abs_jump_mv": float(row["ripple_abs_jump_to_next_mv"]),
    })

    # Simple interpretation.
    if row["ripple_jump_to_next_mv"] < 0:
        result["direction"] = "down"
        if axis["kind"] == "time":
            result["message"] = (
                f"保持中の最大ripple低下は t={row[target_col]:.4g}{axis['unit']} → "
                f"{row['next_target']:.4g}{axis['unit']} の間です。"
            )
        else:
            result["message"] = (
                f"最大のripple低下は {row[target_col]:.4g}{axis['unit']} → "
                f"{row['next_target']:.4g}{axis['unit']} の間です。"
            )
    else:
        result["direction"] = "up"
        if axis["kind"] == "time":
            result["message"] = (
                f"保持中の最大ripple上昇は t={row[target_col]:.4g}{axis['unit']} → "
                f"{row['next_target']:.4g}{axis['unit']} の間です。"
            )
        else:
            result["message"] = (
                f"最大のripple上昇は {row[target_col]:.4g}{axis['unit']} → "
                f"{row['next_target']:.4g}{axis['unit']} の間です。"
            )

    return result


def fmt(v: object, digits: int = 3, suffix: str = "") -> str:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return "-"
        return f"{float(v):.{digits}f}{suffix}"
    except Exception:
        return "-"


def mode_counts_text(df_all: pd.DataFrame) -> str:
    if "mode" not in df_all.columns:
        return "mode列なし"
    counts = df_all["mode"].astype(str).value_counts()
    return ", ".join(f"{k}={v}" for k, v in counts.items())


def build_human_report(df_all: pd.DataFrame, df: pd.DataFrame, summary: pd.DataFrame, boundary: dict[str, object], args: argparse.Namespace) -> str:
    lines: list[str] = []
    device = "KM003C" if "_device" in df.columns and df["_device"].eq("KM003C").any() else "Sweep"
    timed_hold_report = (
        "mode" in df.columns
        and df["mode"].map(_norm_mode).str.match(r"^hold-(?:fixed|pps|avs)$").any()
    )
    sequence_report = (
        "mode" in df.columns
        and df["mode"].map(_norm_mode).eq("avs-sequence").any()
    )
    lines.append(
        "ASD-PD31 AVS sequence CSV かんたんレポート"
        if sequence_report
        else "ASD-PD31 timed hold CSV かんたんレポート"
        if timed_hold_report
        else f"{device} sweep CSV かんたんレポート"
    )
    lines.append("=" * 46)
    lines.append("")
    lines.append(f"入力ファイル: {getattr(args, 'csv', '-')}")
    lines.append(f"全CSV行数: {df_all.attrs.get('input_rows', len(df_all))}")
    lines.append(f"有効な実測行数: {len(df_all)}")
    lines.append(f"解析に使った行数: {len(df)}")
    lines.append(f"mode内訳: {mode_counts_text(df_all)}")
    if device == "KM003C":
        lines.append("測定値は KM003C の ADC 実測値です。電流・電力の符号は元データのままです。")
        lines.append("要求電流は PD の要求値で、外部電子負荷の設定電流ではありません。")
        lines.append("コマンド状態 sent_unverified は PD の受理・規格適合を証明しません。")
    phase = "all" if getattr(args, "include_all_modes", False) else getattr(args, "phase", "measurement")
    if getattr(args, "modes", None):
        lines.append(f"解析対象: カスタムmode指定 ({args.modes})")
    elif phase == "all":
        lines.append("解析対象: 全mode（precondition / measurement-settle も含む）")
        lines.append("⚠️ 注意: 全mode解析では、48V preconditionなどが境界検出に混ざることがあります。")
    elif phase == "measurement":
        lines.append("解析対象: 測定本体のみ（precondition / measurement-settle は除外）")
    else:
        lines.append(f"解析対象: phase={phase}")
    lines.append("")

    if summary.empty:
        lines.append("集計結果が空です。")
        return "\n".join(lines)

    axis = sweep_axis_for(summary)
    target_col = axis["target_col"]
    target_min = summary[target_col].min()
    target_max = summary[target_col].max()
    axis_jp = {"current": "電流", "time": "経過時間"}.get(axis["kind"], "電圧")
    lines.append("1. 何を測ったか")
    range_label = "経過時間範囲" if axis["kind"] == "time" else f"設定{axis_jp}範囲"
    lines.append(f"- {range_label}: {fmt(target_min, 4, axis['unit'])} 〜 {fmt(target_max, 4, axis['unit'])}")
    if axis["kind"] == "current":
        held_voltage = summary["target_voltage_v"].dropna()
        if not held_voltage.empty:
            lines.append(f"- 固定電圧: 約 {fmt(held_voltage.iloc[0], 4, 'V')}")
    elif axis["kind"] == "time":
        held_voltage = summary["target_voltage_v"].dropna()
        held_current = summary.get("target_load_current_a", pd.Series(dtype=float)).dropna()
        if not held_voltage.empty:
            lines.append(f"- 設定電圧: 約 {fmt(held_voltage.iloc[0], 4, 'V')}")
        if not held_current.empty:
            lines.append(f"- 設定負荷電流: 約 {fmt(held_current.iloc[0], 3, 'A')}")
    elif "target_load_current_a" in summary.columns:
        cur = summary["target_load_current_a"].dropna()
        if not cur.empty:
            lines.append(f"- 設定負荷電流: 約 {fmt(cur.iloc[0], 3, 'A')}")
    lines.append(f"- {axis_jp}点数: {len(summary)}")
    if "request_current_a" in summary.columns:
        currents = sorted(summary["request_current_a"].dropna().unique())
        if currents:
            lines.append("- PD要求電流: " + ', '.join(fmt(value, 3, 'A') for value in currents))
    if device == "KM003C" and ("target_load_current_a" not in summary.columns or summary["target_load_current_a"].isna().all()):
        lines.append("- 外部負荷の設定電流: 未記録（要求電流で代用しません）")
    if "run_started_at" in summary.columns:
        lines.append(f"- 実行数: {summary['run_started_at'].nunique(dropna=True)}（別実行・往路/復路は個別集計）")
    # Human-friendly guard: if a huge target gap remains, helper rows may have leaked in.
    if len(summary) >= 3:
        gaps = summary[target_col].sort_values().diff().dropna().abs()
        if not gaps.empty:
            med_gap = gaps.median()
            max_gap = gaps.max()
            if med_gap > 0 and max_gap > med_gap * 10:
                lines.append(f"⚠️ {axis_jp}点に大きな飛びがあります: 最大gap {fmt(max_gap, 4, axis['unit'])} / 通常gap {fmt(med_gap, 4, axis['unit'])}")
                lines.append("   → preconditionなどの補助行が混ざっていないか確認してください。")
    if "samples" in summary.columns:
        lines.append(f"- 1点あたり測定数: 最小 {int(summary['samples'].min())} / 最大 {int(summary['samples'].max())}")
    lines.append("")

    if axis["kind"] == "current":
        lines.append("2. 電流追従と電圧降下")
    elif axis["kind"] == "time":
        lines.append("2. 保持中の電圧・電流は安定しているか")
    else:
        lines.append("2. 電圧は狙い通りか")
    if axis["kind"] == "current" and "current_error_a" in summary.columns:
        lines.append(f"- 平均電流誤差: {fmt(summary['current_error_a'].mean(), 4, 'A')}")
        lines.append(f"- 最大絶対電流誤差: {fmt(summary['current_error_a'].abs().max(), 4, 'A')}")
    if axis["kind"] == "time":
        lines.append(f"- 実測電圧: 平均 {fmt(summary['voltage_mean'].mean(), 4, 'V')} / 最小 {fmt(summary['voltage_min'].min(), 4, 'V')} / 最大 {fmt(summary['voltage_max'].max(), 4, 'V')}")
        lines.append(f"- 実測電流: 平均 {fmt(summary['current_mean'].mean(), 4, 'A')} / 最小 {fmt(summary['current_min'].min(), 4, 'A')} / 最大 {fmt(summary['current_max'].max(), 4, 'A')}")
        lines.append(f"- 実測電力: 平均 {fmt(summary['power_mean'].mean(), 3, 'W')} / 最大 {fmt(summary['power_max'].max(), 3, 'W')}")
    lines.append(f"- 平均電圧誤差: {fmt(summary['voltage_error_v'].mean(), 4, 'V')}")
    lines.append(f"- 最小電圧誤差: {fmt(summary['voltage_error_v'].min(), 4, 'V')}")
    lines.append(f"- 最大電圧誤差: {fmt(summary['voltage_error_v'].max(), 4, 'V')}")
    if device == "KM003C":
        lines.append(f"- 実測電流: 平均 {fmt(df['actual_current_a'].mean(), 4, 'A')} / 最小 {fmt(df['actual_current_a'].min(), 4, 'A')} / 最大 {fmt(df['actual_current_a'].max(), 4, 'A')}")
        lines.append(f"- 実測電力: 平均 {fmt(df['actual_power_w'].mean(), 3, 'W')} / 最小 {fmt(df['actual_power_w'].min(), 3, 'W')} / 最大 {fmt(df['actual_power_w'].max(), 3, 'W')}")
    lines.append("  電圧誤差 = 実測平均電圧 − 設定電圧。負なら設定より低く、正なら設定より高い値です。")
    lines.append("")

    if "ripple_mean" not in summary.columns:
        lines.append("3. リップル")
        lines.append("- リップルは未測定です。ADC 測定点のばらつきからリップルを推定しません。")
        lines.append("- リップル境界の有無は判定できません。")
        lines.append("")
    if "ripple_mean" in summary.columns:
        rmin_idx = summary["ripple_mean"].idxmin()
        rmax_idx = summary["ripple_mean"].idxmax()
        rmin = summary.loc[rmin_idx]
        rmax = summary.loc[rmax_idx]
        lines.append("3. リップルの見どころ")
        location_label = "t=" if axis["kind"] == "time" else "target "
        lines.append(f"- 最小ripple: {fmt(rmin['ripple_mean'], 1, 'mV')} @ {location_label}{fmt(rmin[target_col], 4, axis['unit'])}")
        lines.append(f"- 最大ripple: {fmt(rmax['ripple_mean'], 1, 'mV')} @ {location_label}{fmt(rmax[target_col], 4, axis['unit'])}")
        if boundary.get("found"):
            lines.append("")
            lines.append("4. 保持中の最大変化" if axis["kind"] == "time" else "4. モード境界っぽい場所")
            lines.append(f"- {boundary['message']}")
            target_prefix = "t=" if axis["kind"] == "time" else "target "
            actual_label = "voltage" if axis["kind"] == "time" else "actual"
            lines.append(
                f"- {target_prefix}{float(boundary['lower_target']):.4g}{axis['unit']}: "
                f"{actual_label} {float(boundary['lower_actual']):.4g}{boundary['actual_unit']} / "
                f"ripple {float(boundary['lower_ripple_mv']):.1f}mV"
            )
            lines.append(
                f"- {target_prefix}{float(boundary['upper_target']):.4g}{axis['unit']}: "
                f"{actual_label} {float(boundary['upper_actual']):.4g}{boundary['actual_unit']} / "
                f"ripple {float(boundary['upper_ripple_mv']):.1f}mV"
            )
            lines.append(f"- ripple差: {float(boundary['jump_mv']):+.1f}mV")
            if float(boundary["abs_jump_mv"]) >= 30:
                lines.append("  → ここは単なるノイズではなく、電源内部の制御モード/レンジ切替の候補としてかなり怪しいです。")
            else:
                lines.append("  → 大きな崖は見えません。なだらかな変化の可能性が高いです。")
        else:
            lines.append(f"- 境界検出: {boundary.get('message', '-')}")
        lines.append("")

    if "module_temp_mean_c" in summary.columns:
        lines.append("5. ASD-PD31自身の温度")
        lines.append(f"- 最低温度: {fmt(summary['module_temp_min_c'].min(), 1, '℃')}")
        lines.append(f"- 最高温度: {fmt(summary['module_temp_max_c'].max(), 1, '℃')}")
        lines.append(f"- 測定開始付近: {fmt(summary['module_temp_mean_c'].iloc[0], 1, '℃')}")
        lines.append(f"- 測定終了付近: {fmt(summary['module_temp_mean_c'].iloc[-1], 1, '℃')}")
        if "ripple_mean" in summary.columns and summary["module_temp_mean_c"].nunique(dropna=True) >= 2:
            corr = summary[["module_temp_mean_c", "ripple_mean"]].corr().iloc[0, 1]
            lines.append(f"- 温度とrippleの相関係数: {fmt(corr, 3)}")
            lines.append("  ※相関が高くても因果とは限りません。昇順/降順の比較で切り分けるのが安全です。")
        lines.append("")

    lines.append("出力ファイルの見方")
    lines.append(f"- *_summary.csv: {axis_jp}点ごとの平均値。まずこれを見る。")
    ripple_purpose = "保持中の時間変化を見る主役。" if axis["kind"] == "time" else "モード境界探しの主役。"
    lines.append("- *_voltage_actual.png / *_voltage_error.png: 設定電圧に対する実測電圧・誤差。")
    lines.append("- *_current.png / *_power.png: 実測電流・電力。")
    if "ripple_mean" in summary.columns:
        lines.append(f"- *_ripple.png: {axis_jp}に対するrippleの形。{ripple_purpose}")
        lines.append(f"- *_ripple_jump.png: 隣の{axis_jp}点との差。崖があると一発で見える。")
    if axis["kind"] == "current":
        lines.append("- *_current_error.png: 設定電流に対する実測電流の追従誤差。")
    if "module_temp_mean_c" in summary.columns:
        lines.append("- *_module_temp.png: ASD-PD31自身の温度推移。測定器側のコンディション確認用。")
    lines.append("- *_normalized.csv: 元CSVから解析対象列を整えたもの。")
    lines.append("")
    lines.append("解析の範囲")
    if "ripple_mean" not in summary.columns:
        lines.append("電圧誤差と実測電流・電力を確認してください。リップルは未測定のため評価対象外です。")
    elif boundary.get("found") and float(boundary.get("abs_jump_mv", 0)) >= 30:
        if axis["kind"] == "time":
            lines.append(
                f"この保持測定では t={float(boundary['lower_target']):.4g}{axis['unit']} と "
                f"{float(boundary['upper_target']):.4g}{axis['unit']} の間に、"
                f"rippleが {float(boundary['jump_mv']):+.1f}mV 変化しています。"
            )
        else:
            lines.append(
                f"このCSVでは target {float(boundary['lower_target']):.4g}{axis['unit']} と "
                f"{float(boundary['upper_target']):.4g}{axis['unit']} の間に、"
                f"rippleが {float(boundary['jump_mv']):+.1f}mV 変わる大きな境界が見えます。"
            )
    else:
        lines.append(
            "この保持測定では、rippleの急激な時間変化は目立ちません。"
            if axis["kind"] == "time"
            else "このCSVでは、rippleの急激な境界は目立ちません。"
        )
    return "\n".join(lines)


def save_human_report(df_all: pd.DataFrame, df: pd.DataFrame, summary: pd.DataFrame, boundary: dict[str, object], args: argparse.Namespace, out_prefix: Path) -> Path:
    report_path = Path(f"{out_prefix}_human_report.txt")
    text = build_human_report(df_all, df, summary, boundary, args)
    report_path.write_text(text, encoding="utf-8")
    return report_path


def build_english_report(df_all: pd.DataFrame, df: pd.DataFrame, summary: pd.DataFrame,
                         boundary: dict[str, object], args: argparse.Namespace) -> str:
    axis = sweep_axis_for(summary)
    target = axis['target_col']
    is_km = '_device' in df.columns and df['_device'].eq('KM003C').any()
    device = 'KM003C' if is_km else 'Sweep'
    lines = [f'{device} sweep CSV report', '=' * 46, '',
             f"Input file: {getattr(args, 'csv', '-')}",
             f"Input CSV rows: {df_all.attrs.get('input_rows', len(df_all))}",
             f'Valid measurement rows: {len(df_all)}', f'Analyzed rows: {len(df)}',
             f'Modes: {mode_counts_text(df_all)}',
             f"Phase selection: {getattr(args, 'phase', 'measurement')}",
             f"Mode selection: {getattr(args, 'modes', None) or 'automatic'}", '']
    lines += ['1. Measurement',
              f"- {axis['label']} range: {fmt(summary[target].min(), 4, axis['unit'])} to {fmt(summary[target].max(), 4, axis['unit'])}",
              f'- Separate sweep points: {len(summary)}',
              f"- Samples per point: {int(summary['samples'].min())} to {int(summary['samples'].max())}"]
    if 'run_started_at' in summary.columns:
        lines.append(f"- Recorded runs: {summary['run_started_at'].nunique(dropna=True)}")
    if 'sweep_leg' in summary.columns:
        legs = ', '.join(summary['sweep_leg'].dropna().astype(str).unique())
        lines.append(f'- Sweep legs: {legs or "not recorded"}')
    if 'request_current_a' in summary.columns:
        currents = sorted(summary['request_current_a'].dropna().unique())
        lines.append('- PD request current: ' + (', '.join(fmt(value, 3, 'A') for value in currents) or 'not recorded'))
    load = summary.get('target_load_current_a', pd.Series(dtype=float)).dropna()
    lines.append('- External load current setting: ' +
                 (', '.join(fmt(value, 3, 'A') for value in sorted(load.unique())) if not load.empty else 'not recorded'))
    lines += ['', '2. Voltage, current and power',
              f"- Mean voltage error across points: {fmt(summary['voltage_error_v'].mean(), 4, 'V')}",
              f"- Voltage error range: {fmt(summary['voltage_error_v'].min(), 4, 'V')} to {fmt(summary['voltage_error_v'].max(), 4, 'V')}",
              f"- Maximum absolute voltage error: {fmt(summary['voltage_error_v'].abs().max(), 4, 'V')}",
              f"- Measured voltage range: {fmt(df['actual_voltage_v'].min(), 4, 'V')} to {fmt(df['actual_voltage_v'].max(), 4, 'V')}",
              f"- Measured current mean / min / max: {fmt(df['actual_current_a'].mean(), 4, 'A')} / {fmt(df['actual_current_a'].min(), 4, 'A')} / {fmt(df['actual_current_a'].max(), 4, 'A')}",
              f"- Measured power mean / min / max: {fmt(df['actual_power_w'].mean(), 3, 'W')} / {fmt(df['actual_power_w'].min(), 3, 'W')} / {fmt(df['actual_power_w'].max(), 3, 'W')}"]
    if 'current_error_a' in summary.columns:
        lines.append(f"- Maximum absolute load current error: {fmt(summary['current_error_a'].abs().max(), 4, 'A')}")
    lines += ['', '3. Ripple']
    if 'ripple_mean' in summary.columns and summary['ripple_mean'].notna().any():
        lines.append(f"- Mean ripple range: {fmt(summary['ripple_mean'].min(), 1, 'mV')} to {fmt(summary['ripple_mean'].max(), 1, 'mV')}")
        if boundary.get('found'):
            lines.append(f"- Largest adjacent ripple change: {float(boundary['lower_target']):.4g}{axis['unit']} to {float(boundary['upper_target']):.4g}{axis['unit']}, {float(boundary['jump_mv']):+.1f}mV")
    else:
        lines += ['- Ripple was not measured; no ripple boundary assessment is available.',
                  '- Variation between ADC samples is not treated as ripple.']
    if 'module_temp_mean_c' in summary.columns:
        lines += ['', '4. Instrument temperature',
                  f"- Range: {fmt(summary['module_temp_min_c'].min(), 1, 'C')} to {fmt(summary['module_temp_max_c'].max(), 1, 'C')}"]
    lines += ['', 'Interpretation',
              '- Appended runs, PDO/current settings and outbound/return legs are summarized separately.',
              '- Failed/interrupted request rows and rows without finite measurements are excluded.']
    if is_km:
        lines += ['- Current and power retain the signs recorded by the KM003C ADC.',
                  '- PD request current is not the external electronic load setting.',
                  '- sent_unverified records command delivery; it does not verify PD acceptance or compliance.']
    lines += ['', 'Output files', '- *_normalized.csv: analyzed measurements and original command evidence.',
              '- *_summary.csv: statistics for each separate sweep point.',
              '- *_voltage_actual.png: measured voltage versus requested voltage.',
              '- *_voltage_error.png / *_voltage_error_pct.png: voltage error in V / percent.',
              '- *_current.png / *_power.png: measured current / power.',
              '- *_human_report.txt / *_human_report.en.txt: Japanese / English report.']
    if getattr(args, 'no_plots', False):
        lines.append('- PNG plots were disabled for this analysis.')
    return '\n'.join(lines) + '\n'


def print_console_summary(summary: pd.DataFrame, boundary: dict[str, object], max_rows: int = 999) -> None:
    axis = sweep_axis_for(summary)
    cols = [
        "target_voltage_v",
        "target_load_current_a",
        "request_current_a",
        "mode",
        "pdo_kind",
        "samples",
        "voltage_mean",
        "voltage_error_v",
        "current_mean",
        "current_error_a",
        "power_mean",
    ]
    if "ripple_mean" in summary.columns:
        cols += ["ripple_mean", "ripple_delta_from_prev_mv"]
    if "module_temp_mean_c" in summary.columns:
        cols += ["module_temp_mean_c"]
    if "ripple_mv_per_v" in summary.columns:
        cols += ["ripple_mv_per_v"]

    display = summary[[col for col in first_existing_cols(summary, cols) if summary[col].notna().any()]].copy()

    rename = {
        "target_voltage_v": "set_V",
        "target_load_current_a": "set_A",
        "request_current_a": "req_A",
        "mode": "mode",
        "pdo_kind": "pdo",
        "samples": "n",
        "voltage_mean": "V_mean",
        "voltage_error_v": "V_err",
        "current_mean": "I_mean",
        "current_error_a": "I_err",
        "power_mean": "P_mean",
        "ripple_mean": "Ripple",
        "ripple_delta_from_prev_mv": "dRipple",
        "module_temp_mean_c": "ASD_Temp",
        "ripple_mv_per_v": "Ripple_mV/V",
    }
    display = display.rename(columns=rename)

    title = "Timed Hold Summary" if axis["kind"] == "time" else "Sweep Summary"
    print(f"\n=== {title} ===")
    print(display.head(max_rows).to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    if boundary.get("found"):
        print("\n=== Ripple Boundary Candidate ===")
        print(
            f"{float(boundary['lower_target']):.4g}{axis['unit']} -> "
            f"{float(boundary['upper_target']):.4g}{axis['unit']} : "
            f"{float(boundary['lower_ripple_mv']):.1f}mV -> "
            f"{float(boundary['upper_ripple_mv']):.1f}mV "
            f"({float(boundary['jump_mv']):+.1f}mV)"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Analyze KM003C Sweep CSV {__version__} (ASD-style reports and plots)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("csv", help="Input sweep CSV")
    parser.add_argument("--out", default=None, help="Output prefix. Default: input filename stem")
    parser.add_argument("--discard-first", type=int, default=0, help="Discard first N samples per target in each run/leg")
    parser.add_argument("--phase", choices=["measurement", "all", "helper", "precondition", "settle", "measurement-settle"], default="measurement", help="Which phase to analyze. Default: measurement only")
    parser.add_argument("--primary-sweep", choices=["auto", "off"], default="auto", help="When --phase measurement is used, prefer the main sweep mode if present, e.g. avs-continuous over fixed summary rows. Default: auto")
    parser.add_argument("--include-all-modes", action="store_true", help="Deprecated alias for --phase all")
    parser.add_argument("--modes", default=None, help="Comma-separated raw mode list to analyze. Overrides --phase")
    parser.add_argument("--no-plots", action="store_true", help="Do not generate PNG plots")
    parser.add_argument("--no-print", action="store_true", help="Do not print summary table")
    parser.add_argument("--no-report", action="store_true", help="Do not generate human-readable report")
    parser.add_argument('--report-language', '--lang', choices=['ja', 'en', 'both'], default='both',
                        help='Report language (default: both; Japanese .txt and English .en.txt)')
    args = parser.parse_args()
    if args.discard_first < 0:
        parser.error('--discard-first must be nonnegative')

    path = Path(args.csv)
    if not path.exists():
        print(f"❌ Error: ファイルが見つかりません: {path}")
        return 2

    try:
        if not args.no_plots and plt is None:
            raise ValueError('PNG plots require matplotlib. Install requirements-analysis.txt or use --no-plots.')
        df_all = load_and_normalize(path)
        df = filter_modes(df_all, args)
        df = discard_first_per_target(df, args.discard_first)
        if df.empty:
            print("❌ Error: discard/filter後にデータが空になりました。")
            return 1

        summary = build_summary(df)
        boundary = detect_ripple_boundary(summary)

        out_prefix = Path(args.out) if args.out else path.with_suffix("")
        out_prefix.parent.mkdir(parents=True, exist_ok=True)
        normalized_csv = Path(f"{out_prefix}_normalized.csv")
        summary_csv = Path(f"{out_prefix}_summary.csv")

        df.to_csv(normalized_csv, index=False, encoding="utf-8-sig")
        summary.to_csv(summary_csv, index=False, encoding="utf-8-sig")

        print(f"Loaded rows: {len(df_all)}")
        print(f"Analyzed rows: {len(df)}")
        print(f"Saved normalized data: {normalized_csv}")
        print(f"Saved summary: {summary_csv}")

        if not args.no_report:
            if args.report_language in ('ja', 'both'):
                report_path = save_human_report(df_all, df, summary, boundary, args, out_prefix)
                print(f"Saved Japanese report: {report_path}")
            if args.report_language in ('en', 'both'):
                report_path = Path(f'{out_prefix}_human_report.en.txt')
                report_path.write_text(build_english_report(df_all, df, summary, boundary, args), encoding='utf-8')
                print(f'Saved English report: {report_path}')

        if not args.no_print:
            print_console_summary(summary, boundary)

        if not args.no_plots:
            generate_plots(summary, out_prefix)
            print("All plots generated successfully.")

        return 0

    except Exception as e:
        print(f"❌ Error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
