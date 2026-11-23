import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from studio.model import CONFIG, VERSION, node, span, upgrade_document, validate
from studio.backend import HardwareBackend, SimBackend

path=Path(__file__).resolve().parents[1]/'legacy/oxford/oxford_teslatronpt_api.py'
spec=importlib.util.spec_from_file_location('thermal_audit_api',path)
api=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=api
spec.loader.exec_module(api)

class PrecisionThermalTests(unittest.TestCase):
    def test_generated_points(self):
        self.assertEqual(span(1.3,1.8,.1,axis='temperature'),[1.3,1.4,1.5,1.6,1.7,1.8])
        self.assertEqual(span(.003,-.003,.001,axis='field'),[.003,.002,.001,0.,-.001,-.002,-.003])
        self.assertEqual(span(0,.3,.1,axis='angle'),[0.,.1,.2,.3])
        self.assertEqual(span(0,1,.3,axis='temperature'),[0.,.3,.6,.9,1.])
        with self.assertRaises(ValueError):span(1.34,1.65,.1,axis='temperature')
        for axis, step in [('temperature',.01),('angle',.01),('field',.0001)]:
            with self.assertRaises(ValueError):span(0,1,step,axis=axis)

    def test_vti_uses_target_during_cooling_heating_and_crossing(self):
        for target, expected in [(4.,3.8),(9.9,9.7),(10.,9.),(10.1,9.1),(20.,19.)]:
            for auto in (False,True):
                pt=api.TeslatronPT.__new__(api.TeslatronPT)
                pt.thermal_rule=api.ThermalRule()
                pt.set_probe_temperature=Mock();pt.set_vti_temperature=Mock();pt.set_pressure=Mock()
                pt.set_sample_temperature(target)
                pt.set_vti_temperature.assert_called_with(expected)
                regime=None
                for actual in (20.,10.,4.,10.,20.):
                    regime,_=pt.enforce_thermal_link(actual,target,auto,regime)
                    pt.set_vti_temperature.assert_called_with(expected)
                if not auto:pt.set_pressure.assert_not_called()

    def test_single_global_pressure_setting(self):
        for enabled in (False,True):
            config=dict(CONFIG,auto_pressure_enabled=enabled)
            for cls in (SimBackend,HardwareBackend):
                b=cls(config,lambda _:None)
                if cls is HardwareBackend:
                    b.pt=Mock();b.service=Mock()
                # A stale per-node flag must never override the global switch.
                b.set_environment('temperature',4.,dict(node('temperature')['params'],auto_pressure=not enabled))
                self.assertEqual(b.auto_pressure,enabled)
                if enabled:
                    with self.assertRaises(ValueError):b.set_environment('pressure',5.,{})
                else:b.set_environment('pressure',5.,{})
        self.assertNotIn('auto_pressure',node('sweep')['params'])
        old=node('temperature',auto_pressure=False)
        doc=upgrade_document(dict(schema_version=VERSION,config=copy.deepcopy(CONFIG),nodes=[old]))
        self.assertNotIn('auto_pressure',doc['nodes'][0]['params'])
        self.assertTrue(doc['config']['auto_pressure_enabled'])
        with self.assertRaises(ValueError):validate([node('pressure')],CONFIG)
        validate([node('pressure')],dict(CONFIG,auto_pressure_enabled=False))
