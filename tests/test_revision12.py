import ast
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from studio.model import CONFIG, node, validate, validate_config, upgrade_document
from studio.instrument_options import OPTIONS, display_value, stored_value, wiring_hint
from studio.backend import HardwareBackend

ROOT=Path(__file__).resolve().parents[1]

class RevisionTests(unittest.TestCase):
    def test_all_label_value_roundtrips(self):
        for key,options in OPTIONS.items():
            self.assertEqual(len(options),len(set(label for label,_ in options)))
            for label,value in options:
                self.assertEqual(display_value(key,value),label)
                self.assertEqual(stored_value(key,label),value)


    def test_fixed_ranges_and_invalid_json(self):
        for key in ('voltage_range_v','daq_range_v'):
            for _,v in OPTIONS[key]:validate([node('resistance',**{key:v})],CONFIG)
            for v in (.123,'auto',True):
                with self.assertRaises(ValueError):validate([node('resistance',**{key:v})],CONFIG)
        validate([node('resistance',nplc=2.7,frequency_hz=13.17)],CONFIG)

    def test_delta_range_reaches_adapter_unchanged(self):
        for _,v in OPTIONS['voltage_range_v']:
            b=HardwareBackend(copy.deepcopy(CONFIG),lambda m:None)
            b.src=Mock();b.src.run_delta_once.return_value={'readings':[.001]}
            b.measure('resistance',node('resistance',voltage_range_v=v)['params'],1e-5,lambda x:None)
            self.assertEqual(b.src.run_delta_once.call_args.kwargs['voltage_range'],v)


    def test_pxi_ranges_and_channels_reach_adapter_unchanged(self):
        import types
        for _,v in OPTIONS['daq_range_v']:
            module=types.ModuleType('pxi4462.pxi4462_lockin');module.Pxi4462Lockin=Mock()
            b=HardwareBackend(dict(copy.deepcopy(CONFIG),measurement_device='pxi'),lambda m:None)
            b.src=Mock();b.off=Mock();b.ensure_rotation_idle=Mock()
            with patch.dict('sys.modules',{'pxi4462':types.ModuleType('pxi4462'),'pxi4462.pxi4462_lockin':module}):
                b.begin('resistance',node('resistance',mode='pxi',daq_range_v=v,daq_channels=[0,1,2,3])['params'])
            args=module.Pxi4462Lockin.call_args.kwargs
            self.assertEqual(args['voltage_range_v'],v)
            self.assertEqual(args['channels'],[0,1,2,3])
            self.assertEqual(args['trigger_terminal'],'PFI0')
            self.assertEqual(args['trigger_edge'],'falling')
            self.assertEqual(b.src.configure_ac.call_args.kwargs['marker_line'],1)

    def test_backend_rejects_non_gpib_before_hardware_imports(self):
        b=HardwareBackend(dict(CONFIG,matrix_resource='COM3'),lambda m:None)
        with self.assertRaises(ValueError):b.connect()

    def test_gpib_only(self):
        self.assertNotIn('matrix_backend',CONFIG);self.assertNotIn('matrix_port',CONFIG)
        for value in ['ASRL3::INSTR','TCPIP0::host::INSTR','COM3','GPIB0::31::INSTR','GPIB0::1::99::INSTR']:
            with self.assertRaises(ValueError):validate_config(dict(CONFIG,matrix_resource=value))
        for value in ['GPIB0::20::INSTR','GPIB::0::INSTR','GPIB1::20::2::INSTR']:
            validate_config(dict(CONFIG,matrix_resource=value))

    def test_migration_preserves_routes_and_address(self):
        old=dict(schema_version=2,config=dict(copy.deepcopy(CONFIG),matrix_backend='visa',matrix_port='COM9',matrix_resource='GPIB1::17::INSTR'),nodes=[node('resistance')])
        new=upgrade_document(old)
        self.assertEqual(new['schema_version'],7)
        self.assertEqual(new['config']['matrix_resource'],'GPIB1::17::INSTR')
        self.assertEqual(new['config']['point_routes'],old['config']['point_routes'])
        self.assertIn('matrix_backend',old['config'])
        self.assertEqual(upgrade_document(new),new)

    def test_serial_migration_never_silently_changes_wiring(self):
        for enabled in [True,False]:
            old=dict(schema_version=2,config=dict(CONFIG,matrix_backend='serial',matrix_port='COM9',enable_matrix=enabled),nodes=[node('resistance')])
            with self.assertRaisesRegex(ValueError,'不能自动迁移'):upgrade_document(old)


    def test_all_shipped_examples_migrate(self):
        for path in (ROOT/'examples').glob('*.json'):
            d=upgrade_document(json.loads(path.read_text(encoding='utf-8')))
            validate(d['nodes'],d['config'])

class LayoutTests(unittest.TestCase):
    def test_main_and_forms_at_common_sizes_and_scalings(self):
        import tkinter as tk
        try:
            probe=tk.Tk();probe.destroy()
        except tk.TclError as e:self.skipTest('无可用显示环境，未完成UI实绘验证: '+str(e))
        from studio.gui import App,Form,RouteEditor
        app=App()
        def descendants(w):
            for child in w.winfo_children():
                yield child
                yield from descendants(child)
        try:
            for scale in [1.,1.25,1.5,2.]:
                app.tk.call('tk','scaling',96/72*scale)
                for width,height in [(1024,768),(1366,768),(1920,1080)]:
                    app.geometry(f'{width}x{height}');app.update()
                    split=next(w for w in descendants(app) if w.winfo_class()=='TPanedwindow' and str(w.cget('orient'))=='vertical')
                    split.sashpos(0,int(split.winfo_height()*.55));app.update()
                    stop=next(w for w in descendants(app) if w.winfo_class()=='TButton' and w.cget('text')=='停止运行')
                    self.assertFalse(hasattr(app,'yaxis'))
                    self.assertGreater(app.canvas.winfo_height(),100)
                    self.assertLessEqual(app.canvas.winfo_rooty()+app.canvas.winfo_height(),app.winfo_rooty()+app.winfo_height())
                    self.assertLessEqual(stop.winfo_rooty()+stop.winfo_height(),app.winfo_rooty()+app.winfo_height())
                    self.assertLessEqual(stop.winfo_rootx()+stop.winfo_width(),app.winfo_rootx()+app.winfo_width())
            failures=[]
            def inspect_dialog():
                for dialog in [w for w in app.winfo_children() if isinstance(w,tk.Toplevel)]:
                    dialog.update_idletasks()
                    for w in descendants(dialog):
                        if w.winfo_class()=='TButton' and w.cget('text') in ('应用设置','确定并关闭'):
                            if w.winfo_rooty()+w.winfo_height()>dialog.winfo_rooty()+dialog.winfo_height():failures.append(w.cget('text'))
                    dialog.destroy()
            for params in [CONFIG,node('resistance')['params'],dict(order='temperature, angle, field, electrodes',temperature_values=[4.],angle_values=[0.,1.],field_values=[0.,.01],routes=CONFIG['point_routes'],current_a=1e-6)]:
                app.after(250,inspect_dialog);Form(app,'布局测试',params)
            app.after(250,inspect_dialog);RouteEditor(app,CONFIG['point_routes'])
            self.assertEqual(failures,[])
        finally:app.destroy()

if __name__=='__main__':unittest.main()
