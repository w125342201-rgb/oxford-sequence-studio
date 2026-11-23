import json
import copy
import csv
import tempfile
import unittest
from unittest.mock import Mock, patch
from studio.backend import SimBackend
from studio.engine import Engine
from studio.gui import format_environment_value
from studio.model import CONFIG, node

class DisplayAndElectrodeTests(unittest.TestCase):
    def test_display_precision(self):
        for key in ('temperature','vti','field'):
            self.assertEqual(format_environment_value(key,1.234567),'1.2346')
        self.assertEqual(format_environment_value('angle',12.345),'12.3')
        self.assertEqual(format_environment_value('temperature',4),'4.0000')
        self.assertEqual(format_environment_value('angle',None),'—')

    def test_probe_only_continuous_stability_even_with_warmer_vti(self):
        b=Mock();b.simulated=False
        e=Engine(b,CONFIG);e.wait=Mock()
        readings=iter([(0,4.),(1,4.),(2,4.1),(3,4.),(4,4.),(5,4.)])
        clock=[0.]
        def poll(**kwargs):
            clock[0],temp=next(readings)
            return dict(temperature=temp,vti=6.)
        e.poll=Mock(side_effect=poll)
        p=node('temperature',target=4.,tolerance=.02,stable_s=2.)['params']
        with patch('studio.engine.time.monotonic',side_effect=lambda:clock[0]):
            e.environment('temperature',4.,p)
        self.assertEqual(clock[0],5.)
        self.assertEqual(e.poll.call_count,6)

    def run_routes(self,fail_at=None):
        c=dict(copy.deepcopy(CONFIG),enable_matrix=True,matrix_each_point=True,matrix_off_delay_s=0.,matrix_settle_s=0.)
        class Backend(SimBackend):
            def __init__(self):super().__init__(c,lambda _:None);self.calls=[];self.count=0
            def route(self,p):
                assert not self.output
                self.calls.append((self.env['temperature'],p['name']))
                super().route(p)
            def measure(self,*args):
                self.count+=1
                if self.count==fail_at:raise OSError('test acquisition failure')
                return dict(current_a=1e-5,voltage_v=self.count*1e-6,resistance_ohm=self.count*.1,raw=str(self.count))
        b=Backend();e=Engine(b,c)
        sweep=node('sweep',axis='temperature',values=[4.,5.],stable_s=0.)
        sweep['children']=[node('resistance')]
        with tempfile.TemporaryDirectory() as tmp:
            result=e.run([sweep],tmp)
            raw=[item['data'] for item in map(json.loads,(e.run_dir/'events.jsonl').read_text().splitlines()) if item['event']=='point']
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:wide=list(csv.DictReader(f))
        return c,b,result,raw,wide

    def test_every_setpoint_visits_all_routes_with_distinct_columns(self):
        c,b,result,raw,wide=self.run_routes()
        self.assertEqual(result['status'],'completed')
        n=len(c['point_routes'])
        self.assertEqual(b.calls,[(t,r['name']) for t in (4.,5.) for r in c['point_routes']])
        self.assertEqual(len(raw),2*n);self.assertEqual(len(wide),2)
        for j,row in enumerate(wide):
            self.assertEqual(float(row['temperature_setpoint_k']),4.+j)
            self.assertTrue({'vti_k','pressure_mbar'} <= row.keys());self.assertNotIn('mode',row)
            for i in range(1,n+1):
                self.assertEqual(float(row['V_'+'-'.join(map(str,c['point_routes'][i-1]['columns']))]),raw[j*n+i-1]['voltage_v'])
                self.assertEqual(float(row['temperature_k']),4.+j)
        self.assertFalse(b.output)

    def test_failed_route_keeps_partial_row_with_blank_missing_voltage(self):
        c,b,result,raw,wide=self.run_routes(fail_at=2)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(raw),1);self.assertEqual(len(wide),1)
        self.assertEqual(float(wide[0]['V_'+'-'.join(map(str,c['point_routes'][0]['columns']))]),raw[0]['voltage_v'])
        self.assertEqual(wide[0]['V_'+'-'.join(map(str,c['point_routes'][1]['columns']))],'')
        self.assertFalse(b.output)

    def test_pxi_channels_have_same_simple_layout(self):
        c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
        class Backend(SimBackend):
            def measure(self,*args):
                return dict(current_a=1e-5,voltage_v=11e-6,channels={
                    ch:dict(v_peak_v=(11 if ch==0 else 22)*1e-6,v2_peak_v=(ch+1)*1e-7,x_v=-1e-6,y_v=2e-6,x2_v=-3e-7,y2_v=4e-7) for ch in range(4)},raw='test')
        e=Engine(Backend(c,lambda _:None),c)
        with tempfile.TemporaryDirectory() as tmp:
            result=e.run([node('resistance',mode='pxi',daq_channels=[0,2],samples=2,interval_s=0.)],tmp)
            self.assertEqual(result['status'],'completed')
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:
                reader=csv.DictReader(f);rows=list(reader)
                self.assertEqual(reader.fieldnames,['point','time_utc','temperature_setpoint_k','temperature_k','field_t','angle_deg','current_a','route','columns','path','node_id','measurement_mode','measurement_status','environment_status','environment_error','acquisition_start_utc','acquisition_end_utc','vti_k','pressure_mbar','environment_read_utc','environment_timing','measurement_error','logical_point','route_index','acquisition_index','angle_read_time_unix','environment_overrides']+[f'ai{ch}_{suffix}' for ch in range(4) for suffix in ('V','X_V','Y_V','2f_V','2f_X_V','2f_Y_V')])
            self.assertEqual(len(rows),2)
            self.assertEqual(float(rows[0]['ai0_V']),11e-6)
            self.assertEqual(float(rows[0]['ai2_V']),22e-6)
            self.assertEqual(len(list(e.run_dir.glob('*.csv'))),1)
