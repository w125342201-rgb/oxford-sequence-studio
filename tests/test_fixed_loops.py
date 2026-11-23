import copy,itertools,json,tempfile,unittest
from studio.model import CONFIG,VERSION,SPECS,node,upgrade_document,validate
from studio.engine import Engine
from test_engine import Recorder

class FixedLoops(unittest.TestCase):
    def run_nodes(self,nodes,c):
        points=[];b=Recorder(c)
        with tempfile.TemporaryDirectory() as tmp:
            e=Engine(b,c,lambda k,v:points.append(v) if k=='point' else None)
            e.wait=lambda *args,**kwargs:None
            result=e.run(nodes,tmp)
        self.assertEqual(result['status'],'completed',result)
        self.assertFalse(b.output)
        return points,b
    def test_all_24_environment_electrode_orders(self):
        for order in itertools.permutations(['temperature','field','angle','electrodes']):
            c=dict(copy.deepcopy(CONFIG),enable_matrix=True)
            values={'temperature':[4.,5.],'field':[0.,.01],'angle':[0.,1.],'electrodes':c['point_routes']}
            root=node('resistance',current_a=7e-6)
            for axis in reversed(order):
                outer=node('channels',routes=values[axis]) if axis=='electrodes' else node('sweep',axis=axis,values=values[axis],stable_s=0,rate=.1,tolerance=.01)
                outer['children']=[root];root=outer
            rows,b=self.run_nodes([root],c)
            expected=list(itertools.product(*(values[a] for a in order)))
            self.assertEqual(len(rows),16)
            for row,combination in zip(rows,expected):
                for axis,value in zip(order,combination):
                    if axis=='electrodes':self.assertEqual(json.loads(row['columns']),value['columns'])
                    else:self.assertEqual(row[{'temperature':'temperature_k','field':'field_t','angle':'angle_deg'}[axis]],value)
                self.assertEqual(row['current_a'],7e-6)
    def test_per_point_switch_and_direct_wiring(self):
        for mode in ['delta']:
            n=node('resistance',mode=mode,samples=3)
            for enabled in [True,False]:
                c=dict(copy.deepcopy(CONFIG),enable_matrix=enabled,matrix_each_point=True)
                rows,b=self.run_nodes([n],c)
                self.assertEqual(len(rows),6 if enabled else 3)
                routes=[i for i,x in enumerate(b.calls) if isinstance(x,tuple) and x[0]=='route']
                self.assertEqual(len(routes),6 if enabled else 0)
                for i in routes:self.assertEqual(b.calls[i-1],'off')
    def test_removed_steps_rejected_even_if_disabled(self):
        for kind in ['iv','dcon','current_loop']:
            self.assertNotIn(kind,SPECS)
            n=dict(id='x',kind=kind,enabled=False,params={},children=[])
            d=dict(schema_version=3,config=copy.deepcopy(CONFIG),nodes=[n])
            with self.assertRaisesRegex(ValueError,'已移除'):upgrade_document(d)
            with self.assertRaises(ValueError):validate([n],CONFIG)
    def test_old_fixed_plan_migrates_without_mutation(self):
        n=node('resistance');n['params']['bias_a']=0.
        d=dict(schema_version=3,config=copy.deepcopy(CONFIG),nodes=[n])
        upgraded=upgrade_document(d)
        self.assertEqual(upgraded['schema_version'],VERSION);validate(upgraded['nodes'],upgraded['config'])
        self.assertIn('bias_a',d['nodes'][0]['params'])
        d['nodes'][0]['params']['bias_a']=1e-6
        with self.assertRaises(ValueError):upgrade_document(d)
    def test_delta_still_uses_fixed_plus_minus_current(self):
        from unittest.mock import Mock
        from studio.backend import HardwareBackend
        b=HardwareBackend(copy.deepcopy(CONFIG),lambda m:None);b.src=Mock()
        b.src.run_delta_once.return_value={'readings':[2e-5]}
        p=node('resistance',current_a=5e-6)['params']
        result=b.measure('resistance',p,5e-6,lambda t:None)
        args=b.src.run_delta_once.call_args.kwargs
        self.assertEqual((args['high'],args['low']),(5e-6,-5e-6))
        self.assertEqual(result['resistance_ohm'],4.)
