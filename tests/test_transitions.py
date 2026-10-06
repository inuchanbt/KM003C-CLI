"""AVS analysis and capture defaults tested with synthetic PD and measurements."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import km003c_cli as cli
from km003c_modules.transition_analysis import SyncPDSample, SyncScopeSample, analyze_avs_transitions
from km003c_modules.transition_report import TRANSITION_CSV_COLUMNS, TRANSITION_OUTPUT_SUFFIXES
from km003c_modules.transitions import load_analysis_csv
from test_km003c import frame, pd_payload
from test_vbus_events import response


def avs_pd(target=20, selected=0xD3C096F0):
    payload = struct.pack('<II', (11 << 28) | (round(target / .025) << 9) | 100, selected)
    return [SyncPDSample(0, 1, 'EPR_REQUEST', 100000, 100000, None, payload),
            SyncPDSample(1, 2, 'ACCEPT', 105000, 105000, None, b''),
            SyncPDSample(2, 3, 'PS_RDY', 150000, 150000, None, b'')]


def wave(base=15, moving=16, near=19, plateau=20):
    return [SyncScopeSample(t, base if t <= 100000 else moving if t == 120000
                           else near if t == 140000 else plateau) for t in range(0, 620001, 20000)]


class TransitionTests(unittest.TestCase):
    def test_direction_plateau_and_latency(self):
        for target, values, direction in [(20, (15, 16, 19, 20), 'up'),
                                           (15, (20, 19, 16, 15), 'down'),
                                           (47, (46.5, 46.2, 45.6, 45.5), 'down'),
                                           (17, (17.5, 17.8, 18.4, 18.5), 'up')]:
            result = analyze_avs_transitions(avs_pd(target), wave(*values))[0]
            self.assertEqual(result.direction, direction)
            self.assertEqual(result.accept_latency_us, 5000)
            self.assertEqual(result.ps_rdy_latency_us, 50000)
            self.assertEqual(result.movement_start_us, 120000)
            self.assertEqual(result.observed_plateau_V, values[-1])
            self.assertIsNotNone(result.observed_settling_us)

    def test_overshoot_recovery_is_not_reported_as_ramp_slew(self):
        result = analyze_avs_transitions(avs_pd(16), wave(15, 16.5, 16, 16))[0]
        self.assertEqual(result.direction, 'up')
        self.assertIsNone(result.average_slew_V_per_s)
        self.assertIsNone(result.observed_average_slew_V_per_s)
        self.assertIn('absolute_slew_opposes_movement', result.flags)

    def test_fixed_pps_and_truncated_request_are_not_avs(self):
        for pdo in (0x0001912C, 0xC0DC213C, 0xE0000000):
            self.assertEqual(analyze_avs_transitions(avs_pd(selected=pdo), wave()), [])
        request = avs_pd()[0]
        self.assertEqual(analyze_avs_transitions([
            SyncPDSample(0, 1, request.message, request.start_us, request.end_us, None, request.data[:4])], wave()), [])

    def test_missing_response_and_next_request_bound_matching(self):
        pd = avs_pd()
        second = SyncPDSample(1, 2, 'EPR_REQUEST', 110000, 110000, None, pd[0].data)
        result = analyze_avs_transitions([pd[0], second, pd[2]], wave())
        self.assertIsNone(result[0].accept_start_us)
        self.assertIsNone(result[0].ps_rdy_start_us)
        self.assertIsNone(result[1].accept_start_us)
        self.assertEqual(result[1].ps_rdy_start_us, 150000)

    def test_distant_scope_is_unavailable_and_detach_is_not_plateau(self):
        result = analyze_avs_transitions(avs_pd(), [SyncScopeSample(5000000, 20)])[0]
        self.assertIsNone(result.request_scope_vbus_V)
        self.assertIsNone(result.ps_rdy_scope_vbus_V)
        self.assertIn('request_scope_sample_distant', result.flags)
        scope = wave() + [SyncScopeSample(t, 0) for t in range(640000, 1240001, 20000)]
        self.assertEqual(analyze_avs_transitions(avs_pd(), scope)[0].observed_plateau_V, 20)

    def test_gap_duplicate_and_incomplete_hold_do_not_invent_settling(self):
        pd = avs_pd()
        for tail in ([160000, 400000], [160000, 160000, 180000], [160000, 200000]):
            scope = [SyncScopeSample(t, 15) for t in (0, 40000, 80000)]
            scope += [SyncScopeSample(t, 20) for t in tail]
            result = analyze_avs_transitions(pd, scope)[0]
            self.assertIsNone(result.settling_us)
            self.assertIsNone(result.observed_settling_us)
            self.assertIsNone(result.observed_plateau_V)
        # Even an explicitly short hold cannot skip a long gap after its first sample.
        scope = [SyncScopeSample(0, 15), SyncScopeSample(160000, 20), SyncScopeSample(400000, 20)]
        self.assertIsNone(analyze_avs_transitions(pd, scope, settle_hold_us=20000)[0].settling_us)

    def test_capture_defaults_and_explicit_finite_duration(self):
        parser = cli.build_arg_parser()
        defaults = parser.parse_args(['capture'])
        self.assertIsNone(defaults.seconds)
        self.assertIsNone(defaults.out_prefix)
        self.assertTrue(defaults.scope)
        self.assertTrue(defaults.hide_goodcrc)
        self.assertEqual(defaults.status_interval, 1)
        self.assertFalse(defaults.scope_raw)
        self.assertFalse(defaults.quiet)
        self.assertFalse(defaults.force)
        self.assertFalse(defaults.analyze_transitions)
        self.assertFalse(defaults.infer_vbus_events)
        self.assertEqual(defaults.interval, .04)  # Keep hardware polling unchanged.
        self.assertEqual(parser.parse_args(['scope']).seconds, 5)
        with tempfile.TemporaryDirectory() as folder:
            args = parser.parse_args(['capture', '--seconds', '.1', '--quiet', '--interval', '0',
                                     '--out-prefix', str(Path(folder) / 'finite')])
            with patch.object(cli, 'Meter') as meter, patch.object(cli.time, 'monotonic', side_effect=[0, 1]), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.run_capture(args), 0)
                meter.return_value.__enter__.return_value.get_data.assert_not_called()
            self.assertFalse(args.until_ctrl_c)

    def test_automatic_prefix_continuous_ctrl_c_partial_save_and_no_analysis_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = Path.cwd()
            try:
                os.chdir(folder)
                with patch.object(cli, 'Meter') as meter, redirect_stdout(io.StringIO()):
                    for _ in range(2):
                        meter.return_value.__enter__.return_value.get_data.side_effect = [response(1000, 5000), KeyboardInterrupt()]
                        self.assertEqual(cli.main(['capture', '--interval', '0', '--quiet']), 0)
                prefixes = list(Path('captures').glob('km003c_*.metadata.json'))
                self.assertEqual(len(prefixes), 2)
                for path in prefixes:
                    meta = json.loads(path.read_text())
                    self.assertEqual(meta['status'], 'interrupted')
                    self.assertTrue(meta['arguments']['until_ctrl_c'])
                    self.assertFalse(meta['transition_analysis']['enabled'])
                    self.assertEqual(meta['scope_samples'], 1)
                self.assertEqual(len(list(Path('captures').glob('*.ccgx3'))), 2)
                self.assertEqual(list(Path('captures').glob('*.transitions.csv')), [])
            finally:
                os.chdir(previous)

    def fixture(self):
        rdo = (11 << 28) | (800 << 9) | 100
        result = []
        for ts in range(1000, 1881, 40):
            mv = 15000 if ts <= 1120 else 16000 if ts == 1160 else 19000 if ts == 1200 else 20000
            raw = bytearray(pd_payload(ts=ts)[:12])
            struct.pack_into('<H', raw, 4, mv)
            if ts == 1120:
                raw += pd_payload(ts=ts, header=0x2089, objects=(rdo, 0xD3C096F0))[12:]
            elif ts == 1160:
                # A cable ACCEPT must not match; the subsequent SOP ACCEPT is real.
                raw += pd_payload(ts=ts, header=0x0083, sop=1)[12:]
                raw += pd_payload(ts=ts, header=0x0083)[12:]
            elif ts == 1240:
                raw += pd_payload(ts=ts, header=0x0086)[12:]
            result.append(frame(16, raw))
        return result

    def test_native_offline_and_csv_analysis_match_with_all_formats_and_original_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.records.bin'
            with source.open('wb') as handle:
                for raw in self.fixture():
                    cli.write_native(handle, raw)
            for formats in (['all'], ['original'], ['ccgx3']):
                prefix = root / formats[0]
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['convert', '--input', str(source), '--out-prefix', str(prefix),
                                               '--analyze-transitions', '--formats', *formats]), 0)
                with prefix.with_suffix('.transitions.csv').open(encoding='utf-8-sig') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(float(row['target_voltage_V']), 20)
                self.assertEqual(float(row['requested_current_A']), 5)
                self.assertEqual(int(row['accept_latency_us']), 40000)
                self.assertEqual(int(row['ps_rdy_latency_us']), 120000)
                self.assertEqual(float(row['observed_plateau_V']), 20)
                self.assertIn('km003c_point_timestamps', row['flags'])
                for suffix in TRANSITION_OUTPUT_SUFFIXES:
                    self.assertTrue(prefix.with_suffix(suffix).is_file())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['analyze-sync', '--pd-csv', str(root / 'all.csv'),
                                           '--scope-csv', str(root / 'all.scope.csv'), '--out-prefix', str(root / 'sync')]), 0)
            self.assertEqual((root / 'all.transitions.csv').read_bytes(), (root / 'sync.transitions.csv').read_bytes())
            pd, _ = load_analysis_csv(root / 'all.csv', root / 'all.scope.csv')
            self.assertEqual(len(pd), 3)  # Cable SOP_PRIME is excluded.

    def test_analysis_collisions_and_scope_requirements_before_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'session'
            path = prefix.with_suffix('.transition_summary.txt')
            path.write_text('keep', encoding='utf-8')
            with patch.object(cli, 'Meter', side_effect=AssertionError('hardware opened')), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['capture', '--out-prefix', str(prefix), '--analyze-transitions']), 1)
                for command in (['capture'], ['convert', '--input', 'absent', '--out-prefix', 'absent']):
                    with self.assertRaises(SystemExit):
                        cli.main(command + ['--analyze-transitions', '--no-scope'])
                with self.assertRaises(SystemExit):
                    cli.main(['capture', '--baseline-window-ms', '5', '--baseline-guard-ms', '5'])
            self.assertEqual(path.read_text(), 'keep')

    def test_ctrl_c_runs_partial_analysis_and_retains_native_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'partial'
            frames = self.fixture()
            with patch.object(cli, 'Meter') as meter, redirect_stdout(io.StringIO()):
                meter.return_value.__enter__.return_value.get_data.side_effect = frames + [KeyboardInterrupt()]
                self.assertEqual(cli.main(['capture', '--out-prefix', str(prefix), '--analyze-transitions',
                                           '--interval', '0', '--quiet']), 0)
            self.assertEqual(list(cli.read_native(prefix.with_suffix('.records.bin'))), frames)
            meta = json.loads(prefix.with_suffix('.metadata.json').read_text())
            self.assertEqual(meta['transition_analysis']['transitions'], 1)
            self.assertEqual(meta['status'], 'interrupted')
            with prefix.with_suffix('.transitions.csv').open(encoding='utf-8-sig') as handle:
                self.assertIn('capture_interrupted', next(csv.DictReader(handle))['flags'])

    def test_empty_analysis_and_input_alias_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'session.transitions.csv'
            with source.open('wb') as handle:
                cli.write_native(handle, response(1000, 5000))
            original = source.read_bytes()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['convert', '--input', str(source), '--out-prefix', str(root / 'session'),
                                           '--analyze-transitions', '--force']), 1)
            self.assertEqual(source.read_bytes(), original)
            with redirect_stdout(io.StringIO()) as console:
                self.assertEqual(cli.main(['convert', '--input', str(source), '--out-prefix', str(root / 'empty'),
                                           '--analyze-transitions']), 0)
            self.assertIn('No AVS EPR_REQUEST', console.getvalue())
            with (root / 'empty.transitions.csv').open(encoding='utf-8-sig') as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_status_interval_and_quiet(self):
        with tempfile.TemporaryDirectory() as folder:
            for quiet in (False, True):
                args = cli.build_arg_parser().parse_args(['capture', '--out-prefix', str(Path(folder) / str(quiet)),
                                                         '--status-interval', '.5', '--interval', '0'] + (['--quiet'] if quiet else []))
                console = io.StringIO()
                with patch.object(cli, 'Meter') as meter, patch.object(cli.time, 'monotonic', side_effect=[0, 0, 0, 0, 1, 1, 1, 1]), redirect_stdout(console):
                    meter.return_value.__enter__.return_value.get_data.side_effect = [response(1000, 5000), KeyboardInterrupt()]
                    cli.run_capture(args)
                self.assertEqual('[status]' in console.getvalue(), not quiet)

    def test_analysis_option_validation_and_standalone_host_scope_rejected(self):
        for option in ('--settle-max-gap-ms', '--target-band-percent', '--movement-sustain-samples'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(['capture', option, '0'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pd.csv').write_text('Sno,SOP,Message,Data,Start Time,End Time,Vbus(V)\n')
            (root / 'scope.csv').write_text('Timestamp Raw,Timestamp(us),Vbus(V)\n,1000,5\n')
            with self.assertRaisesRegex(ValueError, 'host-timed'):
                load_analysis_csv(root / 'pd.csv', root / 'scope.csv')

    def test_reference_output_columns_and_shared_thresholds(self):
        reference = os.environ.get('CY4500_CLI_ROOT')
        if not reference:
            self.skipTest('set CY4500_CLI_ROOT for reference compatibility')
        import ast
        source = (Path(reference) / 'cy4500_cli.py').read_text(encoding='utf-8-sig')
        assignment = next(n for n in ast.parse(source).body if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], 'id', None) == 'TRANSITION_CSV_COLUMNS')
        self.assertEqual(tuple(ast.literal_eval(assignment.value)), TRANSITION_CSV_COLUMNS)
        spec = importlib.util.spec_from_file_location('cy_transition_reference', Path(reference) / 'ezpd_protocol.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        # Compare common results with explicit sampling limits; new KM flags are added by the adapter only.
        ref = module.analyze_avs_transitions(avs_pd(), wave(), movement_sustain_samples=2,
              settle_hold_us=80000, settle_max_sample_gap_us=100000, plateau_lookback_us=400000,
              plateau_min_samples=6, observed_settle_hold_us=80000)[0]
        own = analyze_avs_transitions(avs_pd(), wave())[0]
        for name in ('direction', 'target_voltage_V', 'baseline_vbus_V', 'movement_start_us',
                     'accept_latency_us', 'ps_rdy_latency_us', 'observed_plateau_V'):
            self.assertEqual(getattr(own, name), getattr(ref, name))


if __name__ == '__main__':
    unittest.main()
