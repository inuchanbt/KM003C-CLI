"""AVS analysis adapted from CY4500 CLI (MIT, copyright 2026 inuchanbt).

Only pure analysis is reused; KM003C retains its measurement units and clocks.
Sampling-dependent defaults suit the native 40 ms polling interval. See LICENSE.
"""
from __future__ import annotations
from dataclasses import dataclass
from bisect import bisect_left
from typing import Optional, Sequence

TRANSITION_BASELINE_WINDOW_US = 100_000
TRANSITION_BASELINE_GUARD_US = 5_000
TRANSITION_MOVEMENT_MIN_THRESHOLD_V = 0.050
TRANSITION_MOVEMENT_MAD_MULTIPLIER = 6.0
TRANSITION_MOVEMENT_SUSTAIN_SAMPLES = 2
TRANSITION_TARGET_BAND_FRACTION = 0.01
TRANSITION_SETTLE_HOLD_US = 80_000
TRANSITION_SETTLE_MAX_SAMPLE_GAP_US = 100_000

TRANSITION_PLATEAU_LOOKBACK_US = 400_000
TRANSITION_PLATEAU_MIN_SAMPLES = 6
TRANSITION_RELATIVE_BAND_FRACTION = 0.005
TRANSITION_RELATIVE_SETTLE_HOLD_US = 80_000
TRANSITION_PLATEAU_STABILITY_MIN_SPAN_V = 0.100
TRANSITION_PLATEAU_STABILITY_FRACTION = 0.0025
TRANSITION_PLATEAU_TARGET_GUARD_FRACTION = 0.10


@dataclass(frozen=True)
class SyncPDSample:
    row_index: int
    sno: Optional[int]
    message: str
    start_us: int
    end_us: int
    vbus_V: Optional[float]
    data: bytes


@dataclass(frozen=True)
class SyncScopeSample:
    timestamp_us: int
    vbus_V: float
    ibus_A: Optional[float] = None
    cc1_V: Optional[float] = None
    cc2_V: Optional[float] = None


@dataclass(frozen=True)
class AVSTransitionAnalysis:
    request_row_index: int
    request_sno: Optional[int]
    direction: str
    target_voltage_V: float
    requested_current_A: Optional[float]
    request_start_us: int
    request_end_us: int
    accept_start_us: Optional[int]
    accept_latency_us: Optional[int]
    ps_rdy_start_us: Optional[int]
    ps_rdy_latency_us: Optional[int]
    request_scope_timestamp_us: Optional[int]
    request_scope_vbus_V: Optional[float]
    ps_rdy_scope_timestamp_us: Optional[int]
    ps_rdy_scope_vbus_V: Optional[float]
    baseline_vbus_V: Optional[float]
    baseline_noise_mad_V: Optional[float]
    movement_threshold_V: Optional[float]
    movement_start_us: Optional[int]
    movement_latency_us: Optional[int]
    target_crossing_us: Optional[int]
    target_crossing_latency_us: Optional[int]
    target_band_first_entry_us: Optional[int]
    target_band_first_entry_latency_us: Optional[int]
    settling_us: Optional[int]
    settling_latency_us: Optional[int]
    settling_hold_us: int
    target_band_fraction: float
    average_slew_V_per_s: Optional[float]
    observed_plateau_V: Optional[float]
    observed_plateau_mad_V: Optional[float]
    observed_plateau_sample_count: int
    observed_band_fraction: float
    observed_band_first_entry_us: Optional[int]
    observed_band_first_entry_latency_us: Optional[int]
    observed_settling_us: Optional[int]
    observed_settling_latency_us: Optional[int]
    observed_average_slew_V_per_s: Optional[float]
    flags: tuple[str, ...]
    supply_type: str = 'EPR_AVS'
    request_message: str = 'EPR_REQUEST'
    pdo_object_position: Optional[int] = None
    selected_pdo: Optional[int] = None

    @property
    def request_mode(self) -> str:
        return self.supply_type

    @property
    def object_position(self) -> Optional[int]:
        return self.pdo_object_position


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if not n:
        raise ValueError("median of empty sequence")
    mid = n // 2
    if n & 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mad(values: Sequence[float], center: Optional[float] = None) -> float:
    if not values:
        return 0.0
    if center is None:
        center = _median(values)
    return _median([abs(float(v) - center) for v in values])


def _nearest_scope_sample(
    samples: Sequence[SyncScopeSample],
    timestamp_us: int,
) -> Optional[SyncScopeSample]:
    if not samples:
        return None
    import bisect
    times = [s.timestamp_us for s in samples]
    pos = bisect.bisect_left(times, int(timestamp_us))
    choices = []
    if pos < len(samples):
        choices.append(samples[pos])
    if pos:
        choices.append(samples[pos - 1])
    return min(choices, key=lambda s: abs(s.timestamp_us - timestamp_us))


def _decode_avs_request_from_pd_sample(pd):
    """AVS requires an EPR_REQUEST with RDO and selected EPR AVS PDO."""
    if pd.message != 'EPR_REQUEST' or len(pd.data) != 8:
        return None
    rdo = int.from_bytes(pd.data[:4], 'little')
    pdo = int.from_bytes(pd.data[4:], 'little')
    if (pdo >> 28) != 0xD:  # Augmented PDO, EPR AVS subtype 1.
        return None
    return ((rdo >> 9) & 0xFFF) * 0.025, (rdo & 0x7F) * 0.05


@dataclass(frozen=True)
class ProgrammableRequest:
    voltage_V: float
    current_A: float
    supply_type: str
    object_position: int
    selected_pdo: int


RESET_MESSAGES = frozenset(('SOFT_RESET', 'HARD_RESET', 'VBUS_DN', 'DETACH', 'CONNECT', 'DISCONNECT'))
REQUEST_MESSAGES = frozenset(('REQUEST', 'EPR_REQUEST'))
STOP_MESSAGES = RESET_MESSAGES | frozenset(('REJECT', 'WAIT', 'NOT_SUPPORTED'))


def decode_programmable_requests(pd_samples):
    """Resolve each SPR RDO against the most recent captured Source Capabilities.

    PPS uses an 11-bit 20 mV voltage field; SPR/EPR AVS use 12-bit 25 mV.
    APDO subtypes and RDO units agree with Linux include/linux/usb/pd.h.
    Never infer the PDO type from the requested voltage or a future advert.
    """
    source_pdos = []
    decoded, skipped = {}, {}
    for index, sample in enumerate(pd_samples):
        if sample.message in RESET_MESSAGES:
            source_pdos = []
        if sample.message == 'SOURCE_CAPABILITIES':
            data = sample.data
            source_pdos = ([int.from_bytes(data[n:n + 4], 'little') for n in range(0, len(data), 4)]
                           if 0 < len(data) <= 28 and len(data) % 4 == 0 else [])
        if sample.message not in REQUEST_MESSAGES:
            continue
        reason = None
        if sample.message == 'EPR_REQUEST':
            values = _decode_avs_request_from_pd_sample(sample)
            if values is not None:
                rdo = int.from_bytes(sample.data[:4], 'little')
                decoded[index] = ProgrammableRequest(*values, 'EPR_AVS', (rdo >> 28) & 15,
                                                    int.from_bytes(sample.data[4:], 'little'))
            else:
                reason = 'unsupported_or_truncated_epr_request'
        elif len(sample.data) != 4:
            reason = 'truncated_or_invalid_spr_request'
        else:
            rdo = int.from_bytes(sample.data, 'little')
            position = (rdo >> 28) & 15
            if not source_pdos:
                reason = 'source_capabilities_missing'
            elif not 1 <= position <= len(source_pdos):
                reason = 'pdo_object_position_unavailable'
            else:
                pdo = source_pdos[position - 1]
                subtype = pdo >> 28
                if subtype in (0xC, 0xE):
                    step, mask, kind = ((.020, 0x7FF, 'SPR_PPS') if subtype == 0xC
                                        else (.025, 0xFFF, 'SPR_AVS'))
                    decoded[index] = ProgrammableRequest(((rdo >> 9) & mask) * step,
                        (rdo & 0x7F) * .05, kind, position, pdo)
                else:
                    reason = 'non_programmable_or_unsupported_spr_pdo'
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
    return decoded, skipped



def analyze_avs_transitions(
    pd_samples: Sequence[SyncPDSample],
    scope_samples: Sequence[SyncScopeSample],
    *,
    baseline_window_us: int = TRANSITION_BASELINE_WINDOW_US,
    baseline_guard_us: int = TRANSITION_BASELINE_GUARD_US,
    movement_min_threshold_V: float = TRANSITION_MOVEMENT_MIN_THRESHOLD_V,
    movement_mad_multiplier: float = TRANSITION_MOVEMENT_MAD_MULTIPLIER,
    movement_sustain_samples: int = TRANSITION_MOVEMENT_SUSTAIN_SAMPLES,
    target_band_fraction: float = TRANSITION_TARGET_BAND_FRACTION,
    settle_hold_us: int = TRANSITION_SETTLE_HOLD_US,
    settle_max_sample_gap_us: int = TRANSITION_SETTLE_MAX_SAMPLE_GAP_US,
    plateau_lookback_us: int = TRANSITION_PLATEAU_LOOKBACK_US,
    plateau_min_samples: int = TRANSITION_PLATEAU_MIN_SAMPLES,
    observed_band_fraction: float = TRANSITION_RELATIVE_BAND_FRACTION,
    observed_settle_hold_us: int = TRANSITION_RELATIVE_SETTLE_HOLD_US,
    plateau_stability_min_span_V: float = TRANSITION_PLATEAU_STABILITY_MIN_SPAN_V,
    plateau_stability_fraction: float = TRANSITION_PLATEAU_STABILITY_FRACTION,
    plateau_target_guard_fraction: float = TRANSITION_PLATEAU_TARGET_GUARD_FRACTION,
) -> list[AVSTransitionAnalysis]:
    """
    Correlate SPR PPS/AVS REQUEST and EPR AVS EPR_REQUEST with telemetry.

    No host-time offset is estimated or applied. Both streams are compared on
    the raw/unwrapped device microsecond timestamps exactly as captured.

    Matching:
      - For each decoded programmable request, walk forward in PD order.
      - ACCEPT is the first ACCEPT after the request and before the next
        REQUEST/EPR_REQUEST or rejection/reset.
      - PS_RDY is the first PS_RDY after that ACCEPT (or request if ACCEPT is
        absent) and before the next request or rejection/reset.

    Movement detection:
      - baseline = median measurement VBUS in [request-baseline_window,
        request-baseline_guard]
      - noise = MAD around baseline
      - threshold = max(min_threshold, MAD * multiplier)
      - movement starts at the first sustained run of N samples beyond that
        threshold; direction follows the measured sign, independent of nominal
        target offset. Without a sustained run the target-based fallback is
        explicitly flagged.

    Absolute settling:
      - target band is ±target_band_fraction of the requested target voltage.
      - settling is the first in-band sample followed by at least settle_hold_us
        of continuous in-band samples, with no inter-sample gap greater than
        settle_max_sample_gap_us.
      - if capture ends before the hold interval completes, settling is not
        declared.

    Observed final-plateau settling:
      - starting at PS_RDY, scan forward for the earliest stable
        plateau_lookback_us measurement window; this avoids mistaking a later cable
        detach / VBUS collapse for the transition plateau.
      - require at least plateau_min_samples and a bounded VBUS span; among
        plausible candidates near the requested target, choose the stable
        plateau whose own relative band persists the longest.
      - relative band is ±observed_band_fraction of the observed plateau.
      - observed settling is the first sample that remains continuously inside
        that band for observed_settle_hold_us.
      - this does not replace absolute requested-target analysis; both are
        reported side-by-side.
    """
    pd_rows = list(pd_samples)
    requests, _ = decode_programmable_requests(pd_rows)
    scope = sorted(scope_samples, key=lambda s: s.timestamp_us)
    results: list[AVSTransitionAnalysis] = []

    for i, req in enumerate(pd_rows):
        decoded_req = requests.get(i)
        if decoded_req is None:
            continue

        target_v, requested_current = decoded_req.voltage_V, decoded_req.current_A

        next_req_index = len(pd_rows)
        for k in range(i + 1, len(pd_rows)):
            if pd_rows[k].message in REQUEST_MESSAGES | STOP_MESSAGES:
                next_req_index = k
                break

        accept = None
        ps_rdy = None
        for k in range(i + 1, next_req_index):
            candidate = pd_rows[k]
            if accept is None and candidate.message == "ACCEPT":
                accept = candidate
                continue
            if candidate.message == "PS_RDY" and (
                accept is None or candidate.row_index > accept.row_index
            ):
                ps_rdy = candidate
                break

        flags: list[str] = ['protocol_' + decoded_req.supply_type.lower(),
                            f'pdo_object_position_{decoded_req.object_position}']
        if next_req_index < len(pd_rows) and pd_rows[next_req_index].message in STOP_MESSAGES:
            flags.append('terminated_' + pd_rows[next_req_index].message.lower())
        nearest_req = _nearest_scope_sample(scope, req.start_us)
        nearest_ps = (
            _nearest_scope_sample(scope, ps_rdy.start_us)
            if ps_rdy is not None else None
        )
        if nearest_req is not None and abs(nearest_req.timestamp_us - req.start_us) > settle_max_sample_gap_us:
            flags.append('request_scope_sample_distant')
            nearest_req = None
        if nearest_ps is not None and abs(nearest_ps.timestamp_us - ps_rdy.start_us) > settle_max_sample_gap_us:
            flags.append('ps_rdy_scope_sample_distant')
            nearest_ps = None

        baseline_start = req.start_us - int(baseline_window_us)
        for previous in reversed(pd_rows[:i]):
            if previous.message in RESET_MESSAGES:
                baseline_start = max(baseline_start, previous.start_us)
                break
        baseline_end = req.start_us - int(baseline_guard_us)
        baseline_samples = [
            s.vbus_V for s in scope
            if baseline_start <= s.timestamp_us <= baseline_end
        ]

        if baseline_samples:
            baseline_v = _median(baseline_samples)
            baseline_mad = _mad(baseline_samples, baseline_v)
            movement_threshold = max(
                float(movement_min_threshold_V),
                baseline_mad * float(movement_mad_multiplier),
            )
        else:
            baseline_v = (
                nearest_req.vbus_V if nearest_req is not None else None
            )
            baseline_mad = None
            movement_threshold = (
                float(movement_min_threshold_V)
                if baseline_v is not None else None
            )
            flags.append("baseline_window_unavailable")

        if baseline_v is None:
            direction = "unknown"
        elif target_v > baseline_v:
            direction = "up"
        elif target_v < baseline_v:
            direction = "down"
        else:
            direction = "flat"

        search_end_us = (
            pd_rows[next_req_index].start_us
            if next_req_index < len(pd_rows)
            else (scope[-1].timestamp_us + 1 if scope else req.end_us + 1)
        )
        post_scope = [
            s for s in scope
            if req.start_us <= s.timestamp_us < search_end_us
        ]

        movement_start = None
        movement_sample = None
        sustain = max(1, int(movement_sustain_samples))

        if baseline_v is not None and movement_threshold is not None:
            for n in range(0, max(0, len(post_scope) - sustain + 1)):
                window = post_scope[n:n + sustain]
                if not all(0 < b.timestamp_us-a.timestamp_us <= settle_max_sample_gap_us for a,b in zip(window,window[1:])):
                    continue
                up = all(s.vbus_V >= baseline_v + movement_threshold for s in window)
                down = all(s.vbus_V <= baseline_v - movement_threshold for s in window)
                if up or down:
                    # Nominal target vs measured baseline can reverse the sign
                    # when voltage offset exceeds the requested sweep step.
                    direction = "up" if up else "down"
                    movement_sample = window[0]
                    movement_start = movement_sample.timestamp_us
                    break

        if movement_start is None:
            flags.append("movement_not_detected")
            flags.append("direction_from_target_fallback")

        band_abs = abs(target_v) * float(target_band_fraction)
        band_lo = target_v - band_abs
        band_hi = target_v + band_abs

        crossing_sample = None
        band_first = None
        before_request = [s for s in scope if s.timestamp_us < req.start_us]
        previous_v = before_request[-1].vbus_V if before_request else baseline_v
        if previous_v is not None and ((direction == "up" and previous_v >= target_v)
                or (direction == "down" and previous_v <= target_v)):
            flags.append("target_already_beyond_at_request")
        for s in post_scope:
            if crossing_sample is None and previous_v is not None:
                if (
                    (direction == "up" and previous_v < target_v <= s.vbus_V)
                    or (direction == "down" and previous_v > target_v >= s.vbus_V)
                ):
                    crossing_sample = s
            previous_v = s.vbus_V
            if band_first is None and band_lo <= s.vbus_V <= band_hi:
                band_first = s
            if crossing_sample is not None and band_first is not None:
                break

        if crossing_sample is None:
            flags.append("target_crossing_not_observed")
        if band_first is None:
            flags.append("target_band_not_reached")

        settling_sample = None
        if band_first is not None:
            start_idx = post_scope.index(band_first)
            for n in range(start_idx, len(post_scope)):
                candidate = post_scope[n]
                if not (band_lo <= candidate.vbus_V <= band_hi):
                    continue
                hold_end = candidate.timestamp_us + int(settle_hold_us)
                if post_scope[-1].timestamp_us < hold_end:
                    flags.append("capture_ended_before_settle_hold")
                    break

                ok = True
                last_t = candidate.timestamp_us
                reached_hold = False
                for sample_index, s in enumerate(post_scope[n:]):
                    if sample_index and not 0 < s.timestamp_us - last_t <= int(settle_max_sample_gap_us):
                        ok = False
                        break
                    if not (band_lo <= s.vbus_V <= band_hi):
                        ok = False
                        break
                    last_t = s.timestamp_us
                    if last_t >= hold_end:
                        reached_hold = True
                        break

                if ok and reached_hold:
                    settling_sample = candidate
                    break

        if settling_sample is None and band_first is not None:
            if "capture_ended_before_settle_hold" not in flags:
                flags.append("settling_not_observed")

        # Observed final plateau, estimated independently of requested target.
        #
        # Do NOT blindly use the final capture window: the last transition in a
        # session may be followed by cable detach / VBUS collapse. Instead scan
        # forward from PS_RDY (or movement start/request if PS_RDY is absent)
        # for the earliest stable plateau_lookback_us window.
        plateau_scan_start_us = (
            ps_rdy.start_us
            if ps_rdy is not None
            else (
                movement_start
                if movement_start is not None
                else req.start_us
            )
        )

        plateau_samples = []
        observed_plateau_v = None
        observed_plateau_mad = None
        observed_band_first = None
        observed_settling_sample = None
        observed_average_slew = None

        scan_samples = [
            s for s in post_scope
            if s.timestamp_us >= plateau_scan_start_us
        ]

        minimum_count = int(plateau_min_samples)
        window_us = int(plateau_lookback_us)

        # Test every native measurement; KM003C's usual interval is 40 ms.
        # Collect stable candidates and
        # choose the one whose own relative band persists the longest. This
        # rejects short shoulders during a ramp and, together with the target
        # plausibility guard, rejects a later detach-to-zero state.
        best_candidate = None
        best_persist_us = -1
        scan_times = [s.timestamp_us for s in scan_samples]
        # Reuse the end of an already scanned run for the same voltage band.
        # Stable long captures then require one run scan instead of one per sample.
        run_ends = {}

        target_guard_abs = max(
            0.5,
            abs(target_v) * float(plateau_target_guard_fraction),
        )

        for candidate_index in range(len(scan_samples)):
            candidate = scan_samples[candidate_index]
            window_end_us = candidate.timestamp_us + window_us
            window = scan_samples[candidate_index:bisect_left(scan_times, window_end_us)]
            if len(window) < minimum_count:
                continue
            if window[-1].timestamp_us - candidate.timestamp_us < window_us * 0.9:
                continue
            if not all(0 < b.timestamp_us - a.timestamp_us <= settle_max_sample_gap_us
                       for a, b in zip(window, window[1:])):
                continue

            values = [s.vbus_V for s in window]
            median_v = _median(values)
            span_v = max(values) - min(values)
            stable_span_limit = max(
                float(plateau_stability_min_span_V),
                abs(median_v) * float(plateau_stability_fraction),
            )
            if span_v > stable_span_limit:
                continue
            if abs(median_v - target_v) > target_guard_abs:
                continue

            candidate_band_abs = (
                abs(median_v) * float(observed_band_fraction)
            )
            candidate_lo = median_v - candidate_band_abs
            candidate_hi = median_v + candidate_band_abs

            band = (candidate_lo, candidate_hi)
            run_end = run_ends.get(band, candidate_index)
            if run_end <= candidate_index:
                run_end = candidate_index
                last_t = candidate.timestamp_us
                for scan_index in range(candidate_index, len(scan_samples)):
                    s = scan_samples[scan_index]
                    if scan_index > candidate_index and not 0 < s.timestamp_us - last_t <= settle_max_sample_gap_us:
                        break
                    if not candidate_lo <= s.vbus_V <= candidate_hi:
                        break
                    run_end = scan_index + 1
                    last_t = s.timestamp_us
                run_ends[band] = run_end
            if run_end <= candidate_index:
                continue
            persist_us = scan_samples[run_end - 1].timestamp_us - candidate.timestamp_us
            if persist_us > best_persist_us:
                persistent_run = scan_samples[candidate_index:run_end]
                # The initial stable window is only a detector. The plateau
                # value itself is the median of the whole persistent run, which
                # prevents an early shoulder (still inside the relative band)
                # from biasing the reported final plateau.
                run_values = [s.vbus_V for s in persistent_run]
                run_median = _median(run_values)
                best_persist_us = persist_us
                best_candidate = (
                    persistent_run,
                    run_median,
                    _mad(run_values, run_median),
                )

        if best_candidate is not None:
            plateau_samples, observed_plateau_v, observed_plateau_mad = best_candidate

        if observed_plateau_v is not None:
            observed_band_abs = (
                abs(observed_plateau_v) * float(observed_band_fraction)
            )
            observed_band_lo = observed_plateau_v - observed_band_abs
            observed_band_hi = observed_plateau_v + observed_band_abs

            for s in post_scope:
                if observed_band_lo <= s.vbus_V <= observed_band_hi:
                    observed_band_first = s
                    break

            if observed_band_first is not None:
                start_idx = post_scope.index(observed_band_first)
                for n in range(start_idx, len(post_scope)):
                    candidate = post_scope[n]
                    if not (
                        observed_band_lo <= candidate.vbus_V <= observed_band_hi
                    ):
                        continue
                    hold_end = candidate.timestamp_us + int(observed_settle_hold_us)
                    if post_scope[-1].timestamp_us < hold_end:
                        break

                    ok = True
                    reached_hold = False
                    last_t = candidate.timestamp_us
                    for sample_index, s in enumerate(post_scope[n:]):
                        if sample_index and not 0 < s.timestamp_us - last_t <= int(settle_max_sample_gap_us):
                            ok = False
                            break
                        if not (
                            observed_band_lo <= s.vbus_V <= observed_band_hi
                        ):
                            ok = False
                            break
                        last_t = s.timestamp_us
                        if last_t >= hold_end:
                            reached_hold = True
                            break

                    if ok and reached_hold:
                        observed_settling_sample = candidate
                        break

            if (
                movement_sample is not None
                and observed_band_first is not None
                and observed_band_first.timestamp_us > movement_sample.timestamp_us
            ):
                observed_average_slew = (
                    (observed_band_first.vbus_V - movement_sample.vbus_V)
                    / (
                        (
                            observed_band_first.timestamp_us
                            - movement_sample.timestamp_us
                        )
                        / 1e6
                    )
                )
        else:
            flags.append("observed_plateau_not_found")

        if observed_plateau_v is not None and observed_band_first is None:
            flags.append("observed_band_not_reached")
        elif observed_band_first is not None and observed_settling_sample is None:
            flags.append("observed_settling_not_observed")

        average_slew = None
        if (
            movement_sample is not None
            and band_first is not None
            and band_first.timestamp_us > movement_sample.timestamp_us
        ):
            average_slew = (
                (band_first.vbus_V - movement_sample.vbus_V)
                / ((band_first.timestamp_us - movement_sample.timestamp_us) / 1e6)
            )

        # These endpoints may capture overshoot recovery, not the ramp. Never
        # present an opposite-sign recovery rate as this transition's slew.
        sign = 1 if direction == "up" else -1 if direction == "down" else 0
        if observed_average_slew is not None and observed_average_slew * sign < 0:
            observed_average_slew = None
            flags.append("observed_slew_opposes_movement")
        if average_slew is not None and average_slew * sign < 0:
            average_slew = None
            flags.append("absolute_slew_opposes_movement")
        if movement_sample is not None and observed_band_first is not None and observed_band_first.timestamp_us <= movement_sample.timestamp_us:
            flags.append("observed_slew_unresolved")

        results.append(
            AVSTransitionAnalysis(
                request_row_index=req.row_index,
                request_sno=req.sno,
                direction=direction,
                target_voltage_V=target_v,
                requested_current_A=requested_current,
                request_start_us=req.start_us,
                request_end_us=req.end_us,
                accept_start_us=(None if accept is None else accept.start_us),
                accept_latency_us=(
                    None if accept is None
                    else accept.start_us - req.start_us
                ),
                ps_rdy_start_us=(None if ps_rdy is None else ps_rdy.start_us),
                ps_rdy_latency_us=(
                    None if ps_rdy is None
                    else ps_rdy.start_us - req.start_us
                ),
                request_scope_timestamp_us=(
                    None if nearest_req is None else nearest_req.timestamp_us
                ),
                request_scope_vbus_V=(
                    None if nearest_req is None else nearest_req.vbus_V
                ),
                ps_rdy_scope_timestamp_us=(
                    None if nearest_ps is None else nearest_ps.timestamp_us
                ),
                ps_rdy_scope_vbus_V=(
                    None if nearest_ps is None else nearest_ps.vbus_V
                ),
                baseline_vbus_V=baseline_v,
                baseline_noise_mad_V=baseline_mad,
                movement_threshold_V=movement_threshold,
                movement_start_us=movement_start,
                movement_latency_us=(
                    None if movement_start is None
                    else movement_start - req.start_us
                ),
                target_crossing_us=(
                    None if crossing_sample is None
                    else crossing_sample.timestamp_us
                ),
                target_crossing_latency_us=(
                    None if crossing_sample is None
                    else crossing_sample.timestamp_us - req.start_us
                ),
                target_band_first_entry_us=(
                    None if band_first is None
                    else band_first.timestamp_us
                ),
                target_band_first_entry_latency_us=(
                    None if band_first is None
                    else band_first.timestamp_us - req.start_us
                ),
                settling_us=(
                    None if settling_sample is None
                    else settling_sample.timestamp_us
                ),
                settling_latency_us=(
                    None if settling_sample is None
                    else settling_sample.timestamp_us - req.start_us
                ),
                settling_hold_us=int(settle_hold_us),
                target_band_fraction=float(target_band_fraction),
                average_slew_V_per_s=average_slew,
                observed_plateau_V=observed_plateau_v,
                observed_plateau_mad_V=observed_plateau_mad,
                observed_plateau_sample_count=len(plateau_samples),
                observed_band_fraction=float(observed_band_fraction),
                observed_band_first_entry_us=(
                    None if observed_band_first is None
                    else observed_band_first.timestamp_us
                ),
                observed_band_first_entry_latency_us=(
                    None if observed_band_first is None
                    else observed_band_first.timestamp_us - req.start_us
                ),
                observed_settling_us=(
                    None if observed_settling_sample is None
                    else observed_settling_sample.timestamp_us
                ),
                observed_settling_latency_us=(
                    None if observed_settling_sample is None
                    else observed_settling_sample.timestamp_us - req.start_us
                ),
                observed_average_slew_V_per_s=observed_average_slew,
                flags=tuple(dict.fromkeys(flags)),
                supply_type=decoded_req.supply_type,
                request_message=req.message,
                pdo_object_position=decoded_req.object_position,
                selected_pdo=decoded_req.selected_pdo,
            )
        )

    return results
