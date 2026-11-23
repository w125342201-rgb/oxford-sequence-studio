import math
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import numpy as np
from test_pxi_physical_channels import Pxi4462Lockin
from studio.backend import HardwareBackend
from studio.model import CONFIG,VERSION,node,upgrade_document,normalize_pxi_channels
from studio.gui import Form
from studio.instrument_options import visible_fields

class PhaseTests(unittest.TestCase):
    def test_known_signed_xy_marker_invariance_and_independent_offsets(self):
        with patch('pxi4462.pxi4462_lockin.Edge',SimpleNamespace(RISING=1,FALLING=0),create=True):
            daq=Pxi4462Lockin(freq_hz=17,i_peak_a=1e-5,fs_hz=17000,duration_s=1,channels=[0,1,2,3],simulate=True,noise_sim_v=0)
        daq.simulate=False
        t=np.arange(daq._n)/daq.fs_hz
        for marker in (0,90):
            data=[]
            for ch in range(4):
                a=(ch+1)*1e-4;b=(ch+1)*1e-6
                data.append(a*np.sin(2*np.pi*17*t+math.radians(marker+160)) + b*np.sin(2*np.pi*34*t+math.radians(2*marker-130)))
            daq._acquire_hardware=lambda:np.array(data)
            for offset1,offset2 in ((0,0),(20,-40)):
                result=daq.acquire_one(marker_phase_deg=marker,phase_ref_deg=offset1,phase2_ref_deg=offset2,window='rect')
                for ch,v in enumerate(result.values()):
                    a=(ch+1)*1e-4;b=(ch+1)*1e-6
                    self.assertAlmostEqual(v.v_peak_v,a,delta=1e-12)
                    self.assertAlmostEqual(v.x_v,a*math.cos(math.radians(160-offset1)),delta=1e-12)
                    self.assertAlmostEqual(v.y_v,a*math.sin(math.radians(160-offset1)),delta=1e-12)
                    self.assertAlmostEqual(v.v2_peak_v,b,delta=1e-12)
                    self.assertAlmostEqual(v.x2_v,b*math.cos(math.radians(-130-offset2)),delta=1e-12)
                    self.assertAlmostEqual(v.y2_v,b*math.sin(math.radians(-130-offset2)),delta=1e-12)

    def test_marker_is_explicitly_sent_and_offsets_forwarded(self):
        b=HardwareBackend(dict(CONFIG,measurement_device='pxi'),lambda _:None);b.src=Mock();b.off=Mock()
        p=node('resistance',mode='pxi')['params']
        with patch('pxi4462.pxi4462_lockin.Pxi4462Lockin'):
            b.begin('resistance',p)
        self.assertEqual(b.src.configure_ac.call_args.kwargs['marker_position'],90)
        self.assertEqual(b.src.configure_ac.call_args.kwargs['marker_line'],1)

    def test_old_params_gain_explicit_phase_defaults(self):
        n=node('resistance',mode='pxi');n['params'].pop('marker_phase_deg');n['params'].pop('phase2_deg')
        normalize_pxi_channels([n])
        self.assertEqual(n['params']['marker_phase_deg'],0)
        self.assertEqual(n['params']['phase2_deg'],0)

    def test_thermal_default_recovers_signed_x_on_four_channels(self):
        p=node('resistance',mode='pxi')['params']
        self.assertEqual((p['marker_phase_deg'],p['phase_deg'],p['phase2_deg']),(90,0,-90))
        self.assertTrue({'marker_phase_deg','phase_deg','phase2_deg'} <= visible_fields(p))
        daq=Pxi4462Lockin(freq_hz=13,fs_hz=4000,duration_s=1,simulate=True)
        daq.simulate=False
        t=np.arange(daq._n)/daq.fs_hz
        b=HardwareBackend(dict(CONFIG,measurement_device='pxi'),lambda _:None);b.daq=daq
        # Physical Joule-heating waveform, independent of the decoder: -cos(2w).
        # Include both signs, as for magnetization or lead reversal.
        for marker in (90,0):
            p['marker_phase_deg']=marker
            w=2*np.pi*13*t+np.deg2rad(marker)
            truth=[1e-7,-2e-7,3e-7,-4e-7]
            data=np.array([.01+(ch+1)*1e-3*np.sin(w)-a*np.cos(2*w)
                           for ch,a in enumerate(truth)])
            daq._acquire_hardware=lambda:data
            r=b.measure('resistance',p,p['current_a'],lambda _:None)
            for ch,a in enumerate(truth):
                result=r['channels'][str(ch)]
                self.assertAlmostEqual(result['x_v'],(ch+1)*1e-3,delta=1e-13)
                self.assertAlmostEqual(result['x2_v'],a,delta=1e-13)
                self.assertAlmostEqual(result['y2_v'],0,delta=1e-13)

    def test_phase_roundtrip_preserves_explicit_old_values_and_preset_is_local(self):
        for angle in (0,-90,37):
            n=node('resistance',mode='pxi',marker_phase_deg=17,phase_deg=9,phase2_deg=angle)
            doc=upgrade_document(json.loads(json.dumps(dict(schema_version=VERSION,config=CONFIG,nodes=[n]))))
            p=doc['nodes'][0]['params']
            self.assertEqual((p['marker_phase_deg'],p['phase_deg'],p['phase2_deg']),(17,9,angle))
        fields={key:Mock() for key in ('marker_phase_deg','phase_deg','phase2_deg','frequency_hz')}
        Form.thermal_phase_defaults(SimpleNamespace(fields=fields))
        for key,value in (('marker_phase_deg','90'),('phase_deg','0'),('phase2_deg','-90')):
            fields[key].set.assert_called_once_with(value)
        fields['frequency_hz'].set.assert_not_called()

if __name__=='__main__':unittest.main()
