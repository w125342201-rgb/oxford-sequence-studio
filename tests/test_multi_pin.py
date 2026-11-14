import copy
import csv
import json
import tempfile
import unittest
from unittest.mock import Mock
from studio.model import CONFIG, VERSION, node, columns, parse_pins, upgrade_document, validate
from studio.backend import SimBackend, HardwareBackend
from studio.engine import Engine
from studio.gui import RouteEditor
from legacy.matrix.matrix_switch import MatrixSwitch, MatrixError

class MultiPinTests(unittest.TestCase):
    def test_ui_and_roundtrip(self):
        editor=Mock()
        editor.vars=[Mock(get=Mock(return_value=x)) for x in ['parallel','12,11','9','8，7','6']]
        route=RouteEditor.value(editor)
        self.assertEqual(route['columns'],[[12,11],9,[8,7],6])
        d=dict(schema_version=VERSION,config=copy.deepcopy(CONFIG),nodes=[node('route',**route)])
        restored=upgrade_document(json.loads(json.dumps(d)))
        validate(restored['nodes'],restored['config'])
        self.assertEqual(restored['nodes'][0]['params'],route)
        for text in ['12 11','12+11','[12,11]']:
            self.assertEqual(parse_pins(text),[12,11])

    def test_protocol_and_replacement(self):
        switch=MatrixSwitch(simulate=True)
        frame=switch.route_4terminal([1,2],3,[4,5],6)
        self.assertEqual(frame,b'[11233400,00000000,00000000]\r\n')
        switch.route_4terminal(12,9,11,10)
        self.assertEqual(switch.get_mapping(),{12:1,9:2,11:3,10:4})
        before=switch.last_frame
        for values in [([1,2],2,3,4),([],2,3,4),([25],2,3,4)]:
            with self.assertRaises(MatrixError):switch.route_4terminal(*values)
            self.assertEqual(switch.last_frame,before)

    def test_invalid_groups_and_rows(self):
        for v in [[[1,2],2,3,4],[[],2,3,4],[[True],2,3,4],[[25],2,3,4],[[1,1],2,3,4]]:
            with self.assertRaises(ValueError):columns(v)
        with self.assertRaises(ValueError):columns([[1,2],3,4,5],6)

    def test_hardware_order_and_frame_without_instruments(self):
        b=HardwareBackend(copy.deepcopy(CONFIG),lambda _:None)
        b.ensure_rotation_idle=Mock();b.src=Mock()
        b.matrix=MatrixSwitch(simulate=True)
        frames=[];b.matrix._send=lambda frame:frames.append(frame)
        b.off=Mock(side_effect=lambda:frames.append('output_off'))
        b.route(dict(name='parallel',columns=[[1,2],3,[4,5],6]))
        self.assertEqual(frames,['output_off',b'[00000000,00000000,00000000]\r\n',b'[11233400,00000000,00000000]\r\n'])

    def test_simulated_acquisition_and_csv(self):
        routes=[dict(name='parallel',columns=[[12,11],9,[8,7],6]),dict(name='single',columns=[12,9,11,10])]
        c=dict(copy.deepcopy(CONFIG),enable_matrix=True,matrix_each_point=True,point_routes=routes,matrix_off_delay_s=0.,matrix_settle_s=0.)
        backend=SimBackend(c,lambda _:None);engine=Engine(backend,c)
        with tempfile.TemporaryDirectory() as tmp:
            result=engine.run([node('resistance',settle_s=0.)],tmp)
            self.assertEqual(result['status'],'completed')
            with (engine.run_dir/'data.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            self.assertTrue(float(rows[0]['V_12+11-9-8+7-6']))
            self.assertTrue(float(rows[0]['V_12-9-11-10']))
            saved=json.loads((engine.run_dir/'run.json').read_text())
            self.assertIn('12+11',json.dumps(saved))
        self.assertFalse(backend.output)
