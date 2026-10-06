"""GUI export regression tests using synthetic KM003C data only."""
import csv
from contextlib import ExitStack, redirect_stdout, redirect_stderr
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
from km003c_modules import utility_export as gui
from test_km003c import frame, pd_payload


class UtilityExportTests(unittest.TestCase):
    def test_sop_and_revision_names_and_unknown_status_remain_unmeasured(self):
        for sop, name in enumerate(('SOP', 'SOP_PRIME', 'SOP_DPRIME')):
            _, events = proto.decode_pd(pd_payload(header=0x2181, objects=(123, 456), sop=sop), proto.ClockUnwrapper())
            raw, row = gui.packet_for_event(events[0], 7)
            values = dict(zip(proto.PD_COLUMNS, row))
            self.assertEqual(values['SOP'], name)
            self.assertEqual(values['Rev'], 'v3')
            self.assertEqual(values['Data'], '0x2181 0x7B 0x1C8')
            self.assertEqual(values['Start Time'], values['End Time'])
            for key in ('Ok', 'Duration', 'Delta'):
                self.assertEqual(values[key], '')
            self.assertEqual(struct.unpack_from('<I', raw, 16)[0], 0x2181 | sop << 16)
            self.assertEqual(raw[20:], struct.pack('<II', 123, 456))

    def test_extended_wire_bytes_are_retained_in_gui_adapter(self):
        _, events = proto.decode_pd(pd_payload(header=0x9081, objects=(0xABCD0002,)), proto.ClockUnwrapper())
        raw, row = gui.packet_for_event(events[0], 1)
        self.assertEqual(raw[20:], bytes.fromhex('02 00 cd ab'))
        self.assertEqual(row[12], '0x9081 0x2 0xCD 0xAB')

    def test_headerless_and_unsupported_sop_are_not_fabricated_as_pd(self):
        preamble = pd_payload()[:12]
        for tail in (b'\x05\x01', b'\x45\x01\x00\x00\x00\x21'):
            _, events = proto.decode_pd(preamble + tail, proto.ClockUnwrapper())
            self.assertIsNone(gui.packet_for_event(events[0], 1))
        _, events = proto.decode_pd(pd_payload(sop=6), proto.ClockUnwrapper())
        self.assertIsNone(gui.packet_for_event(events[0], 1))

    def test_zip_contains_java_packet_and_graph_streams_with_quantized_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.ccgx3'
            writer = gui.UtilityExport(ccgx3_path=path)
            sample, events = proto.decode_pd(pd_payload(ts=3000), proto.ClockUnwrapper())
            raw, row = gui.packet_for_event(events[0], 1)
            writer.write_packet(raw, row)
            writer.write_scope(sample)
            writer.close()
            writer.close()  # ExitStack cleanup must not recreate an archive.
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                packets = archive.read(next(n for n in archive.namelist() if n.endswith('.part')))
                graph = archive.read(next(n for n in archive.namelist() if n.endswith('.scope')))
            for content in (packets, graph):
                self.assertTrue(content.startswith(b'\xac\xed\x00\x05'))
                self.assertIn(b'\x77\x04\x00\x00\x00\x01', content)
            self.assertIn(b'USBPacketData', packets)
            self.assertIn(raw, packets)
            amp, cc1, cc2, timestamp, volt = struct.unpack('>hhhqH', graph[-17:-1])
            self.assertEqual(timestamp, sample.timestamp_us)
            # Utility charts read GraphData fields directly in mV and mA.
            self.assertEqual((volt, amp, cc1, cc2), (9000, -1000, 633, 370))
            self.assertEqual(writer.graph_clipped, {})
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_epr_voltage_and_negative_current_use_physical_graph_units(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'epr.ccgx3'
            writer = gui.UtilityExport(ccgx3_path=path)
            sample, _ = proto.decode_pd(pd_payload(), proto.ClockUnwrapper())
            sample.vbus_uV = 48000123
            sample.ibus_uA = -1234567
            writer.write_scope(sample)
            sample.vbus_uV = 70000000
            writer.write_scope(sample)
            writer.close()
            with zipfile.ZipFile(path) as archive:
                graph = archive.read(next(n for n in archive.namelist() if n.endswith('.scope')))
            size = 1 + len(gui.GRAPH) + 16
            first = struct.unpack('>hhhqH', graph[-1-size-16:-1-size])
            last = struct.unpack('>hhhqH', graph[-17:-1])
            self.assertEqual((first[0], first[4]), (-1235, 48000))
            self.assertEqual(last[4], 65535)
            self.assertEqual(writer.graph_clipped, {'VBUS': 1})

    def test_defaults_and_cy_ti_format_options(self):
        parser = cli.build_arg_parser()
        default = parser.parse_args(['capture'])
        self.assertTrue(default.scope)
        self.assertEqual(cli.output_formats(default), {'original', 'csv', 'ccgx3'})
        for argv, expected in [(['--no-ccgx3'], {'original', 'csv'}),
                               (['--formats', 'csv'], {'csv'}),
                               (['--formats', 'original', '--ccgx3'], {'original', 'ccgx3'}),
                               (['--formats', 'all', '--no-ccgx3'], {'original', 'csv'})]:
            self.assertEqual(cli.output_formats(parser.parse_args(['capture'] + argv)), expected)
        args = parser.parse_args(['convert', '--input', 'native.bin', '--out-prefix', 'out', '--no-scope'])
        self.assertEqual(args.records, 'native.bin')
        self.assertFalse(args.scope)
        self.assertIs(args.func, cli.run_export)

    def test_collision_is_refused_before_hardware_and_force_overwrites_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / 'session'
            target = prefix.with_suffix('.ccgx3')
            target.write_bytes(b'keep this original')
            with patch.object(cli, 'Meter', side_effect=AssertionError('opened hardware')):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(['capture', '--out-prefix', str(prefix)]), 1)
            self.assertEqual(target.read_bytes(), b'keep this original')
            source = root / 'input.records.bin'
            with source.open('wb') as handle:
                cli.write_native(handle, frame(proto.PD_PACKET, pd_payload()))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['export-gui', '--records', str(source), '--out-prefix', str(prefix), '--force']), 0)
            self.assertTrue(zipfile.is_zipfile(target))

    def test_no_scope_and_selected_formats_do_not_leave_unrequested_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.records.bin'
            with source.open('wb') as handle:
                cli.write_native(handle, frame(proto.PD_PACKET, pd_payload()))
            prefix = root / 'result'
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['convert', '--input', str(source), '--out-prefix', str(prefix),
                                           '--formats', 'csv', 'ccgx3', '--no-scope']), 0)
            self.assertTrue(prefix.with_suffix('.csv').is_file())
            self.assertFalse(prefix.with_suffix('.records.jsonl').exists())
            self.assertFalse(prefix.with_suffix('.scope.csv').exists())
            meta = json.loads(prefix.with_suffix('.metadata.json').read_text())
            self.assertEqual(meta['ccgx3_waveform_samples'], 0)
            with zipfile.ZipFile(prefix.with_suffix('.ccgx3')) as archive:
                graph = archive.read(next(n for n in archive.namelist() if n.endswith('.scope')))
            self.assertIn(b'\x77\x04\x00\x00\x00\x00', graph)

    def test_input_alias_is_protected_even_with_force(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / 'input'
            source = prefix.with_suffix('.csv')
            source.write_bytes(b'original data')
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['convert', '--input', str(source), '--out-prefix', str(prefix), '--force']), 1)
            self.assertEqual(source.read_bytes(), b'original data')

    def test_atomic_archive_failure_preserves_existing_target_and_cleans_temporaries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.ccgx3'
            path.write_bytes(b'original archive')
            writer = gui.UtilityExport(ccgx3_path=path)
            with patch.object(writer, '_write_archive', side_effect=OSError('disk failed')):
                with self.assertRaises(OSError):
                    writer.close()
            self.assertEqual(path.read_bytes(), b'original archive')
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])
            self.assertTrue(writer.packets.closed)


if __name__ == '__main__':
    unittest.main()
