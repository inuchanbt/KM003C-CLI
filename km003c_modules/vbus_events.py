"""Opt-in VBUS transitions inferred from KM003C PD status measurements.

Thresholds and gap policy match the MIT-licensed sibling TI CLI detector.
These estimates are not hardware events or measurements of wire timing.
"""
from collections import Counter


class VbusEventDetector:
    UP_MV = 4000
    DOWN_MV = 800
    MAX_GAP_US = 100_000
    SOURCE = 'KM003C_PD_STATUS_INFERENCE'

    def __init__(self):
        self.previous = None
        self.powered = None
        self.counts = Counter()
        self.gap_resets = 0

    def observe(self, sample):
        # ADC responses have no device timestamp; never mix host and device clocks.
        if sample.timestamp_us is None or sample.clock_source != 'device_ms':
            return None
        current = (sample.timestamp_us, round(sample.voltage * 1000))
        previous = self.previous
        self.previous = current
        timestamp, mv = current
        if previous is None or not 0 < timestamp - previous[0] <= self.MAX_GAP_US:
            if previous is not None:
                self.gap_resets += 1
            self.powered = True if mv >= self.UP_MV else False if mv <= self.DOWN_MV else None
            return None
        event = None
        if self.powered is False and mv >= self.UP_MV:
            event = 'VBUS_UP'
        elif self.powered is True and mv <= self.DOWN_MV:
            event = 'VBUS_DN'
        if mv >= self.UP_MV:
            self.powered = True
        elif mv <= self.DOWN_MV:
            self.powered = False
        if event is None:
            return None
        self.counts[event] += 1
        return {
            'type': 'INFERRED_VBUS_EVENT', 'event': event, 'estimated': True,
            'source': self.SOURCE, 'timestamp_us': timestamp, 'vbus_mV': mv,
            'threshold_mV': self.UP_MV if event == 'VBUS_UP' else self.DOWN_MV,
            'interval_start_us': previous[0], 'interval_end_us': timestamp,
            'sample_interval_us': timestamp - previous[0], 'previous_vbus_mV': previous[1],
            'timestamp_policy': 'first_sample_after_threshold',
        }

    def summary(self):
        return {
            'enabled': True, 'source': self.SOURCE,
            'up_threshold_mV': self.UP_MV, 'down_threshold_mV': self.DOWN_MV,
            'max_sample_gap_us': self.MAX_GAP_US, 'counts': dict(self.counts),
            'timestamp_policy': 'first_sample_after_threshold',
            'initial_sample_emits_event': False, 'sample_gap_resets': self.gap_resets,
        }
