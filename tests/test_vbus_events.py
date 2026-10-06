"""Console filtering and opt-in inference, using synthetic KM003C responses."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import csv
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import km003c_cli as cli
from km003c_modules import protocol as proto
from km003c_modules.utility_export import packet_for_vbus_event
from km003c_modules.vbus_events import VbusEventDetector
from test_km003c import frame, pd_payload, adc_payload


def response(ts, mv, header=None):
    payload = bytearray(pd_payload(ts=ts, header=header or 0x0143))
    struct.pack_into('<H', payload, 4, mv)
    if header is None:
        payload = payload[:12]
    return frame(proto.PD_PACKET, bytes(payload))


def sample(ts, mv):
    value, _ = proto.decode_pd(struct.pack('<IHhHH', 1, mv, 0, 0, 0), proto.ClockUnwrapper())
    value.timestamp_us = ts
    return value


class VbusEventTests(unittest.TestCase):
    def test_hysteresis_and_intervals(self):
        detector = VbusEventDetector()
        values = [0, 2000, 4000, 3999, 4100, 801, 800, 801, 3999, 4000]
        events = [event for i, mv in enumerate(values)
                  if (event := detector.observe(sample(i * 20000, mv))) is not None]
        self.assertEqual([event['event'] for event in events], ['VBUS_UP', 'VBUS_DN', 'VBUS_UP'])
        self.assertEqual([event['timestamp_us'] for event in events], [40000, 120000, 180000])
        for event in events:
            self.assertTrue(event['estimated'])
            self.assertEqual(event['source'], 'KM003C_PD_STATUS_INFERENCE')
            self.assertEqual(event['interval_end_us'] - event['interval_start_us'], 20000)
        self.assertEqual(detector.summary()['counts'], {'VBUS_UP': 2, 'VBUS_DN': 1})

    def test_initial_unknown_and_high_do_not_invent_up(self):
        for initial in (5000, 2000):
            detector = VbusEventDetector()
            self.assertIsNone(detector.observe(sample(0, initial)))
            self.assertIsNone(detector.observe(sample(1000, 5000)))
            self.assertEqual(detector.observe(sample(2000, 800))['event'], 'VBUS_DN')

    def test_gap_duplicate_and_backwards_rebaseline_and_adc_is_ignored(self):
        for next_ts in (100001, 0, -1):
            detector = VbusEventDetector()
            detector.observe(sample(0, 0))
            self.assertIsNone(detector.observe(sample(next_ts, 5000)))
            self.assertEqual(detector.gap_resets, 1)
            self.assertEqual(detector.observe(sample(next_ts + 1000, 800))['event'], 'VBUS_DN')
        detector = VbusEventDetector()
        detector.observe(sample(0, 0))
        self.assertIsNone(detector.observe(proto.decode_adc(adc_payload())))
        self.assertEqual(detector.observe(sample(100000, 4000))['event'], 'VBUS_UP')

    def test_default_off_and_scope_requirement_before_hardware(self):
        self.assertFalse(cli.build_arg_parser().parse_args(['capture']).infer_vbus_events)
        for argv in (['capture'], ['convert', '--input', 'absent.bin', '--out-prefix', 'absent']):
            with patch.object(cli, 'Meter', side_effect=AssertionError('hardware accessed')):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    cli.main(argv + ['--no-scope', '--infer-vbus-events'])
                self.assertEqual(caught.exception.code, 2)

    def export(self, root, name, frames, options=(), live=False):
        args = cli.build_arg_parser().parse_args(['capture', '--out-prefix', str(root / name), *options])
        console = io.StringIO()
        with ExitStack() as stack, redirect_stdout(console):
            export = cli.CaptureExport(stack, args, live=live)
            for raw in frames:
                export.frame(raw)
            export.finalize('completed')
        return export, console.getvalue()

    def test_gui_and_sidecar_preserve_native_records_and_measurements(self):
        frames = [response(1000, 0), response(1020, 5000, 0x0181), response(1040, 800)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Receipt time is incidental; freeze it to compare native JSONL exactly.
            with patch.object(cli.time, 'monotonic', return_value=1):
                plain, _ = self.export(root, 'plain', frames, live=True)
                inferred, console = self.export(root, 'inferred', frames, ['--infer-vbus-events'], live=True)
            for suffix in ('.records.bin', '.records.hex.txt', '.records.jsonl', '.scope.csv'):
                self.assertEqual((root / ('plain' + suffix)).read_bytes(),
                                 (root / ('inferred' + suffix)).read_bytes())
            self.assertFalse((root / 'plain.vbus_events.jsonl').exists())
            rows = list(csv.reader(io.StringIO((root / 'inferred.csv').read_text(encoding='utf-8'))))[1:]
            self.assertEqual([row[0] for row in rows], ['1', '2', '3'])
            self.assertEqual([row[1] for row in rows], ['VBUS_UP', '', 'VBUS_DN'])
            events = [json.loads(line) for line in (root / 'inferred.vbus_events.jsonl').read_text().splitlines()]
            self.assertEqual([event['gui_row_index'] for event in events], [1, 3])
            self.assertEqual([event['timestamp_us'] for event in events], [1020000, 1040000])
            self.assertIn('[VBUS inferred]', console)
            self.assertEqual(inferred.events, plain.events)
            self.assertEqual(inferred.gui_events, 1)
            meta = json.loads((root / 'inferred.metadata.json').read_text())
            self.assertEqual((meta['gui_rows'], meta['gui_pd_messages'], meta['gui_inferred_vbus_events']), (3, 1, 2))
            with zipfile.ZipFile(root / 'plain.ccgx3') as a, zipfile.ZipFile(root / 'inferred.ccgx3') as b:
                graph_a = a.read(next(n for n in a.namelist() if n.endswith('.scope')))
                graph_b = b.read(next(n for n in b.namelist() if n.endswith('.scope')))
                self.assertEqual(graph_a, graph_b)
                packets = b.read(next(n for n in b.namelist() if n.endswith('.part')))
                for event in events:
                    raw, _ = packet_for_vbus_event(event, event['gui_row_index'])
                    self.assertIn(raw, packets)
                    self.assertEqual(struct.unpack_from('<I', raw, 16)[0], 1 << 26)

    def test_original_only_and_quiet_still_write_estimates_and_collision_is_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            export, console = self.export(root, 'original', [response(1000, 0), response(1020, 4000)],
                                          ['--formats', 'original', '--infer-vbus-events', '--quiet'])
            self.assertEqual(console, '')
            self.assertFalse((root / 'original.csv').exists())
            event = json.loads((root / 'original.vbus_events.jsonl').read_text())
            self.assertFalse(event['gui_exported'])
            self.assertIsNone(event['gui_row_index'])
            self.assertEqual(export.detector.counts['VBUS_UP'], 1)
            blocked = root / 'blocked.vbus_events.jsonl'
            blocked.write_text('existing sidecar', encoding='utf-8')
            with patch.object(cli, 'Meter', side_effect=AssertionError('hardware accessed')):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(['capture', '--infer-vbus-events', '--formats', 'csv',
                                               '--out-prefix', str(root / 'blocked')]), 1)
            self.assertEqual(blocked.read_text(encoding='utf-8'), 'existing sidecar')
            self.assertEqual(json.loads((root / 'original.vbus_events.jsonl').read_text()), event)

    def test_offline_alias_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.records.bin'
            with source.open('wb') as handle:
                for raw in (response(1000, 0), response(1020, 4000)):
                    cli.write_native(handle, raw)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['convert', '--input', str(source), '--out-prefix', str(root / 'converted'),
                                           '--infer-vbus-events']), 0)
            event = json.loads((root / 'converted.vbus_events.jsonl').read_text())
            self.assertEqual(event['event'], 'VBUS_UP')

    def test_goodcrc_console_only_default_hide_show_and_unknown_preserved(self):
        goodcrc = response(1000, 5000, 0x0181)
        unknown = frame(proto.PD_PACKET, pd_payload(ts=1020)[:12] + b'\x05\x01')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(cli.time, 'monotonic', return_value=1):
                for name, options, visible in [('default', [], False), ('hide', ['--hide-goodcrc'], False),
                                                ('show', ['--show-goodcrc'], True)]:
                    export, console = self.export(root, name, [goodcrc, unknown], options, live=True)
                    self.assertEqual('GOODCRC' in console, visible)
                    self.assertIn('UNKNOWN_PD_EVENT_0x05', console)
                    self.assertEqual(export.messages['GOODCRC'], 1)
                for suffix in ('.csv', '.records.jsonl', '.records.bin', '.scope.csv'):
                    self.assertEqual((root / ('hide' + suffix)).read_bytes(), (root / ('show' + suffix)).read_bytes())
            _, console = self.export(root, 'quiet', [goodcrc], ['--show-goodcrc', '--quiet'])
            self.assertEqual(console, '')


if __name__ == '__main__':
    unittest.main()
