"""SPR PPS/AVS decoding, request boundaries, native export and plot integration."""
from contextlib import redirect_stdout, redirect_stderr
import csv
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import km003c_cli as cli
from km003c_modules.transition_analysis import (
    SyncPDSample, SyncScopeSample, analyze_avs_transitions, decode_programmable_requests)
from km003c_modules.transition_plots import TRANSITION_PLOT_SUFFIXES, _sample_segments
from test_transitions import avs_pd, wave
from test_km003c import frame, pd_payload


FIXED = 0x0001912C
PPS = 0xC0DC213C
SPR_AVS = 0xE004B12C


def sample(index, message, timestamp, *words):
    return SyncPDSample(index, index+1, message, timestamp, timestamp, None,
                        b''.join(struct.pack('<I', word) for word in words))


def spr_pd(kind='SPR_PPS', target=9, position=5):
    pdo, step = (PPS, .020) if kind == 'SPR_PPS' else (SPR_AVS, .025)
    rdo = (position << 28) | (round(target/step) << 9) | 60
    return [sample(0, 'SOURCE_CAPABILITIES', 0, FIXED, FIXED, 0, 0, pdo),
            sample(1, 'REQUEST', 100000, rdo), sample(2, 'ACCEPT', 105000),
            sample(3, 'PS_RDY', 150000)]


def native_fixture(kind='SPR_PPS'):
    pdo, step = (PPS, .020) if kind == 'SPR_PPS' else (SPR_AVS, .025)
    rdo = (5 << 28) | (round(9/step) << 9) | 60
    frames = []
    for ts in range(1000, 1881, 40):
        mv = 5000 if ts <= 1120 else 6000 if ts == 1160 else 8000 if ts == 1200 else 9000
        raw = bytearray(pd_payload(ts=ts)[:12])
        struct.pack_into('<H', raw, 4, mv)
        if ts == 1000:
            # Cable Source Capabilities must not replace the real SOP advert.
            raw += pd_payload(ts=ts, header=0x5181, objects=(FIXED, FIXED, 0, 0, pdo))[12:]
            raw += pd_payload(ts=ts, header=0x5181, objects=(FIXED,)*5, sop=1)[12:]
        elif ts == 1120:
            raw += pd_payload(ts=ts, header=0x1082, objects=(rdo,))[12:]
        elif ts == 1160:
            raw += pd_payload(ts=ts, header=0x0083, sop=1)[12:]
            raw += pd_payload(ts=ts, header=0x0083)[12:]
        elif ts == 1240:
            raw += pd_payload(ts=ts, header=0x0086)[12:]
        frames.append(frame(16, raw))
    return frames


class ProgrammableTransitionTests(unittest.TestCase):
    def test_spr_pps_and_avs_units_up_down_and_measured_settling(self):
        for kind, target in [('SPR_PPS', 9.020), ('SPR_AVS', 9.025)]:
            for values, direction in [((5, 6, 8, target), 'up'), ((15, 13, 10, target), 'down')]:
                with self.subTest(kind=kind, direction=direction):
                    results = analyze_avs_transitions(spr_pd(kind, target), wave(*values))
                    self.assertEqual(len(results), 1)
                    result = results[0]
                    self.assertAlmostEqual(result.target_voltage_V, target)
                    self.assertEqual(result.requested_current_A, 3)
                    self.assertEqual(result.supply_type, kind)
                    self.assertEqual(result.request_message, 'REQUEST')
                    self.assertEqual(result.pdo_object_position, 5)
                    self.assertEqual(result.direction, direction)
                    self.assertEqual(result.accept_latency_us, 5000)
                    self.assertEqual(result.ps_rdy_latency_us, 50000)
                    self.assertAlmostEqual(result.observed_plateau_V, target)
                    self.assertIsNotNone(result.settling_us)
                    self.assertIsNotNone(result.observed_settling_us)

    def test_observed_spr_avs_rdo_and_pdo_object_position(self):
        pd = spr_pd('SPR_AVS')
        pd[1] = sample(1, 'REQUEST', 100000, 0x5042D03C)
        decoded, skipped = decode_programmable_requests(pd)
        self.assertEqual(skipped, {})
        self.assertEqual(decoded[1].voltage_V, 9)
        self.assertEqual(decoded[1].current_A, 3)
        self.assertEqual(decoded[1].selected_pdo, SPR_AVS)

    def test_capabilities_missing_invalid_updated_and_reset_are_not_guessed(self):
        pd = spr_pd()
        for rows in (pd[1:], [pd[1], pd[0]],
                     [sample(0, 'SOURCE_CAPABILITIES', 0, FIXED), pd[1]],
                     [pd[0], sample(1, 'SOFT_RESET', 50000), pd[1]],
                     [pd[0], sample(1, 'DISCONNECT', 50000), pd[1]],
                     [pd[0], sample(1, 'SOURCE_CAPABILITIES', 50000, FIXED), pd[1]]):
            with self.subTest(rows=rows):
                self.assertEqual(analyze_avs_transitions(rows, wave()), [])
                self.assertTrue(decode_programmable_requests(rows)[1])
        rows = [pd[0], sample(1, 'SOURCE_CAPABILITIES', 50000, FIXED, FIXED, 0, 0, SPR_AVS), pd[1]]
        decoded, _ = decode_programmable_requests(rows)
        self.assertEqual(decoded[2].supply_type, 'SPR_AVS')
        self.assertAlmostEqual(decoded[2].voltage_V, 11.25)
        invalid = SyncPDSample(0, 1, 'SOURCE_CAPABILITIES', 0, 0, None, b'123')
        self.assertEqual(analyze_avs_transitions([invalid, pd[1]], wave()), [])

    def test_fixed_or_truncated_requests_are_excluded(self):
        pd = spr_pd()
        for request in (sample(1, 'REQUEST', 100000, 1 << 28),
                        sample(1, 'REQUEST', 100000, 0),
                        sample(1, 'REQUEST', 100000, 7 << 28),
                        sample(1, 'REQUEST', 100000),
                        sample(1, 'REQUEST', 100000, 5 << 28, PPS)):
            self.assertEqual(analyze_avs_transitions([pd[0], request], wave()), [])

    def test_any_next_request_or_reject_bounds_epr_and_spr_matching(self):
        for pd in (spr_pd(), avs_pd()):
            request = next(row for row in pd if row.message in ('REQUEST', 'EPR_REQUEST'))
            initial = pd[:pd.index(request)+1]
            for message in ('REQUEST', 'EPR_REQUEST', 'REJECT', 'WAIT', 'SOFT_RESET', 'DISCONNECT'):
                rows = initial + [sample(20, message, 110000, 1 << 28),
                                  sample(21, 'ACCEPT', 120000), sample(22, 'PS_RDY', 150000)]
                result = analyze_avs_transitions(rows, wave())[0]
                self.assertIsNone(result.accept_start_us)
                self.assertIsNone(result.ps_rdy_start_us)
                self.assertIsNone(result.observed_plateau_V)

    def test_native_conversion_and_csv_reanalysis_match_for_both_spr_types(self):
        for kind in ('SPR_PPS', 'SPR_AVS'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); source = root/'input.records.bin'
                with source.open('wb') as handle:
                    for raw in native_fixture(kind):
                        cli.write_native(handle, raw)
                prefix = root/'native'
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['convert', '--input', str(source), '--out-prefix', str(prefix),
                                               '--analyze-transitions']), 0)
                    self.assertEqual(cli.main(['analyze-sync', '--pd-csv', str(prefix.with_suffix('.csv')),
                        '--scope-csv', str(prefix.with_suffix('.scope.csv')), '--out-prefix', str(root/'sync')]), 0)
                self.assertEqual(prefix.with_suffix('.transitions.csv').read_bytes(),
                                 (root/'sync.transitions.csv').read_bytes())
                with prefix.with_suffix('.transitions.csv').open(encoding='utf-8-sig') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['request_mode'], kind)
                self.assertEqual(rows[0]['request_message'], 'REQUEST')
                self.assertEqual(rows[0]['object_position'], '5')
                self.assertEqual(float(rows[0]['target_voltage_V']), 9)
                self.assertIn('protocol_' + kind.lower(), rows[0]['flags'])
                meta = json.loads(prefix.with_suffix('.metadata.json').read_text())
                self.assertEqual(meta['transition_analysis']['settings']['protocol_counts'][kind], 1)

    def test_live_capture_ctrl_c_keeps_spr_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)/'capture'
            with patch.object(cli, 'Meter') as meter, redirect_stdout(io.StringIO()):
                meter.return_value.__enter__.return_value.get_data.side_effect = native_fixture() + [KeyboardInterrupt()]
                self.assertEqual(cli.main(['capture', '--analyze-transitions', '--quiet', '--interval', '0',
                                           '--out-prefix', str(prefix)]), 0)
            meta = json.loads(prefix.with_suffix('.metadata.json').read_text())
            self.assertEqual(meta['status'], 'interrupted')
            self.assertEqual(meta['transition_analysis']['transitions'], 1)

    def test_plot_connectors_break_at_gaps_and_duplicate_times(self):
        scope = [SyncScopeSample(t, 5) for t in (0, 40000, 40000, 400000, 440000)]
        segments = list(_sample_segments(scope, 100000))
        self.assertEqual([[s.timestamp_us for s in group] for group in segments],
                         [[0, 40000], [40000], [400000, 440000]])

    def test_plot_option_requires_analysis_and_dependency_before_hardware(self):
        with patch.object(cli, 'Meter', side_effect=AssertionError('hardware opened')), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(['capture', '--transition-plots'])
            with patch.object(cli, 'require_transition_plotting', side_effect=ValueError('matplotlib unavailable')):
                with self.assertRaises(SystemExit):
                    cli.main(['capture', '--analyze-transitions', '--transition-plots'])

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'install requirements-analysis.txt for PNG tests')
    def test_plot_files_metadata_collision_and_input_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); prefix = root/'plots'; frames = native_fixture('SPR_AVS')
            with patch.object(cli, 'Meter') as meter, redirect_stdout(io.StringIO()):
                meter.return_value.__enter__.return_value.get_data.side_effect = frames + [KeyboardInterrupt()]
                self.assertEqual(cli.main(['capture', '--analyze-transitions', '--transition-plots',
                    '--quiet', '--interval', '0', '--out-prefix', str(prefix)]), 0)
            for suffix in TRANSITION_PLOT_SUFFIXES:
                self.assertEqual(prefix.with_suffix(suffix).read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
            meta = json.loads(prefix.with_suffix('.metadata.json').read_text())
            self.assertEqual(len(meta['transition_analysis']['settings']['plots']), 3)
            collision = root/'collision.transitions.png'
            collision.write_bytes(b'keep')
            with patch.object(cli, 'Meter', side_effect=AssertionError('hardware opened')), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['capture', '--analyze-transitions', '--transition-plots',
                                          '--out-prefix', str(root/'collision')]), 1)
                self.assertEqual(cli.main(['analyze-sync', '--pd-csv', str(collision),
                    '--scope-csv', str(prefix.with_suffix('.scope.csv')), '--out-prefix', str(root/'collision'),
                    '--transition-plots', '--force']), 1)
            self.assertEqual(collision.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
