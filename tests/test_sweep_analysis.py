"""Offline KM003C CSV analysis, including signed ADC values and bilingual output."""
import argparse
from contextlib import redirect_stdout
import csv
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

if importlib.util.find_spec('pandas') is not None:
    import analyze_sweep_csv as analyzer
else:
    analyzer = None

import km003c_cli as cli
from km003c_modules import sweep
from test_sweep import Clock, Meter, Serial


def measurement(voltage=15, actual=14.9, *, run='2026-10-09T12:00:00+09:00',
                leg='outbound', sweep_pass=1, mode='avs-continuous', current=-5):
    return dict(timestamp='2026-10-09T12:00:01+09:00', controller_version='0.5.5',
                run_started_at=run, source_name='Test source', cable_name='Test cable',
                mode=mode, sweep_leg=leg, sweep_pass=sweep_pass,
                sweep_direction='up' if leg=='outbound' else 'down', phase_elapsed_s=1,
                target_voltage_v=voltage, target_load_current_a='', request_current_a=5,
                actual_voltage_v=actual, actual_current_a=current,
                actual_power_w=actual*current, pdo_kind='avs', pdo_object_number=11,
                command=f'pd req=11,volt={voltage*1000},cur=5000',
                raw_measure_response='41 00', raw_command_response='4f 4b',
                request_status='sent_unverified', measurement_index=1, error='',
                request_sent_at='2026-10-09T12:00:00+09:00')


@unittest.skipIf(analyzer is None, 'install requirements-analysis.txt for CSV analyzer tests')
class SweepAnalysisTests(unittest.TestCase):
    def write_csv(self, root, rows):
        path=Path(root)/'input.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer=csv.DictWriter(handle, sweep.SWEEP_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def run_analysis(self, *args):
        output=io.StringIO()
        with patch.object(sys, 'argv', ['analyze_sweep_csv.py', *map(str,args)]), redirect_stdout(output):
            result=analyzer.main()
        return result,output.getvalue()

    def test_signed_measurements_missing_load_and_ripple_and_command_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            path=self.write_csv(root,[measurement(actual=14.9),measurement(actual=15.1)])
            frame=analyzer.load_and_normalize(path)
        summary=analyzer.build_summary(frame)
        self.assertEqual(summary['samples'].tolist(),[2])
        self.assertAlmostEqual(summary.iloc[0]['voltage_mean'],15)
        self.assertEqual(summary.iloc[0]['current_mean'],-5)
        self.assertEqual(summary.iloc[0]['power_mean'],-75)
        self.assertNotIn('current_error_a',summary)
        self.assertNotIn('ripple_mean',summary)
        self.assertTrue(summary['target_load_current_a'].isna().all())
        self.assertEqual(frame.iloc[0]['raw_command_response'],'4f 4b')
        self.assertEqual(frame.iloc[0]['controller_version'],'0.5.5')
        boundary=analyzer.detect_ripple_boundary(summary)
        self.assertFalse(boundary['found'])
        args=argparse.Namespace(csv='input.csv',phase='measurement',modes=None,no_plots=True)
        ja=analyzer.build_human_report(frame,frame,summary,boundary,args)
        en=analyzer.build_english_report(frame,frame,summary,boundary,args)
        self.assertIn('KM003C sweep',ja)
        self.assertIn('リップルは未測定',ja)
        self.assertNotIn('リップルの急激な境界は目立ちません',ja)
        self.assertNotIn('*_module_temp.png',ja)
        self.assertIn('KM003C sweep',en)
        self.assertIn('External load current setting: not recorded',en)
        self.assertIn('-75.000W',en)
        self.assertIn('Ripple was not measured',en)

    def test_error_stale_measurements_missing_adc_and_nonfinite_are_excluded(self):
        rows=[measurement()]
        for status in ('device_rejected','send_failed','sent_response_read_failed'):
            rows.append({**measurement(actual=99), 'request_status':status})
        rows += [{**measurement(actual=99),'error':'Ctrl+C'},
                 {**measurement(),'actual_voltage_v':''},
                 {**measurement(),'actual_current_a':'inf'}]
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            frame=analyzer.load_and_normalize(self.write_csv(root,rows))
        self.assertEqual(len(frame),1)
        self.assertEqual(frame.attrs['input_rows'],7)
        self.assertEqual(frame.iloc[0]['actual_voltage_v'],14.9)

    def test_runs_legs_and_request_currents_are_separate_in_summary_and_discard(self):
        rows=[]
        for run in ('run1','run2'):
            for leg,number in (('outbound',1),('return',2)):
                for voltage in (15,16):
                    rows += [measurement(voltage,voltage+.1,run=run,leg=leg,sweep_pass=number),
                             measurement(voltage,voltage+.2,run=run,leg=leg,sweep_pass=number)]
        with tempfile.TemporaryDirectory() as root:
            frame=analyzer.load_and_normalize(self.write_csv(root,rows))
        with redirect_stdout(io.StringIO()):
            remaining=analyzer.discard_first_per_target(frame,1)
        summary=analyzer.build_summary(remaining)
        self.assertEqual(len(remaining),8)
        self.assertEqual(len(summary),8)
        self.assertEqual(summary['samples'].tolist(),[1]*8)
        self.assertEqual(summary['target_voltage_v'].tolist(),[15,16]*4)
        self.assertTrue(summary['voltage_error_v'].between(.199,.201).all())
        self.assertEqual(summary['target_voltage_step_from_prev_v'].isna().sum(),4)
        extra=frame.iloc[:1].copy()
        extra['request_current_a']=3
        combined=analyzer.pd.concat([frame.iloc[:1],extra],ignore_index=True)
        self.assertEqual(len(analyzer.build_summary(combined)),2)

    def test_mode_families_include_noncontinuous_km_sweeps(self):
        with tempfile.TemporaryDirectory() as root:
            frame=analyzer.load_and_normalize(self.write_csv(root,[
                measurement(mode='avs-sweep'),measurement(mode='pps-sweep')]))
        for mode in ('avs','pps'):
            with redirect_stdout(io.StringIO()):
                filtered=analyzer.filter_modes(frame,argparse.Namespace(modes=mode,phase='measurement'))
            self.assertEqual(filtered['mode'].tolist(),[mode+'-sweep'])
        continuous=frame.iloc[:1].copy()
        continuous['mode']='avs-continuous'
        combined=analyzer.pd.concat([frame,continuous],ignore_index=True)
        with redirect_stdout(io.StringIO()):
            selected=analyzer.filter_modes(combined,argparse.Namespace(modes=None,phase='measurement'))
        self.assertEqual(len(selected),3)

    def test_real_sweep_export_is_consumed_without_asd_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'capture.csv';clock=Clock();serial=Serial(clock);meter=Meter()
            args=cli.build_arg_parser().parse_args(['sweep','--sweep','15:17:1:5','--pdo-index','11',
                '--continuous-sweep','--no-initialize','--keep-trigger','--quiet','--csv',str(path)])
            with patch.object(sweep,'SerialTransport',return_value=serial), \
                    patch.object(sweep,'Meter',return_value=meter), \
                    patch.object(sweep.time,'monotonic',clock.monotonic), \
                    patch.object(sweep.time,'sleep',clock.sleep), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.run_sweep(args),0)
            result,output=self.run_analysis(path,'--no-plots','--out',Path(root)/'reports'/'run')
            self.assertEqual(result,0,output)
            summary=analyzer.pd.read_csv(Path(root)/'reports'/'run_summary.csv')
            self.assertEqual(summary['target_voltage_v'].tolist(),[15,16,17])
            self.assertEqual(summary['samples'].tolist(),[1,1,1])
            for suffix in ('_normalized.csv','_human_report.txt','_human_report.en.txt'):
                self.assertTrue((Path(root)/'reports'/('run'+suffix)).is_file())

    def test_english_only_and_no_report_flags(self):
        with tempfile.TemporaryDirectory() as root:
            path=self.write_csv(root,[measurement()]);prefix=Path(root)/'english'
            result,output=self.run_analysis(path,'--no-plots','--lang','en','--out',prefix)
            self.assertEqual(result,0,output)
            self.assertTrue(Path(f'{prefix}_human_report.en.txt').is_file())
            self.assertFalse(Path(f'{prefix}_human_report.txt').exists())
            prefix=Path(root)/'none'
            result,output=self.run_analysis(path,'--no-plots','--no-report','--out',prefix)
            self.assertEqual(result,0,output)
            self.assertEqual(list(Path(root).glob('none*_report*')),[])

    def test_unmeasured_csv_fails_with_actionable_message_without_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            path=self.write_csv(root,[{**measurement(),'actual_voltage_v':'',
                                      'actual_current_a':'','actual_power_w':''}])
            prefix=Path(root)/'out'
            result,output=self.run_analysis(path,'--no-plots','--out',prefix)
            self.assertEqual(result,1)
            self.assertIn('--continuous-sweep',output)
            self.assertEqual(list(Path(root).glob('out*')),[])

    @unittest.skipIf(analyzer is None or analyzer.plt is None,'matplotlib is optional')
    def test_png_outputs_and_separate_run_leg_lines(self):
        rows=[measurement(15,15,run='run1'),measurement(16,16,run='run1'),
              measurement(15,14.9,run='run1',leg='return',sweep_pass=2),
              measurement(15,14.8,run='run2')]
        with tempfile.TemporaryDirectory() as root:
            path=self.write_csv(root,rows);prefix=Path(root)/'plots'
            result,output=self.run_analysis(path,'--out',prefix)
            self.assertEqual(result,0,output)
            self.assertEqual(len(list(Path(root).glob('plots*.png'))),5)
            self.assertFalse(Path(f'{prefix}_ripple.png').exists())
            frame=analyzer.load_and_normalize(path);summary=analyzer.build_summary(frame)
            counts=[]
            with patch.object(analyzer.plt,'savefig',side_effect=lambda *a,**k: counts.append(len(analyzer.plt.gca().lines))):
                analyzer.save_sweep_plot(summary,'voltage_mean','V','V','Test',Path(root)/'line.png')
            self.assertEqual(counts,[3])


if __name__=='__main__':
    unittest.main()
