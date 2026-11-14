import copy
import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from studio.model import CONFIG,node,validate,example,span
from studio.backend import SimBackend,HardwareBackend
from studio.engine import Engine

class Recorder(SimBackend):
    def __init__(self,c,log=lambda m:None):super().__init__(c,log);self.calls=[]
    def off(self):self.calls.append('off');super().off()
    def hold(self):self.calls.append('hold')
    def route(self,p):
        if self.output:raise AssertionError('route invoked with output on')
        self.calls.append(('route',p['columns']));super().route(p)
    def begin(self,k,p):self.calls.append(('begin',k));super().begin(k,p)

class EngineTests(unittest.TestCase):
    def setUp(self):self.c=dict(copy.deepcopy(CONFIG),matrix_off_delay_s=0.,matrix_settle_s=0.);self.tmp=tempfile.TemporaryDirectory()
    def tearDown(self):self.tmp.cleanup()
    def engine(self,b=None,emit=lambda *x:None):return Engine(b or Recorder(self.c),self.c,emit)
    def test_nested_order_and_csv(self):
        self.c['matrix_each_point']=True
        self.c['enable_matrix']=True
        b=Recorder(self.c);e=self.engine(b);d=e.run(example(),self.tmp.name)
        self.assertEqual(d['status'],'completed');self.assertEqual(d['points'],30);self.assertFalse(b.output)
        rows=[item['data'] for item in map(json.loads,(e.run_dir/'events.jsonl').read_text().splitlines()) if item['event']=='point']
        self.assertEqual(len(rows),30);self.assertEqual(rows[0]['columns'],'[12, 9, 11, 10]')
        self.assertEqual(rows[1]['columns'],'[8, 5, 7, 6]')
        self.assertEqual([float(r['current_a']) for r in rows[1:4]],[0.1]*3)
        self.assertEqual(rows[0]['mode'],'SIMULATED');self.assertEqual(rows[0]['delta_divisor'],1.0)
        self.assertEqual(json.loads((e.run_dir/'run.json').read_text())['status'],'completed')
    def test_all_measurements(self):
        for mode in ['delta','pxi']:
            self.c['measurement_device']='2182a' if mode=='delta' else 'pxi'
            e=self.engine();r=e.run([node('resistance',mode=mode)],self.tmp.name)
            self.assertEqual(r['status'],'completed');self.assertEqual(r['points'],1)
    def test_stopped_wait(self):
        b=Recorder(self.c);e=self.engine(b)
        timer=threading.Timer(.05,e.stop.set);timer.start()
        r=e.run([node('wait',seconds=5)],self.tmp.name);timer.join()
        self.assertEqual(r['status'],'stopped');self.assertIn('hold',b.calls);self.assertFalse(b.output)
    def test_pause_boundary_closes_and_restarts_source(self):
        b=Recorder(self.c);e=None;paused=threading.Event()
        def emit(k,v):
            if k=='point' and v['point']==1:e.pause.set()
            if k=='state' and str(v).startswith('已暂停'):paused.set()
        e=self.engine(b,emit)
        out={}
        t=threading.Thread(target=lambda:out.update(e.run([node('resistance',samples=3)],self.tmp.name)));t.start()
        self.assertTrue(paused.wait(2));self.assertFalse(b.output)
        e.pause.clear();t.join(3);self.assertFalse(t.is_alive());self.assertEqual(out['status'],'completed')
        self.assertEqual(b.calls.count(('begin','resistance')),2)
    def test_failed_acquisition_aborts(self):
        class Bad(Recorder):
            def measure(self,*a):raise OSError('GPIB disconnected')
        b=Bad(self.c);e=self.engine(b);r=e.run([node('resistance',samples=3),node('route')],self.tmp.name)
        self.assertEqual(r['status'],'failed');self.assertIn('hold',b.calls)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='route' for x in b.calls))
        self.assertFalse(b.output)
    def test_nan_is_raw_error_not_valid_data(self):
        class Bad(Recorder):
            def measure(self,*a):
                self.serial+=1
                if self.serial==1:return {'voltage_v':float('nan'),'raw':'NAN'}
                return super().measure(*a)
        e=self.engine(Bad(self.c));e.wait=lambda _:e.check();r=e.run([node('resistance',samples=3)],self.tmp.name)
        self.assertEqual(r['status'],'completed');self.assertEqual(r['points'],3)
        self.assertEqual(r['invalid_points'],1)
        self.assertIn('invalid_raw',(e.run_dir/'events.jsonl').read_text())
    def test_wait_timeout(self):
        class Stuck(Recorder):
            def set_environment(self,*a):pass
        e=self.engine(Stuck(self.c));timer=threading.Timer(.15,e.stop.set);timer.start()
        r=e.run([node('temperature',target=8,stable_s=0,timeout_s=.04)],self.tmp.name);timer.join()
        self.assertEqual(r['status'],'stopped');self.assertIn('用户请求停止',r['error'])
    def test_cleanup_failure_is_not_success(self):
        class Bad(Recorder):
            def off(self):raise OSError('OFF failed')
        e=self.engine(Bad(self.c));r=e.run([node('wait',seconds=0)],self.tmp.name)
        self.assertEqual(r['status'],'cleanup_failed');self.assertTrue(r['cleanup_errors'])
    def test_relay_fail_closed(self):
        class Source:
            def __init__(self):self.calls=[]
            def write(self,x):self.calls.append(x)
            def abort(self):pass
            def output_off(self):raise OSError('write failed')
            def set_current(self,x):pass
            def output_state(self):return True
        class Matrix:
            called=False
            def open_all(self):self.called=True
        b=HardwareBackend(self.c,lambda m:None);b.src=Source();b.matrix=Matrix()
        with self.assertRaises(RuntimeError):b.route({'name':'a','columns':[1,2,3,4]})
        self.assertFalse(b.matrix.called)
    def test_disabled_instrument_node_needs_no_connection(self):
        n=node('angle');n['enabled']=False
        validate([n,node('wait',seconds=0)],self.c,hardware=True)
    def test_reject_invalid_plans(self):
        variants=[node('route',columns=[1,1,2,3]),node('resistance',current_a=float('nan')),node('resistance',current_a=.2),node('field',rate=.3),node('pressure',target=30),node('sweep'),node('repeat',count=0),node('resistance',delta_divisor=3)]
        for n in variants:
            with self.subTest(kind=n['kind']),self.assertRaises(ValueError):validate([n],self.c)
    def test_true_hardware_dependencies(self):
        for n in [node('resistance',mode='dc'),node('angle')]:
            with self.assertRaises(ValueError):validate([n],self.c,hardware=True)
    def test_expansion_cap_and_duplicate_ids(self):
        a=node('repeat',count=1000);b=node('repeat',count=1000);a['children']=[b];b['children']=[node('resistance',samples=3)]
        with self.assertRaises(ValueError):validate([a],self.c)
        n=node('resistance',samples=3)
        with self.assertRaises(ValueError):validate([n,n],self.c)
    def test_range_preserves_direction(self):
        self.assertEqual(span(1,-1,.5),[1,.5,0,-.5,-1]);self.assertEqual(span(0,1,.3)[-1],1)


class RotationTests(unittest.TestCase):
    def test_no_snapshot_reads_during_or_after_motion(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        class Rot:
            def __init__(self):self.reads=0;self.actual=0;self.moves=[]
            def read_deg(self):self.reads+=1;return self.actual
            def rotate_to_degree(self,t,s):self.moves.append((t,s))
        class PT:
            def readings(self,**kwargs):return SimpleNamespace(probe_temperature_k=4,vti_temperature_k=3.8,field_t=0,pressure_mbar=4)
            def magnet_action(self):return 'HOLD'
        b=HardwareBackend(dict(CONFIG,auto_pressure_enabled=False),lambda m:None);b.rot=Rot();b.pt=PT()
        with patch('studio.backend.time.monotonic',return_value=100):
            b.set_environment('angle',10,dict(rate=1,stable_s=1,timeout_s=30,tolerance=.05))
            for _ in range(5):b.snapshot();b.service()
            self.assertEqual(b.rot.reads,1)
            with self.assertRaises(RuntimeError):b.refresh_angle()
        b.rot.actual=10
        with patch('studio.backend.time.monotonic',return_value=113):
            b.service();self.assertIsNone(b.rotation_target);self.assertEqual(b.rot.reads,2)
            for _ in range(5):b.snapshot();b.service()
            self.assertEqual(b.rot.reads,2)
    def test_failed_rotation_blocks_measurement(self):
        b=HardwareBackend(copy.deepcopy(CONFIG),lambda m:None);b.rotation_target=10
        with self.assertRaises(RuntimeError):b.begin('resistance',node('resistance')['params'])
    def test_ambiguous_source_state_blocks_relays(self):
        class Source:
            def write(self,*a):pass
            def abort(self):pass
            def output_off(self):pass
            def set_current(self,*a):pass
            def query(self,*a):return 'garbled'
        b=HardwareBackend(copy.deepcopy(CONFIG),lambda m:None);b.src=Source()
        with self.assertRaises(RuntimeError):b.off()

if __name__=='__main__':unittest.main()
