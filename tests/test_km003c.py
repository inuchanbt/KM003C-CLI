"""Offline tests with vendor example bytes and synthetic PD/transport fixtures."""
import argparse
import ast
from contextlib import redirect_stdout, redirect_stderr
import csv
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import km003c_cli as cli
from km003c_modules import protocol as proto
from km003c_modules import transport


def adc_payload():
    return struct.pack('<6ih5H4B', 9_000_000, -1_000_000, 8_999_900, -999_999,
                       8_888_888, -888_888, -128, 6330, 3700, 2690, 2780, 33000, 2, 0, 0, 0)


def frame(attribute, payload, transaction=0):
    return struct.pack('<II', 0x41 | (transaction << 8) | (((len(payload) + 3) // 4) << 22),
                       attribute | (len(payload) << 22)) + payload


def pd_payload(ts=1000, header=0x0143, objects=(), sop=0):
    wire = struct.pack('<H', header) + b''.join(struct.pack('<I', x) for x in objects)
    wrapper = bytes([0x80 | (len(wire) + 5)]) + struct.pack('<I', ts - 1) + bytes([sop]) + wire
    return struct.pack('<IHhHH', ts, 9000, -1000, 633, 370) + wrapper


class FakeTransport:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.writes = []
        self.closed = False
    def open(self):
        return self
    def close(self):
        self.closed = True
    def read(self, timeout):
        return self.chunks.pop(0) if self.chunks else b''
    def write(self, data):
        self.writes.append(data)


class ProtocolTests(unittest.TestCase):
    def test_vendor_get_adc_request(self):
        self.assertEqual(proto.control_packet(proto.GET_DATA, proto.ADC), bytes.fromhex('0c 00 02 00'))
        self.assertEqual(proto.control_packet(proto.GET_DATA, proto.PD_PACKET, 9), bytes.fromhex('0c 09 20 00'))

    def test_signed_calibrated_adc_and_aux_units(self):
        sample = proto.decode_adc(adc_payload())
        self.assertAlmostEqual(sample.voltage, 8.9999)
        self.assertAlmostEqual(sample.current, -0.999999)
        self.assertEqual(sample.cc1_mV, 633)
        self.assertEqual(sample.temp_raw, -128)
        self.assertLess(sample.power, 0)
        self.assertEqual(proto.decode_adc(adc_payload(), average=False).voltage, 9)
        self.assertIsNone(sample.timestamp_us)

    def test_chained_packets_and_truncation(self):
        p = adc_payload()
        q = pd_payload()
        raw = (struct.pack('<I', 0x41 | (30 << 22)) +
               struct.pack('<I', proto.ADC | 0x8000 | (len(p) << 22)) + p +
               struct.pack('<I', proto.PD_PACKET | (len(q) << 22)) + q)
        self.assertEqual(proto.frame_length(raw), len(raw))
        self.assertEqual([x.attribute for x in proto.logical_packets(raw)], [1, 16])
        self.assertIsNone(proto.frame_length(raw[:-1]))
        with self.assertRaises(proto.ProtocolError):
            proto.logical_packets(raw[:-1])

    def test_nonzero_tail_rejected(self):
        with self.assertRaises(proto.ProtocolError):
            proto.logical_packets(frame(1, adc_payload()) + b'bad')

    def test_empty_putdata(self):
        self.assertEqual(proto.logical_packets(b'\x41\0\0\0'), [])
        self.assertEqual(proto.frame_length(b'\x41\0\0\0' + bytes(60)), 4)

    def test_pd_message_and_millisecond_clock(self):
        sample, events = proto.decode_pd(pd_payload(), proto.ClockUnwrapper())
        self.assertEqual(sample.timestamp_us, 1_000_000)
        self.assertEqual(sample.current, -1)
        self.assertEqual(events[0].timestamp_us, 999_000)
        self.assertEqual(events[0].message, 'ACCEPT')
        row = dict(zip(proto.PD_COLUMNS, events[0].csv_row(1)))
        self.assertEqual(row['Vbus(V)'], 9000)
        self.assertEqual(row['Start Time'], row['End Time'])
        for key in ('Ok', 'Duration', 'Delta'):
            self.assertEqual(row[key], '')

    def test_pd_source_objects_and_sop(self):
        _, events = proto.decode_pd(pd_payload(header=0x2181, objects=(0x1912C, 0x2D12C), sop=1), proto.ClockUnwrapper())
        event = events[0]
        self.assertEqual(event.objects, [0x1912C, 0x2D12C])
        row = dict(zip(proto.PD_COLUMNS, event.csv_row(2)))
        self.assertEqual(row['Message'], 'SOURCE_CAPABILITIES')
        self.assertEqual(row['SOP'], "SOP'")
        self.assertEqual(row['Data'], '0x2181 0x1912C 0x2D12C')

    def test_wrapped_pd_truncation_is_not_reported_as_good(self):
        with self.assertRaises(proto.ProtocolError):
            proto.decode_pd(pd_payload()[:-1], proto.ClockUnwrapper())
        malformed = pd_payload(header=0x1143)
        with self.assertRaises(proto.ProtocolError):
            proto.decode_pd(malformed, proto.ClockUnwrapper())

    def test_connection_24bit_timestamp(self):
        ts = (1 << 24) + 123
        preamble = struct.pack('<IHhHH', ts, 5000, 0, 500, 0)
        event = b'\x45' + (122).to_bytes(3, 'little') + bytes([0, 0x21])
        _, events = proto.decode_pd(preamble + event, proto.ClockUnwrapper())
        self.assertEqual(events[0].timestamp_us, ((1 << 24) + 122) * 1000)
        self.assertEqual(events[0].message, 'CONNECT')

    def test_clock_rollover_and_small_backwards_jump(self):
        clock = proto.ClockUnwrapper()
        self.assertEqual(clock.unwrap(0xFFFFFFF0), 0xFFFFFFF0)
        self.assertEqual(clock.unwrap(5), (1 << 32) + 5)
        self.assertEqual(clock.unwrap(4), (1 << 32) + 4)
        self.assertEqual(clock.unwrap(9), (1 << 32) + 9)

    def test_vendor_new_cdc_example_without_invented_crc_or_clock_unit(self):
        raw = bytes.fromhex('02 12 05 6A 20 2A 0B 00 68 F9 88 00 2F DF EF FF 79 02 72 01 0D 01 16 01')
        sample = proto.decode_cdc_adc(raw, proto.ClockUnwrapper())[0]
        self.assertAlmostEqual(sample.voltage, 8.976744)
        self.assertAlmostEqual(sample.current, -1.056977)
        self.assertEqual(sample.cc1_mV, 633)
        self.assertIsNone(sample.timestamp_us)
        self.assertIsNone(sample.extra['crc_verified'])
        self.assertEqual(sample.extra['cdc_header_crc_raw'], 0x6A)

    def test_cdc_ambiguous_second_example_fails_with_raw_available(self):
        raw = bytes.fromhex('02 02 06 F2 CB 2D 00 00 40 1C 89 00 97 37 F7 FF 03 00 02 03 BC 0C 27 02')
        with self.assertRaises(proto.ProtocolError):
            proto.decode_cdc_adc(raw, proto.ClockUnwrapper())


class TransportTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(timeout=0.005)

    def test_fragmented_reply_and_zero_hid_padding(self):
        raw = frame(1, adc_payload(), transaction=9)
        fake = FakeTransport([raw[:3], raw[3:19], raw[19:] + bytes(64 - len(raw))])
        with transport.Meter(self.args(), fake) as meter:
            self.assertEqual(meter.get_data(1), raw)
            self.assertEqual(meter.buffer, b'')
        self.assertTrue(fake.closed)
        self.assertEqual(fake.writes, [bytes.fromhex('0c 00 02 00')])

    def test_concatenated_replies_kept_separate(self):
        one, two = frame(1, adc_payload()), frame(16, pd_payload())
        fake = FakeTransport([one + two])
        with transport.Meter(self.args(), fake) as meter:
            self.assertEqual(meter.read_frame(0.01), one)
            self.assertEqual(meter.read_frame(0.01), two)

    def test_timeout_retains_raw_transfer_callback_and_closes(self):
        seen = []
        fake = FakeTransport([b'\x41\x00'])
        with self.assertRaises(TimeoutError):
            with transport.Meter(self.args(), fake, seen.append) as meter:
                meter.get_data(1)
        self.assertEqual(seen, [b'\x41\x00'])
        self.assertTrue(fake.closed)

    def test_cdc_start_rate_stop_and_partial_frame(self):
        raw = bytes.fromhex('02 12 05 6A 20 2A 0B 00 68 F9 88 00 2F DF EF FF 79 02 72 01 0D 01 16 01')
        fake = FakeTransport([raw[:7], raw[7:]])
        args = argparse.Namespace(rate=50)
        with transport.CdcStream(args, fake) as stream:
            self.assertEqual(stream.read_frame(0.01), raw)
        self.assertEqual(fake.writes, [b'\x02\x02', b'\x03'])
        self.assertTrue(fake.closed)

    def test_ascii_sends_one_line_and_closes(self):
        fake = FakeTransport([b'ready\r\n'])
        args = argparse.Namespace(wait=0.005)
        self.assertEqual(transport.ascii_command(args, 'entry pd', fake), b'ready\r\n')
        self.assertEqual(fake.writes, [b'entry pd\r\n'])
        self.assertTrue(fake.closed)
        with self.assertRaises(ValueError):
            transport.ascii_command(args, 'entry pd\r\nreset', fake)


class CliTests(unittest.TestCase):
    def test_common_options(self):
        parser = cli.build_arg_parser()
        args = parser.parse_args(['capture', '--seconds', '10', '--scope', '--scope-raw', '--out-prefix', 'test'])
        self.assertEqual(args.seconds, 10)
        self.assertTrue(args.scope_raw)
        args = parser.parse_args(['volt-amp', '--count', '20', '--interval', '0.1', '--median', '7', '--csv', 'out.csv'])
        self.assertEqual(args.median, 7)
        self.assertIs(args.func, cli.run_live)

    def test_fixed_numeric_validation(self):
        with redirect_stderr(io.StringIO()):
            for argv in [['scope', '--seconds', 'nan'], ['scope', '--seconds', 'inf'],
                         ['live-status', '--count', '0'], ['capture', '--interval', '-1']]:
                with self.assertRaises(SystemExit):
                    cli.build_arg_parser().parse_args(argv)

    def test_ascii_document_examples(self):
        examples = [
            (['pdm', 'open'], 'pdm open'),
            (['pdm', 'set', '--type', '2', '--em', '2', '--sink', '1'], 'pdm set type=2,em=2,sink=1'),
            (['entry', 'list+'], 'entry list+'),
            (['pd', '--pdo'], 'pd pdo'),
            (['pd', '--req', '2', '--cur', '20000'], 'pd req=2,cur=20000'),
            (['pd', '--req', '5', '--volt', '12000', '--cur', '3000'], 'pd req=5,volt=12000,cur=3000'),
            (['pd', '--data', '008F1201A800FF'], 'pd data=008F1201A800FF'),
            (['pd', '--drp'], 'pd drp'),
            (['pd', '--cmd', '18'], 'pd cmd=18'),
            (['qc', '--voltage', '9'], 'qc 9V'),
            (['qc3', '--volt', '3800'], 'qc3 volt=3800'),
            (['qc3', '--inc', '8'], 'qc3 inc=8'),
            (['scp', '--volt', '11000', '--cur', '5000'], 'scp volt=11000,cur=5000'),
            (['ufcs', '--req', '1', '--volt', '11000', '--cur', '4000'], 'ufcs req=1,volt=11000,cur=4000'),
        ]
        for argv, expected in examples:
            with self.subTest(argv=argv):
                self.assertEqual(cli.build_ascii_command(cli.build_arg_parser().parse_args(argv)), expected)

    def test_dry_run_never_opens_a_port(self):
        with patch.object(cli, 'ascii_command', side_effect=AssertionError('opened hardware')):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['pd', '--cmd', '18', '--dry-run']), 0)

    def test_invalid_request_is_rejected_before_hardware(self):
        with patch.object(cli, 'ascii_command', side_effect=AssertionError('opened hardware')):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(['pd', '--pdo', '--volt', '9000'])

    def test_scope_metadata_collision(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['scope', '--csv', 'out.csv', '--raw', 'out.csv.metadata.json'])

    def test_native_records_offline_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.records.bin'
            raw = frame(16, pd_payload())
            with source.open('wb') as handle:
                cli.write_native(handle, raw)
            self.assertEqual(list(cli.read_native(source)), [raw])
            prefix = root / 'result'
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['export-gui', '--records', str(source), '--out-prefix', str(prefix), '--scope']), 0)
            with prefix.with_suffix('.csv').open(encoding='utf-8', newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]['Message'], 'ACCEPT')
            self.assertEqual(rows[0]['Vbus(V)'], '9000')
            self.assertEqual(rows[0]['Start Time'], '999000')
            self.assertEqual(rows[0]['End Time'], '999000')
            self.assertEqual(rows[0]['Duration'], '')
            with prefix.with_suffix('.scope.csv').open(encoding='utf-8-sig', newline='') as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(float(row['Ibus(A)']), -1)
            self.assertEqual(float(row['Vbus(V)']), 9)
            meta = json.loads(prefix.with_suffix('.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['timestamp_resolution_us'], 1000)
            self.assertEqual(meta['events'], 1)
            self.assertFalse(prefix.with_suffix('.records.bin').exists())

    def test_truncated_native_record(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'bad.bin'
            source.write_bytes(struct.pack('<I', 50) + b'\x41\0')
            with self.assertRaises(proto.ProtocolError):
                list(cli.read_native(source))

    def test_capture_decode_error_keeps_native_evidence(self):
        with tempfile.TemporaryDirectory() as folder, cli.ExitStack() as stack:
            prefix = Path(folder) / 'session'
            args = cli.build_arg_parser().parse_args(['capture', '--out-prefix', str(prefix)])
            export = cli.CaptureExport(stack, args, live=True)
            bad = frame(16, b'bad')
            with self.assertRaises(proto.ProtocolError):
                export.frame(bad)
            self.assertEqual(list(cli.read_native(prefix.with_suffix('.records.bin'))), [bad])
            evidence = json.loads(prefix.with_suffix('.records.jsonl').read_text(encoding='utf-8'))
            self.assertIn('decode_error', evidence)
            self.assertEqual(evidence['raw'], bad.hex(' '))
            self.assertEqual(export.events, 0)

    def test_cy4500_schema_matches_local_reference(self):
        reference_root = os.environ.get('CY4500_CLI_ROOT')
        if not reference_root:
            self.skipTest('set CY4500_CLI_ROOT to enable reference compatibility tests')
        reference = Path(reference_root) / 'ezpd_protocol.py'
        if not reference.is_file():
            self.skipTest('local CY4500 source is not available')
        tree = ast.parse(reference.read_text(encoding='utf-8-sig'))
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for name in node.targets:
                    if isinstance(name, ast.Name) and name.id in ('SCOPE_CSV_COLUMNS', 'CSV_COLUMNS'):
                        values[name.id] = ast.literal_eval(node.value)
        self.assertEqual(list(values['SCOPE_CSV_COLUMNS']), proto.SCOPE_COLUMNS)
        self.assertEqual(list(values['CSV_COLUMNS']), proto.PD_COLUMNS)

    def test_existing_cy4500_reader_consumes_point_events_and_scope(self):
        reference_root = os.environ.get('CY4500_CLI_ROOT')
        if not reference_root:
            self.skipTest('set CY4500_CLI_ROOT to enable reference compatibility tests')
        reference = Path(reference_root) / 'cy4500_cli.py'
        if not reference.is_file():
            self.skipTest('local CY4500 source is not available')
        # Execute only the actual read-only CSV loaders; no USB dependency or device access.
        from types import SimpleNamespace
        source = reference.read_text(encoding='utf-8-sig')
        wanted = {'_optional_int', '_optional_float', '_load_pd_capture_csv', '_load_scope_csv'}
        nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in wanted]
        self.assertEqual(len(nodes), len(wanted))
        namespace = {'csv': csv, 'math': math, 'Path': Path, 'SyncPDSample': SimpleNamespace,
                     'SyncScopeSample': SimpleNamespace}
        code = 'from __future__ import annotations\n' + '\n\n'.join(ast.get_source_segment(source, n) for n in nodes)
        exec(compile(code, str(reference), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as folder, cli.ExitStack() as stack:
            prefix = Path(folder) / 'session'
            args = cli.build_arg_parser().parse_args(['capture', '--scope', '--out-prefix', str(prefix), '--quiet'])
            export = cli.CaptureExport(stack, args, live=True)
            export.frame(frame(16, pd_payload(header=0x1142, objects=(0x12345678,))))
            pd_samples = namespace['_load_pd_capture_csv'](prefix.with_suffix('.csv'))
            scope_samples = namespace['_load_scope_csv'](prefix.with_suffix('.scope.csv'))
            self.assertEqual(len(pd_samples), 1)
            self.assertEqual(pd_samples[0].message, 'REQUEST')
            self.assertEqual(pd_samples[0].data, bytes.fromhex('78 56 34 12'))
            self.assertEqual(pd_samples[0].vbus_V, 9)
            self.assertEqual(pd_samples[0].start_us, pd_samples[0].end_us)
            self.assertEqual(len(scope_samples), 1)
            self.assertEqual(scope_samples[0].ibus_A, -1)

    def test_auto_capture_uses_usb_and_adc_uses_hid(self):
        for command, expected in [('capture', transport.UsbTransport), ('live-status', transport.HidTransport)]:
            args = cli.build_arg_parser().parse_args([command])
            self.assertIsInstance(transport.selected_transport(args), expected)
        args = cli.build_arg_parser().parse_args(['capture', '--port', 'COM3'])
        self.assertIsInstance(transport.selected_transport(args), transport.SerialTransport)


if __name__ == '__main__':
    unittest.main()
