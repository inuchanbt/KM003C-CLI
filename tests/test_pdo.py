"""Offline PDO acquisition tests: one COM session, shared setup and exit cleanup."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import km003c_cli as cli
from km003c_modules import pdo, sweep
from test_sweep import Clock, Serial


SPR = b'max power 92W\r\nFixed:     5.00V 3.00A\r\nAVS: 9-20V3.00A,4.60A\r\n'
EPR = b'max power 240W\r\nFixed:     5.00V 3.00A\r\nFixed:    48.00V 5.00A\r\nAVS: 15.00-48.00V 240W\r\n'


class PdoTests(unittest.TestCase):
    def execute(self, *extra, serial=None, clock=None):
        clock = clock or Clock()
        serial = serial or Serial(clock, {'pd pdo': EPR})
        output, error = io.StringIO(), io.StringIO()
        with patch.object(pdo, 'SerialTransport', return_value=serial), \
                patch.object(sweep, 'Meter', side_effect=AssertionError('ADC not needed')), \
                patch.object(sweep.time, 'monotonic', clock.monotonic), \
                patch.object(sweep.time, 'sleep', clock.sleep), \
                redirect_stdout(output), redirect_stderr(error):
            result = cli.main(['pdo', '--port', 'COM14', '--wait', '.1', *extra])
        return result, serial, clock, output.getvalue(), error.getvalue()

    def test_shared_sequence_uses_one_connection_and_never_requests_voltage(self):
        result, serial, _, output, error = self.execute()
        self.assertEqual(result, 0)
        self.assertEqual(serial.raw_writes, [b'pdm open', b'pdm set type=2,em=2,sink=1',
                                            b'entry pd', b'pd pdo', b'reset', b'pdm close'])
        self.assertEqual((serial.opens, serial.closes), (1, 1))
        self.assertIn('Source PDOs:', output)
        self.assertIn('48.00V', output)
        self.assertEqual(error, '')

    def test_epr_polling_and_busy_recovery_share_sweep_behavior(self):
        clock = Clock()
        class RecoveringSerial(Serial):
            def write(self, data):
                super().write(data)
                if data == b'pdm open' and self.writes.count('pdm open') == 1:
                    self.pending = [b'pdm busy\r\n']
                if data == b'pd pdo':
                    self.pending = [SPR if self.writes.count('pd pdo') == 1 else EPR]
        serial = RecoveringSerial(clock)
        result, serial, _, output, _ = self.execute(serial=serial, clock=clock)
        self.assertEqual(result, 0)
        self.assertEqual(serial.writes[:4], ['pdm open', 'pdm close', 'pdm open',
                                             'pdm set type=2,em=2,sink=1'])
        self.assertEqual(serial.writes.count('pd pdo'), 2)
        self.assertEqual((serial.opens, serial.closes), (1, 1))
        self.assertIn('240W', output.split('Source PDOs:')[1])

    def test_spr_source_succeeds_and_retains_valid_reply_after_empty_polls(self):
        clock = Clock()
        class SprSerial(Serial):
            def write(self, data):
                super().write(data)
                if data == b'pd pdo':
                    self.pending = [SPR if self.writes.count('pd pdo') == 1 else b'']
        result, serial, _, output, error = self.execute('--entry-timeout', '.2', '--quiet',
                                                       serial=SprSerial(clock), clock=clock)
        self.assertEqual(result, 0)
        self.assertGreater(serial.writes.count('pd pdo'), 1)
        self.assertIn('EPR PDOs were not observed', error)
        self.assertIn('max power 92W', output)
        self.assertNotIn('pdm open', output)
        self.assertEqual(serial.writes[-2:], ['reset', 'pdm close'])

    def test_no_epr_and_pd30_settings_do_not_wait_for_extended_pdos(self):
        for options in (['--no-epr'], ['--type', '1', '--em', '1'], ['--em', '0']):
            with self.subTest(options=options):
                clock = Clock(); serial = Serial(clock, {'pd pdo': SPR})
                result, serial, _, output, error = self.execute(*options, serial=serial, clock=clock)
                self.assertEqual(result, 0)
                self.assertEqual(serial.writes.count('pd pdo'), 1)
                self.assertEqual(error, '')
                self.assertIn('92W', output)

    def test_epr_fixed_only_source_is_recognized_without_avs(self):
        clock = Clock(); serial = Serial(clock, {'pd pdo': b'Fixed: 28.00V 5.00A\n'})
        result, serial, _, output, error = self.execute(serial=serial, clock=clock)
        self.assertEqual(result, 0)
        self.assertEqual(serial.writes.count('pd pdo'), 1)
        self.assertIn('28.00V', output)
        self.assertEqual(error, '')

    def test_failures_and_interrupts_always_cleanup_without_success_output(self):
        for stage, reply, message, code in (
                ('entry pd', b'', 'did not reply ready', 1),
                ('pd pdo', b'', 'No PDO capabilities', 1),
                ('pd pdo', b'OK\n', 'No PDO capabilities', 1),
                ('pdm set type=2,em=2,sink=1', b'error: rejected\n', 'Device rejected', 1),
                ('pd pdo', [KeyboardInterrupt()], '', 130)):
            with self.subTest(stage=stage, reply=reply):
                clock = Clock(); serial = Serial(clock, {stage: reply})
                result, serial, _, output, error = self.execute('--entry-timeout', '.2',
                                                               serial=serial, clock=clock)
                self.assertEqual(result, code)
                self.assertEqual(serial.writes[-2:], ['reset', 'pdm close'])
                self.assertEqual(serial.closes, 1)
                self.assertNotIn('Source PDOs:', output)
                if message:
                    self.assertIn(message, error)

    def test_original_failure_preserved_and_successful_query_cleanup_failure_fails(self):
        clock = Clock(); serial = Serial(clock, {'pd pdo': EPR, 'reset': b'error: reset failed\n'})
        result, serial, _, output, error = self.execute(serial=serial, clock=clock)
        self.assertEqual(result, 1)
        self.assertIn('48.00V', output)
        self.assertIn('Trigger cleanup failed', error)
        self.assertEqual(serial.writes[-1], 'pdm close')
        clock = Clock(); serial = Serial(clock, {'entry pd': b'error: no source\n',
                                                'reset': b'error: reset failed\n'})
        result, serial, _, _, error = self.execute(serial=serial, clock=clock)
        self.assertEqual(result, 1)
        self.assertIn('Error: Device rejected entry pd', error)
        self.assertEqual(serial.writes[-1], 'pdm close')

    def test_exact_response_file_force_preflight_and_dry_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'pdo.txt'
            self.assertEqual(self.execute('--response-file', str(path))[0], 0)
            self.assertEqual(path.read_bytes(), EPR)
            with patch.object(pdo, 'SerialTransport', side_effect=AssertionError('hardware')), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['pdo', '--response-file', str(path)]), 1)
            path.write_bytes(b'previous')
            result, serial, _, _, _ = self.execute('--response-file', str(path), '--force', '--dry-run')
            self.assertEqual(result, 0)
            self.assertEqual(serial.opens, 0)
            self.assertEqual(path.read_bytes(), b'previous')
            self.assertEqual(self.execute('--response-file', str(path), '--force')[0], 0)
            self.assertEqual(path.read_bytes(), EPR)
            path.unlink(); path.mkdir()
            with patch.object(pdo, 'SerialTransport', side_effect=AssertionError('hardware')), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['pdo', '--response-file', str(path), '--force']), 1)

    def test_explicit_keep_trigger_and_option_defaults(self):
        args = cli.build_arg_parser().parse_args(['pdo'])
        self.assertTrue(args.epr)
        self.assertEqual((args.wait, args.entry_timeout, args.pdm_startup_wait), (1, 10, 2))
        self.assertFalse(args.keep_trigger)
        result, serial, _, _, _ = self.execute('--keep-trigger')
        self.assertEqual(result, 0)
        self.assertEqual(serial.writes[-1], 'pd pdo')
        self.assertNotIn('reset', serial.writes)
        self.assertEqual(serial.closes, 1)


if __name__ == '__main__':
    unittest.main()
