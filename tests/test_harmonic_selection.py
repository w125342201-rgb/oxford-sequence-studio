"""Independent analytic inputs through production estimator/CSV/view mapping."""
import copy
import csv
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from test_pxi_physical_channels import Pxi4462Lockin
from studio.backend import HardwareBackend,SimBackend
from studio.engine import Engine
from studio.gui import App,display_readings
from studio.model import CONFIG,VERSION,node,upgrade_document,validate
from studio.instrument_options import visible_fields

class HarmonicSelectionTests(unittest.TestCase):
    def test_3f_analytic_csv_events_and_display_all_channels(self):
        c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
        d=Pxi4462Lockin(freq_hz=13.37,fs_hz=4000,duration_s=.1,simulate=True)
        d.simulate=False
        # Deliberately noninteger window, strong 1f AND 2f, signed weak 3f.
        d._n=417
        t=np.arange(d._n)/d.fs_hz
        for marker in (0,90):
            for phase in (-180,23):
                w=2*np.pi*d.freq_hz*t+np.deg2rad(marker)
                truth=[((-1)**ch*(ch+1)*1e-8,(ch+1)*2e-8) for ch in range(4)]
                data=np.array([.01+(ch+1)*.001*np.sin(w)+.002*np.cos(2*w+.4)
                    +x*np.sin(3*w+np.deg2rad(phase))+y*np.cos(3*w+np.deg2rad(phase))
                    for ch,(x,y) in enumerate(truth)])
                captures=[np.zeros((3,d._n)),data]
                d._acquire_hardware=lambda:captures.pop(0)
                hw=HardwareBackend(c,lambda _:None);hw.daq=d
                class Backend(SimBackend):
                    def measure(self,kind,p,bias,wait):return hw.measure(kind,p,bias,lambda _:None)
                e=Engine(Backend(c,lambda _:None),c)
                n=node('resistance',mode='pxi',harmonic_order=3,marker_phase_deg=marker,
                       phase3_deg=phase,samples=2)
                with tempfile.TemporaryDirectory() as tmp:
                    result=e.run([n],tmp)
                    self.assertEqual(result['status'],'completed',result)
                    with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
                    self.assertEqual(len(rows[1]),51)
                    self.assertFalse(any('2f' in key for key in rows[1]))
                    self.assertEqual([v for k,v in rows[0].items() if k.startswith('ai')],['NULL']*24)
                    points=[v['data'] for v in map(json.loads,(e.run_dir/'events.jsonl').read_text().splitlines()) if v['event']=='point']
                    manifest=json.loads((e.run_dir/'run.json').read_text())
                    self.assertEqual(manifest['voltage_columns']['ai3_3f_X_V']['harmonic'],3)
                    for ch,(x,y) in enumerate(truth):
                        for suffix,value in [('X_V',(ch+1)*.001),('Y_V',0),('V',(ch+1)*.001),
                                             ('3f_X_V',x),('3f_Y_V',y),('3f_V',np.hypot(x,y))]:
                            self.assertAlmostEqual(float(rows[1][f'ai{ch}_{suffix}']),value,delta=1e-13)
                        self.assertIsNone(points[1]['channels'][str(ch)]['x2_v'])
                    for component,index in [('X',0),('Y',1)]:
                        for ch,(_,v) in enumerate(display_readings(points[1],3,component)):
                            self.assertAlmostEqual(v,truth[ch][index],delta=1e-13)
                    self.assertTrue(all(v is None for _,v in display_readings(points[1],2)))

    def test_physical_joule_heating_default_sign(self):
        d=Pxi4462Lockin(freq_hz=13,fs_hz=4000,duration_s=1,simulate=True)
        d.simulate=False
        w=2*np.pi*13*np.arange(d._n)/d.fs_hz+np.pi/2
        # I=sin(w); dR proportional to [1-cos(2w)], so 3f=-A*sin(3w).
        data=np.array([.001*np.sin(w)+a*np.sin(w)*(1-np.cos(2*w))
                       for a in (2e-7,-4e-7,6e-7,-8e-7)])
        d._acquire_hardware=lambda:data
        r=d.acquire_one(marker_phase_deg=90,harmonic_order=3)
        for v,a in zip(r.values(),(1e-7,-2e-7,3e-7,-4e-7)):
            self.assertAlmostEqual(v.x3_v,a,delta=1e-13)
            self.assertAlmostEqual(v.y3_v,0,delta=1e-13)

    def test_old_import_phase_persistence_and_invalid_orders(self):
        c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
        n=node('resistance',mode='pxi',phase2_deg=37)
        n['params'].pop('harmonic_order');n['params'].pop('phase3_deg')
        doc=upgrade_document(dict(schema_version=VERSION,config=c,nodes=[n]))
        self.assertEqual(doc['nodes'][0]['params']['harmonic_order'],2)
        self.assertEqual(doc['nodes'][0]['params']['phase2_deg'],37)
        validate(doc['nodes'],c)
        n=node('resistance',mode='pxi',harmonic_order=3,phase3_deg=21)
        saved=upgrade_document(json.loads(json.dumps(dict(schema_version=VERSION,config=c,nodes=[n]))))
        self.assertEqual(saved['nodes'][0]['params']['phase3_deg'],21)
        self.assertIn('phase3_deg',visible_fields(n['params']))
        self.assertNotIn('phase2_deg',visible_fields(n['params']))
        for h in (1,4,True,2.5):
            with self.assertRaises(ValueError):validate([node('resistance',mode='pxi',harmonic_order=h)],c)
        with self.assertRaises(ValueError):
            validate([node('resistance',mode='pxi',harmonic_order=3,frequency_hz=200,daq_fs_hz=1000)],c)
        with self.assertRaises(ValueError):
            validate([node('resistance',mode='pxi',harmonic_order=h) for h in (2,3)],c)

    def test_third_harmonic_plot_and_simulation(self):
        rows=[dict(point=i,path='sweep',kind='resistance',route='直接接线',channels={str(ch):dict(x3_v=-(ch+1)*i*1e-8)
               for ch in range(4)}) for i in (1,2)]
        canvas=Mock();canvas.winfo_width.return_value=1000;canvas.winfo_height.return_value=300
        app=SimpleNamespace(sync_plot_mode=lambda:True,data=rows,canvas=canvas,xaxis=Mock(),group=Mock(),harmonic=Mock(),latest_voltages=Mock())
        app.xaxis.get.return_value='point';app.group.get.return_value='ai2';app.harmonic.get.return_value='三次谐波 3f'
        App.draw(app)
        self.assertIn('3f X',app.latest_voltages.set.call_args.args[0])
        self.assertIn('ai2: -6e-08 V',app.latest_voltages.set.call_args.args[0])
        c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
        e=Engine(SimBackend(c,lambda _:None),c)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(e.run([node('resistance',mode='pxi',harmonic_order=3)],tmp)['status'],'completed')
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:
                self.assertEqual(float(next(csv.DictReader(f))['ai0_3f_X_V']),0)
