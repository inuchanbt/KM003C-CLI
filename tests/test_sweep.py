"""Offline sweep tests: ASD options, endpoint order and partial command evidence."""
import argparse
import ast
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import os
import signal
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import km003c_cli as cli
from km003c_modules import sweep, transport
from test_km003c import adc_payload, frame
from km003c_modules.protocol import ADC, ProtocolError


class Clock:
    def __init__(self):
        self.now = 0.
        self.sleeps = []
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Serial:
    def __init__(self, clock, replies=None, fail_write=None):
        self.clock = clock
        self.replies = replies or {}
        self.fail_write = fail_write
        self.writes = []
        self.raw_writes = []
        self.opens = self.closes = 0
        self.pending = []
    def open(self):
        self.opens += 1
    def close(self):
        self.closes += 1
    def write(self, data):
        command = data.decode('ascii').strip()
        if self.fail_write == command:
            raise OSError('write failed')
        self.writes.append(command)
        self.raw_writes.append(data)
        reply = self.replies.get(command, b'ready\r\n' if command == 'entry pd' else b'OK\r\n')
        self.pending = list(reply) if isinstance(reply, list) else [reply]
    def read(self, seconds):
        self.clock.now += seconds
        result = self.pending.pop(0) if self.pending else b''
        if isinstance(result, BaseException):
            raise result
        return result


class Meter:
    def __init__(self, raw=None):
        self.raw = raw if raw is not None else frame(ADC, adc_payload())
        self.reads = self.closes = 0
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.closes += 1
    def get_data(self, attribute):
        self.reads += 1
        if isinstance(self.raw, BaseException):
            raise self.raw
        return self.raw


class SweepTests(unittest.TestCase):
    def args(self, *extra):
        return cli.build_arg_parser().parse_args([
            'sweep', '--sweep', '15:17:1:5', '--pdo-index', '11',
            '--wait', '.1', '--quiet', '--keep-trigger', *extra])

    def execute(self, args, *, serial=None, meter=None, clock=None, output=None):
        clock = clock or Clock()
        serial = serial or Serial(clock)
        meter = meter or Meter()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sweep.time, 'monotonic', clock.monotonic))
            stack.enter_context(patch.object(sweep.time, 'sleep', clock.sleep))
            stack.enter_context(patch.object(sweep, 'SerialTransport', return_value=serial))
            stack.enter_context(patch.object(sweep, 'Meter', return_value=meter))
            stack.enter_context(redirect_stdout(output if output is not None else io.StringIO()))
            result = cli.run_sweep(args)
        return result, clock, serial, meter

    def rows(self, path):
        with Path(path).open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

    def test_pause_after_epr_and_adc_preparation_before_first_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; clock=Clock(); meter=self.release_meter()
            class EprSerial(Serial):
                def write(self,data):
                    super().write(data)
                    if data==b'pd pdo':
                        self.pending=[b'AVS: 9-20V3.00A,4.60A\n' if self.writes.count('pd pdo')==1
                                      else b'AVS: 15.00-48.00V 240W\n']
            serial=EprSerial(clock)
            def resume(prompt):
                self.assertIn('5 A',prompt)
                self.assertEqual(serial.writes,['pdm open','pdm set type=2,em=2,sink=1',
                                                'entry pd','pd pdo','pd pdo'])
                self.assertEqual(meter.reads,0)
                self.assertIs(sweep.Meter.return_value,meter)
                self.assertEqual(sweep.Meter.call_count,1)
                clock.sleep(7)
                return ''
            with patch('builtins.input',side_effect=resume) as wait:
                result,*_=self.execute(self.args('--sweep','15:48:1:5','--continuous-sweep',
                    '--pause-before-sweep','--csv',str(path),'--no-keep-trigger'),
                    clock=clock,serial=serial,meter=meter)
            self.assertEqual(result,0)
            self.assertEqual(wait.call_count,1)
            self.assertTrue(serial.writes[5].startswith('pd req='))
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['pause_before_sweep'],dict(status='continued',elapsed_s=7.0))
            self.assertEqual(info['sent_requests'],34)
            self.assertEqual(len(self.rows(path)),34)

    def test_quiet_pause_prompt_visible_and_no_init_supported(self):
        output=io.StringIO();clock=Clock();serial=Serial(clock)
        with patch('sys.stdin',io.StringIO('\n')):
            result,*_=self.execute(self.args('--pause-before-sweep','--no-initialize','--no-csv'),
                                   clock=clock,serial=serial,output=output)
        self.assertEqual(result,0)
        self.assertIn('press Enter to start sweep',output.getvalue())
        self.assertTrue(all(c.startswith('pd req=') for c in serial.writes))

    def test_pause_interrupt_and_eof_release_without_sweep_requests(self):
        for exception in (KeyboardInterrupt(),EOFError()):
            with self.subTest(exception=type(exception).__name__), tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'run.csv';clock=Clock();serial=Serial(clock);meter=self.release_meter()
                args=self.args('--pause-before-sweep','--continuous-sweep',
                               '--no-keep-trigger','--csv',str(path))
                with patch('builtins.input',side_effect=exception):
                    if isinstance(exception,KeyboardInterrupt):
                        result,*_=self.execute(args,clock=clock,serial=serial,meter=meter)
                        self.assertEqual(result,130)
                    else:
                        with self.assertRaisesRegex(ProtocolError,'no Enter received'):
                            self.execute(args,clock=clock,serial=serial,meter=meter)
                self.assertEqual(serial.writes[-2:],['reset','pdm close'])
                self.assertFalse(any(c.startswith('pd req=') for c in serial.writes))
                self.assertEqual((serial.closes,meter.closes),(1,1))
                self.assertEqual(self.rows(path),[])
                info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
                self.assertEqual(info['sent_requests'],0)
                self.assertEqual(info['pause_before_sweep']['status'],
                                 'interrupted' if isinstance(exception,KeyboardInterrupt) else 'failed')
                self.assertEqual(info['cleanup']['status'],'acknowledged')

    def test_pause_not_reached_on_failed_preparation_and_off_by_default(self):
        with patch('builtins.input',side_effect=AssertionError('unexpected pause')):
            self.assertFalse(self.args().pause_before_sweep)
            self.execute(self.args('--no-csv'))
            clock=Clock();serial=Serial(clock,{'entry pd':b'error: no source\n'})
            with self.assertRaisesRegex(ProtocolError,'Device rejected entry pd'):
                self.execute(self.args('--pause-before-sweep','--no-csv'),clock=clock,serial=serial)
            with patch.object(sweep,'Meter',side_effect=OSError('ADC open failed')):
                with patch.object(sweep,'SerialTransport',return_value=Serial(clock)), \
                        patch.object(sweep.time,'monotonic',clock.monotonic), \
                        patch.object(sweep.time,'sleep',clock.sleep), redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(OSError,'ADC open failed'):
                        cli.run_sweep(self.args('--pause-before-sweep','--measure','--no-csv'))

    def test_pause_and_force_option_first_dry_run_never_waits_or_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv';path.write_text('old log',encoding='utf-8')
            meta=Path(str(path)+'.metadata.json');meta.write_text('old metadata',encoding='utf-8')
            output=io.StringIO()
            with patch('builtins.input',side_effect=AssertionError('input')), \
                    patch.object(sweep,'SerialTransport',side_effect=AssertionError('hardware')), \
                    redirect_stdout(output):
                self.assertEqual(cli.main(['--mode','avs','--sweep','15:17:1:5','--pdo-index','11',
                    '--pause-before-sweep','--force','--csv',str(path),'--dry-run']),0)
            text=output.getvalue()
            self.assertLess(text.index('pd pdo'),text.index('Pause before sweep'))
            self.assertLess(text.index('Pause before sweep'),text.index('pd req='))
            self.assertEqual(path.read_text(encoding='utf-8'),'old log')
            self.assertEqual(meta.read_text(encoding='utf-8'),'old metadata')

    def test_force_overwrites_csv_and_matching_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv';path.write_text('old log',encoding='utf-8')
            meta=Path(str(path)+'.metadata.json');meta.write_text('old metadata',encoding='utf-8')
            args=self.args('--csv',str(path),'--force','--test-note','replacement')
            self.assertEqual(args.csv_mode,'overwrite')
            self.execute(args)
            self.assertEqual(len(self.rows(path)),3)
            self.assertTrue(all(row['test_note']=='replacement' for row in self.rows(path)))
            self.assertEqual(path.read_bytes().count(bytes.fromhex('efbbbf')),1)
            info=json.loads(meta.read_text(encoding='utf-8'))
            self.assertEqual(info['arguments']['csv_mode'],'overwrite')
            self.assertEqual(info['sent_requests'],3)
            self.assertEqual(list(Path(folder).glob('*.run_*.metadata.json')),[])

    def test_force_requires_csv_and_rejects_append_and_directories(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['--sweep','15:17:1:5','--pdo-index','11','--force'])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args('--csv','run.csv','--force','--csv-append')
        with tempfile.TemporaryDirectory() as folder, patch.object(
                sweep,'SerialTransport',side_effect=AssertionError('hardware')):
            path=Path(folder)/'run.csv';path.mkdir()
            with self.assertRaises(FileExistsError):
                cli.run_sweep(self.args('--csv',str(path),'--force'))
            path.rmdir();Path(str(path)+'.metadata.json').mkdir()
            with self.assertRaises(FileExistsError):
                cli.run_sweep(self.args('--csv',str(path),'--force'))

    def test_full_avs_roundtrip_and_descending(self):
        p = sweep.build_plan(self.args('--sweep', '15:48:1:5', '--round-trip-sweep'))
        self.assertEqual([x.voltage_mv for x in p], list(range(15000,49000,1000)) + list(range(47000,14000,-1000)))
        self.assertEqual(len(p), 67)
        self.assertEqual(p[34].leg, 'return')
        self.assertEqual(p[34].sweep_pass, 2)
        self.assertEqual(p[-1].command(11), 'pd req=11,volt=15000,cur=5000')
        p = sweep.build_plan(self.args('--sweep', '17:15:-1:5', '--round-trip-sweep'))
        self.assertEqual([x.voltage_mv for x in p], [17000,16000,15000,16000,17000])

    def test_decimal_endpoints_and_return_on_actual_grid(self):
        p = sweep.build_plan(self.args('--sweep', '5:5.06:.02:1'))
        self.assertEqual([x.voltage_mv for x in p], [5000,5020,5040,5060])
        p = sweep.build_plan(self.args('--sweep', '5:6:.3:1', '--round-trip-sweep'))
        self.assertEqual([x.voltage_mv for x in p], [5000,5300,5600,5900,5600,5300,5000])
        self.assertEqual(len(sweep.build_plan(self.args('--sweep','15:15:1:5','--round-trip-sweep'))),1)

    def test_pps_default_and_request_current_override(self):
        args = cli.build_arg_parser().parse_args(['sweep','--pps-sweep','5:21:1','--pdo-index','6'])
        self.assertEqual(sweep.build_plan(args)[0].current_ma,1000)
        self.assertEqual(sweep.build_plan(self.args('--request-current','3'))[0].current_ma,3000)
        self.assertEqual(sweep.build_plan(self.args('--sweep','15:17:1','--request-current','2'))[0].current_ma,2000)

    def test_invalid_plan_rejected_before_hardware(self):
        expressions = ['15:17:0:5','15:17:-1:5','17:15:1:5','nan:17:1:5',
                       '15:inf:1:5','15:17:1:-1','15:17:1:6.4','15:17:1',
                       '0:17:1:5','15:66:1:5','15:17:.0001:5','15:17:1:1.0001',
                       '1:65:.001:5','15:17:1:nan','15:17:1:inf','1e9999:17:1:5',
                       '15:17:1e1000000:5','15.000000000000000000000000000000001:17:1:5']
        with patch.object(sweep,'SerialTransport', side_effect=AssertionError('hardware')):
            for expression in expressions:
                with self.subTest(expression=expression), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        cli.main(['sweep','--sweep',expression,'--pdo-index','11','--round-trip-sweep'])
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(['sweep','--sweep','15:17:1:5','--pdo-index','16'])

    def test_asd_option_first_and_load_alias_dry_run_no_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'existing.csv'
            path.write_text('keep',encoding='utf-8')
            with patch.object(sweep,'SerialTransport',side_effect=AssertionError('hardware')), patch.object(sweep,'Meter',side_effect=AssertionError('hardware')):
                for prefix in ([],['sweep'],['load']):
                    output=io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(cli.main([*prefix,'--mode','avs','--sweep','15:48:1:5',
                            '--pdo-index','11','--round-trip-sweep','--continuous-sweep',
                            '--csv',str(path),'--csv-overwrite','--dry-run']),0)
                    self.assertIn('67 requests',output.getvalue())
                    self.assertIn('volt=48000',output.getvalue())
            self.assertEqual(path.read_text(encoding='utf-8'),'keep')
            self.assertEqual(len(list(Path(folder).iterdir())),1)

    def test_shared_defaults(self):
        a=self.args()
        self.assertEqual((a.mode,a.delay,a.continuous_settle,a.apdo_voltage_hold,a.measure_loop),
                         ('auto',.5,.5,0.,0))
        self.assertFalse(a.round_trip_sweep)
        self.assertFalse(a.continuous_sweep)
        self.assertFalse(a.measure)
        self.assertEqual(a.csv_mode,'error')
        self.assertEqual(a.entry_timeout,10.0)

    def test_single_serial_session_and_unverified_request_log(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'
            result,clock,serial,meter=self.execute(self.args('--csv',str(path),'--round-trip-sweep'))
            self.assertEqual(result,0)
            self.assertEqual((serial.opens,serial.closes),(1,1))
            self.assertEqual(serial.writes[:4],['pdm open','pdm set type=2,em=2,sink=1','entry pd','pd pdo'])
            self.assertEqual(len(serial.writes),9)
            self.assertEqual(serial.raw_writes,[c.encode('ascii') for c in serial.writes])
            rows=self.rows(path)
            self.assertEqual([r['target_voltage_v'] for r in rows],['15.0','16.0','17.0','16.0','15.0'])
            self.assertTrue(all(r['actual_voltage_v']==r['target_load_current_a']=='' for r in rows))
            self.assertTrue(all(r['request_status']=='sent_unverified' for r in rows))
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual((info['planned_points'],info['sent_requests'],info['completed_points']),(5,5,5))
            self.assertEqual(len(info['setup']),4)

    def test_firmware_exact_entry_match_without_crlf(self):
        clock=Clock()
        class ExactSerial(Serial):
            def write(self,data):
                super().write(data)
                if data.startswith(b'entry pd') and data != b'entry pd':
                    self.pending=[]
        serial=ExactSerial(clock)
        result,*_=self.execute(self.args('--no-csv'),clock=clock,serial=serial)
        self.assertEqual(result,0)
        self.assertEqual(serial.raw_writes[:4],[b'pdm open',b'pdm set type=2,em=2,sink=1',b'entry pd',b'pd pdo'])
        self.assertEqual(serial.raw_writes[-1],b'pd req=11,volt=17000,cur=5000')

    def test_continuous_measurement_values_and_adc_interface(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'
            args=self.args('--csv',str(path),'--continuous-sweep','--measure-loop','2','--apdo-voltage-hold','2')
            clock=Clock(); serial=Serial(clock); meter=Meter()
            with patch.object(sweep,'Meter',return_value=meter) as constructor:
                with patch.object(sweep.time,'monotonic',clock.monotonic), patch.object(sweep.time,'sleep',clock.sleep), patch.object(sweep,'SerialTransport',return_value=serial),redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.run_sweep(args),0)
            self.assertEqual(meter.reads,6)
            self.assertEqual(meter.closes,1)
            adc_args=constructor.call_args.args[0]
            self.assertEqual(adc_args.transport,'hid')
            self.assertIsNone(adc_args.port)
            rows=self.rows(path)
            self.assertEqual(len(rows),6)
            self.assertAlmostEqual(float(rows[0]['actual_voltage_v']),8.9999)
            self.assertAlmostEqual(float(rows[0]['actual_current_a']),-.999999)
            self.assertEqual(rows[0]['measurement_index'],'1')
            self.assertEqual(rows[1]['measurement_index'],'2')
            # Replies .1 + settle .5 + second measurement delay .5 leave .9 of minimum hold.
            self.assertEqual(len(clock.sleeps),9)
            self.assertAlmostEqual(sum(clock.sleeps),5.7)

    def test_measurement_timestamp_is_receipt_time_not_request_time(self):
        clock=Clock()
        class DateTime:
            @staticmethod
            def now():
                return datetime(2026,10,7,tzinfo=timezone(timedelta(hours=9)))+timedelta(seconds=clock.now)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'
            with patch.object(sweep,'datetime',DateTime):
                self.execute(self.args('--csv',str(path),'--no-initialize','--continuous-sweep'),clock=clock)
            row=self.rows(path)[0]
            sent=datetime.fromisoformat(row['request_sent_at'])
            received=datetime.fromisoformat(row['timestamp'])
            self.assertAlmostEqual((received-sent).total_seconds(),.6)

    def test_failed_second_measurement_does_not_reuse_first_values(self):
        class ChangingMeter(Meter):
            def get_data(self, attribute):
                self.reads += 1
                return frame(ADC,adc_payload()) if self.reads==1 else frame(ADC,b'bad')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; meter=ChangingMeter()
            with self.assertRaises(ProtocolError):
                self.execute(self.args('--csv',str(path),'--no-initialize','--measure-loop','2'),meter=meter)
            rows=self.rows(path)
            self.assertTrue(rows[0]['actual_voltage_v'])
            self.assertEqual(rows[-1]['actual_voltage_v'],'')
            self.assertEqual(rows[-1]['actual_current_a'],'')
            self.assertEqual(rows[-1]['measurement_index'],'2')
            self.assertEqual(bytes.fromhex(rows[-1]['raw_measure_response']),frame(ADC,b'bad'))

    def test_minimum_hold_includes_reply_window(self):
        result,clock,serial,_=self.execute(self.args('--no-csv','--no-initialize','--apdo-voltage-hold','2'))
        self.assertAlmostEqual(clock.now,6)
        self.assertEqual(len(clock.sleeps),3)
        self.assertTrue(all(abs(x-1.9)<1e-8 for x in clock.sleeps))

    def test_entry_not_ready_stops_before_first_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; clock=Clock()
            serial=Serial(clock,{'entry pd':b'not ready\r\n'})
            with self.assertRaises(ProtocolError):
                self.execute(self.args('--csv',str(path)),clock=clock,serial=serial)
            self.assertEqual(serial.writes,['pdm open','pdm set type=2,em=2,sink=1','entry pd'])
            self.assertEqual(serial.closes,1)
            self.assertEqual(self.rows(path),[])
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['status'],'failed')
            self.assertEqual(info['sent_requests'],0)

    def test_delayed_split_ready_waits_then_queries_pdo_without_resending(self):
        clock=Clock()
        # More than the old 1 s cutoff, followed by fragmented ready bytes.
        serial=Serial(clock,{'entry pd':[b'']*24+[b'rea',b'dy\r\n']})
        result,*_=self.execute(self.args('--no-csv'),clock=clock,serial=serial)
        self.assertEqual(result,0)
        self.assertEqual(serial.writes[:4],['pdm open','pdm set type=2,em=2,sink=1','entry pd','pd pdo'])
        self.assertEqual(serial.writes.count('entry pd'),1)
        # Ready ends initialization early; per-target reply windows remain .1 s.
        self.assertAlmostEqual(clock.now,2+.1+1.3+.1+3*.1)
        self.assertEqual(serial.closes,1)

    def test_empty_entry_reply_times_out_before_pdo_or_voltage_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; clock=Clock()
            serial=Serial(clock,{'entry pd':b''})
            with self.assertRaisesRegex(ProtocolError,'within 2s.*--entry-timeout'):
                self.execute(self.args('--csv',str(path),'--entry-timeout','2'),
                             clock=clock,serial=serial)
            self.assertAlmostEqual(clock.now,4.1)
            self.assertEqual(serial.writes,['pdm open','pdm set type=2,em=2,sink=1','entry pd'])
            self.assertEqual(serial.closes,1)
            self.assertEqual(self.rows(path),[])
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['setup'][-1]['response_hex'],'')
            self.assertEqual(info['sent_requests'],0)

    def test_entry_rejection_stops_wait_early(self):
        clock=Clock(); serial=Serial(clock,{'entry pd':b'error: no source\r\n'})
        with self.assertRaisesRegex(ProtocolError,'Device rejected entry pd'):
            self.execute(self.args('--no-csv'),clock=clock,serial=serial)
        self.assertAlmostEqual(clock.now,2.15)
        self.assertEqual(serial.writes,['pdm open','pdm set type=2,em=2,sink=1','entry pd'])
        self.assertEqual(serial.closes,1)

    def test_entry_interrupt_preserves_partial_reply_without_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; clock=Clock()
            serial=Serial(clock,{'entry pd':[b'rea',KeyboardInterrupt()]})
            result,*_=self.execute(self.args('--csv',str(path)),clock=clock,serial=serial)
            self.assertEqual(result,130)
            self.assertEqual(serial.writes,['pdm open','pdm set type=2,em=2,sink=1','entry pd'])
            self.assertEqual(serial.closes,1)
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['status'],'interrupted')
            self.assertEqual(info['sent_requests'],0)
            self.assertEqual(bytes.fromhex(info['setup'][-1]['response_hex']),b'rea')

    def test_epr_sweep_waits_for_extended_avs_range_after_spr_ready(self):
        clock=Clock()
        class EprSerial(Serial):
            def write(self,data):
                super().write(data)
                if data==b'pd pdo':
                    self.pending=[b'max power 92W\nAVS: 9-20V3.00A,4.60A\n' if self.writes.count('pd pdo')==1
                                  else b'max power 240W\nAVS: 15.00-48.00V 240W\n']
        serial=EprSerial(clock)
        result,*_=self.execute(self.args('--sweep','15:48:1:5','--no-csv'),clock=clock,serial=serial)
        self.assertEqual(result,0)
        self.assertEqual(serial.writes.count('pd pdo'),2)
        self.assertEqual(serial.writes[4],'pd pdo')
        self.assertTrue(serial.writes[5].startswith('pd req='))

    def test_epr_capabilities_timeout_does_not_send_voltage_requests(self):
        clock=Clock();serial=Serial(clock,{'pd pdo':b'max power 92W\nAVS: 9-20V3.00A,4.60A\n'})
        with self.assertRaisesRegex(ProtocolError,'EPR AVS capabilities'):
            self.execute(self.args('--sweep','15:48:1:5','--no-csv','--entry-timeout','.2'),clock=clock,serial=serial)
        self.assertFalse(any(c.startswith('pd req=') for c in serial.writes))
        self.assertEqual(serial.closes,1)

    def test_serial_poll_preserves_com_configuration_and_deadline(self):
        clock=Clock()
        class Handle:
            remaining=65537
            @property
            def timeout(self):
                return 0
            @timeout.setter
            def timeout(self,value):
                raise AssertionError('COM port reconfigured during read')
            @property
            def in_waiting(self):
                return self.remaining if clock.now>=.01 else 0
            def read(self,count):
                self.remaining-=count
                return b'x'*count
        serial=transport.SerialTransport(self.args());serial.handle=Handle()
        with patch.object(transport.time,'monotonic',clock.monotonic),patch.object(transport.time,'sleep',clock.sleep):
            self.assertEqual(serial.read(0),b'')
            self.assertEqual(len(serial.read(.02)),65536)
            self.assertAlmostEqual(clock.now,.01)
            self.assertEqual(serial.read(0),b'x')
            self.assertEqual(serial.read(.02),b'')
            self.assertAlmostEqual(clock.now,.03)
            self.assertEqual(serial.read(-1),b'')
            self.assertAlmostEqual(clock.now,.03)

    def test_binary_pdo_console_response_is_ascii_without_control_bytes(self):
        raw=b'ok\npdo:5\x0b\xff\x00\x81\nready:5100mV,0mA\n'
        text=transport.format_ascii_response(raw)
        self.assertIn('pdo:5\\x0b\\xff\\x00\\x81',text)
        self.assertIn('ready:5100mV,0mA',text)
        text.encode('ascii')
        self.assertNotIn('\x00',text)
        self.assertNotIn('\x0b',text)

    def test_measurement_interface_opens_only_after_ready_and_pdo_query(self):
        clock=Clock();serial=Serial(clock);meter=Meter()
        def open_meter(args):
            self.assertEqual(serial.writes[-1],'pd pdo')
            return meter
        with patch.object(sweep.time,'monotonic',clock.monotonic),patch.object(sweep.time,'sleep',clock.sleep),patch.object(sweep,'SerialTransport',return_value=serial),patch.object(sweep,'Meter',side_effect=open_meter),redirect_stdout(io.StringIO()):
            self.assertEqual(cli.run_sweep(self.args('--continuous-sweep','--no-csv')),0)
        self.assertEqual(meter.reads,3)
        clock=Clock();serial=Serial(clock,{'entry pd':b'error: no source\n'})
        with patch.object(sweep,'Meter',side_effect=AssertionError('measurement opened before readiness')):
            with patch.object(sweep.time,'monotonic',clock.monotonic),patch.object(sweep.time,'sleep',clock.sleep),patch.object(sweep,'SerialTransport',return_value=serial),redirect_stdout(io.StringIO()),self.assertRaises(ProtocolError):
                cli.run_sweep(self.args('--continuous-sweep','--no-csv'))

    def test_pps_pdm_defaults_and_explicit_configuration(self):
        args=cli.build_arg_parser().parse_args(['sweep','--pps-sweep','5:6:1:3','--pdo-index','6'])
        self.assertEqual(sweep.initialization_commands(args),
                         ['pdm open','pdm set type=1,em=1,sink=1','entry pd','pd pdo'])
        args=self.args('--type','0','--em','0','--sink','0')
        self.assertEqual(sweep.initialization_commands(args)[1],'pdm set type=0,em=0,sink=0')
        self.assertEqual(args.pdm_startup_wait,2)

    def test_busy_trigger_restarts_before_configuration_and_request(self):
        clock=Clock()
        class BusySerial(Serial):
            def write(self,data):
                super().write(data)
                if data==b'pdm open':
                    self.pending=[b'pdm busy\n' if self.writes.count('pdm open')==1 else b'pdm mode entry\nver1.0\n']
        serial=BusySerial(clock)
        result,*_=self.execute(self.args('--no-csv'),clock=clock,serial=serial)
        self.assertEqual(result,0)
        self.assertEqual(serial.writes[:6],['pdm open','pdm close','pdm open',
                         'pdm set type=2,em=2,sink=1','entry pd','pd pdo'])
        self.assertTrue(serial.writes[6].startswith('pd req='))
        self.assertEqual(serial.closes,1)

    def test_persistent_busy_or_rejected_config_stops_before_requests(self):
        for replies,expected in [({'pdm open':b'pdm busy\n'},'remained busy'),
            ({'pdm set type=2,em=2,sink=1':b'error: config\n'},'Device rejected')]:
            clock=Clock();serial=Serial(clock,replies)
            with self.subTest(replies=replies),self.assertRaisesRegex(ProtocolError,expected):
                self.execute(self.args('--no-csv'),clock=clock,serial=serial)
            self.assertNotIn('entry pd',serial.writes)
            self.assertFalse(any(c.startswith('pd req=') for c in serial.writes))
            self.assertEqual(serial.closes,1)

    def test_rejection_stops_and_preserves_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; clock=Clock()
            command='pd req=11,volt=16000,cur=5000'
            serial=Serial(clock,{command:b'error: refused\r\n'})
            with self.assertRaises(ProtocolError):
                self.execute(self.args('--csv',str(path),'--no-initialize'),clock=clock,serial=serial)
            self.assertEqual(len(serial.writes),2)
            rows=self.rows(path)
            self.assertEqual(rows[-1]['request_status'],'device_rejected')
            self.assertEqual(bytes.fromhex(rows[-1]['raw_command_response']),b'error: refused\r\n')
            self.assertEqual(serial.closes,1)

    def test_interrupt_during_reply_keeps_partial_bytes_and_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; clock=Clock()
            serial=Serial(clock,{'pd req=11,volt=16000,cur=5000':[b'part',KeyboardInterrupt()]})
            result,*_=self.execute(self.args('--csv',str(path),'--no-initialize'),clock=clock,serial=serial)
            self.assertEqual(result,130)
            self.assertEqual(len(serial.writes),2)
            self.assertEqual(bytes.fromhex(self.rows(path)[-1]['raw_command_response']),b'part')
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual((info['status'],info['sent_requests'],info['completed_points']),('interrupted',2,1))
            self.assertEqual(serial.closes,1)

    def test_write_error_keeps_failed_target_without_claiming_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; clock=Clock()
            serial=Serial(clock,fail_write='pd req=11,volt=15000,cur=5000')
            with self.assertRaises(OSError):
                self.execute(self.args('--csv',str(path),'--no-initialize'),clock=clock,serial=serial)
            row=self.rows(path)[0]
            self.assertEqual(row['request_status'],'send_failed')
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['sent_requests'],0)
            self.assertEqual(serial.closes,1)

    def test_measurement_decode_failure_retains_raw_and_closes_both(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; meter=Meter(raw=frame(ADC,b'bad'))
            clock=Clock(); serial=Serial(clock)
            with self.assertRaises(ProtocolError):
                self.execute(self.args('--csv',str(path),'--no-initialize','--measure'),meter=meter,clock=clock,serial=serial)
            self.assertEqual(bytes.fromhex(self.rows(path)[0]['raw_measure_response']),frame(ADC,b'bad'))
            self.assertEqual((meter.closes,serial.closes),(1,1))

    def test_collisions_and_invalid_append_stop_before_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'; path.write_text('keep',encoding='utf-8')
            with patch.object(sweep,'SerialTransport',side_effect=AssertionError('hardware')):
                with self.assertRaises(FileExistsError):
                    cli.run_sweep(self.args('--csv',str(path)))
                with self.assertRaises(ValueError):
                    cli.run_sweep(self.args('--csv',str(path),'--csv-append'))
                path.unlink()
                Path(str(path)+'.metadata.json').write_text('keep',encoding='utf-8')
                with self.assertRaises(FileExistsError):
                    cli.run_sweep(self.args('--csv',str(path)))
            self.assertEqual(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'),'keep')

    def test_append_header_once_and_distinct_run_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'
            self.execute(self.args('--csv',str(path)))
            initial_meta=Path(str(path)+'.metadata.json').read_text(encoding='utf-8')
            self.execute(self.args('--csv',str(path),'--csv-append'))
            self.assertEqual(len(self.rows(path)),6)
            self.assertEqual(path.read_bytes().count(bytes.fromhex('efbbbf')),1)
            self.assertEqual(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'),initial_meta)
            self.assertEqual(len(list(Path(folder).glob('*.run_*.metadata.json'))),1)

    def test_no_csv_and_no_init_use_only_request_commands(self):
        result,_,serial,_=self.execute(self.args('--no-csv','--no-initialize'))
        self.assertEqual(result,0)
        self.assertTrue(all(c.startswith('pd req=') for c in serial.writes))

    def release_meter(self):
        payload=bytearray(adc_payload())
        struct.pack_into('<i',payload,0,5_100_000)
        struct.pack_into('<i',payload,8,5_100_000)
        struct.pack_into('<i',payload,4,0)
        struct.pack_into('<i',payload,12,0)
        return Meter(frame(ADC,bytes(payload)))

    def test_default_policy_releases_and_dry_run_lists_cleanup(self):
        args=cli.build_arg_parser().parse_args(['sweep','--sweep','15:17:1:5','--pdo-index','11'])
        self.assertFalse(args.keep_trigger)
        args.dry_run=True
        output=io.StringIO()
        with redirect_stdout(output),patch.object(sweep,'SerialTransport',side_effect=AssertionError('hardware')):
            self.assertEqual(cli.run_sweep(args),0)
        self.assertIn('Exit cleanup: reset -> pdm close',output.getvalue())

    def test_completed_sweep_releases_before_close_and_records_low_voltage(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv';meter=self.release_meter()
            result,clock,serial,_=self.execute(self.args('--no-keep-trigger','--continuous-sweep','--csv',str(path)),meter=meter)
            self.assertEqual(result,0)
            self.assertEqual(serial.raw_writes[-2:],[b'reset',b'pdm close'])
            self.assertEqual((serial.closes,meter.closes,meter.reads),(1,1,4))
            self.assertEqual(len(self.rows(path)),3)
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual((info['status'],info['sent_requests'],info['last_requested_voltage_V']),('completed',3,17))
            cleanup=info['cleanup']
            self.assertEqual(cleanup['status'],'acknowledged')
            self.assertTrue(cleanup['voltage_verified'])
            self.assertAlmostEqual(cleanup['actual_voltage_V'],5.1)
            self.assertFalse(cleanup['external_load_controlled'])
            self.assertEqual([c['command'] for c in cleanup['commands']],['reset','pdm close'])

    def test_ctrl_c_during_reply_preserves_partial_data_then_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv';clock=Clock()
            serial=Serial(clock,{'pd req=11,volt=16000,cur=5000':[b'part',KeyboardInterrupt()]})
            result,*_=self.execute(self.args('--no-keep-trigger','--no-initialize','--csv',str(path)),clock=clock,serial=serial)
            self.assertEqual(result,130)
            self.assertEqual(serial.writes[-2:],['reset','pdm close'])
            self.assertEqual(bytes.fromhex(self.rows(path)[-1]['raw_command_response']),b'part')
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual((info['status'],info['sent_requests'],info['completed_points']),('interrupted',2,1))
            self.assertEqual(info['cleanup']['status'],'acknowledged')
            self.assertFalse(info['cleanup']['voltage_verified'])

    def test_interrupt_during_hold_also_releases(self):
        class HoldClock(Clock):
            def sleep(self,seconds):
                raise KeyboardInterrupt()
        clock=HoldClock();serial=Serial(clock)
        result,*_=self.execute(self.args('--no-keep-trigger','--no-initialize','--no-csv','--apdo-voltage-hold','2'),clock=clock,serial=serial)
        self.assertEqual(result,130)
        self.assertEqual(serial.writes,['pd req=11,volt=15000,cur=5000','reset','pdm close'])
        self.assertEqual(serial.closes,1)

    def test_original_error_survives_cleanup_failure_and_close_is_attempted(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv';clock=Clock()
            serial=Serial(clock,{'reset':[OSError('cleanup read failed')]},fail_write='pd req=11,volt=16000,cur=5000')
            warnings=io.StringIO()
            with redirect_stderr(warnings),self.assertRaisesRegex(OSError,'^write failed$'):
                self.execute(self.args('--no-keep-trigger','--no-initialize','--csv',str(path)),clock=clock,serial=serial)
            self.assertEqual(serial.writes[-2:],['reset','pdm close'])
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['error'],'write failed')
            self.assertEqual(info['cleanup']['status'],'failed')
            self.assertEqual(info['cleanup']['commands'][-1]['status'],'acknowledged')
            self.assertIn('cleanup read failed',warnings.getvalue())
            self.assertEqual(serial.closes,1)

    def test_cleanup_negative_empty_and_write_failure_are_not_success(self):
        cases=[({'reset':b'error: busy\n'},None),({'reset':b''},None),({},'reset'),({'pdm close':b'error: close\n'},None)]
        for replies,fail_write in cases:
            with self.subTest(replies=replies,fail_write=fail_write),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'run.csv';clock=Clock();serial=Serial(clock,replies,fail_write=fail_write)
                with redirect_stderr(io.StringIO()),self.assertRaisesRegex(ProtocolError,'Trigger cleanup failed'):
                    self.execute(self.args('--no-keep-trigger','--no-initialize','--csv',str(path)),clock=clock,serial=serial)
                self.assertEqual(serial.writes[-1],'pdm close')
                info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
                self.assertEqual(info['status'],'failed')
                self.assertEqual(info['completed_points'],3)
                self.assertEqual(info['cleanup']['status'],'failed')
                self.assertEqual(len(self.rows(path)),3)
                self.assertEqual(serial.closes,1)

    def test_cleanup_detects_retained_high_voltage_even_with_ok_replies(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv';meter=Meter()
            with redirect_stderr(io.StringIO()),self.assertRaisesRegex(ProtocolError,'Output remains'):
                self.execute(self.args('--no-keep-trigger','--continuous-sweep','--csv',str(path)),meter=meter)
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['cleanup']['status'],'failed')
            self.assertFalse(info['cleanup']['voltage_verified'])
            self.assertAlmostEqual(info['cleanup']['actual_voltage_V'],8.9999)
            self.assertEqual(len(self.rows(path)),3)
            self.assertEqual(meter.closes,1)

    def test_initialization_failure_also_runs_cleanup(self):
        clock=Clock();serial=Serial(clock,{'entry pd':b'error: no source\n'})
        with self.assertRaisesRegex(ProtocolError,'Device rejected entry pd'):
            self.execute(self.args('--no-keep-trigger','--no-csv'),clock=clock,serial=serial)
        self.assertEqual(serial.writes[-2:],['reset','pdm close'])
        self.assertFalse(any(c.startswith('pd req=') for c in serial.writes))

    def test_keep_trigger_skips_cleanup_and_final_adc(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv';meter=Meter()
            _,_,serial,_=self.execute(self.args('--continuous-sweep','--csv',str(path)),meter=meter)
            self.assertEqual(meter.reads,3)
            self.assertNotIn('reset',serial.writes)
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['cleanup']['status'],'skipped')
            self.assertEqual(info['cleanup']['commands'],[])

    def test_cleanup_restores_sigint_handler_after_second_interrupt(self):
        clock=Clock();serial=Serial(clock,{'reset':[b'ok\n',KeyboardInterrupt()]})
        handler=signal.getsignal(signal.SIGINT)
        with patch.object(sweep.time,'monotonic',clock.monotonic),patch.object(sweep.time,'sleep',clock.sleep),redirect_stderr(io.StringIO()):
            result=sweep.release_trigger(serial,self.args('--no-keep-trigger'))
        self.assertEqual(signal.getsignal(signal.SIGINT),handler)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(serial.writes,['reset','pdm close'])
        self.assertEqual(bytes.fromhex(result['commands'][0]['response_hex']),b'ok\n')

    def test_output_policy_requires_explicit_csv(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['sweep','--sweep','15:17:1:5','--pdo-index','11','--csv-overwrite'])

    def test_asd_reference_points_defaults_and_common_columns(self):
        reference=os.environ.get('ASD_PD31_CLI_ROOT')
        if not reference:
            self.skipTest('set ASD_PD31_CLI_ROOT to src/current for reference comparison')
        reference=Path(reference)
        utils=ast.parse((reference/'asd_pd31_modules/utils.py').read_text(encoding='utf-8-sig'))
        functions=[n for n in utils.body if isinstance(n,ast.FunctionDef) and n.name=='frange']
        namespace={'Iterable':list}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<ASD frange>','exec'),namespace)
        actual=sweep.build_plan(self.args('--sweep','15:48:1:5','--round-trip-sweep'))
        expected=list(namespace['frange'](15,48,1))+list(namespace['frange'](47,15,-1))
        self.assertEqual([p.voltage_mv/1000 for p in actual],expected)
        tree=ast.parse((reference/'asd_pd31_modules/csvlog.py').read_text(encoding='utf-8-sig'))
        fields=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CSV_FIELDS' for t in n.targets))
        self.assertTrue(set(sweep.SWEEP_COLUMNS[:20])<=set(fields))
        tree=ast.parse((reference/'asd_pd31_cli.py').read_text(encoding='utf-8-sig'))
        defaults={}
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='add_argument' and node.args and isinstance(node.args[0],ast.Constant):
                name=node.args[0].value
                if name in ('--continuous-settle','--apdo-voltage-hold','--delay','--measure-loop'):
                    defaults[name]=next(ast.literal_eval(k.value) for k in node.keywords if k.arg=='default')
        args=self.args()
        for name,default in defaults.items():
            self.assertEqual(getattr(args,name[2:].replace('-','_')),default)


if __name__=='__main__':
    unittest.main()
