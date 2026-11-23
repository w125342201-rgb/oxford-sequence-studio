"""Compare saved-device switching against a previous delivery; no hardware I/O.

Usage: python audit/verify_device_routing.py /path/to/1.7.1.zip
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PROBE=r'''
import copy,csv,json,tempfile,tkinter as tk
from types import SimpleNamespace
from unittest.mock import Mock
from studio.gui import App
from studio.model import CONFIG,node,validate
from studio.instrument_options import display_value
from studio.backend import SimBackend
from studio.engine import Engine

tcl=tk.Tcl()
n=node('resistance',mode='pxi',settle_s=0.)
app=SimpleNamespace(config_data=dict(copy.deepcopy(CONFIG),measurement_device='pxi'),
    nodes=[n],connected=False,busy=False,device_box=Mock(),kind_box=Mock(),
    kind=tk.StringVar(tcl),progress=tk.StringVar(tcl),refresh_tree=Mock(),emit=Mock(),
    device=tk.StringVar(tcl,value=display_value('measurement_device','2182a')))
app.sync_device_options=lambda:App.sync_device_options(app)
App.select_device(app)
result=dict(selected_device=app.config_data['measurement_device'],step_mode=n['params']['mode'])
try:
    validate(app.nodes,app.config_data,hardware=True)
    result['hardware_preflight']='accepted'
except ValueError as exc:result['hardware_preflight']='rejected: '+str(exc)
with tempfile.TemporaryDirectory() as tmp:
    e=Engine(SimBackend(app.config_data,lambda _:None),app.config_data)
    e.wait=lambda _:e.check()
    manifest=e.run(app.nodes,tmp)
    with (e.run_dir/'data.csv').open(encoding='utf-8-sig') as f:row=next(csv.DictReader(f))
    result['simulation_status']=manifest['status']
    result['voltage_columns']=[key for key in row if key.startswith('ai') or key=='voltage_v']
print(json.dumps(result,ensure_ascii=False))
'''

def probe(root):
    run=subprocess.run([sys.executable,'-c',PROBE],cwd=root,capture_output=True,text=True,check=True)
    return json.loads(run.stdout)

def compare(archive):
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if Path(name).is_absolute() or '..' in Path(name).parts:
                    raise ValueError('Unsafe archive member: '+name)
            z.extractall(tmp)
        old_root=next(Path(tmp).glob('*/studio/model.py')).parents[1]
        before=probe(old_root)
    after=probe(ROOT)
    assert before['selected_device']=='2182a' and before['step_mode']=='pxi'
    assert before['hardware_preflight'].startswith('rejected:')
    assert len(before['voltage_columns'])==24
    assert after['selected_device']=='2182a' and after['step_mode']=='delta'
    assert after['hardware_preflight']=='accepted'
    assert after['voltage_columns']==['voltage_v']
    return dict(baseline=Path(archive).name,before=before,after=after,
        scope='Real application callbacks, validation and simulated CSV; no hardware verification.')

if __name__=='__main__':
    print(json.dumps(compare(sys.argv[1]),ensure_ascii=False,indent=2))
