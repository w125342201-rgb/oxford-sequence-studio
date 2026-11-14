"""NI-boundary tests: specified Vpk ranges, overload exception and volts scale.
DAQ/native responses are explicit substitutes; not hardware validation.
"""
import copy
import csv
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from test_pxi_physical_channels import Pxi4462Lockin
from pxi4462 import pxi4462_lockin as m
from studio.backend import HardwareBackend,SimBackend
from studio.engine import Engine
from studio.model import CONFIG,VERSION,node,upgrade_document,validate
from studio.instrument_options import OPTIONS

class TaskBoundary:
    def __init__(self,readback=None,signal=None,status=False,status_error=False):
        self.readback=readback;self.signal=signal;self.status=status;self.status_error=status_error
        self.calls=[];self.added=[];self.closed=False
        owner=self
        class Channels(list):
            def add_ai_voltage_chan(self,name,**kw):
                owner.added.append((name,kw))
                lo,hi=owner.readback or (kw['min_val'],kw['max_val'])
                self.append(SimpleNamespace(ai_min=lo,ai_max=hi,ai_gain=30))
        class Stream:
            @property
            def overloaded_chans_exist(self):
                assert not owner.closed
                owner.calls.append('overload_exists')
                if owner.status_error:raise RuntimeError('property unavailable')
                return owner.status
            @property
            def overloaded_chans(self):
                assert owner.calls[-1]=='overload_exists' and not owner.closed
                owner.calls.append('overload_channels')
                return ['Dev1/ai2']
        self.ai_channels=Channels();self.in_stream=Stream()
        self.timing=SimpleNamespace(samp_clk_rate=4000.,cfg_samp_clk_timing=lambda *a,**k:None)
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def start(self):self.calls.append('start')
    def read(self,number_of_samples_per_channel,timeout):
        self.calls.append('read');self.read_n=number_of_samples_per_channel
        t=np.arange(self.read_n)/4000.
        if self.signal is not None:return self.signal(t)
        return np.array([.001+(ch+1)*.01*np.sin(2*np.pi*13*t) for ch in range(4)])

def api(task):
    return patch.multiple(m,create=True,nidaqmx=SimpleNamespace(Task=lambda:task),
        TerminalConfiguration=SimpleNamespace(DIFF=1,PSEUDO_DIFF=2),
        VoltageUnits=SimpleNamespace(VOLTS=1),Coupling=SimpleNamespace(AC=1,DC=0),
        ExcitationSource=SimpleNamespace(NONE=0),AcquisitionType=SimpleNamespace(FINITE=1))

def daq(rng):
    d=Pxi4462Lockin(freq_hz=13,fs_hz=4000,duration_s=1,simulate=True,
                   voltage_range_v=rng,trigger_terminal=None)
    d.simulate=False;d.device='Dev1';d._chan_names=[f'Dev1/ai{ch}' for ch in range(4)]
    return d

class RangeTests(unittest.TestCase):
    def test_six_ranges_reach_four_inputs_without_rescaling(self):
        expected=(.316,1.,3.16,10.,31.6,42.4)
        self.assertEqual(tuple(v for _,v in OPTIONS['daq_range_v']),expected)
        for rng in expected:
            with self.subTest(range=rng):
                task=TaskBoundary();d=daq(rng)
                with api(task):r=d.acquire_one()
                self.assertEqual([v[0] for v in task.added],d._chan_names)
                self.assertTrue(all(kw['min_val']==-rng and kw['max_val']==rng for _,kw in task.added))
                for ch,value in enumerate(r.values()):
                    self.assertAlmostEqual(value.x_v,(ch+1)*.01,delta=1e-13)
                self.assertEqual(task.calls.count('overload_exists'),int(rng!=42.4))
                self.assertEqual(d.last_capture_metadata['requested_input_range_v'],rng)
                self.assertEqual(len(d.last_capture_metadata['signal_peak_abs_v']),4)

    def test_coerced_readback_peak_and_dc_use_actual_volts(self):
        # NI may return a more precise range than the rounded nominal label.
        actual=np.sqrt(.1)
        task=TaskBoundary(readback=(-actual,actual));d=daq(.316)
        with api(task):d._acquire_hardware()
        meta=d.last_capture_metadata
        self.assertEqual(meta['input_ranges_v'],[(-actual,actual)]*4)
        self.assertAlmostEqual(meta['signal_peak_abs_v'][3],.041,places=12)
        self.assertAlmostEqual(meta['range_usage_fraction'][3],.041/actual,places=12)
        self.assertAlmostEqual(meta['signal_mean_v'][3],.001,places=12)

    def test_highest_range_unknown_overload_and_frontend_cap(self):
        task=TaskBoundary(readback=(-100.,100.),status_error=True);d=daq(42.4)
        with api(task):d._acquire_hardware()
        self.assertNotIn('overload_exists',task.calls)
        self.assertIsNone(d.last_capture_metadata['overload_detected'])
        self.assertEqual(d.last_capture_metadata['guard_ranges_v'],[(-42.4,42.4)]*4)
        bad=TaskBoundary(readback=(-100.,100.),signal=lambda t:np.full((4,len(t)),43.))
        with api(bad),self.assertRaisesRegex(ValueError,'rail'):d._acquire_hardware()

    def test_ordinary_range_hardware_overload_remains_null_then_continues(self):
        c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
        d=daq(.316);hw=HardwareBackend(c,lambda _:None);hw.daq=d
        tasks=[TaskBoundary(status=True),TaskBoundary(status_error=True),TaskBoundary()]
        class Backend(SimBackend):
            def measure(self,kind,p,bias,wait):
                with api(tasks.pop(0)):return hw.measure(kind,p,bias,lambda _:None)
        e=Engine(Backend(c,lambda _:None),c)
        with tempfile.TemporaryDirectory() as tmp:
            result=e.run([node('resistance',mode='pxi',marker_phase_deg=0,samples=3)],tmp)
            self.assertEqual(result['status'],'completed',result)
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            for row in rows[:2]:self.assertEqual([v for k,v in row.items() if k.startswith('ai')],['NULL']*24)
            self.assertAlmostEqual(float(rows[2]['ai3_X_V']),.04,places=12)
            points=[v['data'] for v in map(json.loads,(e.run_dir/'events.jsonl').read_text().splitlines()) if v['event']=='point']
            self.assertEqual(points[-1]['daq_metadata']['requested_input_range_v'],.316)
            self.assertEqual(len(points[-1]['daq_metadata']['range_usage_fraction']),4)

    def test_legacy_autorange_full_period_all_ranges_and_bad_prescan(self):
        for peak,expected in ((.2,.316),(.5,1),(2.,3.16),(5.,10),(15.,31.6),(35.,42.4)):
            task=TaskBoundary(signal=lambda t:np.full((4,len(t)),peak));d=daq('auto');d.freq_hz=1
            with api(task):self.assertEqual(d._effective_range_v(),expected)
            self.assertGreaterEqual(task.read_n,4000)
            self.assertTrue(all(kw['max_val']==42.4 for _,kw in task.added))
        for signal in (lambda t:np.full((4,len(t)),43.),lambda t:np.full((4,len(t)),39.),
                       lambda t:np.full((4,len(t)),np.nan),lambda t:np.zeros((3,len(t)))):
            d=daq('auto')
            with api(TaskBoundary(signal=signal)),self.assertRaises(ValueError):d._effective_range_v()

    def test_default_and_import_preserve_ranges(self):
        self.assertEqual(node('resistance',mode='pxi')['params']['daq_range_v'],.316)
        old=node('resistance',mode='pxi',daq_range_v=1.)
        new=upgrade_document(dict(schema_version=VERSION,config=CONFIG,nodes=[old]))
        self.assertEqual(new['nodes'][0]['params']['daq_range_v'],1.)
        for bad in (.001,-1,0,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):daq(bad)
        with self.assertRaises(ValueError):validate([node('resistance',daq_range_v='auto')],CONFIG)
