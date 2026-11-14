"""Recovery, manual acceptance, partial environments and durable-count regressions."""
import copy,csv,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from studio.model import CONFIG,node
from studio.engine import Engine,Stopped
from studio.backend import HardwareBackend,SimBackend,MeasurementError
from legacy.oxford.oxford_teslatronpt_api import normalize_action,TeslatronPT,MercuryInvalidResponseError,MercuryCommunicationError
from audit.recover_points import recover
import test_reliability_173 as reliability

class Audit176(unittest.TestCase):
    def setup_engine(self,**config):
        c=dict(copy.deepcopy(CONFIG),**config);b=SimBackend(c,lambda _:None);e=Engine(b,c)
        e.stop.wait=Mock();return e,b
    def run_in(self,e,nodes,check):
        with tempfile.TemporaryDirectory() as out:
            r=e.run(nodes,out)
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            events=[json.loads(x) for x in (e.run_dir/'events.jsonl').read_text().splitlines()]
            check(r,rows,events)
    def test_field_io_recovers_without_replaying(self):
        e,b=self.setup_engine();b.set_environment=Mock(side_effect=MercuryCommunicationError('closed connection'))
        def wait(_):b.env['field']=.005
        e.wait=wait
        def check(r,rows,events):
            self.assertEqual(r['status'],'completed');self.assertEqual(len(rows),1);b.set_environment.assert_called_once()
        self.run_in(e,[node('field',target=.005,tolerance=.0002,stable_s=0),node('resistance',settle_s=0)],check)
    def test_field_configuration_error_still_fails(self):
        e,b=self.setup_engine();b.set_environment=Mock(side_effect=RuntimeError('not connected'))
        self.run_in(e,[node('field')],lambda r,*_:self.assertEqual(r['status'],'failed'))
    def test_noop_recovers_with_actual_target(self):
        e,b=self.setup_engine();b.set_environment=Mock(return_value=dict(no_op_ramp=True,field_readback_t=0,rtos_attempts=1))
        e.wait=lambda _:b.env.update(field=.005)
        def check(r,rows,events):
            self.assertEqual(r['status'],'completed');self.assertEqual(float(rows[0]['field_t']),.005)
            self.assertEqual(rows[0]['environment_status'],'ok');b.set_environment.assert_called_once()
        self.run_in(e,[node('field',target=.005,tolerance=.0002,stable_s=0),node('resistance',settle_s=0)],check)
    def test_action_aliases_and_unknown(self):
        for raw,expected in [('CLAMP','CLMP'),('TO SET','RTOS'),('TOSET','RTOS'),('PAUSE','PAUS'),('TO ZERO','RTOZ'),('xRTOSx','UNKNOWN')]:
            self.assertEqual(normalize_action(raw),expected)
        e,b=self.setup_engine();b.snapshot=lambda:dict(b.env,magnet_action='CLAMP')
        self.run_in(e,[node('field',stable_s=0)],lambda r,*_:self.assertEqual(r['status'],'completed'))
    def test_optional_bad_pressure_is_partial(self):
        e,b=self.setup_engine(auto_pressure_enabled=False);b.env['pressure']=float('nan')
        def check(r,rows,events):
            self.assertEqual(r['status'],'completed');self.assertEqual(rows[0]['environment_status'],'partial')
            self.assertEqual(rows[0]['pressure_mbar'],'');self.assertTrue(float(rows[0]['voltage_v'])>0)
            self.assertIn('pressure',rows[0]['environment_error'])
        self.run_in(e,[node('resistance',settle_s=0)],check)
    def test_bad_control_channel_still_blocks(self):
        e,b=self.setup_engine();b.env['pressure']=float('nan');original=b.snapshot
        def snapshot():
            if e.read_failures>=2:e.stop.set()
            return dict(original(),pressure=float('nan'))
        b.snapshot=snapshot;b.measure=Mock()
        self.run_in(e,[node('resistance')],lambda r,*_:self.assertEqual(r['status'],'stopped'))
        b.measure.assert_not_called();self.assertGreaterEqual(e.read_failures,2)
    def test_previously_controlled_axis_remains_required(self):
        e,b=self.setup_engine(auto_pressure_enabled=False);e.required_environment.add('field');env=b.snapshot();env['field']=float('nan')
        with self.assertRaises(ValueError):e.clean_environment(env,require=True)
    def test_linked_vti_and_all_missing_cannot_be_ignored(self):
        e,b=self.setup_engine(auto_pressure_enabled=False);b.temp_target=4
        env=b.snapshot();env['vti']=None
        with self.assertRaises(ValueError):e.clean_environment(env,require=True)
        b.temp_target=None;env={k:None for k in ('temperature','vti','field','pressure','angle')}
        with self.assertRaises(ValueError):e.clean_environment(env,require=True)
    def test_per_signal_invalid_response_does_not_mask_transport_loss(self):
        p=TeslatronPT.__new__(TeslatronPT)
        for name in ('vti_temperature','probe_temperature','field','target_field','field_ramp_rate'):setattr(p,name,Mock(return_value=1.))
        p.pressure=Mock(side_effect=MercuryInvalidResponseError('bad pressure'))
        e,b=self.setup_engine(auto_pressure_enabled=False);r=p.readings(allow_partial=True)
        self.assertEqual(r.field_t,1.)
        with self.assertRaises(MercuryInvalidResponseError):p.readings()
        p.pressure.side_effect=MercuryCommunicationError('connection lost')
        with self.assertRaises(MercuryCommunicationError):p.readings(allow_partial=True)
    def test_invalid_temperature_never_drives_thermal_controls(self):
        b=HardwareBackend(copy.deepcopy(CONFIG),lambda _:None);b.pt=Mock();b.pt.probe_temperature.return_value=float('nan')
        with self.assertRaises(ValueError):b.service()
        b.pt.set_pressure.assert_not_called();b.pt.enforce_thermal_link.assert_not_called()
    def test_csv_failure_keeps_acquisition_but_not_committed_row_count(self):
        for failure in ('write','fsync'):
            e,b=self.setup_engine();original=e.record_table
            def record(row):
                if failure=='write':
                    e.writer=Mock();e.writer.writerow.side_effect=OSError('disk full');original(row)
                else:
                    with patch('studio.engine.os.fsync',side_effect=OSError('flush failure')):original(row)
            e.record_table=record
            def check(r,rows,events):
                self.assertEqual(r['status'],'failed');self.assertEqual(r['acquisition_count'],1);self.assertEqual(r['csv_row_count'],0)
                self.assertTrue(any(x['event']=='acquisition' for x in events))
            self.run_in(e,[node('resistance',settle_s=0)],check)
    def test_invalid_streak_alerts_and_valid_point_resets_streak(self):
        e,b=self.setup_engine();original=b.measure;counter=[0];quality=[]
        def measure(*args):
            counter[0]+=1
            if counter[0]<=10:raise MeasurementError('bad trace')
            return original(*args)
        b.measure=measure;e.emit=lambda k,v:quality.append(v) if k=='quality' else None
        def check(r,rows,events):
            self.assertEqual(r['status'],'completed');self.assertEqual(r['points'],11);self.assertEqual(r['invalid_points'],10)
            self.assertTrue(any(x['event']=='invalid_streak' for x in events));self.assertEqual(quality[-1],dict(total=10,consecutive=0))
        self.run_in(e,[node('resistance',samples=11,settle_s=0,interval_s=0)],check)
    def test_verify_setting_requeries_only_and_remains_fatal(self):
        s=reliability.Wire();q=Mock(side_effect=['garbage','2','1']);s.verification_log=Mock()
        self.assertEqual(s.verify_number('TEST?',1,query=q),1);self.assertEqual(q.call_count,3);self.assertFalse(s.commands)
        s.verification_log.assert_called_once()
        with self.assertRaises(reliability.drv.Keithley6221Error):s.verify_number('TEST?',1,query=Mock(return_value='2'))
    def test_source_compliance_still_fatal_without_retry(self):
        s=reliability.Wire();s.query=Mock(side_effect=['8','0','16'])
        with self.assertRaises(reliability.drv.Keithley6221Error):s.source_health()
        self.assertEqual(s.query.call_count,3)
    def test_close_disconnects_relays_only_after_verified_off(self):
        for failed in (False,True):
            b=HardwareBackend(copy.deepcopy(CONFIG),lambda _:None);b.src=Mock();matrix=b.matrix=Mock()
            b.off=Mock(side_effect=RuntimeError('unknown output') if failed else None)
            with patch('studio.backend.time.sleep'):errors=b.close()
            if failed:matrix.open_all.assert_not_called();self.assertTrue(errors)
            else:matrix.open_all.assert_called_once();self.assertFalse(errors)
            matrix.close.assert_called_once()
    def test_raw_pre_errors_remain_in_events(self):
        e,b=self.setup_engine();h=HardwareBackend(e.c,lambda _:None);h.src=Mock()
        h.src.run_delta_once.return_value=dict(readings=[1e-6],pre_errors=['-241,history','0,ok']);b.measure=h.measure
        def check(r,rows,events):
            event=next(x for x in events if x['event']=='point')
            self.assertEqual(event['data']['raw']['pre_errors'][0],'-241,history')
        self.run_in(e,[node('resistance',settle_s=0)],check)
    def test_recovered_pxi_names_match_main_csv(self):
        e,b=self.setup_engine(measurement_device='pxi')
        with tempfile.TemporaryDirectory() as d:
            r=e.run([node('resistance',mode='pxi',settle_s=0)],d);self.assertEqual(r['status'],'completed')
            target=Path(d)/'recovered.csv';recover(e.run_dir/'events.jsonl',target)
            with target.open(encoding='utf-8-sig') as f:row=next(csv.DictReader(f))
            self.assertIn('ai0_2f_V',row);self.assertIn('ai0_V',row);self.assertNotIn('ai0_v_peak_v',row)
    def test_manual_recovery_entries_are_removed(self):
        """D-023/W-008：四个手动入口删除后，不应留下任何可调用的残留。"""
        e,b=self.setup_engine()
        for name in ('environment_accept','wait_context','rotation_recheck'):
            self.assertFalse(hasattr(e,name),name)
        for name in ('accept_current_environment','_accept_environment_request'):
            self.assertFalse(hasattr(e,name),name)
        self.assertFalse(hasattr(b,'accept_stationary_angle'))
        self.assertFalse(hasattr(HardwareBackend(e.c,lambda _:None),'accept_stationary_angle'))
    def test_invalid_command_diagnostics_do_not_break_json_journal(self):
        e,b=self.setup_engine();b.set_environment=Mock(return_value=dict(field_readback_t=float('nan'),field_wait_reason='readback invalid'))
        def check(r,rows,events):
            self.assertEqual(r['status'],'completed')
            event=next(x for x in events if x['event']=='set_environment')
            self.assertIn('invalid_numeric_result',event['data']['result'])
        self.run_in(e,[node('field',target=0,stable_s=0)],check)
