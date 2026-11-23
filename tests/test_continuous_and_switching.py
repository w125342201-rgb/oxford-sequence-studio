import copy
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from studio.backend import SimBackend
from studio.backend import HardwareBackend
from unittest.mock import Mock
from studio.engine import Engine
from studio.model import CONFIG,node,validate,upgrade_document

class StateRecorder(SimBackend):
    def __init__(self,c):
        super().__init__(c,lambda x:None);self.calls=[]
    def begin(self,k,p):
        self.calls.append('begin');super().begin(k,p)
    def off(self):
        self.calls.append('off');super().off()
    def set_environment(self,axis,target,p):
        self.calls.append(('environment',axis,self.output));super().set_environment(axis,target,p)
    def route(self,p):
        assert not self.output
        self.calls.append('route');super().route(p)
    def measure(self,k,p,bias,wait):
        assert self.output
        self.calls.append('measure');return super().measure(k,p,bias,wait)

class ContinuousTests(unittest.TestCase):
    def pxi_plan(self):
        n=node('sweep',axis='angle',values=[0.,1.],rate=.5,stable_s=0)
        b=node('sweep',axis='field',values=[0.,.01,0.],rate=.1,stable_s=0)
        b['children']=[node('wait',seconds=.01),node('resistance',mode='pxi',samples=2)]
        n['children']=[b]
        return [n]
    def run_with(self,nodes,c,emit=lambda *args:None,backend=None):
        b=backend or StateRecorder(c);e=Engine(b,c,emit)
        def wait(seconds):
            e.check();b.calls.append(('wait',seconds,b.output))
        e.wait=wait
        with tempfile.TemporaryDirectory() as tmp:result=e.run(nodes,tmp)
        return result,b
    def test_pxi_stays_on_across_all_environment_points(self):
        c=dict(CONFIG,measurement_device='pxi')
        result,b=self.run_with(self.pxi_plan(),c)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['points'],12)
        self.assertEqual(b.calls.count('begin'),1)
        self.assertEqual(b.calls.count('off'),1)
        self.assertEqual(b.calls[-1],'off')
        self.assertTrue(all(x[-1] for x in b.calls if isinstance(x,tuple) and x[0] in ('environment','wait')))
    def test_pxi_failure_closes_source(self):
        class Failing(StateRecorder):
            def measure(self,*args):raise RuntimeError('DAQ failure')
        c=dict(CONFIG,measurement_device='pxi');b=Failing(c)
        result,b=self.run_with(self.pxi_plan(),c,backend=b)
        self.assertEqual(result['status'],'failed');self.assertFalse(b.output)
    def test_pxi_pause_restarts_and_stop_closes(self):
        c=dict(CONFIG,measurement_device='pxi');b=StateRecorder(c)
        for action in ('pause','stop'):
            e=Engine(b,c);seen=[]
            def emit(kind,value):
                if kind=='point' and value['point']==1:
                    (e.pause if action=='pause' else e.stop).set()
                if kind=='state' and str(value).startswith('已暂停'):
                    seen.append(b.output);e.pause.clear()
            e.emit=emit;e.wait=lambda seconds:e.check()
            with tempfile.TemporaryDirectory() as tmp:r=e.run(self.pxi_plan(),tmp)
            self.assertEqual(r['status'],'completed' if action=='pause' else 'stopped')
            self.assertFalse(b.output)
            if action=='pause':self.assertEqual(seen,[False]);self.assertEqual(b.calls.count('begin'),2)
    def test_pxi_matrix_or_changed_excitation_rejected(self):
        with self.assertRaisesRegex(ValueError,'矩阵'):
            validate(self.pxi_plan(),dict(CONFIG,measurement_device='pxi',enable_matrix=True))
        a=node('resistance',mode='pxi');b=node('resistance',mode='pxi',frequency_hz=17.)
        with self.assertRaisesRegex(ValueError,'相同频率'):validate([a,b],dict(CONFIG,measurement_device='pxi'))
    def test_pxi_allows_different_current_per_step(self):
        """D-024：电流允许逐步骤不同；其余条件仍固定。"""
        c=dict(CONFIG,measurement_device='pxi');b=StateRecorder(c);e=Engine(b,c)
        nodes=[node('resistance',mode='pxi',current_a=.1,settle_s=0),
               node('resistance',mode='pxi',current_a=.02,settle_s=0)]
        with tempfile.TemporaryDirectory() as tmp:
            r=e.run(nodes,tmp)
            self.assertEqual(r['status'],'completed');self.assertEqual(r['acquisition_count'],2)
            self.assertEqual(b.calls.count('begin'),2)
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:
                self.assertEqual([float(x['current_a']) for x in csv.DictReader(f)],[.1,.02])
            events=[json.loads(x) for x in (e.run_dir/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(r['pxi_per_step_current']);self.assertEqual(r['pxi_contract']['frequency_hz'],13.0)
        self.assertEqual([x['data']['current_a'] for x in events if x['event']=='pxi_reconfigure'],[.02])
    def test_pxi_same_current_avoids_reconfiguration(self):
        c=dict(CONFIG,measurement_device='pxi');b=StateRecorder(c);e=Engine(b,c)
        nodes=[node('resistance',mode='pxi',current_a=.05,settle_s=0),
               node('resistance',mode='pxi',current_a=.05,settle_s=0)]
        with tempfile.TemporaryDirectory() as tmp:
            r=e.run(nodes,tmp)
            events=[json.loads(x) for x in (e.run_dir/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(r['status'],'completed');self.assertFalse(r['pxi_per_step_current'])
        self.assertEqual(b.calls.count('begin'),1)
        self.assertFalse(any(x['event']=='pxi_reconfigure' for x in events))
    def test_pxi_pause_resume_uses_last_applied_current(self):
        c=dict(CONFIG,measurement_device='pxi');b=StateRecorder(c);e=Engine(b,c);seen=[]
        nodes=[node('resistance',mode='pxi',current_a=.08,settle_s=0,samples=2)]
        def emit(kind,value):
            if kind=='point':
                e.pause.set()
            if kind=='state' and str(value).startswith('已暂停'):
                seen.append(e.pxi_applied['current_a']);e.pause.clear()
        e.emit=emit;e.wait=lambda seconds:e.check()
        with tempfile.TemporaryDirectory() as tmp:r=e.run(nodes,tmp)
        self.assertEqual(r['status'],'completed');self.assertEqual(seen,[.08])
        self.assertEqual(b.calls.count('begin'),2)
    def test_delta_delays_bracket_switch_before_output(self):
        c=dict(CONFIG,enable_matrix=True,matrix_each_point=True,matrix_off_delay_s=.25,matrix_settle_s=.75)
        result,b=self.run_with([node('resistance')],c)
        self.assertEqual(result['status'],'completed')
        for i,x in enumerate(b.calls):
            if x=='route':
                self.assertEqual(b.calls[i-2:i],['off',('wait',.25,False)])
                after=b.calls[i+1:]
                wait_index=after.index(('wait',.75,False))
                self.assertGreater(after.index('begin'),wait_index)
        self.assertEqual(b.calls.count('route'),len(c['point_routes']))
    def test_auto_pressure_switch_and_hysteresis(self):
        for enabled in (False,True):
            c=dict(CONFIG,auto_pressure_enabled=enabled,auto_pressure_low_mbar=3.5,auto_pressure_high_mbar=9.)
            b=SimBackend(c,lambda x:None)
            p=node('temperature')['params']
            b.set_environment('temperature',4.,p)
            self.assertEqual(b.env['pressure'],3.5 if enabled else 4.)
            b.set_environment('temperature',10.,p)
            self.assertEqual(b.env['pressure'],3.5 if enabled else 4.)
            b.set_environment('temperature',11.,p)
            self.assertEqual(b.env['pressure'],9. if enabled else 4.)
    def test_hardware_auto_pressure_without_temperature_scan(self):
        b=HardwareBackend(dict(CONFIG),lambda x:None);b.pt=Mock()
        b.pt.probe_temperature.return_value=4.
        b.pt.thermal_rule.pressure_regime.return_value='low'
        b.pt.automatic_pressure_target.return_value=3.
        b.service();b.pt.set_pressure.assert_called_once_with(3.)
        b.pt.enforce_thermal_link.assert_not_called()
        b.pt.set_vti_temperature.assert_not_called()
        b.last_service=0;b.service();self.assertEqual(b.pt.set_pressure.call_count,1)
        b.auto_pressure=False;b.last_service=0;b.service();self.assertEqual(b.pt.set_pressure.call_count,1)
    def test_dc_rejected_even_disabled(self):
        for enabled in (True,False):
            n=node('resistance',mode='dc');n['enabled']=enabled
            with self.assertRaises(ValueError):validate([n,node('wait')],CONFIG)
            with self.assertRaisesRegex(ValueError,'DC测量已删除'):
                upgrade_document(dict(schema_version=6,config=CONFIG,nodes=[n]))
    def test_old_pressure_values_preserved_on_migration(self):
        old=copy.deepcopy(CONFIG)
        for key in ('auto_pressure_enabled','auto_pressure_low_mbar','auto_pressure_high_mbar','matrix_off_delay_s'):old.pop(key)
        old.update(dmm_mode='delta',dmm_address='GPIB0::7::INSTR')
        d=upgrade_document(dict(schema_version=6,config=old,nodes=[node('resistance')]))
        self.assertEqual(d['config']['auto_pressure_low_mbar'],4.)
        self.assertEqual(CONFIG['auto_pressure_low_mbar'],3.)
        self.assertEqual(d['config']['matrix_off_delay_s'],0.)
    def test_matrix_has_six_lettered_rows_default_abcd(self):
        path=Path(__file__).resolve().parents[1]/'legacy/matrix/matrix_switch.py'
        spec=importlib.util.spec_from_file_location('audit_matrix',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.N_ROWS,6);self.assertEqual(module.N_COLS,24)
        self.assertEqual(module.ROW_LABELS,tuple('ABCDEF'))
        self.assertEqual([module._norm_row(k) for k in 'ABCD'],CONFIG['matrix_rows'])
