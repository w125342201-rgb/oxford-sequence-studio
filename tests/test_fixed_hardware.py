import copy,sys,types,unittest
from unittest.mock import Mock,patch
from studio.model import CONFIG,SPECS,node,validate,upgrade_document,measurement_node
from studio.instrument_options import OPTIONS,wiring_hint
from studio.backend import HardwareBackend

class FixedHardwareTests(unittest.TestCase):
    def test_no_selectable_trigger_or_sr830(self):
        self.assertNotIn('sr830',SPECS);self.assertNotIn('sr830_address',CONFIG)
        for k in ['marker_line','daq_trigger','daq_edge']:
            self.assertNotIn(k,OPTIONS);self.assertNotIn(k,node('resistance')['params'])
        self.assertEqual([v for _,v in OPTIONS['measurement_device']],['2182a','pxi'])
        self.assertIn('固定线1',wiring_hint('pxi'))
        with self.assertRaises(ValueError):wiring_hint('dc')
    def test_migration_rejects_changed_pxi_wiring(self):
        for key,value in [('marker_line',3),('daq_edge','rising'),('daq_trigger','PFI1')]:
            n=node('resistance',mode='pxi');n['params'][key]=value
            d=dict(schema_version=5,config=dict(CONFIG,sr830_address='GPIB0::8::INSTR'),nodes=[n])
            before=copy.deepcopy(d)
            with self.assertRaisesRegex(ValueError,'不能自动转换'):upgrade_document(d)
            self.assertEqual(d,before)
    def test_default_migration_preserves_measurement(self):
        n=node('resistance',mode='pxi',daq_channels=[0,1,2,3]);old=copy.deepcopy(n)
        old['params'].update(marker_line=1,daq_trigger='PFI0',daq_edge='falling')
        c=dict(CONFIG,measurement_device='pxi',sr830_address='GPIB0::8::INSTR')
        d=upgrade_document(dict(schema_version=5,config=c,nodes=[old]))
        self.assertEqual(d['nodes'],[n]);validate(d['nodes'],d['config'],hardware=True)
    def test_sr830_nodes_rejected_even_disabled(self):
        for enabled in (True,False):
            n=dict(id='old',kind='sr830',enabled=enabled,params={},children=[])
            with self.assertRaisesRegex(ValueError,'SR830'):upgrade_document(dict(schema_version=5,config=CONFIG,nodes=[n]))
    def test_ac_coupling_is_optional_uniform_and_validated(self):
        self.assertEqual(OPTIONS['daq_coupling'],[('DC 耦合（默认）','dc'),('AC 耦合（−3 dB @ 3.4 Hz）','ac')])
        self.assertEqual(node('resistance')['params']['daq_coupling'],'dc')
        c=dict(CONFIG,measurement_device='pxi')
        validate([node('resistance',mode='pxi')],c)
        bad=node('resistance',mode='pxi');bad['params']['daq_coupling']='AC'
        with self.assertRaisesRegex(ValueError,'daq_coupling'):validate([bad],c)
        a=node('resistance',mode='pxi');b=node('resistance',mode='pxi',daq_coupling='ac')
        with self.assertRaisesRegex(ValueError,'相同'):validate([a,b],c)
    def test_ac_coupling_reaches_daq_constructor(self):
        c=dict(CONFIG,measurement_device='pxi')
        b=HardwareBackend(c,lambda _:None);b.src=Mock();b.src.query.return_value='OFF';b.pt=Mock()
        fake=Mock()
        pkg=types.ModuleType('pxi4462');mod=types.ModuleType('pxi4462.pxi4462_lockin');mod.Pxi4462Lockin=fake
        with patch.dict(sys.modules,{'pxi4462':pkg,'pxi4462.pxi4462_lockin':mod}):
            b.begin('resistance',node('resistance',mode='pxi',daq_coupling='ac')['params'])
        self.assertEqual(fake.call_args.kwargs['coupling'],'ac')
        fake.reset_mock()
        with patch.dict(sys.modules,{'pxi4462':pkg,'pxi4462.pxi4462_lockin':mod}):
            b.begin('resistance',node('resistance',mode='pxi')['params'])
        self.assertEqual(fake.call_args.kwargs['coupling'],'dc')
    def test_device_connections_and_mode_guard(self):
        for mode in ('delta','pxi'):
            c=dict(CONFIG,measurement_device='pxi' if mode=='pxi' else '2182a')
            ox=types.ModuleType('oxford.oxford_teslatronpt_api');ox.TeslatronPT=Mock();ox.ThermalRule=Mock()
            visa=types.ModuleType('pyvisa');visa.ResourceManager=Mock()
            drv=types.ModuleType('keithely6221_2182A.keithley_6221_2182a_driver')
            drv.Keithley6221=Mock();drv.Keithley2182A=Mock()
            for fac in (drv.Keithley6221,drv.Keithley2182A):fac.return_value.idn.return_value='FAKE'
            modules={'oxford':types.ModuleType('oxford'),'oxford.oxford_teslatronpt_api':ox,'pyvisa':visa,'keithely6221_2182A':types.ModuleType('keithely6221_2182A'),'keithely6221_2182A.keithley_6221_2182a_driver':drv}
            b=HardwareBackend(c,lambda x:None);b.off=Mock()
            with patch.dict(sys.modules,modules):b.connect()
            drv.Keithley2182A.assert_not_called()
            ox.ThermalRule.assert_called_once_with(low_pressure_mbar=3.,high_pressure_mbar=10.)
            b.close()
            validate([measurement_node(c)],c,hardware=True)
            other=node('resistance',mode='delta' if mode=='pxi' else 'pxi')
            with self.assertRaisesRegex(ValueError,'不符'):validate([other],c,hardware=True)
