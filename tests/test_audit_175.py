"""1.7.5 failure-path regressions; deterministic hardware boundaries only."""
import copy,csv,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from collections import deque
from unittest.mock import Mock,patch
from studio.model import CONFIG,node,validate,validate_config,pxi_run_params,upgrade_document,VERSION
from studio.backend import HardwareBackend,SimBackend
from studio.engine import Engine,Stopped
from studio.gui import App,Form,format_value,display_readings
import test_field_retry as field_retry
from test_reliability_173 import Wire,drv

class Audit175(unittest.TestCase):
    def config(self,**kw):return dict(copy.deepcopy(CONFIG),**kw)
    def events(self,e):return [json.loads(x) for x in (e.run_dir/'events.jsonl').read_text().splitlines()]
    def rows(self,e):
        with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
    def test_noop_driver_preserves_actual_field_and_strict_tolerance(self):
        pt=field_retry.FieldRetryTests().device(['HOLD','HOLD']);pt.target_field.return_value=.005
        result=pt.set_field_verified(.005,.1,arrival_tolerance_t=.0002,wait=Mock())
        self.assertTrue(result['no_op_ramp']);self.assertFalse(result['arrival_verified'])
        self.assertEqual(result['field_readback_t'],0);self.assertEqual(result['rtos_attempts'],1)
        self.assertEqual(pt.ips.set_action.call_count,1)
        self.assertEqual(pt.ips.set_action.call_args.args[-1],'RTOS')
    def test_noop_outside_tolerance_waits_without_replaying_command(self):
        c=self.config();b=SimBackend(c,lambda _:None)
        b.set_environment=Mock(return_value=dict(started=False,no_op_ramp=True,field_readback_t=0,fset_readback_t=.005,rtos_attempts=1))
        e=Engine(b,c);e.wait=lambda _:e.stop.set()
        with tempfile.TemporaryDirectory() as out:
            r=e.run([node('field',target=.005,tolerance=.0002),node('resistance')],out)
            self.assertEqual(r['status'],'stopped');self.assertEqual(r['points'],0)
            b.set_environment.assert_called_once()
            self.assertTrue(any(x['event']=='field_not_started' for x in self.events(e)))
    def test_noop_inside_measurement_tolerance_can_complete(self):
        c=self.config()
        class B(SimBackend):
            def set_environment(self,axis,target,p):
                super().set_environment(axis,target,p)
                return dict(no_op_ramp=True,field_readback_t=target,started=False)
        e=Engine(B(c,lambda _:None),c)
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(e.run([node('field',target=.005,stable_s=0)],out)['status'],'completed')
    def hardware_rotator(self):
        c=self.config(auto_pressure_enabled=False,enable_rotator=True)
        b=HardwareBackend(c,lambda _:None);b.rot=Mock()
        b.pt=Mock();b.pt.magnet_action.return_value='HOLD'
        b.pt.readings.return_value=SimpleNamespace(probe_temperature_k=4,vti_temperature_k=3.8,field_t=0,pressure_mbar=3)
        b.rotation_target=1.;b.rotation_tolerance=.05;b.rotation_until=0
        return b,c
    def test_angle_mismatch_preserves_environment_without_repeated_query(self):
        b,c=self.hardware_rotator();b.rot.read_deg.return_value=.45
        for _ in range(4):b.service();self.assertEqual(b.snapshot()['temperature'],4)
        self.assertEqual(b.rot.read_deg.call_count,1);self.assertIsNotNone(b.rotation_error)
        # W-008：无人工复核/解除入口，目标不会被自动清除，采集与切电极保持禁止
        self.assertEqual(b.rotation_target,1.)
        with self.assertRaises(RuntimeError):b.ensure_rotation_idle()
        self.assertIn('停止后检查',b.snapshot()['angle_status'])
        self.assertEqual(b.angle_cache,.45)
    def test_angle_read_failure_does_not_disable_monitoring(self):
        b,c=self.hardware_rotator();b.rot.read_deg.side_effect=TimeoutError('serial timeout')
        b.service();b.service();self.assertEqual(b.rot.read_deg.call_count,1)
        self.assertEqual(b.snapshot()['field'],0);self.assertIsNotNone(b.rotation_error)
    def test_disabled_operational_limits_and_conflicts_are_ignored(self):
        c=self.config()
        for n in (node('pressure'),node('temperature',target=400),node('resistance',current_a=-1)):
            n['enabled']=False;validate([n,node('wait',seconds=0)],c)
            n['enabled']=True
            with self.assertRaises(ValueError):validate([n,node('wait',seconds=0)],c)
        parent=node('repeat');parent['enabled']=False;parent['children']=[node('pressure')]
        validate([parent,node('wait',seconds=0)],c)
        parent['children'][0]['params']['unknown']=1
        with self.assertRaises(ValueError):validate([parent,node('wait',seconds=0)],c)
    def test_long_wait_has_periodic_structured_events(self):
        c=self.config();b=SimBackend(c,lambda _:None);e=Engine(b,c);b.set_environment=Mock();clock=[0.]
        def wait(_):
            clock[0]+=61
            if clock[0]>=183:b.env['field']=1
        e.wait=wait
        with tempfile.TemporaryDirectory() as out,patch('studio.engine.time.monotonic',side_effect=lambda:clock[0]):
            self.assertEqual(e.run([node('field',target=1,stable_s=0)],out)['status'],'completed')
            waits=[x['data'] for x in self.events(e) if x['event']=='environment_wait']
            self.assertEqual([x['elapsed_s'] for x in waits],[0,61,122,183])
            self.assertTrue(all(x['axis']=='field' and x['path'] for x in waits))
    def test_preacquisition_io_wait_is_logged_and_never_skips_point(self):
        c=self.config();b=SimBackend(c,lambda _:None);original=b.snapshot
        b.snapshot=Mock(side_effect=[TimeoutError('offline'),original(),original()]);b.measure=Mock(wraps=b.measure)
        e=Engine(b,c);e.stop.wait=Mock()
        with tempfile.TemporaryDirectory() as out:
            result=e.run([node('resistance',settle_s=0)],out)
            self.assertEqual(result['status'],'completed');self.assertEqual(result['points'],1)
            self.assertEqual(b.measure.call_count,1)
            waits=[x for x in self.events(e) if x['event']=='io_wait'];self.assertEqual(len(waits),1)
            self.assertEqual(waits[0]['data']['label'],'环境回读')
    def test_off_requeries_but_never_ignores_command_failure(self):
        b=HardwareBackend(self.config(),lambda _:None);b.src=Mock()
        b.src.query.side_effect=['', '0.0']
        with patch('studio.backend.time.sleep'):b.off()
        self.assertEqual(b.src.query.call_count,2)
        b.src.query.side_effect=['0V','garbage','1']
        with patch('studio.backend.time.sleep'),self.assertRaises(RuntimeError):b.off()
        b.src.query.side_effect=['0'];b.src.output_off.side_effect=RuntimeError('write failed')
        with self.assertRaisesRegex(RuntimeError,'write failed'):b.off()
    def test_inactive_harmonic_phase_does_not_block_pxi(self):
        a=node('resistance',mode='pxi',harmonic_order=3,phase2_deg=0)
        b=node('resistance',mode='pxi',harmonic_order=3,phase2_deg=-90)
        self.assertEqual(pxi_run_params([a,b])['harmonic_order'],3)
        b['params']['phase3_deg']=-90
        with self.assertRaises(ValueError):pxi_run_params([a,b])
    def test_form_hidden_text_is_not_parsed(self):
        params=node('resistance',mode='pxi')['params']
        from studio.instrument_options import OPTIONS,display_value
        fields={k:Mock(get=Mock(return_value=display_value(k,v) if k in OPTIONS else v if isinstance(v,bool) else format_value(v))) for k,v in params.items()}
        fields['nplc'].get.return_value='garbage invisible text'
        form=SimpleNamespace(params=params,fields=fields,validator=None,destroy=Mock())
        with patch('studio.gui.messagebox.showerror') as error:Form.save(form)
        error.assert_not_called();self.assertEqual(form.result['nplc'],params['nplc']);form.destroy.assert_called_once()
    def test_explicit_counts_and_incomplete_environment(self):
        c=self.config(enable_matrix=True,matrix_each_point=True,matrix_off_delay_s=0,matrix_settle_s=0)
        b=SimBackend(c,lambda _:None);e=Engine(b,c)
        def emit(k,v):
            if k=='point':e.stop.set()
        e.emit=emit
        with tempfile.TemporaryDirectory() as out:
            result=e.run([node('resistance',samples=2,settle_s=0)],out);row=self.rows(e)[0]
            self.assertEqual((result['acquisition_count'],result['csv_row_count']),(1,1))
            self.assertEqual(row['environment_status'],'incomplete')
            self.assertEqual(row['route1_environment_status'],'ok')
            self.assertEqual(row['route1_acquisition_index'],'1');self.assertEqual(row['route2_acquisition_index'],'')
    def test_plot_work_is_bounded_to_recent_3000(self):
        data=deque((dict(voltage_v=1,route='A') for _ in range(20000)),maxlen=20000)
        a=SimpleNamespace(data=data,sync_plot_mode=lambda:False,harmonic=Mock(get=lambda:'一次谐波 1f'),component=Mock(get=lambda:'X'),group=Mock(get=lambda:'全部'),groups={},latest_voltages=Mock(),canvas=Mock())
        a.canvas.winfo_width.return_value=100;a.canvas.winfo_height.return_value=50
        with patch('studio.gui.display_readings',wraps=display_readings) as reads:App.draw(a)
        self.assertLessEqual(reads.call_count,3001)
    def test_delta_runs_1017_flow_without_serial_round_trips(self):
        # 2026-11-07 user decision: delta execution restored verbatim to the
        # proven 1017-era flow (261031buckup snapshot). The 2182A is only ever
        # WRITTEN over the 6221 serial link (NPLC/RANG configuration); no
        # serial-link round-trip queries -- their variable ~4 s latency on
        # real hardware failed every run of the 2026-11-06 sequence at the first point.
        s=Wire();r=s.run_delta_once(high=1e-5,low=-1e-5,reset_before=False)
        self.assertEqual(r['readings'],[.001]);self.assertAlmostEqual(r['voltage_delta'],.001)
        self.assertAlmostEqual(r['resistance'],100.)
        self.assertIn(('write',':SOUR:DELT:ARM'),s.commands)
        self.assertTrue(any(op=='write' and 'SENS:VOLT:NPLC' in cmd and 'SERIAL:SEND' in cmd for op,cmd in s.commands))
        self.assertFalse(any('SERIAL:SEND' in cmd and cmd.split('SEND')[-1].strip('"').rstrip().endswith('?') for op,cmd in s.commands))
    def test_delta_cache_invalidation_at_session_boundaries(self):
        b=HardwareBackend(self.config(),lambda _:None);b.src=Mock();b.src._delta_2182_cache=(5,.1)
        b.invalidate_measurement_cache();self.assertIsNone(b.src._delta_2182_cache)
    def test_temperature_cap_and_old_config_upgrade(self):
        with self.assertRaises(ValueError):validate_config(self.config(max_temperature_rate=150))
        c=self.config();c.pop('rotation_margin_s')
        upgraded=upgrade_document(dict(schema_version=VERSION,config=c,nodes=[node('wait')]))
        self.assertEqual(upgraded['config']['rotation_margin_s'],1.5);validate(upgraded['nodes'],upgraded['config'])
    def test_manual_rejects_empty_output_path(self):
        app=SimpleNamespace(connected=True,idle_required=lambda:True,config_data=self.config(),backend=SimpleNamespace(simulated=True),output=Mock(get=lambda:'  '),submit=Mock())
        with patch('studio.gui.Form',return_value=SimpleNamespace(result=node('temperature')['params'])),patch('studio.gui.messagebox.showerror') as err:
            App.manual(app,'temperature')
        app.submit.assert_not_called();err.assert_called_once();self.assertIn('数据目录',err.call_args.args[1])
