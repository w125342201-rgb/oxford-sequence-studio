"""Verify delivery against supplied 1.6.6 archive, including untouched Delta."""
import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def branch(source, method, condition):
    tree=ast.parse(source)
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==method:
            for s in ast.walk(n):
                if isinstance(s,ast.If) and ast.unparse(s.test)==condition:
                    return ast.dump(s,include_attributes=False)
    raise AssertionError((method,condition))

def run(path):
    changed=[];hashes={}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            parts=Path(name).parts
            if len(parts)<2 or name.endswith("/") or "__pycache__" in parts:
                continue
            rel=Path(*parts[1:]);p=ROOT/rel
            if not p.is_file():continue
            a=z.read(name);b=p.read_bytes()
            if a!=b:changed.append(str(rel))
            if str(rel).endswith("keithley_6221_2182a_driver.py"):
                assert a==b,"Keithley/Delta driver changed"
                hashes[str(rel)]=hashlib.sha256(b).hexdigest()
            if str(rel)=="studio/backend.py":
                a=a.decode();b=b.decode()
                # Exact source-level AST of the existing Delta measure branch.
                for key in ("kind == 'resistance' and p['mode'] == 'delta'",):
                    # Locate by a condition containing both mode and delta.
                    matches=[s for s in ast.walk(ast.parse(a)) if isinstance(s,ast.If)
                             and "p['mode']" in ast.unparse(s.test) and "'delta'" in ast.unparse(s.test)]
                    assert matches,"No Delta branches located"
                    for s in matches:
                        cond=ast.unparse(s.test)
                        candidates=[t for t in ast.walk(ast.parse(b)) if isinstance(t,ast.If)
                                    and ast.unparse(t.test)==cond]
                        assert any(ast.dump(s)==ast.dump(t) for t in candidates),cond
    return dict(baseline_zip_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                changed_existing_files=changed,unchanged_driver_sha256=hashes,
                delta_branch_AST_unchanged=True)

if __name__=="__main__":
    print(json.dumps(run(sys.argv[1]),ensure_ascii=False,indent=2))
