import json
import unittest
from pathlib import Path
from km003c_modules import analysis_quality as Q

def request(i,t,mid=0):return dict(row_index=i,sno=i+1,t=t,message='EPR_REQUEST',role='SNK',message_id=mid,objects=[123,456])
def response(i,t,message,role='SRC',mid=0):return dict(row_index=i,sno=i+1,t=t,message=message,role=role,message_id=mid,objects=[])

class QualityTests(unittest.TestCase):
    def test_duplicates_crc_reset(self):
        pd=[request(0,1000000,2),response(1,1001000,'GOODCRC',mid=2),request(2,1002000,3),response(3,1003000,'GOODCRC',mid=3),response(4,1004000,'ACCEPT'),response(5,1009000,'SOFT_RESET','SNK')]
        d=Q.diagnostics(pd)[1]
        self.assertEqual((d['duplicate_classification'],d['previous_goodcrc_sno'],d['goodcrc_sno'],d['protocol_status'],d['reset_sender'],d['cause_classification']),('new_id_same_rdo',2,4,'failed','SNK','indeterminate'))
        pd[2]['message_id']=2;self.assertEqual(Q.diagnostics(pd)[1]['duplicate_classification'],'same_id_retry_or_duplicate')
    def test_missing_vs_failed(self):
        self.assertEqual(Q.diagnostics([request(0,1000000)])[0]['protocol_status'],'unconfirmed')
        self.assertEqual(Q.diagnostics([request(0,1000000),response(1,1001000,'REJECT')])[0]['protocol_status'],'failed')
    def test_stable_voltage_drop(self):
        scope=[dict(t=1000000+i*48000,v=28.4+i%2*.001,a=-5) for i in range(30)]
        d=Q.diagnostics([request(0,900000),response(1,901000,'ACCEPT'),response(2,902000,'PS_RDY')])[0]
        p=Q.qualify(dict(target_voltage_v=29),d,scope)
        self.assertTrue(p['point_valid']);self.assertLess(p['steady_voltage_mean'],28.5)
        self.assertGreater(p['settling_upper_ms'],p['settling_lower_ms']);self.assertEqual(Q.resolution(scope)['scope_interval_median_us'],48000)
    def test_recorded_lenovo29(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/lenovo29-recorded.json').read_text(encoding='utf-8'));points=fixture['points']
        self.assertEqual(points[0]['pd_status'],'unconfirmed');self.assertIn('ended_by_soft_reset',points[0]['transition_flags']);self.assertGreater(points[1]['voltage_mean']-points[0]['voltage_mean'],.9)
        for p in points:Q.qualify(p,None,[])
        rows=Q.pairs(points);self.assertIsNone(rows[0]['hysteresis_mV']);self.assertEqual(Q.counts(rows),dict(total=1,valid=0,excluded=1))
    def test_regulation(self):
        a=dict(point_valid=True,target_voltage_v=29,steady_voltage_mean=29,steady_current_mean=-1,raw_row_number=1);b=dict(a,steady_voltage_mean=28.8,steady_current_mean=-3,raw_row_number=2)
        self.assertAlmostEqual(Q.regulation([a,b])[0]['equivalent_output_resistance_ohm'],.1)
        self.assertEqual(Q.regulation([a,dict(b,target_voltage_v=30)]),[])
    def test_load_plateaus_and_message_id_conflict(self):
        d=Q.diagnostics([request(0,900000),response(1,901000,'ACCEPT'),response(2,902000,'PS_RDY')])
        samples=[dict(t=1000000+i*48000,v=29 if i<12 else 28.8,a=-1 if i<12 else -3) for i in range(24)]
        rows=Q.load_windows([dict(target_voltage_v=29,request_start_us=900000,raw_row_number=1)],samples,d)
        self.assertEqual(len(rows),1);self.assertAlmostEqual(rows[0]['equivalent_output_resistance_ohm'],.1)
        pd=[request(0,1000000,2),dict(request(1,1002000,2),objects=[789])]
        self.assertEqual(Q.diagnostics(pd)[1]['cause_classification'],'SNK')
