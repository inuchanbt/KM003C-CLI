"""Offline sweep tests: ASD options, endpoint order and partial command evidence."""
import argparse
import ast
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import km003c_cli as cli
from km003c_modules import sweep
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
            '--wait', '.1', '--quiet', *extra])

    def execute(self, args, *, serial=None, meter=None, clock=None):
        clock = clock or Clock()
        serial = serial or Serial(clock)
        meter = meter or Meter()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sweep.time, 'monotonic', clock.monotonic))
            stack.enter_context(patch.object(sweep.time, 'sleep', clock.sleep))
            stack.enter_context(patch.object(sweep, 'SerialTransport', return_value=serial))
            stack.enter_context(patch.object(sweep, 'Meter', return_value=meter))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = cli.run_sweep(args)
        return result, clock, serial, meter

    def rows(self, path):
        with Path(path).open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

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

    def test_single_serial_session_and_unverified_request_log(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run.csv'
            result,clock,serial,meter=self.execute(self.args('--csv',str(path),'--round-trip-sweep'))
            self.assertEqual(result,0)
            self.assertEqual((serial.opens,serial.closes),(1,1))
            self.assertEqual(serial.writes[:3],['pdm open','entry pd','pd pdo'])
            self.assertEqual(len(serial.writes),8)
            rows=self.rows(path)
            self.assertEqual([r['target_voltage_v'] for r in rows],['15.0','16.0','17.0','16.0','15.0'])
            self.assertTrue(all(r['actual_voltage_v']==r['target_load_current_a']=='' for r in rows))
            self.assertTrue(all(r['request_status']=='sent_unverified' for r in rows))
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual((info['planned_points'],info['sent_requests'],info['completed_points']),(5,5,5))
            self.assertEqual(len(info['setup']),3)

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
            self.assertEqual(serial.writes,['pdm open','entry pd'])
            self.assertEqual(serial.closes,1)
            self.assertEqual(self.rows(path),[])
            info=json.loads(Path(str(path)+'.metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(info['status'],'failed')
            self.assertEqual(info['sent_requests'],0)

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
