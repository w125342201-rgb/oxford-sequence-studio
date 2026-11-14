"""Regression probes for the reviewed 1.7.3 findings; no live hardware."""
import copy,csv,json,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
from studio.model import CONFIG,node,span,validate,workload,example
from studio.backend import SimBackend,MeasurementError
from studio.engine import Engine,Stopped
from studio.gui import enable_mousewheel
from studio.instrument_options import visible_fields
from audit.recover_points import recover
from test_pxi_ranges import daq
from test_reliability_173 import Wire,drv

class Audit174(unittest.TestCase):
    def config(self,**kw):return dict(copy.deepcopy(CONFIG),**kw)
    def rows(self,e):
        with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
    def test_grid_rejects_each_invalid_input(self):
        for values in ((0,.01,.0025,'field'),(0,2,.25,'temperature'),(0,1.05,.1,'temperature'),(1.34,1.6,.1,'temperature')):
            with self.subTest(values=values),self.assertRaises(ValueError):span(*values)
    def test_grid_preserves_interval_direction_and_endpoint_policy(self):
        self.assertEqual(span(0,.3,.07,'field'),[0.,.07,.14,.21,.28,.3])
        self.assertEqual(span(0,.3,.07,'field',False),[0.,.07,.14,.21,.28])
        self.assertEqual(span(.3,0,.07,'field',False),[.3,.23,.16,.09,.02])
        self.assertEqual(span(1,1,.1,'temperature'),[1.])
        for start,stop in ((0,.3),(.3,0)):
            for include in (True,False):
                pts=span(start,stop,.07,'field',include)
                self.assertTrue(all(min(start,stop)<=v<=max(start,stop) for v in pts))
    def field_messages(self,action):
        c=self.config();b=SimBackend(c,lambda _:None);b.set_environment=Mock()
        b.snapshot=lambda:dict(b.env,field=0,magnet_action=action)
        messages=[];e=Engine(b,c,lambda k,v:messages.append(str(v)) if k in ('log','state') else None)
        e.wait=Mock(side_effect=Stopped())
        with self.assertRaises(Stopped):e.environment('field',1,node('field',timeout_s=1e-12)['params'])
        return '\n'.join(messages)
    def test_field_ramping_is_not_front_panel_fault(self):
        text=self.field_messages('RTOS')
        self.assertIn('励磁进行中',text);self.assertIn('估算剩余',text)
        self.assertNotIn('请在励磁电源前面板',text);self.assertNotIn('等待励磁电源前面板恢复',text)
    def test_field_paused_hold_and_unknown_differ(self):
        self.assertIn('暂停态',self.field_messages('PAUS'))
        self.assertIn('保持/钳位态',self.field_messages('HOLD'))
        self.assertIn('状态未知',self.field_messages('UNKNOWN'))
        self.assertIn('正在回零',self.field_messages('RTOZ'))
    def test_route_environments_and_states_remain_independent(self):
        c=self.config(enable_matrix=True,matrix_each_point=True,matrix_off_delay_s=0,matrix_settle_s=0)
        class Backend(SimBackend):
            def measure(self,*args):
                self.env['temperature']+=.01;self.env['vti']+=.02
                if self.route_name==c['point_routes'][0]['name']:raise MeasurementError('bad contact')
                return super().measure(*args)
        e=Engine(Backend(c,lambda _:None),c)
        with tempfile.TemporaryDirectory() as directory:
            r=e.run([node('resistance',samples=2,settle_s=0)],directory);rows=self.rows(e)
            self.assertEqual((r['status'],r['points'],len(rows)),('completed',4,2))
            first=rows[0];self.assertEqual(first['measurement_status'],'partial_invalid')
            self.assertEqual(first['route1_measurement_status'],'invalid');self.assertEqual(first['route2_measurement_status'],'valid')
            self.assertAlmostEqual(float(first['route2_temperature_k'])-float(first['route1_temperature_k']),.01)
            self.assertEqual(first['temperature_k'],first['route1_temperature_k'])
            self.assertEqual(first['environment_timing'],'first_route_after_point')
            self.assertEqual(first['acquisition_end_utc'],first['route2_acquisition_end_utc'])
            self.assertTrue(first['route1_environment_read_utc']);self.assertTrue(first['route2_environment_read_utc'])
            self.assertIn('route1_vti_k',first);self.assertIn('route2_pressure_mbar',first)
            for i,(key,meta) in enumerate(r['voltage_columns'].items(),1):
                self.assertEqual(meta['detail_prefix'],f'route{i}_')
            restored=Path(directory)/'restored.csv';self.assertEqual(recover(e.run_dir/'events.jsonl',restored),4)
            with restored.open(encoding='utf-8-sig') as f:long=list(csv.DictReader(f))
            self.assertEqual(long[1]['temperature_k'],first['route2_temperature_k'])
            self.assertEqual(long[1]['vti_k'],first['route2_vti_k']);self.assertTrue(long[1]['pressure_mbar'])
    def test_stop_keeps_voltage_without_claiming_environment_failure(self):
        c=self.config();b=SimBackend(c,lambda _:None);e=Engine(b,c);original=b.measure
        def measure(*args):r=original(*args);e.stop.set();return r
        b.measure=measure
        with tempfile.TemporaryDirectory() as directory:
            r=e.run([node('resistance',samples=2,settle_s=0)],directory)
            row=self.rows(e)[0];self.assertEqual(r['status'],'stopped')
            self.assertEqual(row['environment_status'],'stopped');self.assertEqual(row['temperature_k'],'')
            self.assertTrue(float(row['voltage_v']));self.assertEqual(row['environment_read_utc'],'')
    def test_unfinished_electrode_table_preserves_blank_pending_and_incomplete(self):
        c=self.config(enable_matrix=True,matrix_each_point=True,matrix_off_delay_s=0,matrix_settle_s=0)
        b=SimBackend(c,lambda _:None);e=Engine(b,c);original=b.measure
        def measure(*args):r=original(*args);e.stop.set();return r
        b.measure=measure
        with tempfile.TemporaryDirectory() as directory:
            r=e.run([node('resistance',settle_s=0)],directory);row=self.rows(e)[0]
            self.assertEqual(r['status'],'stopped');self.assertEqual(row['measurement_status'],'incomplete')
            self.assertEqual(row['route1_environment_status'],'stopped');self.assertEqual(row['route2_measurement_status'],'')
    def test_workload_separates_operations_acquisitions_and_rows(self):
        c=self.config();self.assertEqual(validate(example(),c),33)
        self.assertEqual(workload(example(),c),dict(operations=33,acquisitions=15,csv_rows=15))
        a=node('repeat',count=300);b=node('repeat',count=300);b['children']=[node('resistance')];a['children']=[b]
        self.assertEqual(validate([a],c),90000)
        n=node('wait',seconds=1);self.assertEqual(workload([n],c),dict(operations=1,acquisitions=0,csv_rows=0))
    def test_workload_matches_execution_in_matrix_mode(self):
        c=self.config(enable_matrix=True,matrix_each_point=True,matrix_off_delay_s=0,matrix_settle_s=0)
        n=node('repeat',count=2);n['children']=[node('resistance',samples=3,settle_s=0)]
        b=SimBackend(c,lambda _:None);e=Engine(b,c)
        with tempfile.TemporaryDirectory() as directory:
            r=e.run([n],directory);w=workload([n],c)
            self.assertEqual(w['acquisitions'],r['points']);self.assertEqual(w['csv_rows'],len(self.rows(e)))
            self.assertEqual(w['operations'],24);self.assertEqual(r['planned_workload'],w)
    def test_no_measurement_sequence_counts_and_disabled_container_is_zero(self):
        c=self.config();n=node('repeat',count=2);n['children']=[node('wait',seconds=0)]
        self.assertEqual(validate([n],c),2);self.assertEqual(workload([n],c)['acquisitions'],0)
        n['enabled']=False;self.assertEqual(workload([n],c)['operations'],0)
    def test_route_conflict_is_rejected_but_disabled_is_allowed(self):
        c=self.config(enable_matrix=True,matrix_each_point=True)
        for kind in ('route','channels'):
            n=node(kind)
            if kind=='channels':n['children']=[node('resistance')]
            with self.assertRaisesRegex(ValueError,'显式电极'):validate([n,node('resistance')],c)
            n['enabled']=False;validate([n,node('resistance')],c)
    def test_resume_retries_one_transport_timeout_and_stop_still_wins(self):
        c=self.config();b=SimBackend(c,lambda _:None);e=Engine(b,c);e.pause.set()
        b.off=lambda:e.pause.clear();e.stop.wait=Mock()
        e.measurement_restart=Mock(side_effect=[TimeoutError('once'),None]);e.gate()
        self.assertEqual(e.measurement_restart.call_count,2);self.assertEqual(e.state,'running')
        e.pause.set();e.measurement_restart=Mock(side_effect=TimeoutError('again'))
        e.stop.wait=lambda _:e.stop.set()
        with self.assertRaises(Stopped):e.gate()
    def test_hardware_polling_reuses_sample_within_one_second(self):
        c=self.config();b=SimBackend(c,lambda _:None);b.simulated=False;b.snapshot=Mock(wraps=b.snapshot)
        e=Engine(b,c);clock=[0.]
        with patch('studio.engine.time.monotonic',side_effect=lambda:clock[0]):
            e.poll();clock[0]=.5;e.poll();self.assertEqual(b.snapshot.call_count,1)
            clock[0]=1;e.poll();self.assertEqual(b.snapshot.call_count,2)
            e.poll(force=True);self.assertEqual(b.snapshot.call_count,3)
    def test_simulation_rng_advances_and_can_be_reproduced(self):
        d=daq(.316);d.simulate=True;d.noise_sim_v=1e-5
        a=d.acquire_one();b=d.acquire_one();self.assertNotEqual(a,b)
        rng=np.random.default_rng(42);a=d.acquire_one(rng=rng);b=d.acquire_one(rng=rng)
        rng=np.random.default_rng(42);self.assertEqual(a,d.acquire_one(rng=rng));self.assertEqual(b,d.acquire_one(rng=rng))
    def test_source_compliance_still_stops_whole_run_by_user_choice(self):
        class Backend(SimBackend):
            def measure(self,*a):
                s=Wire();s.measurement=8;s.source_health()
        c=self.config();e=Engine(Backend(c,lambda _:None),c)
        with tempfile.TemporaryDirectory() as directory:
            result=e.run([node('resistance',samples=2)],directory)
            self.assertEqual(result['status'],'failed');self.assertEqual(result['points'],0)
            self.assertIn('Keithley6221Error',result['error'])
    def test_pxi_hides_unused_delta_timeout(self):
        self.assertNotIn('timeout_s',visible_fields(node('resistance',mode='pxi')['params']))
        self.assertIn('timeout_s',visible_fields(node('resistance')['params']))
    def test_mousewheel_has_single_root_handler_and_destroy_cleanup(self):
        class Root:
            def __init__(self):self.bindings={}
            def bind_all(self,key,fn,add):self.bindings.setdefault(key,[]).append(fn)
        class Widget:
            def __init__(self,master=None):self.master=master
        class Canvas(Widget):
            def __init__(self,root):super().__init__(root);self.bindings={};self.moves=[]
            def _root(self):return self.master
            def bind(self,key,fn,add):self.bindings[key]=fn
            def yview_scroll(self,*args):self.moves.append(args)
        root=Root()
        for _ in range(30):
            canvas=Canvas(root);region=Widget(canvas);child=Widget(region)
            enable_mousewheel(canvas,region);enable_mousewheel(canvas,region)
            self.assertEqual(len(root._scroll_panels),1)
            callback=root.bindings['<MouseWheel>'][0]
            self.assertEqual(callback(SimpleNamespace(widget=child,num=None,delta=-120)),'break')
            self.assertEqual(canvas.moves,[(1,'units')])
            canvas.bindings['<Destroy>'](SimpleNamespace(widget=canvas))
            self.assertEqual(len(root._scroll_panels),0)
        self.assertEqual({k:len(v) for k,v in root.bindings.items()},dict.fromkeys(('<MouseWheel>','<Button-4>','<Button-5>'),1))
