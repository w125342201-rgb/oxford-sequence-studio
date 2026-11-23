import ast
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from studio.model import CONFIG, node, validate, upgrade_document
from studio.backend import HardwareBackend

ROOT = Path(__file__).resolve().parents[1]

class PressureAndAuditTests(unittest.TestCase):
    def test_pressure_sweep_rejected_at_any_depth_and_disabled(self):
        for enabled in (True, False):
            n = node('sweep', axis='pressure', values=[4., 6.])
            n['enabled'] = enabled
            n['children'] = [node('resistance')]
            outer = node('repeat'); outer['children'] = [n]
            with self.assertRaisesRegex(ValueError, '气压扫描已移除'):
                validate([outer], CONFIG)
            d = dict(schema_version=6, config=copy.deepcopy(CONFIG), nodes=[outer])
            snapshot = copy.deepcopy(d)
            with self.assertRaisesRegex(ValueError, '气压扫描已移除'):
                upgrade_document(d)
            self.assertEqual(d, snapshot)

    def test_thermal_pressure_management_and_manual_control_retained(self):
        self.assertNotIn('auto_pressure',node('temperature')['params'])
        validate([node('pressure'), node('temperature')], dict(CONFIG,auto_pressure_enabled=False))

    def test_original_ac_driver_emits_fixed_trigger_commands(self):
        path = ROOT/'legacy/keithely6221_2182A/keithley_6221_2182a_driver.py'
        spec = importlib.util.spec_from_file_location('audit_legacy_source', path)
        driver = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'pyvisa': types.ModuleType('pyvisa'), 'audit_legacy_source': driver}):
            spec.loader.exec_module(driver)
        source = driver.Keithley6221.__new__(driver.Keithley6221)
        source.write = Mock()
        source.configure_ac(freq=13., amplitude=10e-6)
        commands = [call.args[0] for call in source.write.call_args_list]
        for command in ['SOUR:WAVE:FUNC SIN', 'SOUR:WAVE:OFFS 0',
                        'SOUR:WAVE:PMAR:STAT ON', 'SOUR:WAVE:PMAR 0',
                        'SOUR:WAVE:PMAR:OLIN 1']:
            self.assertIn(command, commands)

    def test_pxi_gui_line_and_driver_edge_agree_with_adapter(self):
        gui = ast.parse((ROOT/'legacy/measureGUI/teslatronpt_mr_ac_gui.py').read_text(encoding='utf-8-sig'))
        line = next(ast.literal_eval(n.value) for n in gui.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id=='DEFAULT_MARKER_LINE' for t in n.targets))
        self.assertEqual(line, 1)
        tree = ast.parse((ROOT/'legacy/pxi4462/pxi4462_lockin.py').read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=='Pxi4462Lockin')
        init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name=='__init__')
        defaults = dict(zip([a.arg for a in init.args.args][-len(init.args.defaults):], init.args.defaults))
        for key,value in [('terminal','diff'),('coupling','dc'),('trigger_terminal','PFI0'),('trigger_edge','falling')]:
            self.assertEqual(ast.literal_eval(defaults[key]), value)

    def test_delta_keeps_original_voltage_without_extra_divisor(self):
        b = HardwareBackend(copy.deepcopy(CONFIG), lambda x:None)
        b.src = Mock()
        b.src.run_delta_once.return_value = {'readings':[.002], 'actual_count':1}
        p = node('resistance')['params']
        result = b.measure('resistance', p, 1e-5, lambda seconds:None)
        self.assertEqual(result['voltage_v'], .002)
        self.assertEqual(result['resistance_ohm'], .002/1e-5)
        args = b.src.run_delta_once.call_args.kwargs
        self.assertEqual(args['high'], 1e-5); self.assertEqual(args['low'], -1e-5)
        self.assertEqual(args['delta_unit'], 'V')
        self.assertTrue(args['configure_2182'])
        self.assertFalse(args['reset_before'])
        self.assertEqual(args['nplc'], p['nplc'])
        b.src.write.assert_not_called()


    def test_driver_delta_voltage_and_resistance_match_manual(self):
        path = ROOT/'legacy/keithely6221_2182A/keithley_6221_2182a_driver.py'
        spec = importlib.util.spec_from_file_location('voltage_audit_driver', path)
        driver = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'pyvisa': types.ModuleType('pyvisa'), 'voltage_audit_driver': driver}):
            spec.loader.exec_module(driver)
        source = driver.Keithley6221.__new__(driver.Keithley6221)
        for name in ('clear_errors', 'setup_delta', 'arm_delta', 'start_delta',
                     'disarm_delta', 'trace_data_type', 'verify_delta_settings', 'check_errors',
                     'source_health', 'write', 'query', 'trace_feed', 'trace_clear', 'output_off'):
            setattr(source, name, Mock())
        source.wait_for_sweep_done = Mock(return_value=True)
        # Manual three-reading algorithm for 100 ohms, +/-10 uA: 1 mV.
        source.trace_data = Mock(return_value='0.001')
        result = source.run_delta_once(high=10e-6, low=-10e-6, delta_unit='V')
        self.assertEqual(result['readings'], [.001])
        self.assertEqual(result['voltage_delta'], .001)
        self.assertAlmostEqual(result['resistance'], 100.)
        source.trace_data.return_value = '100'
        result = source.run_delta_once(high=10e-6, low=-10e-6, delta_unit='OHMS')
        self.assertEqual(result['resistance'], 100.)
        self.assertIsNone(result['voltage_delta'])
