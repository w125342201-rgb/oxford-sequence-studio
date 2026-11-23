"""No GUI/hardware dependencies required: python run_simulation.py [sequence.json]."""
import copy,json,sys
from pathlib import Path
from studio.model import CONFIG,example,upgrade_document
from studio.backend import SimBackend
from studio.engine import Engine
if __name__=='__main__':
    d=upgrade_document(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))) if len(sys.argv)>1 else dict(config=copy.deepcopy(CONFIG),nodes=example())
    b=SimBackend(d['config'],print);b.connect()
    result=Engine(b,d['config'],lambda k,v:print(k,v) if k in ('state','run_dir') else None).run(d['nodes'],Path(__file__).parent/'simulation_data')
    b.close();print('Points:',result['points']);sys.exit(0 if result['status']=='completed' else 1)
