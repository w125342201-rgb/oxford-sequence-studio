import copy
import csv
import tempfile
import unittest
from unittest.mock import Mock, patch
from studio.backend import SimBackend, HardwareBackend, is_timeout
from studio.engine import Engine, Stopped
from studio.model import CONFIG, node
import test_field_retry as driver

class UnlimitedWaitTests(unittest.TestCase):
    def test_all_environment_axes_recover_past_deadline(self):
        for axis in ('temperature','field','pressure','angle'):
            with self.subTest(axis=axis):
                c=dict(copy.deepcopy(CONFIG),auto_pressure_enabled=False)
                b=SimBackend(c,lambda _:None);e=Engine(b,c)
                b.set_environment=Mock();b.env[axis]=0
                p=node(axis,target=1,stable_s=0,timeout_s=.001)['params']
                clock=[0.];loops=[]
                def wait(_):
                    loops.append(1);clock[0]+=10000
                    if len(loops)==3:b.env[axis]=1
                e.wait=wait
                with patch('studio.engine.time.monotonic',side_effect=lambda:clock[0]):
                    e.environment(axis,1,p)
                self.assertEqual(len(loops),3)
                b.set_environment.assert_called_once()

    def test_pxi_timeout_writes_null_and_advances_with_waveform(self):
        class DaqTimeout(Exception):error_code=-200284
        c=dict(copy.deepcopy(CONFIG),measurement_device='pxi');b=SimBackend(c,lambda _:None);e=Engine(b,c)
        original=b.measure;attempts=[]
        def measure(*args):
            attempts.append(1)
            if len(attempts)==1:raise DaqTimeout('samples not available')
            return original(*args)
        b.measure=measure;b.off=Mock(wraps=b.off);e.wait=lambda _:e.check()
        with tempfile.TemporaryDirectory() as out:
            result=e.run([node('resistance',mode='pxi',samples=2)],out)
            self.assertEqual(result['status'],'completed',result['error'])
            self.assertEqual(result['points'],2)
            self.assertEqual(result['invalid_points'],1)
            self.assertEqual(b.off.call_count,1) # only final cleanup
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),2);self.assertEqual(rows[0]['point'],'1')
            for key in rows[0]:
                if key.startswith('ai'):
                    self.assertEqual(rows[0][key],'NULL')
                    self.assertNotEqual(rows[1][key],'NULL')

    def test_permanent_overload_writes_null_without_retry(self):
        c=copy.deepcopy(CONFIG);b=SimBackend(c,lambda _:None);e=Engine(b,c)
        b.measure=Mock(return_value={'voltage_v':9.9e37})
        with tempfile.TemporaryDirectory() as out:
            result=e.run([node('resistance',samples=3)],out)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['points'],3)
            self.assertEqual(result['invalid_points'],3)
            self.assertEqual(b.measure.call_count,3)
            with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:
                self.assertTrue(all(row['voltage_v']=='NULL' for row in csv.DictReader(f)))

    def test_continuous_stability_resets_after_auto_zero(self):
        c=copy.deepcopy(CONFIG);b=SimBackend(c,lambda _:None);b.simulated=False
        e=Engine(b,c);b.set_environment=Mock();clock=[0.]
        def snapshot():
            t=clock[0]
            field=0 if 1<=t<3 else 1
            return dict(b.env,field=field,magnet_action='RTOZ' if 1<=t<2 else 'HOLD')
        b.snapshot=snapshot
        def wait(_):clock[0]+=.5
        e.wait=wait
        p=node('field',target=1,stable_s=2,timeout_s=.1)['params']
        with patch('studio.engine.time.monotonic',side_effect=lambda:clock[0]):
            e.environment('field',1,p)
        self.assertEqual(clock[0],5.) # only 3..5 is continuously stable
        b.set_environment.assert_called_once()

    def test_explicit_point_wait_is_preserved(self):
        b=SimBackend(CONFIG,lambda _:None);e=Engine(b,CONFIG)
        e.wait=Mock()
        e.execute([node('wait',seconds=17)])
        e.wait.assert_called_once_with(17)

    def test_wrapped_io_timeout_retries_and_can_stop(self):
        e=Engine(SimBackend(CONFIG,lambda _:None),CONFIG)
        e.stop.wait=Mock()
        operation=Mock(side_effect=[RuntimeError('output off: VI_ERROR_TMO timeout'),42])
        self.assertEqual(e.retry_io('输出关闭',operation),42)
        self.assertEqual(operation.call_count,2)
        def stop(_):e.stop.set()
        e.stop.wait.side_effect=stop
        with self.assertRaises(Stopped):e.retry_io('回读',Mock(side_effect=TimeoutError()))

    def test_motion_command_timeout_is_not_reissued(self):
        b=SimBackend(CONFIG,lambda _:None);e=Engine(b,CONFIG)
        b.set_environment=Mock(side_effect=TimeoutError('lost ack'))
        b.env['field']=0
        def wait(_):b.env['field']=1
        e.wait=wait
        e.environment('field',1,node('field',target=1,stable_s=0)['params'])
        b.set_environment.assert_called_once()

    def test_rotation_mismatch_can_recover_by_readback(self):
        b=HardwareBackend(CONFIG,lambda _:None);b.rot=Mock()
        b.rotation_target=10;b.rotation_until=0;b.rotation_tolerance=.05
        b.rot.read_deg.side_effect=[5,10]
        self.assertEqual(b.refresh_angle(),5)
        self.assertEqual(b.rotation_target,10)
        self.assertEqual(b.refresh_angle(),10)
        self.assertIsNone(b.rotation_target);self.assertIsNone(b.rotation_error)
        b.rot.rotate_to_degree.assert_not_called()

    def test_reconnect_only_opens_disconnected_sockets(self):
        pt=driver.api.TeslatronPT.__new__(driver.api.TeslatronPT)
        pt.itc_vti=Mock(_sock=None);pt.itc_probe=Mock(_sock=object());pt.ips=None
        pt.reconnect_disconnected()
        pt.itc_vti.connect.assert_called_once()
        pt.itc_probe.connect.assert_not_called()
        pt.itc_vti.query.assert_not_called()

if __name__=='__main__':unittest.main()
