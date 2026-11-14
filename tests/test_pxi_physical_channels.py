import copy
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
from studio.backend import HardwareBackend, SimBackend
from studio.engine import Engine
from studio.model import CONFIG, node

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy'))
from pxi4462.pxi4462_lockin import Pxi4462Lockin

class PhysicalChannelTests(unittest.TestCase):
    def test_real_driver_result_through_hardware_adapter_and_csv(self):
        for selected in ([0],[0,2],[3,1]):
            with self.subTest(channels=selected):
                c=dict(copy.deepcopy(CONFIG),measurement_device='pxi',enable_matrix=False)
                with patch('pxi4462.pxi4462_lockin.Edge',SimpleNamespace(RISING=1,FALLING=0),create=True):
                    daq=Pxi4462Lockin(freq_hz=17.,i_peak_a=1e-5,
                    fs_hz=10000.,duration_s=1.,channels=[0,1,2,3],simulate=True,
                    sim_r_ohm=[10.+ch for ch in range(4)],sim_v2_ratio=[.01,.02,.03,.04],noise_sim_v=0.)
                # Actual driver algorithm and physical-name result format.
                daq._chan_names=[f'PXI1Slot2/ai{ch}' for ch in range(4)]
                hw=HardwareBackend(c,lambda _:None);hw.daq=daq
                class Backend(SimBackend):
                    def measure(self,kind,p,bias,wait):
                        return hw.measure(kind,p,bias,lambda _:None)
                e=Engine(Backend(c,lambda _:None),c)
                n=node('resistance',mode='pxi',daq_channels=selected,samples=2,interval_s=0.)
                with tempfile.TemporaryDirectory() as tmp:
                    result=e.run([n],tmp)
                    self.assertEqual(result['status'],'completed',result)
                    with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:
                        rows=list(csv.DictReader(f))
                    points=[x['data'] for x in map(json.loads,(e.run_dir/'events.jsonl').read_text().splitlines()) if x['event']=='point']
                    self.assertEqual(len(rows),2)
                    self.assertEqual(len(rows[0]),51)
                    for ch in range(4):
                        self.assertAlmostEqual(float(rows[0][f'ai{ch}_V']),(10.+ch)*1e-5,delta=1e-7)
                        logical=points[0]['channels'][str(ch)]
                        physical=points[0]['raw'][f'PXI1Slot2/ai{ch}']
                        self.assertEqual(logical,physical)
                        self.assertAlmostEqual(float(rows[0][f'ai{ch}_2f_V']),(10.+ch)*1e-5*(ch+1)*.01,delta=1e-8)
                        self.assertEqual(float(rows[0][f'ai{ch}_2f_V']),logical['v2_peak_v'])
                        for suffix,key in (('X_V','x_v'),('Y_V','y_v'),('2f_X_V','x2_v'),('2f_Y_V','y2_v')):
                            self.assertEqual(float(rows[0][f'ai{ch}_{suffix}']),logical[key])
                    saved=json.loads((e.run_dir/'run.json').read_text())
                    self.assertEqual(saved['nodes'][0]['params']['daq_channels'],[0,1,2,3])

if __name__=='__main__':unittest.main()
