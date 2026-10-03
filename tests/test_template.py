from __future__ import annotations
import json, subprocess, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
# Platform tests use this fixed reference strategy, so they pass whatever strategy the app runs (its own trade.py is
# checked by the contract validator, its self-test and core.doctor).
REF=ROOT/'tests'/'fixtures'/'reference_trade.py'
REF22=ROOT/'tests'/'fixtures'/'reference_trade_v22.py'   # the same strategy on contract 2.2 (market data from tt_input)
from core.common import Store, load_config, load_root_env, resolve
from manage.manage import ManageEngine
from think.think import ThinkEngine

def test_doctor_full():
    p=subprocess.run([sys.executable,'-m','core.doctor','--full'],capture_output=True,text=True); assert p.returncode==0,p.stdout+p.stderr; out=json.loads(p.stdout); assert out['ok']

def test_manage_action_contract_rejects_protected_edit(tmp_path):
    import os
    old=os.environ.get('APP_RUNTIME_DIR'); os.environ['APP_RUNTIME_DIR']=str(tmp_path)
    try:
        eng=ManageEngine(ROOT)
        for bad in ('.env','.venv/pyvenv.cfg','release_manifest.json','../etc/passwd'):
            try: eng.propose_file_edit(bad,'a','b','test')
            except (ValueError,PermissionError,FileNotFoundError): pass
            else: raise AssertionError(f'locked path editable: {bad}')
        trade=ROOT/'trade'/'trade.py'; before=trade.read_bytes()
        aid=eng.propose_file_edit('trade/trade.py','"paper_only": True','"paper_only": False','test')
        result=eng.approve_action(aid)
        assert not result['ok'] and result['stage']=='validation', result
        assert trade.read_bytes()==before
        assert eng.store.actions()[0]['status']=='FAILED'
        rid=eng.propose_restart('trade','test'); eng.reject_action(rid)
        assert eng.store.actions()[0]['status']=='REJECTED' and eng.store.get('control','restart') is None
        eng.update_settings('manage',{'provider':'mock'})   # installed apps use "auto": with keys the chat would call a real, paid AI
        r=eng.chat('ping'); assert r['answer'].startswith('[mock]') and len(eng.history(r['chat_id']))==2
        eng.chat('again',r['chat_id']); eng.chat('other')
        assert len(eng.history(r['chat_id']))==4 and len(eng.chats())==2
        eng.delete_chat(r['chat_id']); assert len(eng.chats())==1 and eng.history(r['chat_id'])==[]
    finally:
        if old is None: os.environ.pop('APP_RUNTIME_DIR',None)
        else: os.environ['APP_RUNTIME_DIR']=old

def test_think_autonomous_promotion():
    import os
    from core.common import import_module
    from think.replay import replay, recorded_span
    from think.think import set_config_defaults
    from trade.worker import Recorder, TradeEngine
    with tempfile.TemporaryDirectory() as td:
        old=os.environ.get('APP_RUNTIME_DIR'); os.environ['APP_RUNTIME_DIR']=td
        try:
            eng=ThinkEngine(ROOT)
            assert eng.status()['state']=='IDLE' and eng.status()['pool']==[]
            r=eng.run_cycle(True); assert r['ran'] and r['proposed']==0  # mock provider: analysis only, no AI experiments
            assert eng.store.get('think','analysis')['champion']['stats']=={'n':0} and eng.store.get('think','champion')['id']=='baseline'
            # record clean events and replay them through two variants in one pass
            m=import_module(REF,'t_rec'); rt=m.create_trade_system(); rt.initialize({}); rt.input.mark_connected(); rt.input.mark_subscribed('book'); rt.input.mark_subscribed('trades')
            n=time.time_ns(); rt.on_l3_snapshot([[1,'100','1'],[2,'101','-1']],1,n); rt.on_l3_checksum(rt.input.book.checksum(),2,n+1)
            rt.on_public_trade([9,n//1_000_000,m.Decimal('0.01'),m.Decimal('100.5')],3,n+2)
            rec=Recorder(Path(td)/'recordings')
            for e in rt.core.event_history: rec.write(e,e.kind=='public_trade')
            rec.close(); assert recorded_span(Path(td)/'recordings')['files']==1
            res=replay([('baseline',m,{}),('v',m,{'min_expected_move_bps':10.0})],Path(td)/'recordings',0,int(time.time()*1000)+1000)
            assert res['events']==3 and all(a['error'] is None for a in res['arms'].values())
            # evaluation of a young shadow is WAITING with all criteria reported
            ex=[{'action':'ENTER_LONG','fill_price':100.0,'size':0.01,'fee_usd':0.0005,'reason':'reversal_long_confirmed','timestamp_ms':1,'symbol':'t','side':'buy','slippage_bps':1,'realized_pnl':0},
                {'action':'EXIT','fill_price':101.0,'size':0.01,'fee_usd':0.0005,'reason':'target_reached','timestamp_ms':2,'symbol':'t','side':'sell','slippage_bps':1,'realized_pnl':0.009}]
            eng.store.put('shadow','s1',{'broker':{'executions':ex}})
            ev=eng.evaluate({'id':'s1','started_ms':int(time.time()*1000)-3600_000})
            assert ev['verdict']=='WAITING' and len(ev['checks'])==11 and ev['candidate']['n']==1
            # a shadow that is clearly losing once it has its trade count is failed early (frees the slot)
            lose=[]
            for i in range(30):
                px=100.0-0.2-0.01*(i%4); lose+=[{**ex[0],'timestamp_ms':10+i*10},{**ex[1],'fill_price':px,'timestamp_ms':15+i*10,'reason':'stop_loss','realized_pnl':(px-100)*0.01}]
            eng.store.put('shadow','s2',{'broker':{'executions':lose}})
            ev=eng.evaluate({'id':'s2','started_ms':int(time.time()*1000)-3600_000}); assert ev['verdict']=='FAIL' and ev['why'].startswith('clearly losing'), ev['why']
            eng.store.put('shadow','s3',{'broker':{'executions':lose[:32]}})   # 16 trades, overwhelming evidence: stopped before 30
            ev=eng.evaluate({'id':'s3','started_ms':int(time.time()*1000)-3600_000}); assert ev['verdict']=='FAIL' and ev['candidate']['n']==16, ev['why']
            eng._save_pool([{'id':'s2','status':'SHADOW'}]); eng.store.put('think_retire','7',{'candidate':'s2','reason':'losing'})
            assert eng.apply_retire_requests()==1 and eng.pool()[0]['status']=='RETIRED' and eng.apply_retire_requests()==0
            src=REF.read_text(); new=set_config_defaults(src,{'min_expected_move_bps':25,'warmup_seconds':600.0})
            assert 'min_expected_move_bps: float = 25.0' in new and 'warmup_seconds: int = 600' in new
            # a new strategy version archives a flat account instead of blocking recovery
            eng.store.put('trade_state','checkpoint',{'identity':{'source_sha256':'old'},'broker':{'positions':{}}})
            TradeEngine(ROOT); assert eng.store.get('trade_state','checkpoint') is None
            with eng.store.connect() as db: assert db.execute("select count(*) from kv where scope='trade_state_archive'").fetchone()[0]==1
        finally:
            if old is None: os.environ.pop('APP_RUNTIME_DIR',None)
            else: os.environ['APP_RUNTIME_DIR']=old

def test_paper_only():
    cfg=load_config(ROOT); assert cfg['mode']['paper_only'] is True; text=(ROOT/'trade'/'trade.py').read_text(); assert '"paper_only": True' in text

if __name__=='__main__':
    funcs=[test_doctor_full,test_think_autonomous_promotion,test_paper_only]
    for f in funcs:f(); print('PASS',f.__name__)

def test_openai_adapter_shape_without_network():
    import os, types, sys as _sys
    from core.common import AgentProvider
    calls={}
    class Resp: output_text='OK'
    class Responses:
        def create(self, **kwargs): calls.update(kwargs); return Resp()
    class Client:
        def __init__(self): self.responses=Responses()
    fake=types.ModuleType('openai'); fake.OpenAI=Client
    old=_sys.modules.get('openai'); _sys.modules['openai']=fake; oldkey=os.environ.get('OPENAI_API_KEY'); os.environ['OPENAI_API_KEY']='test-key'
    try:
        out=AgentProvider({'provider':'openai','model':'gpt-5.6-sol','reasoning':'high','web_research':'automatic'}).chat('hello',system='sys',allow_web=True)
        assert out=='OK'; assert calls['model']=='gpt-5.6-sol'; assert calls['reasoning']=={'effort':'high'}; assert calls['tools']==[{'type':'web_search'}]
    finally:
        if old is None:_sys.modules.pop('openai',None)
        else:_sys.modules['openai']=old
        if oldkey is None:os.environ.pop('OPENAI_API_KEY',None)
        else:os.environ['OPENAI_API_KEY']=oldkey


def test_claude_adapter_tool_loop_and_fallback_without_network():
    import os, types, sys as _sys
    from core.common import AgentProvider
    calls=[]
    B=lambda **k: types.SimpleNamespace(**k)
    class Messages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls)==1: return B(stop_reason='tool_use',stop_details=None,content=[B(type='text',text='checking'),B(type='tool_use',id='t1',name='get_status',input={})])
            return B(stop_reason='end_turn',stop_details=None,content=[B(type='text',text='all healthy')])
    class Client:
        def __init__(self,**kw): self.messages=Messages()
    fake=types.ModuleType('anthropic'); fake.Anthropic=Client
    old=_sys.modules.get('anthropic'); _sys.modules['anthropic']=fake; oldkey=os.environ.get('ANTHROPIC_API_KEY'); os.environ['ANTHROPIC_API_KEY']='test-key'
    try:
        tool={'type':'function','name':'get_status','description':'d','parameters':{'type':'object','properties':{}}}
        p=AgentProvider({'provider':'anthropic','model':'gpt-5.6-sol','reasoning':'high','web_research':'automatic'})
        out=p.run('hi',system='sys',allow_web=True,tools=[tool],handler=lambda n,a:{'ok':n})
        assert out=='all healthy' and p.used=='anthropic' and p.fallback_error is None
        first=calls[0]; assert first['model']=='claude-opus-5-5' and first['thinking']=={'type':'adaptive'} and first['output_config']=={'effort':'high'}
        assert [t.get('name') for t in first['tools']]==['get_status','web_search'] and first['tools'][1]['type']=='web_search_20260209'
        msgs=calls[1]['messages']; assert [m['role'] for m in msgs[:3]]==['user','assistant','user'] and msgs[2]['content'][0]['tool_use_id']=='t1'
        os.environ.pop('ANTHROPIC_API_KEY')  # primary now fails -> falls back
        p=AgentProvider({'provider':'anthropic','fallback_provider':'mock'})
        assert p.run('ping',system='s').startswith('[mock]') and p.used=='mock' and 'ANTHROPIC_API_KEY' in p.fallback_error
    finally:
        if old is None:_sys.modules.pop('anthropic',None)
        else:_sys.modules['anthropic']=old
        if oldkey is None:os.environ.pop('ANTHROPIC_API_KEY',None)
        else:os.environ['ANTHROPIC_API_KEY']=oldkey

def _hyp(i, conf=0.7, params=None):
    return {'id':f'h{i}','title':f'idea {i}','kind':'params','change':{'params':params or {'max_stop_bps':[16.0]}},'rationale':'r','evidence':'e',
            'predictions':[{'metric':'net_bps_per_trade','direction':'up','target':'>= 0'}],'min_trades':20,'kill_rule':'edge < -5 after 20','falsified_if':'edge <= 0','confidence':conf}

def _report(hs): return {'checklist':{k:'x' for k in ('lessons','signal','costs','entries','exits','risk','regimes','alternative_explanation','cheapest_test')},'diagnosis':'d','hypotheses':hs}

def test_scientists_unit_debate_merge_degraded_and_resume():
    import os
    from agents.unit import Member
    from think.scientists import run_scientists
    calls=[]
    def fake(member, system, prompt, tools, handler, submit):
        calls.append((member.name, submit))
        if member.name=='gpt' and 'FAIL' in os.environ.get('TT_FAKE',''): return None, {'input_tokens':10,'output_tokens':0,'calls':1}, 'RateLimitError: no credits'
        u={'input_tokens':1000,'output_tokens':200,'calls':1}
        if submit=='submit_report':
            if member.name=='claude' and 'REJECTED' not in prompt and 'BAD' in os.environ.get('TT_FAKE',''): return {'diagnosis':'incomplete'}, u, None
            return _report([_hyp(1)] if member.name=='claude' else [_hyp(1,0.8,{'profit_trigger_net_bps':[30.0]}),_hyp(2,0.3)]), u, None
        other='Colleague B' if member.name=='claude' else 'Colleague A'
        ids=['h1','h2'] if member.name=='claude' else ['h1']
        stance={'h1':'agree' if member.name=='gpt' else 'disagree','h2':'partial'}
        return {'reviews':[{'member':other,'hypothesis_id':i,'stance':stance[i],'reason':'because'} for i in ids],
                'revised_report':_report([_hyp(1)] if member.name=='claude' else [_hyp(1,0.8,{'profit_trigger_net_bps':[30.0]}),_hyp(2,0.3)])}, u, None
    members=[Member('claude','anthropic','claude-opus-5-5'),Member('gpt','openai','gpt-5.6-sol')]
    rec=[]
    with tempfile.TemporaryDirectory() as td:
        os.environ['TT_FAKE']='BAD'
        b=run_scientists(members,{'edge':{}},Path(td),rec.append,lambda:True,[],None,rounds=2,call=fake)
        by={h['key']:h for h in b['hypotheses']}
        assert by['claude:h1']['agreement']=='agreed' and by['gpt:h1']['agreement']=='disagreed' and by['gpt:h2']['agreement']=='partial'
        assert b['selected'][0]=='claude:h1' and 'gpt:h1' in b['selected'] and 'gpt:h2' in b['selected'] and not b['reduced_independence']
        assert b['rounds']==1 and (Path(td)/'scientists'/'claude.json').is_file() and (Path(td)/'brief.json').is_file()
        assert sum(1 for c in calls if c==('claude','submit_report'))==2   # invalid first submission sent back once
        assert all(r['prompt'].startswith('scientist.md@') for r in rec) and {r['model'] for r in rec}=={'claude-opus-5-5','gpt-5.6-sol'}
        assert all((r['cost_usd'] or 0)>0 for r in rec if r['model']=='claude-opus-5-5') and all(r['cost_usd'] is None for r in rec if r['model']=='gpt-5.6-sol')
        n=len(calls); run_scientists(members,{'edge':{}},Path(td),rec.append,lambda:True,[],None,rounds=2,call=fake); assert len(calls)==n  # resumed from disk
    with tempfile.TemporaryDirectory() as td:
        os.environ['TT_FAKE']='FAIL'
        b=run_scientists(members,{'edge':{}},Path(td),rec.append,lambda:True,[],None,rounds=2,call=fake)
        assert b['reduced_independence'] and 'gpt' in b['unavailable'] and [m['member'] for m in b['members']]==['claude']
        assert b['rounds']==0 and all(h['agreement']=='unreviewed' for h in b['hypotheses'])   # never replaced by another model
    os.environ.pop('TT_FAKE',None)

def test_lab_study_state_manifest_and_budget():
    import os
    from think.loop import Lab
    from agents.unit import BudgetExceeded
    def fake(member, system, prompt, tools, handler, submit):
        u={'input_tokens':200_000,'output_tokens':40_000,'calls':1}
        if submit=='submit_report': return _report([_hyp(1)]), u, None
        return {'reviews':[{'member':'Colleague B' if member.name=='claude-opus-5-5' else 'Colleague A','hypothesis_id':'h1','stance':'agree','reason':'ok'}],'revised_report':_report([_hyp(1)])}, u, None
    with tempfile.TemporaryDirectory() as td:
        old=os.environ.get('APP_RUNTIME_DIR'); os.environ['APP_RUNTIME_DIR']=td
        try:
            t=ThinkEngine(ROOT); lab=Lab(t); lab.cfg['groups']['scientists']['members']=[{'name':'claude-opus-5-5','provider':'anthropic','model':'claude-opus-5-5'},{'name':'claude-sonnet-5','provider':'anthropic','model':'claude-sonnet-5'}]
            lab.cfg['budget_usd_per_day']=10.0   # set what this test needs; an app's config may differ
            sid=lab.new_study(); b=lab.run_science(sid,[],None,call=fake)
            st=lab.study(sid); m=json.loads((t.think_dir/'studies'/sid/'study.json').read_text())
            assert st['stage']=='science_done' and st['status']=='waiting_for_workers' and b['hypotheses'][0]['agreement']=='agreed'
            assert m['champion']['sha256'] and m['prompts']['scientists'].startswith('scientist.md@') and m['cost']['usd']>0 and 'data_window' in m
            lab.cfg['budget_usd_per_day']=0.01; sid2=lab.new_study()
            try: lab.run_science(sid2,[],None,call=fake)
            except BudgetExceeded: pass
            else: raise AssertionError('budget cap ignored')
            assert lab.study(sid2)['status']=='paused'
        finally:
            if old is None: os.environ.pop('APP_RUNTIME_DIR',None)
            else: os.environ['APP_RUNTIME_DIR']=old

def test_seat_fallback_is_used_and_recorded():
    import os, agents.unit as U
    from think.scientists import run_scientists
    class FakeProvider:
        def __init__(self, s): self.s=s; self.usage={'input_tokens':100,'output_tokens':10,'calls':1}
        def run(self, prompt, system, tools, handler, **kw):
            if self.s['provider']=='openai': raise RuntimeError('RateLimitError 429: no credits')
            if 'submit_report' in [t['name'] for t in tools]: handler('submit_report',_report([_hyp(1)]))
            else: handler('submit_exchange',{'reviews':[{'member':'Colleague B' if 'opus' in self.s['model'] else 'Colleague A','hypothesis_id':'h1','stance':'agree','reason':'ok'}],'revised_report':_report([_hyp(1)])})
    real=U.AgentProvider; U.AgentProvider=FakeProvider
    try:
        seats=[U.Member('seat-1','anthropic','claude-opus-5-5'),U.Member('seat-2','openai','gpt-5.6-sol',fallback={'provider':'anthropic','model':'claude-sonnet-5'})]
        with tempfile.TemporaryDirectory() as td:
            rec=[]; b=run_scientists(seats,{'edge':{}},Path(td),rec.append,lambda:True,[],None,rounds=1)
            s2=[r for r in rec if r['member']=='seat-2']
            assert s2 and all(r['model']=='claude-sonnet-5' and r['substitute_for']=='openai/gpt-5.6-sol' and r['status']=='ok' for r in s2)
            ind=b['independence']; assert ind['substitutes']=={'seat-2':'openai/gpt-5.6-sol'} and ind['distinct_models']==2 and ind['single_provider'] and not ind['reduced']
            assert b['hypotheses'][0]['agreement']=='agreed'
        seats[1]=U.Member('seat-2','openai','gpt-5.6-sol')   # no fallback configured -> seat unavailable, never replaced
        with tempfile.TemporaryDirectory() as td:
            b=run_scientists(seats,{'edge':{}},Path(td),[].append,lambda:True,[],None,rounds=1)
            assert 'seat-2' in b['unavailable'] and b['reduced_independence']
    finally: U.AgentProvider=real

def test_evaluators_gate_consensus_scorecards_and_issues():
    import os
    from think.loop import Lab
    from think.evaluators import decide, ready, systematic_checks
    from agents.documents import check_evaluation
    sh={'shadow':'c1'}; gate=lambda v,n=40,age=30: {'gate':{'verdict':v,'why':'x','candidate':{'n':n},'age_h':age}}
    assert decide({**sh,**gate('PASS')},['promote','promote'],7)[0]=='promote' and decide({**sh,**gate('PASS')},['promote','retire'],7)[0]=='hold'
    assert decide({**sh,**gate('FAIL')},['promote','promote'],7)[0]=='retire' and decide({**sh,**gate('WAITING')},['promote','promote'],7)[0]=='extend'
    assert decide({**sh,**gate('WAITING',age=200)},['extend','extend'],7)[0]=='extend' and decide({'shadow':None},['rethink'],7)[0]=='close'   # no time limit
    assert ready({**sh,**gate('WAITING',n=3,age=200)},7) and not ready({**sh,**gate('WAITING',n=3,age=20)},7)   # after 7 days: reviewed, not removed
    pr=lambda c,age: {'route':'probe','probe':{'complete':c},'age_days':age}
    assert not ready(pr(False,1),7) and ready(pr(False,8),7) and ready(pr(True,0),7)   # a short probe waits for more data, up to its cap
    assert decide({**sh,**gate('PASS',age=200)},['promote','retire'],7)[0]=='hold'   # a split on a gate pass stays for the user (stuck alarm)
    assert not ready({'pending':True,'age_days':30},7) and decide({'pending':True},[],7)[0]=='extend'
    assert any('missing verdict on H2' in p for p in check_evaluation({'verdicts':[],'systematic_issues':[],'lessons':['l']},['H2']))
    hist=[{'id':f's{i}','evaluation':{'outcomes':{'k':'falsified'}},'builds':{'k':{'status':'ambiguous'}},'reduced_independence':{'scientists':True}} for i in range(3)]
    assert {i['owner'] for i in systematic_checks(hist,[])}=={'scientists','lab'}
    def verdict(s,rec): return {'subject':s,'outcome':'supported' if rec=='promote' else 'inconclusive','recommendation':rec,'reason':'r',
                                'prediction_checks':[{'metric':'net_bps_per_trade','expected':'>= 0','observed':'+3','met':True}]}
    recs={'a':'promote','b':'promote'}
    def fake(member, system, prompt, tools, handler, submit):
        u={'input_tokens':100,'output_tokens':50,'calls':1}
        doc={'verdicts':[verdict('H1',recs[member.name]),verdict('H2','rethink')],'systematic_issues':[{'issue':'i','evidence':'e','owner':'scientists','severity':'low','suggestion':'s'}],'lessons':['keep stops tight']}
        if submit=='submit_evaluation': return doc,u,None
        other='Colleague B' if member.name=='a' else 'Colleague A'
        return {'reviews':[{'member':other,'subject':s,'stance':'agree','reason':'ok'} for s in ('H1','H2')],'revised_evaluation':doc},u,None
    with tempfile.TemporaryDirectory() as td:
        old=os.environ.get('APP_RUNTIME_DIR'); os.environ['APP_RUNTIME_DIR']=td
        try:
            t=ThinkEngine(ROOT); lab=Lab(t); lab.cfg['groups']['evaluators']['members']=[{'name':'a','provider':'anthropic','model':'claude-opus-5-5'},{'name':'b','provider':'anthropic','model':'claude-sonnet-5'}]
            def study(g):
                sid=lab.new_study(); d=t.think_dir/'studies'/sid
                (d/'brief.json').write_text(json.dumps({'hypotheses':[{**_hyp(1),'key':'s1:h1','agreement':'agreed'},{**_hyp(2),'key':'s1:h2','kind':'probe','agreement':'partial'}],'selected':['s1:h1','s1:h2']}))
                for hid,card in (('h1',{'status':'built','route':'params','shadow':f'{sid}-h1'}),('h2',{'status':'measured','route':'probe','horizons':{}})):
                    (d/'build'/hid).mkdir(parents=True); (d/'build'/hid/'build_card.json').write_text(json.dumps(card))
                t._save_pool(t.pool()+[{'id':f'{sid}-h1','study_id':sid,'kind':'params','hypothesis':'idea 1','overrides':{},'status':'SHADOW','started_ms':1}])
                t.evaluate=lambda e: g; lab.set_stage(sid,'testing'); return sid
            promoted=[]; t._propose_promotion=lambda e,ev: (promoted.append(e['id']), e.update(status='PASS_PENDING'))
            sid=study({'verdict':'WAITING','why':'failing: t-stat','candidate':{'n':3},'age_h':2})
            assert lab.run_evaluate(sid,[],None,call=fake)=={'ready':False,'waiting':{'H1':'failing: t-stat'}}
            t.evaluate=lambda e: {'verdict':'PASS','why':'all PASS rules met','candidate':{'n':40},'age_h':30}
            ev=lab.run_evaluate(sid,[],None,call=fake); x=ev['subjects']
            assert x['H1']['action']=='promote' and x['H1']['agreement']=='agreed' and promoted==[f'{sid}-h1']
            assert x['H2']['action']=='close' and x['H2']['back_to_scientists'] and lab.study(sid)['stage']=='done'
            m=json.loads((t.think_dir/'studies'/sid/'study.json').read_text())
            assert m['evaluation']['actions']=={'s1:h1':'promote','s1:h2':'close'} and m['prompts']['evaluators'].startswith('evaluator.md@') and m['reduced_independence']['evaluators'] is False
            assert (t.think_dir/'studies'/sid/'evaluation'/'pass1'/'evaluators'/'a.json').is_file()
            recs['b']='retire'; sid2=study({'verdict':'PASS','why':'all PASS rules met','candidate':{'n':40},'age_h':30})
            assert lab.run_evaluate(sid2,[],None,call=fake)['subjects']['H1']['action']=='hold' and len(promoted)==1   # split evaluators never promote
            assert lab.study(sid2)['stage']=='testing'   # held: stays under its time limit
            sid3=lab.new_study(); d=t.think_dir/'studies'/sid3; (d/'build'/'h1').mkdir(parents=True)
            (d/'brief.json').write_text(json.dumps({'hypotheses':[{**_hyp(1),'key':'s1:h1','agreement':'agreed'}],'selected':['s1:h1']}))
            (d/'build'/'h1'/'trade.py').write_text((ROOT/'trade'/'trade.py').read_text())
            (d/'build'/'h1'/'build_card.json').write_text(json.dumps({'status':'built','route':'params','overrides':{},'shadow':None,'shadow_pending':True}))
            lab.set_stage(sid3,'testing'); assert lab.pending_shadows()==1 and lab.shadow_room()
            assert lab.run_evaluate(sid3,[],None,call=fake)['ready'] is False   # built, waiting for room (Trade not running in tests)
            lab.shadow_room=lambda: None; lab._start_pending(sid3)
            assert lab.pending_shadows()==0 and any(p['id']==f'{sid3}-h1' and p['status']=='SHADOW' for p in t.pool())
            sc=lab.scorecards(); e={r['member']:r for r in sc['evaluators']}
            assert e['a']['verdicts']==4 and e['b']['agrees_with_team_pct']==75.0 and {r['group'] for r in sc['seats']}=={'evaluators'}
            assert any(n['type']=='lesson' for n in t.notebook(50)) and t.store.get('lab','issues')['issues']
        finally:
            if old is None: os.environ.pop('APP_RUNTIME_DIR',None)
            else: os.environ['APP_RUNTIME_DIR']=old

def test_supervisor_loop_chat_review_and_lab_change():
    import os
    from think.loop import Lab
    from agents.unit import Member
    def fake(member, system, prompt, tools, handler, submit):
        u={'input_tokens':1000,'output_tokens':100,'calls':1}; other='Colleague B' if member.name=='seat-1' else 'Colleague A'
        if submit=='submit_report': return _report([{**_hyp(1),'kind':'note'}]), u, None
        if submit=='submit_review': return {'summary':'all fine','health':'attention' if member.name=='seat-2' else 'ok','alarms':[],'directives':[{'id':'d1','text':f'focus entries {member.name}','reason':'no gross edge'}],'proposals':[]}, u, None
        if submit=='submit_exchange' and 'daily review' in prompt:
            return {'reviews':[{'member':other,'directive_id':'d1','stance':'agree' if member.name=='seat-1' else 'partial','reason':'r'}],
                    'revised_review':{'summary':'all fine','health':'attention' if member.name=='seat-2' else 'ok','alarms':[],'directives':[{'id':'d1','text':f'focus entries {member.name}','reason':'no gross edge'}],'proposals':[]}}, u, None
        return {'reviews':[{'member':other,'hypothesis_id':'h1','stance':'agree','reason':'ok'}],'revised_report':_report([{**_hyp(1),'kind':'note'}])}, u, None
    with tempfile.TemporaryDirectory() as td:
        old=os.environ.get('APP_RUNTIME_DIR'); os.environ['APP_RUNTIME_DIR']=td
        try:
            t=ThinkEngine(ROOT); lab=Lab(t); pair=[{'name':'seat-1','provider':'anthropic','model':'claude-opus-5-5'},{'name':'seat-2','provider':'anthropic','model':'claude-sonnet-5'}]
            lab.update_settings({'auto':True,'min_hours_between_studies':12})   # set what this test needs; an app's config may differ
            for g in ('scientists','supervisor'): lab.update_seats(g,pair)
            r=lab.advance([],None,call=fake); sid=r['study']; assert r['step']=='science' and r.get('new') and lab.study(sid)['stage']=='science_done'
            assert lab.advance([],None,call=fake)['step']=='build' and lab.study(sid)['stage']=='built'           # only a note: nothing to build
            assert lab.advance([],None,call=fake)['step']=='evaluate' and lab.study(sid)['stage']=='done'        # nothing to judge: closed without AI
            assert lab.advance([],None,call=fake)=={'step':'idle','why':'too soon after the last study'}
            lab.update_settings({'min_hours_between_studies':0,'max_active_studies':1}); lab.set_stage(lab.new_study(),'science','error','boom')
            assert lab.advance([],None,call=lambda *a: (_ for _ in ()).throw(RuntimeError('net')))['step']=='error'   # transient error: retried later
            lab.update_settings({'auto':False}); assert lab.advance([],None,call=fake)['why']=='automatic loop is off'
            for bad in ({'budget_usd_per_day':-1},{'nope':1},{'max_active_studies':9}):
                try: lab.check_settings(bad)
                except ValueError: pass
                else: raise AssertionError(f'accepted {bad}')
            eng=ManageEngine(ROOT); sup=eng.supervisor; sup._lab=lab
            eng.store.put('settings','manage_agent',{'provider':'mock','fallback_provider':'anthropic','reasoning':'high'})
            seat=sup.chat_seat(); assert seat.provider=='mock' and seat.fallback['provider']=='anthropic'   # default: the Manage settings page
            r=eng.chat('status?'); assert r['answer'].startswith('[mock]')
            with t.store.connect() as db: assert db.execute("select count(*) from agent_outputs where study_id='chat' and grp='supervisor'").fetchone()[0]==1
            aid=sup.propose_change(settings={'budget_usd_per_day':5},group='supervisor',key='chat',seats=[{'provider':'openai','model':'gpt-5.6-sol','fallback':{'provider':'anthropic','model':'claude-sonnet-5'}}],reason='cheaper')
            try: sup.propose_change(group='supervisor',key='chat',seats=pair,reason='x')
            except ValueError: pass
            else: raise AssertionError('two chat seats accepted')
            res=eng.approve_action(aid); assert res['ok'] and lab.load_config()['budget_usd_per_day']==5.0
            seat=sup.chat_seat(); assert (seat.provider,seat.model,seat.fallback['model'])==('openai','gpt-5.6-sol','claude-sonnet-5')
            rv=sup.daily_review(call=fake,force=True)
            assert rv['health']=='attention' and [d['by'] for d in rv['directives_applied']]==['seat-2'] and [d['by'] for d in rv['directives_disputed']]==['seat-1']
            assert [d['text'] for d in sup.directives()]==['focus entries seat-2'] and sup.daily_review(call=fake) is None   # once a day
            assert any(n['type']=='report' for n in t.notebook(50)) and sup.overview(full=False)['last_review']['health']=='attention'
            from manage.supervisor import similar
            a='Every probe or edge test must name one decision horizon in its pre-registration, or else require t>=2.5 (Bonferroni over the 4 horizons tested). Forward-return windows must not overlap, or t-stats must come from a block bootstrap. Setup x direction cells with fewer than 10 signals are exploratory only.'
            b='Every probe or edge test must pre-specify a single decision horizon, or else require t>=2.5 (Bonferroni over horizons tested). Forward-return windows must not overlap or must use a block bootstrap. Setup x direction cells with fewer than ~10 signals are exploratory only, not decision-grade.'
            c='Pause new hypotheses that tune entry-gate parameters. First run an adequately powered forward-return test of whether confirmed setups move at least the ~13 bps round-trip cost at a pre-specified horizon.'
            d='Each pre-registration must state the expected signal rate and show that the stop rule can plausibly reach min_trades within the time cap.'
            assert similar(a,b) and not similar(a,c) and not similar(a,d) and not similar(c,d)
            try: sup.set_directive('Focus entries SEAT-2','dup',1)
            except ValueError: pass
            else: raise AssertionError('duplicate directive accepted')
            t._save_pool(t.pool()+[{'id':'c9','status':'SHADOW','evaluation':{'candidate':{'n':31,'expectancy_bps':-8.6,'t_stat':-3.2}}}])
            try: sup.propose_retire('nope','x')
            except ValueError: pass
            else: raise AssertionError('unknown candidate accepted')
            assert eng.approve_action(sup.propose_retire('c9','losing'))['ok'] and t.apply_retire_requests()==1
            assert next(p for p in t.pool() if p['id']=='c9')['status']=='RETIRED'
            t._set_state(lab_last={'step':'idle','why':'no free shadow slot','since':time.time()-90000})
            assert any('idle for 25 h' in x for x in sup.stuck()) and sup.overview(full=False)['stuck']
            for i in range(4): sup.set_directive(f'x{i}','y',1)
            try: sup.set_directive('too many','y',1)
            except ValueError: pass
            else: raise AssertionError('directive cap ignored')
            assert sup.read_study(sid,'manifest')['id']==sid
            try: sup.read_study('../../etc','manifest')
            except ValueError: pass
            else: raise AssertionError('bad study id accepted')
        finally:
            if old is None: os.environ.pop('APP_RUNTIME_DIR',None)
            else: os.environ['APP_RUNTIME_DIR']=old

def test_workers_triage_params_code_probe():
    import os
    from agents.unit import Member
    from think.workers import triage, apply_edits, build_params, build_code, forward_return_probe
    assert triage({'kind':'code','change':{'description':'Instrumentation only; trading decisions do not change.'}})[0]=='probe'
    assert triage({'kind':'params'})==('params',None)
    src=REF.read_text()
    try: apply_edits(src,[{'old_text':'TRADE_CONTRACT_VERSION','new_text':'X'}])
    except ValueError: pass
    else: raise AssertionError('edit outside editable section accepted')
    with tempfile.TemporaryDirectory() as td:
        old=os.environ.get('APP_RUNTIME_DIR'); os.environ['APP_RUNTIME_DIR']=td
        try:
            t=ThinkEngine(ROOT); t.trade_file=REF; t.rec_dir=Path(td)/'recordings'; out=Path(td)/'b'; out.mkdir()
            assert build_params(t,{'change':{'params':{'max_stop_bps':[t.tunables()['max_stop_bps']]}}},out,1)['status']=='rejected'  # equal to champion
            c=build_params(t,{'change':{'params':{'max_stop_bps':[16.0]}}},out,1)
            assert c['status']=='built' and c['overrides']=={'max_stop_bps':16.0} and 'max_stop_bps: float = 16.0' in (out/'trade.py').read_text()
            edit={'old_text':'normal_cooldown_seconds: int = 30','new_text':'normal_cooldown_seconds: int = 31'}
            big={'old_text':'loss_cooldown_seconds: int = 90','new_text':'loss_cooldown_seconds: int = 90\n    # extra line'}
            def fake(member, system, prompt, tools, handler, submit):
                u={'input_tokens':100,'output_tokens':50,'calls':1}
                b={'edits':[edit] if member.name=='a' else [edit,big],'summary':'s','spec_mapping':[{'point':'cooldown','how':'31'}]}
                if submit=='submit_build': return b,u,None
                return {'reviews':[{'member':'Colleague B' if member.name=='a' else 'Colleague A','verdict':'equivalent','reason':'same decisions'}],'revised_build':b},u,None
            out2=Path(td)/'c'; out2.mkdir(); rec=[]
            card=build_code(t,{'key':'k','title':'t','change':{'description':'cooldown 31'}},out2,[Member('a','anthropic','claude-opus-5-5'),Member('b','anthropic','claude-sonnet-5')],
                            rec.append,lambda:True,[],None,1,2,call=fake)
            assert card['status']=='built' and card['chosen_build']=='a' and card['decision_groups']==[['a','b']] and card['changed_lines']==2
            assert 'normal_cooldown_seconds: int = 31' in (out2/'trade.py').read_text() and len(rec)==4
            p=forward_return_probe(t,min_signals=5,max_days=0.05)
            assert p['status']=='measured' and p['signals']==0 and p['complete'] is False and not p['any_horizon_beats_cost'] and set(p['horizons'])=={'1m','5m','15m','30m'}
        finally:
            if old is None: os.environ.pop('APP_RUNTIME_DIR',None)
            else: os.environ['APP_RUNTIME_DIR']=old

def test_root_env_loading_and_precedence():
    import os, tempfile
    from pathlib import Path
    from core.common import load_root_env
    key1='TEMPLATE_TEST_ENV_LOAD'; key2='TEMPLATE_TEST_ENV_KEEP'
    old1=os.environ.pop(key1,None); old2=os.environ.get(key2)
    os.environ[key2]='service-wins'
    try:
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'.env').write_text(f'{key1}=from-file\n{key2}=from-file\n')
            status=load_root_env(root)
            assert status['present'] and status['loaded']
            assert os.environ[key1]=='from-file'
            assert os.environ[key2]=='service-wins'
    finally:
        if old1 is None: os.environ.pop(key1,None)
        else: os.environ[key1]=old1
        if old2 is None: os.environ.pop(key2,None)
        else: os.environ[key2]=old2


_CURRENT_REF=REF
def _trade_mod(name='test_trade_v2', ref=None):
    from core.common import import_module
    return import_module(ref or _CURRENT_REF,name)

def test_checksum_gate_hides_unverified_book_evidence():
    m=_trade_mod('gate'); r=m.create_trade_system(); r.initialize({}); r.input.mark_connected(); r.input.mark_subscribed('book'); r.input.mark_subscribed('trades'); n=time.time_ns(); r.on_l3_snapshot([[1,'100','1'],[2,'101','-1']],1,n); assert len(r.core.event_history)==0; cs=r.input.book.checksum(); r.on_l3_checksum(cs,2,n+1_000_000); assert [x.kind for x in r.core.event_history][:2]==['l3_snapshot','l3_checksum']; assert r.health()['ok']

def test_public_trade_preserves_raw_fields():
    m=_trade_mod('trades'); r=m.create_trade_system(); r.initialize({}); r.input.mark_connected(); r.input.mark_subscribed('book'); r.input.mark_subscribed('trades'); n=time.time_ns(); r.on_l3_snapshot([[1,'100','1'],[2,'101','-1']],1,n); r.on_l3_checksum(r.input.book.checksum(),2,n+1); raw='[12,"te",[77,123456,0.03,100.05],3]'; r.on_public_trade([77,123456,m.Decimal('0.03'),m.Decimal('100.05')],3,n+2,12,raw); e=r.core.event_history[-1]; assert e.kind=='public_trade' and e.payload[0]==77 and e.payload[1]==123456 and str(e.payload[2])=='0.03' and str(e.payload[3])=='100.05' and e.raw_message==raw

def test_tiny_decimal_checksum_serialization_regression():
    m=_trade_mod('tiny'); assert m._checksum_value(m.Decimal('0.00000003'))=='3e-8'; assert m._checksum_value(m.Decimal('0.000001'))=='0.000001'; assert m._checksum_value(m.Decimal('0.0000001'))=='1e-7'

def test_malformed_snapshot_fails_closed():
    m=_trade_mod('malformed'); r=m.create_trade_system(); r.initialize({}); r.input.mark_connected(); r.input.mark_subscribed('book'); r.input.mark_subscribed('trades')
    try: r.on_l3_snapshot([[1,'100']],1,time.time_ns())
    except m.L3IntegrityError: return
    raise AssertionError('malformed snapshot accepted')

def test_mock_bitfinex_websocket_parser_end_to_end():
    import asyncio, json as _json
    m=_trade_mod('mock_ws')
    # Pre-compute checksum for the synthetic raw R0 snapshot.
    b=m._ProtectedL3Book(); now_ms=int(time.time()*1000); snap=[[101,m.Decimal('100.00'),m.Decimal('1.0')],[102,m.Decimal('100.10'),m.Decimal('-1.0')]]; b.apply_snapshot(snap,now_ms); cs=b.checksum()
    frames=[
        _json.dumps({'event':'subscribed','channel':'book','chanId':11,'symbol':'tBTCUSD','prec':'R0','len':'250'}),
        _json.dumps({'event':'subscribed','channel':'trades','chanId':12,'symbol':'tBTCUSD'}),
        '[11,[[101,100.00,1.0],[102,100.10,-1.0]],1]',
        f'[11,"cs",{cs},2]',
        '[12,[[7001,1234567890000,0.02,100.05]],3]',
        '[12,"te",[7002,1234567890100,-0.01,100.04],4]',
        '[12,"tu",[7002,1234567890100,-0.01,100.04],5]',
    ]
    sent=[]
    class WS:
        async def send(self,x): sent.append(_json.loads(x))
        def __init__(self): self.i=0
        async def recv(self):
            if self.i>=len(frames): raise StopAsyncIteration
            x=frames[self.i]; self.i+=1; return x
    class CM:
        async def __aenter__(self): return WS()
        async def __aexit__(self,*a): return False
    class FakeWebsockets:
        @staticmethod
        def connect(*a,**k): return CM()
    async def run():
        r=m.create_trade_system(market_config={'symbol':'tBTCUSD'}); r.initialize({}); feed=m._BitfinexPublicFeed(r,{'symbol':'tBTCUSD'})
        try: await feed._run_connection(FakeWebsockets)
        except StopAsyncIteration: pass
        return r
    r=asyncio.run(run())
    kinds=[e.kind for e in r.core.event_history]
    assert kinds[:2]==['l3_snapshot','l3_checksum']
    assert 'public_trade_snapshot' in kinds and 'public_trade' in kinds
    assert r.input.last_sequence==5
    assert r.input.checksum_count==1 and r.input.checksum_mismatches==0
    assert any(x.get('channel')=='book' and x.get('prec')=='R0' for x in sent)
    assert any(x.get('channel')=='trades' for x in sent)
    assert any(x.get('event')=='conf' and x.get('flags')==196608 for x in sent)

def test_split_sequence_tolerates_exchange_timestamps():
    mm=_trade_mod('split',REF22); assert mm.TRADE_CONTRACT_VERSION=='2.2'
    F=mm._BitfinexPublicFeed; ts=1790000000123
    assert F._split_sequence([11,[1,'100','1'],7])==([11,[1,'100','1']],7) and F._split_sequence([11,[1,'100','1'],7,ts])==([11,[1,'100','1']],7)
    assert F._split_sequence([11,'cs',-12345,9,ts])==([11,'cs',-12345],9) and F._split_sequence([11,'hb',3,ts])==([11,'hb'],3)

def test_tt_input_source_snapshot_then_exact_messages():
    import asyncio, json as _json
    m=_trade_mod('tti_src',REF22); ts=1790000000000; assert m.TRADE_CONTRACT_VERSION=='2.2'
    b=m._ProtectedL3Book(); b.apply_snapshot([[101,m.Decimal('100.00'),m.Decimal('1.0')],[102,m.Decimal('100.10'),m.Decimal('-1.0')]],1)
    b.apply_update([103,m.Decimal('99.90'),m.Decimal('2.0')],1); cs=b.checksum()
    frames=[_json.dumps({'type':'hello','mode':'raw'}),
            _json.dumps({'type':'snapshot','source':'tt_input','at_seq':40,'t_recv':ts,'channels':{'11':'book','12':'trades'},
                         'orders':[[101,'100.00','1.0'],[102,'100.10','-1.0']]}),
            _json.dumps({'type':'raw','r':ts,'m':'[11,[103,99.90,2.0],41,%d]'%ts}),
            _json.dumps({'type':'raw','r':ts,'m':'[12,"te",[7002,%d,-0.01,100.04],42,%d]'%(ts,ts)}),
            _json.dumps({'type':'raw','r':ts,'m':'[11,"hb",43,%d]'%ts}),
            _json.dumps({'type':'raw','r':ts,'m':'[11,"cs",%d,44,%d]'%(cs,ts)})]
    sent=[]
    class WS:
        def __init__(self): self.i=0
        async def send(self,x): sent.append(_json.loads(x))
        async def recv(self):
            if self.i>=len(frames): raise StopAsyncIteration
            x=frames[self.i]; self.i+=1; return x
    class CM:
        async def __aenter__(self): return WS()
        async def __aexit__(self,*a): return False
    class FakeWebsockets:
        @staticmethod
        def connect(*a,**k): return CM()
    async def run():
        r=m.create_trade_system(market_config={'symbol':'tBTCUSD','source':'tt_input'}); r.initialize({}); feed=m._BitfinexPublicFeed(r,r.market_config)
        try: await feed._run_tt_input(FakeWebsockets)
        except StopAsyncIteration: pass
        return r
    r=asyncio.run(run())
    assert sent==[{'op':'subscribe','feed':'bitfinex.tBTCUSD','mode':'raw'}]
    assert r.input.last_sequence==44 and r.input.checksum_count==1 and r.input.checksum_mismatches==0 and r.input.sequence_gaps==0
    kinds=[e.kind for e in r.core.event_history]
    assert kinds[:4]==['l3_snapshot','l3_update','public_trade','l3_checksum'] and r.health()['ok']
    e=[x for x in r.core.event_history if x.kind=='l3_update'][0]; assert e.raw_message=='[11,[103,99.90,2.0],41,%d]'%ts  # the exchange text, unchanged

def test_tt_input_notice_marks_data_untrusted():
    m=_trade_mod('tti_notice',REF22); assert m.TRADE_CONTRACT_VERSION=='2.2'
    r=m.create_trade_system(market_config={'source':'tt_input'}); r.initialize({})
    r.input.mark_connected(); r.input.mark_subscribed('book'); r.input.mark_subscribed('trades'); n=time.time_ns()
    r.on_l3_snapshot([[1,'100','1'],[2,'101','-1']],1,n); r.on_l3_checksum(r.input.book.checksum(),2,n+1); assert r.health()['ok']
    r.input.reset_connection('tt_input_upstream_lost:test'); assert not r.health()['ok']

def test_update_and_trade_wait_for_checksum_then_release_in_order():
    m=_trade_mod('gate_order'); r=m.create_trade_system(); r.initialize({}); r.input.mark_connected(); r.input.mark_subscribed('book'); r.input.mark_subscribed('trades'); n=time.time_ns(); r.on_l3_snapshot([[1,'100','1'],[2,'101','-1']],1,n); r.on_l3_checksum(r.input.book.checksum(),2,n+1); before=len(r.core.event_history)
    r.on_l3_update([1,'100','1.2'],3,n+2); r.on_public_trade([9,123456,m.Decimal('0.01'),m.Decimal('100.5')],4,n+3); assert len(r.core.event_history)==before
    r.on_l3_checksum(r.input.book.checksum(),5,n+4); kinds=[x.kind for x in list(r.core.event_history)[before:]]; assert kinds==['l3_update','public_trade','l3_checksum']

def test_protected_layer_on_contract_22():
    """The protected-layer tests above, repeated on the contract 2.2 reference: 2.2 must be at least as strict as 2.1."""
    global _CURRENT_REF
    _CURRENT_REF=REF22
    try:
        for fn in (test_checksum_gate_hides_unverified_book_evidence,test_public_trade_preserves_raw_fields,test_tiny_decimal_checksum_serialization_regression,
                   test_malformed_snapshot_fails_closed,test_mock_bitfinex_websocket_parser_end_to_end,test_update_and_trade_wait_for_checksum_then_release_in_order):
            fn()
    finally: _CURRENT_REF=REF

def test_strategy_starter_stays_valid():
    """examples/starter is what new strategies start from: it must keep passing the contract (2.2, tt_input), the
    platform interface and its own scenarios (never trades as shipped; stop / target / trail / max hold / cooldown work)."""
    import dataclasses, importlib.util
    starter=ROOT/'examples'/'starter'
    p=subprocess.run([sys.executable,str(ROOT/'trade'/'validate_trade_contract.py'),str(starter/'trade.py'),'--canonical',str(REF22),'--run-self-test'],
                     capture_output=True,text=True,timeout=60)
    assert p.returncode==0 and json.loads(p.stdout)['ok'], p.stdout[-800:]
    spec=importlib.util.spec_from_file_location('starter_scenarios',starter/'scenario_test.py'); sc=importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)
    sc.test_never_trades_as_shipped(starter/'trade.py'); sc.test_risk_exits(starter/'trade.py')
    m=sc.load(starter/'trade.py'); core=m.ScientificCore(m.TradeConfig())
    assert m.TRADE_CONTRACT_VERSION=='2.2' and dataclasses.is_dataclass(m.TradeConfig) and isinstance(core.position_state,dict) and isinstance(core.cooldown_until_ms,int)

def test_auto_provider_follows_the_configured_keys():
    """Installed apps start with the chat on provider 'auto': it must use whichever key exists, and mock without keys."""
    import os
    from core.common import AgentProvider, resolve_provider
    saved={k:os.environ.pop(k,None) for k in ('ANTHROPIC_API_KEY','OPENAI_API_KEY')}
    try:
        assert resolve_provider('auto')=='mock' and AgentProvider({'provider':'auto'}).run('ping',system='s').startswith('[mock]')
        os.environ['OPENAI_API_KEY']='x'; assert resolve_provider('auto')=='openai'
        os.environ['ANTHROPIC_API_KEY']='x'; assert resolve_provider('auto')=='anthropic' and resolve_provider('openai')=='openai'
    finally:
        for k,v in saved.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v

def test_ui_theme_palette_and_contrast():
    """The UI's colours come only from ui/theme.json, and every text colour is readable on the navy background."""
    import re
    theme=json.loads((ROOT/'ui'/'theme.json').read_text(encoding='utf-8')); colors={k:v['hex'] for k,v in theme['colors'].items()}
    assert set(colors)=={'navy','text','muted','up','down','brand'} and all(re.fullmatch(r'#[0-9A-Fa-f]{6}',h) for h in colors.values()), colors
    def lum(h):
        c=[int(h[i:i+2],16)/255 for i in (1,3,5)]; c=[x/12.92 if x<=0.03928 else ((x+0.055)/1.055)**2.4 for x in c]
        return 0.2126*c[0]+0.7152*c[1]+0.0722*c[2]
    bg=lum(colors['navy'])
    for k in ('text','muted','up','down','brand'):
        ratio=(lum(colors[k])+0.05)/(bg+0.05); assert ratio>=theme['min_contrast_on_navy'], f'{k} contrast {ratio:.1f}:1 on navy'
    src=(ROOT/'ui'/'ui.py').read_text(encoding='utf-8')
    assert not re.findall(r'#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?\b|rgba?\(',src), 'ui/ui.py must take colours from ui/theme.json'
    from ui.ui import THEME_VARS, CSS
    assert all(f'--{k}:{h};' in THEME_VARS for k,h in colors.items()) and 'var(--navy)' in CSS

def test_ui_trade_chart():
    """Trade page chart: vendored Lightweight Charts (checksum), any Bitfinex time frame, older pages via end=, chart above the Daily check."""
    import hashlib, io, os, urllib.request
    from ui import ui as U
    js=ROOT/'ui'/'static'/'lightweight-charts.standalone.production.js'
    assert hashlib.sha256(js.read_bytes()).hexdigest()=='e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf'
    assert set(U.STATIC_FILES)=={js.name} and all((ROOT/'ui'/'static'/n).is_file() for n in U.STATIC_FILES)
    old=os.environ.get('APP_RUNTIME_DIR'); td=tempfile.mkdtemp(); os.environ['APP_RUNTIME_DIR']=td; real=urllib.request.urlopen; seen=[]
    def fake(req,timeout=0):
        seen.append(req.full_url); return io.BytesIO(json.dumps([[1_700_000_060_000,2,3,4,1,5.5],[1_700_000_000_000,1,2,3,0.5,1.0]]).encode())
    try:
        urllib.request.urlopen=fake; app=U.App(ROOT); U.App._candles={}
        for tf,step in U.CANDLE_TFS.items():
            r=app.candles(tf); assert r['tf']==tf and r['step']==step and r['candles'][0][0]<r['candles'][1][0] and f'trade:{tf}:' in seen[-1] and 'limit=500' in seen[-1], (tf,seen[-1])
        r=app.candles('2m'); assert r['tf']=='1m' and r['step']==60_000 and r['candles']           # unknown time frame -> 1m
        app.candles('1h',str(1_700_000_000_000)); assert seen[-1].endswith('&end=1699999999999'), seen[-1]
        page=app.render('/trade/overview'); assert page.index('id="chartcard"')<page.index('Daily check') and '/static/'+js.name in page
    finally:
        urllib.request.urlopen=real; U.App._candles={}
        if old is None: os.environ.pop('APP_RUNTIME_DIR',None)
        else: os.environ['APP_RUNTIME_DIR']=old

def test_ui_theme_sizes():
    """Spacing, radii, heights and font sizes in the UI stylesheet come from ui/theme.json "sizes" (1-3px hairlines allowed)."""
    import re
    theme=json.loads((ROOT/'ui'/'theme.json').read_text(encoding='utf-8')); sizes=theme['sizes']
    assert all(re.fullmatch(r'\d+px',v) for v in sizes.values()), sizes
    from ui.ui import CSS, THEME_VARS
    assert all(f'--{k}:{v};' in THEME_VARS for k,v in sizes.items())
    bad=[m.group(0) for m in re.finditer(r'(?:padding|margin|gap|font-size|border-radius|min-height|height|max-width)(?:-[a-z]+)?:[^;{}]*?\b(?:[4-9]|\d{2,})px',re.sub(r'@media\([^)]*\)','@media()',CSS))]   # breakpoints cannot use CSS variables
    assert not bad, f'hard-coded sizes in ui/ui.py CSS (use theme sizes): {bad[:5]}'
