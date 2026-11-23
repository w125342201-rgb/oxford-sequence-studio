import unittest
from unittest.mock import Mock
from studio.model import CONFIG, node, upgrade_document, validate_config, VERSION
from studio.backend import HardwareBackend, SimBackend
from test_precision_thermal import api

class VTISwitchTests(unittest.TestCase):
    def test_wire_commands_and_background_crossings(self):
        for linked in (False,True):
            for pressure in (False,True):
                with self.subTest(linked=linked,pressure=pressure):
                    b=HardwareBackend(dict(CONFIG,link_vti_temperature=linked,auto_pressure_enabled=pressure),lambda _:None)
                    pt=api.TeslatronPT.__new__(api.TeslatronPT)
                    pt.itc_probe=Mock();pt.itc_vti=Mock();pt.thermal_rule=api.ThermalRule()
                    pt.probe_temperature=Mock(return_value=4.);b.pt=pt
                    b.set_environment('temperature',20.,node('temperature')['params'])
                    for actual in (20.,10.,4.,20.):
                        pt.probe_temperature.return_value=actual;b.last_service=0;b.service()
                    self.assertEqual([c.args[4] for c in pt.itc_probe.set_signal.call_args_list],['RSET','RENA','TSET'])
                    vti=[c.args for c in pt.itc_vti.set_signal.call_args_list]
                    thermal=[c for c in vti if c[0]==api.VTI_MODULE]
                    gas=[c for c in vti if c[0]==api.PRESSURE_MODULE]
                    self.assertEqual(bool(thermal),linked)
                    self.assertEqual(bool(gas),pressure)
                    if linked:self.assertEqual(thermal[-1][-1],'19')

    def test_simulation_and_saved_config(self):
        c=dict(CONFIG,link_vti_temperature=False)
        b=SimBackend(c,lambda _:None);initial=b.env['vti']
        b.set_environment('temperature',20.,node('temperature')['params']);b.service()
        self.assertEqual(b.env['vti'],initial)
        c['link_vti_temperature']=True
        b.set_environment('temperature',20.,node('temperature')['params'])
        self.assertEqual(b.env['vti'],19.)
        for value in (False,True):
            d=upgrade_document(dict(schema_version=VERSION,config=dict(CONFIG,link_vti_temperature=value),nodes=[]))
            validate_config(d['config']);self.assertEqual(d['config']['link_vti_temperature'],value)
        old=dict(CONFIG);old.pop('link_vti_temperature')
        self.assertTrue(upgrade_document(dict(schema_version=VERSION,config=old,nodes=[]))['config']['link_vti_temperature'])
