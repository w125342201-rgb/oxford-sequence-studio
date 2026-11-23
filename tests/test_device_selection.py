"""Device routing regressions; protocol substitutes are not hardware validation."""
import builtins
import copy
import csv
import json
import sys
import tempfile
import tkinter as tk
import types
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from studio.backend import HardwareBackend,SimBackend
from studio.engine import Engine
from studio.gui import App
from studio.instrument_options import display_value
from studio.model import (CONFIG,VERSION,node,set_measurement_device,
                          validate_device_selection,upgrade_document)


def nested_plan():
    loop=node('repeat',count=1)
    loop['children']=[node('resistance',mode='pxi',phase2_deg=37,daq_range_v=1.,settle_s=0.)]
    disabled=node('repeat',count=2);disabled['enabled']=False
    disabled['children']=[node('resistance',mode='pxi',nplc=2.,harmonic_order=3)]
    return [node('temperature',stable_s=127.),loop,disabled]


class DeviceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tk=tk.Tcl()

    def app(self):
        """Exercise production callbacks with real Tcl variables, without a display."""
        app=types.SimpleNamespace(config_data=copy.deepcopy(CONFIG),nodes=nested_plan(),
            connected=False,busy=False,dirty=False,refresh_tree=Mock(),emit=Mock(),
            device=tk.StringVar(self.tk,value=display_value('measurement_device','2182a')),
            mode=tk.StringVar(self.tk,value='真实硬件'),kind=tk.StringVar(self.tk,value=''),
            progress=tk.StringVar(self.tk),device_box=Mock(),kind_box=Mock(),submit=Mock(),
            idle_required=lambda:True,sync_plot_mode=Mock())
        app.sync_device_options=lambda:App.sync_device_options(app)
        app.select_device=lambda:App.select_device(app)
        app.sync_matrix_options=app.sync_device_options
        return app

    def test_explicit_selection_converts_nested_and_disabled_steps_losslessly(self):
        nodes=nested_plan();original=copy.deepcopy(nodes);c=copy.deepcopy(CONFIG)
        self.assertEqual(set_measurement_device(nodes,c,'2182a'),2)
        self.assertEqual(nodes[0],original[0])
        for index in (1,2):
            p=nodes[index]['children'][0]['params']
            self.assertEqual(p,dict(original[index]['children'][0]['params'],mode='delta'))
        validate_device_selection(nodes,c)
        self.assertEqual(set_measurement_device(nodes,c,'pxi'),2)
        self.assertEqual(nodes,original)
        before=copy.deepcopy(c)
        with self.assertRaises(ValueError):set_measurement_device(nodes,c,'unknown')
        self.assertEqual(c,before);self.assertEqual(nodes,original)

    def test_ui_selection_updates_config_tree_and_log(self):
        app=self.app();App.select_device(app)
        validate_device_selection(app.nodes,app.config_data)
        self.assertTrue(app.dirty);app.refresh_tree.assert_called_once()
        self.assertIn('切换 2 个',app.progress.get())
        self.assertIn('2182A',app.emit.call_args.args[1])
        app.device.set(display_value('measurement_device','pxi'));App.select_device(app)
        self.assertEqual(app.config_data['measurement_device'],'pxi')
        validate_device_selection(app.nodes,app.config_data)

    def test_connect_reads_visible_choice_even_if_tk_event_was_missed(self):
        app=self.app();app.config_data['measurement_device']='pxi'
        App.connect(app)
        args=app.submit.call_args.args
        self.assertEqual(args[0],'connect');self.assertFalse(args[1][0])
        self.assertEqual(args[1][1]['measurement_device'],'2182a')
        validate_device_selection(app.nodes,args[1][1])
        app.config_data['measurement_device']='pxi'
        self.assertEqual(args[1][1]['measurement_device'],'2182a')

    def test_connected_or_busy_choice_is_reverted(self):
        for state in ('connected','busy'):
            app=self.app();setattr(app,state,True);before=copy.deepcopy(app.nodes)
            app.device.set(display_value('measurement_device','pxi'))
            with patch('studio.gui.messagebox.showinfo') as info:App.select_device(app)
            self.assertEqual(app.config_data['measurement_device'],'2182a')
            self.assertEqual(app.device.get(),display_value('measurement_device','2182a'))
            self.assertEqual(app.nodes,before);info.assert_called_once()

    def test_loading_restores_saved_choice_then_explicit_switch_wins(self):
        app=self.app()
        saved=dict(schema_version=VERSION,config=dict(CONFIG,measurement_device='pxi'),nodes=nested_plan())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old_plan.json';path.write_text(json.dumps(saved),encoding='utf-8')
            with patch('studio.gui.filedialog.askopenfilename',return_value=str(path)),patch('studio.gui.messagebox.showerror') as error:
                App.load_sequence(app)
                error.assert_not_called()
        self.assertEqual(app.device.get(),display_value('measurement_device','pxi'))
        self.assertIn('恢复为',app.progress.get())
        app.device.set(display_value('measurement_device','2182a'));App.select_device(app)
        restored=upgrade_document(json.loads(json.dumps(dict(schema_version=VERSION,config=app.config_data,nodes=app.nodes))))
        validate_device_selection(restored['nodes'],restored['config'])
        self.assertEqual(restored['nodes'][1]['children'][0]['params']['phase2_deg'],37)

    def test_mismatched_plan_blocks_connect_before_queueing_io(self):
        app=self.app()
        with patch('studio.gui.messagebox.showerror') as error:App.connect(app)
        error.assert_called_once();app.submit.assert_not_called()
        self.assertIn('不符',error.call_args.args[1])

    def test_start_rejects_display_changed_after_connection(self):
        app=self.app();app.connected=True
        app.backend=SimBackend(app.config_data,lambda _:None)
        app.device.set(display_value('measurement_device','pxi'))
        with patch('studio.gui.messagebox.showerror') as error:App.start(app)
        self.assertIn('本次连接',error.call_args.args[1]);app.submit.assert_not_called()

    def test_backends_freeze_selection_and_reject_wrong_device_before_io(self):
        for cls in (SimBackend,HardwareBackend):
            for device,wrong_mode in (('2182a','pxi'),('pxi','delta')):
                c=dict(CONFIG,measurement_device=device);b=cls(c,lambda _:None)
                c['measurement_device']='pxi' if device=='2182a' else '2182a'
                self.assertEqual(b.measurement_device,device)
                b.src=Mock();b.daq=Mock();b.off=Mock()
                p=node('resistance',mode=wrong_mode)['params']
                with self.assertRaisesRegex(ValueError,'不符'):b.begin('resistance',p)
                with self.assertRaisesRegex(ValueError,'不符'):b.measure('resistance',p,1e-5,Mock())
                self.assertEqual(b.src.mock_calls,[]);self.assertEqual(b.daq.mock_calls,[])
                b.off.assert_not_called()

    def test_engine_rejects_mismatch_in_simulation_before_output_or_files(self):
        for device,mode in (('2182a','pxi'),('pxi','delta')):
            c=dict(CONFIG,measurement_device=device);b=SimBackend(c,lambda _:None)
            b.begin=Mock();e=Engine(b,c)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError,'不符'):e.run([node('resistance',mode=mode)],tmp)
                self.assertEqual(list(Path(tmp).iterdir()),[])
            b.begin.assert_not_called()
        # A different connection snapshot cannot be masked by matching nodes.
        c=copy.deepcopy(CONFIG);b=SimBackend(dict(CONFIG,measurement_device='pxi'),lambda _:None)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'已连接测量设备'):Engine(b,c).run([node('resistance')],tmp)
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_delta_connection_and_csv_never_access_available_or_absent_ni(self):
        for available in (False,True):
            for bad_first_reading in (False,True):
                with self.subTest(ni_available=available,bad_first_reading=bad_first_reading):
                    self.run_delta_with_ni_sentinel(available,bad_first_reading)

    def run_delta_with_ni_sentinel(self,available,bad_first_reading):
        ox=types.ModuleType('oxford.oxford_teslatronpt_api');ox.TeslatronPT=Mock();ox.ThermalRule=Mock()
        visa=types.ModuleType('pyvisa');visa.ResourceManager=Mock()
        drv=types.ModuleType('keithely6221_2182A.keithley_6221_2182a_driver');drv.Keithley6221=Mock()
        src=drv.Keithley6221.return_value;src.idn.return_value='KEITHLEY 6221';src.query.return_value='0'
        src.run_delta_once.side_effect=[TimeoutError('2182A timeout') if bad_first_reading else {'readings':[.001]}, {'readings':[-.002]}]
        ni=types.ModuleType('nidaqmx');ni.Task=Mock()
        system=types.ModuleType('nidaqmx.system');system.System=Mock()
        system.System.local.return_value.devices=[types.SimpleNamespace(product_type='PXI-4462',name='PXI1Slot2')]
        pxi=types.ModuleType('pxi4462.pxi4462_lockin');pxi.Pxi4462Lockin=Mock()
        modules={'oxford':types.ModuleType('oxford'),'oxford.oxford_teslatronpt_api':ox,
            'pyvisa':visa,'keithely6221_2182A':types.ModuleType('keithely6221_2182A'),
            'keithely6221_2182A.keithley_6221_2182a_driver':drv,
            'nidaqmx':ni if available else None,'nidaqmx.system':system if available else None,
            'pxi4462':types.ModuleType('pxi4462'),'pxi4462.pxi4462_lockin':pxi}
        original_import=builtins.__import__;attempted=[]
        def guarded_import(name,*args,**kwargs):
            if name.startswith('nidaqmx') or name=='pxi4462.pxi4462_lockin':
                attempted.append(name)
                raise AssertionError('2182A selection touched NI: '+name)
            return original_import(name,*args,**kwargs)
        c=copy.deepcopy(CONFIG);logs=[];b=HardwareBackend(c,logs.append)
        # Oxford environmental transport is outside the device-routing test.
        b.snapshot=lambda:dict(temperature=4.2,vti=4.,field=0.,pressure=4.,angle=0.,magnet_action='HOLD')
        b.service=lambda:None
        with patch.dict(sys.modules,modules),patch('builtins.__import__',side_effect=guarded_import),tempfile.TemporaryDirectory() as tmp:
            b.connect()
            e=Engine(b,c);e.wait=lambda _:e.check()
            result=e.run([node('resistance',samples=2,settle_s=0.)],tmp)
            self.assertEqual(result['status'],'completed',result)
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),2);self.assertFalse(any(k.startswith('ai') for k in rows[0]))
            self.assertEqual(rows[0]['voltage_v'],'NULL' if bad_first_reading else '0.001')
            self.assertEqual(float(rows[1]['voltage_v']),-.002)
            self.assertEqual(result['config']['measurement_device'],'2182a')
            self.assertIn('2182A Delta',(e.run_dir/'events.jsonl').read_text(encoding='utf-8'))
            self.assertIsNone(b.daq);b.close()
        self.assertEqual(attempted,[]);self.assertEqual(src.run_delta_once.call_count,2)
        src.configure_ac.assert_not_called();src.init_ac.assert_not_called()
        ni.Task.assert_not_called();system.System.local.assert_not_called();pxi.Pxi4462Lockin.assert_not_called()
        self.assertTrue(any('本次连接锁定测量设备：2182A' in s for s in logs))


if __name__=='__main__':unittest.main()
