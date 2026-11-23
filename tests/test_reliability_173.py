import copy,csv,json,tempfile,threading,time,sys,types,importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
import tkinter as tk
import numpy as np
from studio.model import CONFIG,node,validate_config,upgrade_document,VERSION,validate
from studio.backend import SimBackend,HardwareBackend
from studio.engine import Engine,Stopped
from studio.gui import App,display_readings
from legacy.pxi4462.capture_limits import capture_sample_count
from test_pxi_ranges import daq,api,TaskBoundary
from audit.recover_points import recover
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('reliability_driver',ROOT/'legacy/keithely6221_2182A/keithley_6221_2182a_driver.py')
drv=importlib.util.module_from_spec(spec)
with patch.dict(sys.modules,{'pyvisa':types.ModuleType('pyvisa'),'reliability_driver':drv}):spec.loader.exec_module(drv)

class Wire(drv.Keithley6221):
    """Explicit SCPI state machine; production configure/verify/parse paths run."""
    def __init__(self):
        self.commands=[];self.values={};self.remote={};self.inbox=''
        self.done=2;self.measurement=0;self.running=False;self.raw='0.001'
        self.error='0,"No error"';self.polls=0;self.started=False
    def _wait(self,seconds):self._check_cancel()
    def write(self,cmd):
        self.commands.append(('write',cmd));key,_,v=cmd.lstrip(':').partition(' ')
        if key=='SYST:COMM:SERIAL:SEND':
            remote=v.strip('"').lstrip(':')
            if remote=='SYST:ERR?':self.inbox='0,"No error"'
            elif remote.endswith('?'):self.inbox=self.remote[remote[:-1]]
            else:
                rk,_,rv=remote.partition(' ');self.remote[rk]=rv
                if rk=='SENS:VOLT:RANG':self.remote['SENS:VOLT:RANG:AUTO']='0'
        else:self.values[key]=v
        if key=='INIT:IMM':self.started=True;self.done=0;self.polls=0
        if key=='SOUR:WAVE:INIT':self.running=True
        if key=='OUTPUT' and v=='OFF':self.running=False
    def query(self,cmd):
        self.commands.append(('query',cmd));key=cmd.lstrip(':')
        if key=='SYST:ERR?':r=self.error;self.error='0,"No error"';return r
        if key=='SYST:COMM:SERIAL:ENT?':r=self.inbox;self.inbox='';return r
        if key=='STAT:OPER:EVEN?':
            if self.started:
                self.polls+=1;return '2' if self.polls>=2 else '0'
            r=self.done;self.done=0;return str(r)
        if key=='STAT:MEAS:EVEN?':r=self.measurement;self.measurement=0;return str(r)
        if key=='STAT:MEAS:COND?':return '0'
        if key=='STAT:OPER:COND?':return '16' if self.running else '1024'
        if key=='OUTP?':return '1' if self.running else '0'
        if key=='TRAC:DATA?':return self.raw
        v=self.values[key[:-1]]
        return {'ON':'1','OFF':'0'}.get(v,v)

class ReliabilityTests(TestCase):
    def rows(self,e):
        with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as stream:
            return list(csv.DictReader(stream))
    def test_acquired_point_survives_environment_failure(self):
        class Backend(SimBackend):
            acquired=False
            def measure(self,*a):r=super().measure(*a);self.acquired=True;return r
            def snapshot(self):
                if self.acquired:raise TimeoutError('post-read outage')
                return super().snapshot()
        c=copy.deepcopy(CONFIG);b=Backend(c,lambda _:None);e=Engine(b,c)
        with tempfile.TemporaryDirectory() as out:
            result=e.run([node('resistance',settle_s=0)],out)
            self.assertEqual(result['points'],1)
            row=self.rows(e)[0];self.assertEqual(row['environment_status'],'unavailable')
            self.assertEqual(row['temperature_k'],'');self.assertTrue(float(row['voltage_v'])>0)
            events=[json.loads(x)['event'] for x in (e.run_dir/'events.jsonl').read_text().splitlines()]
            self.assertLess(events.index('acquisition'),events.index('point'))
    def test_completed_point_survives_stop_before_record(self):
        c=copy.deepcopy(CONFIG);b=SimBackend(c,lambda _:None);e=Engine(b,c);original=b.measure
        def measure(*a):r=original(*a);e.stop.set();return r
        b.measure=measure
        with tempfile.TemporaryDirectory() as out:
            r=e.run([node('resistance',settle_s=0,samples=2)],out)
            self.assertEqual((r['status'],r['points']),('stopped',1));self.assertEqual(len(self.rows(e)),1)
    def test_explicit_routes_are_identified(self):
        c=dict(CONFIG,enable_matrix=True,matrix_each_point=False,matrix_off_delay_s=0,matrix_settle_s=0)
        e=Engine(SimBackend(c,lambda _:None),c);n=node('channels');n['children']=[node('resistance',settle_s=0)]
        with tempfile.TemporaryDirectory() as out:
            r=e.run([n],out);rows=self.rows(e)
            self.assertEqual([x['route'] for x in rows],['通道 A','通道 B'])
            self.assertNotEqual(rows[0]['columns'],rows[1]['columns'])
            self.assertEqual(r['voltage_columns']['voltage_v']['kind'],'row_route')
    def test_compliance_bounds(self):
        for v in (.01,0,106,float('nan')):
            with self.assertRaises(ValueError):validate_config(dict(CONFIG,compliance_v=v))
            with self.assertRaises(ValueError):Wire().set_compliance(v)
        for v in (.1,1,105):validate_config(dict(CONFIG,compliance_v=v));Wire().set_compliance(v)
    def test_delta_waits_for_done_after_init(self):
        # 2026-11-07: 1017 flow restored (user decision, the 2026-11-06 sequence
        # post-mortem) -- STAT:OPER:EVEN? is only polled AFTER :INIT:IMM
        # (no pre-init event clears); completion = bit 1; disarm last.
        s=Wire();r=s.run_delta_once(high=1e-5,low=-1e-5,reset_before=False)
        self.assertAlmostEqual(r['resistance'],100)
        init=s.commands.index(('write',':INIT:IMM'))
        self.assertNotIn(('query','STAT:OPER:EVEN?'),s.commands[:init])
        self.assertEqual(s.polls,2)
        self.assertEqual(r['actual_count'],1)
        self.assertEqual(s.commands[-1],('write',':SOUR:SWE:ABOR'))
    def test_delta_malformed_trace_parse_semantics(self):
        # 2026-11-07: 1017 flow restored -- bad tokens are skipped (no raise,
        # no driver-level OUTPUT OFF); an empty trace falls back to
        # :SENS:DATA? and may yield reading None, which the backend rejects
        # ('Delta返回点数不是1').
        s=Wire();s.raw='1e-5,BAD'
        r=s.run_delta_once(high=1e-5,low=-1e-5,reset_before=False)
        self.assertEqual(r['readings'],[1e-5])
        s=Wire();s.raw=''
        r=s.run_delta_once(high=1e-5,low=-1e-5,reset_before=False)
        self.assertIn(('query',':SENS:DATA?'),s.commands)
        self.assertIsNone(r['reading'])
    # 2026-11-07: test_wrong_delta_readback_aborts_before_start removed --
    # the 6221 readback-verification layer was retired together with the
    # restoration of the 1017 delta flow (user decision, the 2026-11-06 sequence
    # post-mortem: per-point serial round trips are incompatible with the
    # variable ~4 s 2182A serial-link latency on real hardware).
    def test_ac_initializes_and_verifies(self):
        s=Wire();s.set_compliance(1);s.configure_ac(13,1e-5,marker_position=90)
        r=s.verify_ac(13,1e-5,90,1)
        self.assertEqual(r['SOUR:WAVE:EXTR?'],0)
        self.assertIn(('write','SOUR:WAVE:RANG BEST'),s.commands)
        self.assertIn(('write','TRIG:OLIN 2'),s.commands)
        s.values['SOUR:WAVE:AMPL']='0.01'
        with self.assertRaises(drv.Keithley6221Error):s.verify_ac(13,1e-5,90,1)
    def test_error_queue_and_compliance_fail_closed(self):
        s=Wire();s.error='-222,"Out of range"'
        with self.assertRaises(drv.Keithley6221Error):s.check_errors('setup')
        s.running=True;s.measurement=8
        with self.assertRaises(drv.Keithley6221Error):s.source_health(require_wave=True)
        s.running=False
        with self.assertRaises(drv.Keithley6221Error):s.source_health(require_wave=True)
    def test_driver_wait_stops_without_sleep(self):
        c=copy.deepcopy(CONFIG);e=Engine(SimBackend(c,lambda _:None),c);e.stop.set()
        s=drv.Keithley6221.__new__(drv.Keithley6221);s.wait_callback=e.io_wait
        before=time.monotonic()
        with self.assertRaises(Stopped):s._wait(10)
        self.assertLess(time.monotonic()-before,.2)
    def test_rotation_mismatch_does_not_repeat_queries(self):
        b=HardwareBackend(dict(CONFIG,auto_pressure_enabled=False),lambda _:None)
        b.rot=Mock();b.rot.read_deg.return_value=5;b.rotation_target=10;b.rotation_until=0;b.rotation_tolerance=.05
        for _ in range(3):b.service()
        self.assertIsNotNone(b.rotation_error)
        with self.assertRaises(RuntimeError):b.ensure_rotation_idle()
        self.assertEqual(b.rot.read_deg.call_count,1)
        b.rot.read_deg.return_value=10;b.refresh_angle();self.assertIsNone(b.rotation_error)
    def test_pause_during_environment_wait_closes_source(self):
        class Backend(SimBackend):
            def set_environment(self,*a):pass
        c=dict(CONFIG,measurement_device='pxi');b=Backend(c,lambda _:None);paused=threading.Event()
        e=Engine(b,c,lambda k,v:paused.set() if k=='state' and str(v).startswith('已暂停') else None)
        out={}
        with tempfile.TemporaryDirectory() as directory:
            thread=threading.Thread(target=lambda:out.update(e.run([node('temperature',target=8),node('resistance',mode='pxi')],directory)))
            thread.start();e.pause.set()
            self.assertTrue(paused.wait(2));self.assertFalse(b.output)
            e.stop.set();thread.join(2);self.assertFalse(thread.is_alive());self.assertEqual(out['status'],'stopped')
    def test_daq_can_cancel_while_waiting_for_trigger(self):
        task=TaskBoundary();task.is_task_done=lambda:False;d=daq(.316)
        def cancel():raise Stopped('stop')
        d.cancel_check=cancel
        with api(task),self.assertRaises(Stopped):d._acquire_hardware()
        self.assertTrue(task.closed);self.assertNotIn('read',task.calls)
    def test_optional_waveform_is_saved_before_overload_rejection(self):
        b=HardwareBackend(CONFIG,lambda _:None);task=TaskBoundary(status=True);d=daq(.316)
        with tempfile.TemporaryDirectory() as out:
            b.raw_directory=Path(out)/'waveforms';d.waveform_sink=b.save_waveform
            with api(task),self.assertRaises(ValueError):d.acquire_one()
            file=Path(out)/d.last_capture_metadata['waveform_file']
            with np.load(file) as r:self.assertEqual(r['voltage_v'].shape,(4,d._n))
    def test_capture_limits_are_bounded_and_old_schemes_import(self):
        self.assertEqual(capture_sample_count(51200,13,1),51200)
        for f,seconds in ((.0001,1),(13,1e9)):
            with self.assertRaises(ValueError):capture_sample_count(51200,f,seconds)
        n=node('resistance');n['params'].pop('daq_save_waveforms')
        d=upgrade_document(dict(schema_version=VERSION,config=CONFIG,nodes=[n]));validate(d['nodes'],d['config'])
        self.assertFalse(d['nodes'][0]['params']['daq_save_waveforms'])
    def plot_app(self,rows,device='2182a'):
        interp=tk.Tcl();a=SimpleNamespace(data=rows,config_data=dict(CONFIG,measurement_device=device),harmonic_box=Mock(),component_box=Mock(),groups=Mock(),harmonic=tk.StringVar(interp,value='三次谐波 3f'),component=tk.StringVar(interp,value='Y'))
        a.sync_plot_mode=lambda:App.sync_plot_mode(a);a._interp=interp
        return a
    def test_delta_hides_harmonic_controls_and_reads_voltage(self):
        a=self.plot_app([{'voltage_v':-1e-5}]);self.assertFalse(a.sync_plot_mode())
        a.harmonic_box.pack_forget.assert_called_once();a.component_box.pack_forget.assert_called_once()
        self.assertEqual(list(display_readings(a.data[0],3,'Y'))[0][1],-1e-5)
    def test_pxi_only_offers_exported_harmonics(self):
        a=self.plot_app([{'channels':{'0':{'harmonic_order':2}}}],'pxi');self.assertTrue(a.sync_plot_mode())
        self.assertEqual(a.harmonic.get(),'一次谐波 1f')
        self.assertEqual(a.harmonic_box.configure.call_args.kwargs['values'],['一次谐波 1f','二次谐波 2f'])
    def test_recovery_uses_acquisition_when_point_metadata_missing(self):
        with tempfile.TemporaryDirectory() as out:
            source=Path(out)/'events.jsonl';destination=Path(out)/'recovered.csv'
            source.write_text(json.dumps(dict(event='acquisition',data=dict(point=1,route='A',data=dict(voltage_v=.01))))+'\n{"broken":')
            self.assertEqual(recover(source,destination),1)
            with destination.open(encoding='utf-8-sig') as stream:
                row=next(csv.DictReader(stream))
            self.assertEqual(row['route'],'A');self.assertEqual(row['environment_status'],'unavailable')
            with self.assertRaises(FileExistsError):recover(source,destination)
