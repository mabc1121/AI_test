from pathlib import Path
import importlib.util, inspect, sys
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
modules=[]
for filename in ('test_template.py','test_regressions.py'):
    spec=importlib.util.spec_from_file_location(filename[:-3],Path(__file__).with_name(filename)); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); modules.append(m)
failed=[]
for m in modules:
  for name,fn in sorted(inspect.getmembers(m,inspect.isfunction)):
    if name.startswith('test_'):
        try:
            if len(inspect.signature(fn).parameters):
                if name=='test_manage_action_contract_rejects_protected_edit':
                    import tempfile
                    with tempfile.TemporaryDirectory() as td: fn(Path(td))
                else: continue
            else: fn()
            print('PASS',name)
        except Exception as e:
            failed.append((name,repr(e))); print('FAIL',name,repr(e))
if failed: raise SystemExit(1)
