import copy
import csv
import json
import tempfile
import unittest
from unittest.mock import Mock
from studio.backend import SimBackend, HardwareBackend
from studio.engine import Engine
from studio.model import CONFIG, node
import test_field_retry as retry

class FrontPanelTests(unittest.TestCase):
    def test_exhausted_start_returns_wait_to_engine(self):
        pt=retry.FieldRetryTests().device(['HOLD']*4)
        b=HardwareBackend(dict(CONFIG),lambda _:None)
        b.pt=pt;b.field_wait=Mock()
        result=b.set_environment('field',1.,{'rate':.1})
        self.assertIn('RTOS',result['field_wait_reason'])
        self.assertEqual(pt.ips.set_action.call_count,3)

    def test_exhausted_fset_returns_wait_without_start(self):
        pt=retry.FieldRetryTests().device(['HOLD']);pt.target_field.return_value=0.
        result=pt.set_field_verified(1.,.1,wait=Mock(),wait_for_front_panel=True)
        self.assertIn('FSET',result['field_wait_reason'])
        pt.ips.set_action.assert_not_called()

    def run_case(self,reason=False,stop=False):
        c=copy.deepcopy(CONFIG);c['enable_matrix']=False
        class Panel(SimBackend):
            def __init__(self):
                super().__init__(c,lambda _:None)
                self.commands=0;self.holds=0;self.action='HOLD';self.waits=0
            def set_environment(self,axis,target,p):
                self.commands+=1;self.action='PAUS';self.env['field']=.2
                return {'field_wait_reason':'启动未确认'} if reason else None
            def snapshot(self):return dict(super().snapshot(),magnet_action=self.action)
            def hold(self):self.holds+=1
        b=Panel();e=Engine(b,c)
        def wait(seconds):
            b.waits+=1
            self.assertEqual(e.points,1)
            self.assertEqual(b.holds,0)
            if stop:e.stop.set();e.check()
            if b.waits==1:b.env['field']=1.;b.action='RTOS'
            elif b.waits==2:b.action='HOLD'
        original_wait=e.wait
        def emit(kind,data):
            if kind=='node' and data==field['id']:e.wait=wait
            if kind=='node' and data==last['id']:e.wait=original_wait
        field=node('field',target=1.,stable_s=0,timeout_s=.000001)
        last=node('resistance');e.emit=emit
        with tempfile.TemporaryDirectory() as out:
            result=e.run([node('resistance'),field,last],out)
            events=[json.loads(x) for x in (e.run_dir/'events.jsonl').read_text().splitlines()]
            self.assertEqual(result['status'],'stopped' if stop else 'completed',result['error'])
            self.assertEqual(result['points'],1 if stop else 2)
            self.assertEqual(b.commands,1)
            self.assertEqual(b.holds,1 if stop else 0)
            self.assertTrue(any(x['event']=='field_wait' for x in events))
            if not stop:
                self.assertEqual(b.waits,2)
                self.assertTrue(any(x['event']=='field_recovered' for x in events))
                with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
                self.assertEqual([float(r['field_t']) for r in rows],[0.,1.])
    def test_start_failure_front_panel_recovery_preserves_data(self):self.run_case(reason=True)
    def test_mid_ramp_pause_and_timeout_do_not_end_run(self):self.run_case()
    def test_manual_stop_still_works_while_waiting(self):self.run_case(reason=True,stop=True)

if __name__=='__main__':unittest.main()
