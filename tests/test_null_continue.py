import copy
import csv
import json
import tempfile
import unittest
from unittest.mock import Mock
from studio.backend import SimBackend, HardwareBackend, MeasurementError
from studio.model import CONFIG, node
from studio.engine import Engine, Stopped

class NullContinueTests(unittest.TestCase):
    def run_bad_good(self, explicit=False, failure=None):
        c=dict(copy.deepcopy(CONFIG),enable_matrix=True,matrix_each_point=not explicit,matrix_off_delay_s=0.,matrix_settle_s=0.)
        class Backend(SimBackend):
            def measure(self,k,p,bias,wait):
                if self.route_name=='A':
                    if failure:raise failure
                    return dict(voltage_v=9.9e37,raw='9.9E37')
                return super().measure(k,p,bias,wait)
        b=Backend(c,lambda _:None);e=Engine(b,c)
        sweep=node('sweep',axis='temperature',values=[4.,5.],stable_s=0.)
        if explicit:
            child=node('channels',routes=c['point_routes']);child['children']=[node('resistance',settle_s=0.)]
        else:child=node('resistance',settle_s=0.)
        sweep['children']=[child]
        with tempfile.TemporaryDirectory() as tmp:
            result=e.run([sweep],tmp)
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            events=[json.loads(x) for x in (e.run_dir/'events.jsonl').read_text().splitlines()]
        self.assertEqual(result['status'],'completed');self.assertEqual(result['invalid_points'],2)
        self.assertEqual(result['points'],4);self.assertFalse(b.output)
        points=[x['data'] for x in events if x['event']=='point']
        self.assertEqual([x['route'] for x in points],['A','B','A','B'])
        self.assertIsNone(points[0]['voltage_v']);self.assertIsNone(points[0]['resistance_ohm'])
        if explicit:
            self.assertEqual([r['voltage_v'] for r in rows][::2],['NULL','NULL'])
            self.assertTrue(all(float(r['voltage_v']) for r in rows[1::2]))
        else:
            self.assertEqual([r['V_12-9-11-10'] for r in rows],['NULL','NULL'])
            self.assertTrue(all(float(r['V_8-5-7-6']) for r in rows))
    def test_overload_does_not_stop_good_routes_or_next_temperature(self):self.run_bad_good()
    def test_explicit_channel_loop(self):self.run_bad_good(explicit=True)
    def test_acquisition_error(self):self.run_bad_good(failure=MeasurementError('Delta 采集超时'))
    def test_backend_empty_and_text_readings_and_timeout(self):
        b=HardwareBackend(CONFIG,lambda _:None);b.src=Mock();p=node('resistance')['params']
        for r in [dict(readings=[]),dict(readings=['OVLD']),dict(readings=[None]),{}]:
            b.src.run_delta_once.return_value=r
            with self.assertRaises(MeasurementError):b.measure('resistance',p,1e-5,lambda _:None)
        b.src.run_delta_once.side_effect=TimeoutError('Delta sweep expired')
        with self.assertRaises(MeasurementError):b.measure('resistance',p,1e-5,lambda _:None)
    def test_off_failure_and_stop_not_swallowed(self):
        c=copy.deepcopy(CONFIG);b=SimBackend(c,lambda _:None);e=Engine(b,c)
        b.measure=Mock(return_value=dict(voltage_v=float('inf')))
        b.off=Mock(side_effect=OSError('off failed'))
        with self.assertRaises(OSError):e.acquire_delta(node('resistance'),1e-5)
        self.assertEqual(e.invalid_points,0)
        b.measure.side_effect=Stopped('user stop')
        with self.assertRaises(Stopped):e.acquire_delta(node('resistance'),1e-5)
