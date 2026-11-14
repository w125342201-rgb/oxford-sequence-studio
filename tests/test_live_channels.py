import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from studio.gui import App,display_readings

class LiveChannelTests(unittest.TestCase):
    def test_four_separate_traces_and_filter(self):
        rows=[dict(point=i,path='sweep1',route='直接接线',kind='resistance',voltage_v=999.,
            channels={str(ch):dict(v_peak_v=(ch+1)*i*1e-6,x_v=-(ch+1)*i*1e-6) for ch in range(4)}) for i in (1,2)]
        canvas=Mock();canvas.winfo_width.return_value=1000;canvas.winfo_height.return_value=300
        app=SimpleNamespace(sync_plot_mode=lambda:True,data=rows,canvas=canvas,xaxis=Mock(),group=Mock(),latest_voltages=Mock())
        app.xaxis.get.return_value='point';app.group.get.return_value='全部'
        App.draw(app)
        traces=[c for c in canvas.create_line.call_args_list if c.kwargs.get('width')==1.5]
        self.assertEqual(len(traces),4)
        self.assertEqual(len({c.kwargs['fill'] for c in traces}),4)
        latest=app.latest_voltages.set.call_args.args[0]
        for ch in range(4):self.assertIn(f'ai{ch}:',latest)
        all_colors={c.kwargs['fill'] for c in traces}
        canvas.reset_mock();app.group.get.return_value='ai2';App.draw(app)
        traces=[c for c in canvas.create_line.call_args_list if c.kwargs.get('width')==1.5]
        self.assertEqual(len(traces),1)
        self.assertIn(traces[0].kwargs['fill'],all_colors)

    def test_legacy_names_and_delta(self):
        self.assertEqual(list(display_readings(dict(channels={'Dev1/ai2':{'v_peak_v':2.,'x_v':-2.}},route='直接接线'))),[('ai2',-2.)])
        self.assertEqual(list(display_readings(dict(voltage_v=3.,route='电极A'))),[('电极A',3.)])

    def test_select_second_harmonic_and_channel(self):
        rows=[dict(point=i,path='sweep',route='直接接线',kind='resistance',
            channels={str(ch):dict(v_peak_v=(ch+1)*i*1e-6,x_v=(ch+1)*i*1e-6,v2_peak_v=(ch+1)*i*1e-8,x2_v=-(ch+1)*i*1e-8) for ch in range(4)}) for i in (1,2)]
        canvas=Mock();canvas.winfo_width.return_value=1000;canvas.winfo_height.return_value=300
        app=SimpleNamespace(sync_plot_mode=lambda:True,data=rows,canvas=canvas,xaxis=Mock(),group=Mock(),harmonic=Mock(),latest_voltages=Mock())
        app.xaxis.get.return_value='point';app.harmonic.get.return_value='二次谐波 2f'
        for ch in range(4):
            app.group.get.return_value=f'ai{ch}';canvas.reset_mock();App.draw(app)
            self.assertEqual(len([c for c in canvas.create_line.call_args_list if c.kwargs.get('width')==1.5]),1)
            latest=app.latest_voltages.set.call_args.args[0]
            self.assertIn(f'ai{ch}: {-(ch+1)*2e-8:.6g} V',latest)
            self.assertEqual(latest.count('ai'),1)
        app.group.get.return_value='全部';canvas.reset_mock();App.draw(app)
        self.assertEqual(len([c for c in canvas.create_line.call_args_list if c.kwargs.get('width')==1.5]),4)
