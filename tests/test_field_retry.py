import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call
from studio.backend import HardwareBackend
from studio.engine import Engine, Stopped
from studio.model import CONFIG

path=Path(__file__).resolve().parents[1]/'legacy/oxford/oxford_teslatronpt_api.py'
spec=importlib.util.spec_from_file_location('field_retry_api',path)
api=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=api
spec.loader.exec_module(api)

class FieldRetryTests(unittest.TestCase):
    def device(self, actions):
        pt=api.TeslatronPT.__new__(api.TeslatronPT)
        pt.ips=Mock()
        pt.magnet_action=Mock(side_effect=actions)
        pt.set_field_ramp_rate=Mock()
        pt.field_ramp_rate=Mock(return_value=.1)
        pt.target_field=Mock(return_value=1.)
        pt.field=Mock(return_value=0.)
        return pt

    def test_normal_start_has_no_cooldown(self):
        pt=self.device(['HOLD','RTOS']);wait=Mock()
        result=pt.set_field_verified(1.,.1,wait=wait)
        self.assertTrue(result['started'])
        self.assertEqual(wait.call_args_list,[call(1.),call(1.)])

    def test_start_failure_then_recovery(self):
        pt=self.device(['HOLD','HOLD','RTOS']);wait=Mock()
        result=pt.set_field_verified(1.,.1,wait=wait)
        self.assertEqual(result['rtos_attempts'],2)
        self.assertEqual(wait.call_args_list,[call(1.),call(1.),call(60.),call(1.)])

    def test_exhausted_retries_have_only_two_cooldowns(self):
        pt=self.device(['HOLD']*4);wait=Mock()
        with self.assertRaises(api.TeslatronControlError):
            pt.set_field_verified(1.,.1,wait=wait)
        self.assertEqual(pt.ips.set_action.call_count,3)
        self.assertEqual(wait.call_args_list.count(call(60.)),2)
        self.assertEqual(wait.call_args_list[-1],call(1.))

    def test_fset_failure_cools_down_before_rewrite(self):
        pt=self.device(['HOLD','RTOS']);wait=Mock()
        pt.target_field.side_effect=[0.,1.]
        result=pt.set_field_verified(1.,.1,wait=wait)
        self.assertEqual(result['fset_attempts'],2)
        self.assertEqual(wait.call_args_list,[call(1.),call(60.),call(1.),call(1.)])

    def test_stop_during_cooldown_prevents_next_command(self):
        pt=self.device(['HOLD','HOLD','RTOS'])
        b=HardwareBackend(dict(CONFIG),lambda _:None);b.pt=pt
        engine=Engine(b,dict(CONFIG));engine.poll=Mock()
        real_wait=engine.wait
        def wait(seconds):
            if seconds==60.:
                engine.stop.set()
                real_wait(seconds)
        b.field_wait=wait
        with self.assertRaises(Stopped):
            b.set_environment('field',1.,{'rate':.1})
        self.assertEqual(pt.ips.set_action.call_count,1)

if __name__=='__main__':unittest.main()
