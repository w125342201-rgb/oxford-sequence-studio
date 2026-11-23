import copy
import csv
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from test_pxi_physical_channels import Pxi4462Lockin
from pxi4462 import pxi4462_lockin as m
from studio.backend import HardwareBackend, SimBackend
from studio.engine import Engine
from studio.model import node, CONFIG
from studio.gui import display_readings

class AuditTests(unittest.TestCase):
    def daq(self):
        d=Pxi4462Lockin(freq_hz=13.37,fs_hz=4000,duration_s=.23,
                       simulate=True,noise_sim_v=0)
        d.simulate=False
        return d

    def test_analytic_four_channel_full_pipeline_and_signed_display(self):
        d=self.daq();t=np.arange(d._n)/d.fs_hz
        for marker in (0,90):
            data=[];truth=[]
            for ch in range(4):
                x,y=-(ch+1)*1e-3,(ch+1)*2e-4
                u,v=(ch+1)*1e-8,-(ch+1)*2e-8
                w=2*np.pi*d.freq_hz*t+np.deg2rad(marker)
                data.append(.01+x*np.sin(w)+y*np.cos(w)+u*np.sin(2*w)+v*np.cos(2*w))
                truth.append((x,y,u,v))
            d._acquire_hardware=lambda:np.array(data)
            c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
            hw=HardwareBackend(c,lambda _:None);hw.daq=d
            class Backend(SimBackend):
                def measure(self,kind,p,bias,wait):
                    return hw.measure(kind,p,bias,lambda _:None)
            e=Engine(Backend(c,lambda _:None),c)
            n=node('resistance',mode='pxi',marker_phase_deg=marker,phase2_deg=0,samples=1)
            with tempfile.TemporaryDirectory() as tmp:
                result=e.run([n],tmp)
                self.assertEqual(result['status'],'completed',result)
                with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:
                    row=next(csv.DictReader(f))
                point=next(x['data'] for x in map(json.loads,
                    (e.run_dir/'events.jsonl').read_text().splitlines()) if x['event']=='point')
                self.assertEqual(len(row),51)
                for ch,(x,y,u,v) in enumerate(truth):
                    for suffix,expected in [('X_V',x),('Y_V',y),('V',np.hypot(x,y)),
                            ('2f_X_V',u),('2f_Y_V',v),('2f_V',np.hypot(u,v))]:
                        self.assertAlmostEqual(float(row[f'ai{ch}_{suffix}']),expected,delta=1e-14)
                self.assertTrue(all(v<0 for _,v in display_readings(point)))

    def test_missing_channel_invalid_samples_become_null_and_advance(self):
        c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
        d=self.daq();hw=HardwareBackend(c,lambda _:None);hw.daq=d
        captures=[np.zeros((3,d._n)),np.full((4,d._n),np.nan),np.zeros((4,d._n))]
        d._acquire_hardware=lambda:captures.pop(0)
        class Backend(SimBackend):
            def measure(self,kind,p,bias,wait):
                return hw.measure(kind,p,bias,lambda _:None)
        e=Engine(Backend(c,lambda _:None),c)
        with tempfile.TemporaryDirectory() as tmp:
            r=e.run([node('resistance',mode='pxi',samples=3)],tmp)
            self.assertEqual(r['status'],'completed',r)
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f))
            for row in rows[:2]:
                self.assertEqual([row[k] for k in row if k.startswith('ai')],['NULL']*24)
            self.assertEqual(float(rows[2]['ai0_X_V']),0.)

    def test_fake_daqmx_actual_rate_and_clipping(self):
        d=self.daq()
        d.device='Dev1';d._chan_names=[f'Dev1/ai{i}' for i in range(4)]
        channels=[]
        class Channels(list):
            def add_ai_voltage_chan(self,name,**kwargs):
                channels.append(name)
                self.append(SimpleNamespace(ai_min=-1.,ai_max=1.))
        task=Mock();task.__enter__=Mock(return_value=task);task.__exit__=Mock(return_value=False)
        task.ai_channels=Channels()
        task.in_stream=SimpleNamespace(overloaded_chans_exist=False)
        task.timing.samp_clk_rate=4096.
        t=np.arange(d._n)/4096.
        wave=.001*np.sin(2*np.pi*d.freq_hz*t)+1e-8*np.cos(4*np.pi*d.freq_hz*t)
        task.read.return_value=np.tile(wave,(4,1))
        def new_task():
            task.ai_channels=Channels()
            return task
        enums=dict(nidaqmx=SimpleNamespace(Task=new_task),
            TerminalConfiguration=SimpleNamespace(DIFF=1,PSEUDO_DIFF=2),
            VoltageUnits=SimpleNamespace(VOLTS=1),Coupling=SimpleNamespace(AC=1,DC=0),
            ExcitationSource=SimpleNamespace(NONE=0),AcquisitionType=SimpleNamespace(FINITE=1))
        with patch.multiple(m,create=True,**enums):
            results=d.acquire_one()
            self.assertEqual(channels,d._chan_names)
            self.assertEqual(results['Dev1/ai0'].fs_hz,4096.)
            self.assertAlmostEqual(results['Dev1/ai0'].x_v,.001,delta=1e-14)
            self.assertAlmostEqual(results['Dev1/ai0'].y2_v,1e-8,delta=1e-14)
            self.assertEqual(d.last_capture_metadata['actual_fs_hz'],4096.)
            self.assertEqual(task.ai_channels[0].ai_excit_src,0)
            task.read.return_value=np.ones((4,d._n))
            with self.assertRaisesRegex(ValueError,'rail'):
                d.acquire_one()
            # Hardware can report analog overload even with samples below rails.
            task.read.return_value=np.zeros((4,d._n))
            task.in_stream=SimpleNamespace(overloaded_chans_exist=True,
                                           overloaded_chans=['Dev1/ai2'])
            with self.assertRaisesRegex(ValueError,'hardware overload: Dev1/ai2'):
                d.acquire_one()
            self.assertTrue(d.last_capture_metadata['overload_detected'])
            task.in_stream=SimpleNamespace()
            with self.assertRaisesRegex(ValueError,'overload status unavailable'):
                d.acquire_one()
            task.in_stream=SimpleNamespace(overloaded_chans_exist=False)
            d.freq_hz=.01;d.duration_s=1;d._n=409600
            task.read.return_value=np.zeros((4,d._n))
            d._acquire_hardware()
            self.assertGreaterEqual(task.read.call_args.kwargs['timeout'],205.)

    def test_zero_nyquist_bad_window_rejected(self):
        for freq,window in [(0,'hann'),(500,'hann'),(13,'typo')]:
            with self.assertRaises(ValueError):
                m.digital_lockin(np.ones(1000),1000,freq,1,window=window)

if __name__=='__main__': unittest.main()
