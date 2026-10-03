from __future__ import annotations
import argparse, html, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core.common import AGENT_OPTIONS, MODEL_CHOICES, Health, Store, load_config, resolve
from manage.manage import WORKERS, ManageEngine, edit_diff
from think.think import ThinkEngine
from think.replay import stats as stats_of

# Look and feel: colours and fonts come only from ui/theme.json (as CSS variables); see its "rules".
THEME=json.loads(Path(__file__).with_name('theme.json').read_text(encoding='utf-8'))
THEME_VARS=':root{'+''.join(f'--{k}:{v["hex"]};' for k,v in THEME['colors'].items())+''.join(f'--{k}:{v["value"]};' for k,v in THEME['tints'].items())+''.join(f'--{k}:{v};' for k,v in THEME['fonts'].items())+''.join(f'--{k}:{v};' for k,v in THEME['sizes'].items())+'color-scheme:dark}'
CSS='''body{font-family:var(--sans);font-size:var(--fs-md);line-height:1.45;margin:0;background:var(--navy);color:var(--text)}.wrap{max-width:var(--page-max);margin:auto;padding:0 var(--s5) var(--s6)}
.row{display:flex;gap:var(--s3);align-items:center;flex-wrap:wrap;justify-content:space-between}
.top{display:flex;justify-content:space-between;align-items:center;gap:var(--s3);flex-wrap:wrap;padding:var(--s3) 0;border-bottom:1px solid var(--line);margin-bottom:var(--s4)}.top>b{color:var(--brand);font-size:var(--fs-lg);letter-spacing:.02em}.top small{color:var(--muted);font-size:var(--fs-sm)}
.tabs{display:inline-grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:var(--s1);padding:var(--s1);border:1px solid var(--line);border-radius:var(--r-card);margin-bottom:var(--s3)}
.tabs a{display:flex;align-items:center;justify-content:center;min-width:var(--tab-min);height:var(--h-sm);padding:0 var(--s4);border-radius:var(--r-ctl);text-decoration:none;color:var(--muted);font-weight:700;font-size:var(--fs-sm);letter-spacing:.08em}.tabs a:hover{color:var(--text);background:var(--hover)}.tabs a.active{background:var(--line);color:var(--text)}
.sub{display:flex;gap:var(--s5);border-bottom:1px solid var(--line);margin-bottom:var(--s4);overflow-x:auto;scrollbar-width:none}.sub a{display:flex;align-items:center;gap:var(--s1);height:var(--h-md);text-decoration:none;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap}.sub a:hover{color:var(--text)}.sub a.active{color:var(--text);border-bottom-color:var(--text)}
.card{background:var(--navy);border:1px solid var(--line);border-radius:var(--r-card);padding:var(--card-pad);margin:0 0 var(--s4)}.card h2{margin:0 0 var(--s3);font-size:var(--fs-lg);font-weight:600;color:var(--text)}.card>.row:first-child{margin-bottom:var(--s3)}.card>.row:first-child h2{margin:0}.card>.row:first-child small{font-size:var(--fs-sm)}
h3{font-size:var(--fs-sm);font-weight:600;margin:var(--s3) 0 var(--s1);color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(var(--col-min),1fr));gap:var(--s4);margin-bottom:var(--s4)}.grid>.card{margin:0}
@media(min-width:900px){.card{padding:var(--card-pad-wide)}}
.kv{display:flex;justify-content:space-between;align-items:center;gap:var(--s3);min-height:var(--h-sm);padding:var(--s1) 0;border-bottom:1px solid var(--line)}.kv>span:first-child{color:var(--muted)}.kv b,td,.strip b,.num,#px{font-variant-numeric:tabular-nums}#px,.strip b,.num{font-family:var(--mono)}
.ok{color:var(--up)}.bad{color:var(--down)}.warn{color:var(--muted)}.warn::before{content:"\\25C6\\00a0"}.muted{color:var(--muted)}a{color:var(--muted)}a:hover{color:var(--text)}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px}input,textarea,select,button{font:inherit;min-height:var(--h-sm);padding:0 var(--s3);border:1px solid var(--input);border-radius:var(--r-ctl);background:var(--navy);color:var(--text);box-sizing:border-box}textarea{width:100%;min-height:calc(var(--h-md)*2);padding:var(--s2) var(--s3);margin-top:var(--s3)}
button{background:transparent;border-color:var(--muted);cursor:pointer}button:hover{background:var(--hover)}button:disabled{opacity:.5;cursor:wait}button.ghost{border-color:var(--line)}button.reject{color:var(--down);border-color:var(--down)}button.small{min-height:var(--h-xs);padding:0 var(--s2)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--hover);padding:var(--s3);border-radius:var(--r-ctl);font-size:var(--fs-sm);margin:var(--s2) 0;font-family:var(--mono)}code{background:var(--hover);padding:1px var(--s1);border-radius:var(--r-ctl);font-family:var(--mono)}
.pool{display:grid;grid-template-columns:1fr auto auto;gap:var(--s2);padding:var(--s2) 0;border-bottom:1px solid var(--line)}#log{max-height:60vh;overflow-y:auto}.msg{padding:var(--s3);border-radius:var(--r-card);margin:var(--s2) 0;border:1px solid var(--line)}.msg.user{background:var(--hover);margin-left:15%;white-space:pre-wrap}.msg.error{color:var(--down);border-color:var(--down)}.msg h3,.msg h4{margin:var(--s2) 0}.msg ul{margin:var(--s1) 0;padding-left:var(--s5)}
.diff .add{color:var(--up)}.diff .del{color:var(--down)}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:var(--s2);border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.chatwrap{display:grid;grid-template-columns:var(--chat-list-w) 1fr;gap:var(--s4);align-items:start}.chats{padding:var(--s3);max-height:75vh;overflow-y:auto}.chats button{width:100%;margin-bottom:var(--s2)}.chats a{display:block;padding:var(--s2);border-radius:var(--r-ctl);text-decoration:none;color:var(--muted)}.chats a:hover{background:var(--hover)}.chats a.active{background:var(--hover);color:var(--text)}.chats small{display:block;opacity:.7;font-size:var(--fs-sm)}
@media(max-width:700px){.chatwrap{grid-template-columns:1fr}.chats{max-height:30vh}}@media(max-width:600px){.wrap{padding:0 var(--s3) var(--s5)}.tabs{display:grid;width:100%;box-sizing:border-box}.tabs a{min-width:0}.sub{gap:var(--s4)}.pool{grid-template-columns:1fr auto}.msg.user{margin-left:5%}}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}table.daily td:first-child,table.daily th:first-child{position:sticky;left:0;background:var(--navy);z-index:1}table.daily{background:var(--navy)}
.lv-g{border-left:3px solid var(--up)}.lv-a{border-left:3px solid var(--muted)}.lv-r{border-left:3px solid var(--down)}.grp td{color:var(--muted);font-size:var(--fs-sm);padding-top:var(--s3);border:0}
.seg{display:inline-flex;border:1px solid var(--input);border-radius:var(--r-ctl);overflow:hidden}.seg button{background:transparent;color:var(--muted);border:0;border-radius:0;min-height:var(--h-xs);padding:0 var(--s3)}.seg button.on{background:var(--line);color:var(--text)}
.banner{padding:var(--s2) var(--s3);border-radius:var(--r-ctl);margin:var(--s1) 0 var(--s2);border:1px solid}.ok-bg{border-color:var(--up);color:var(--up)}.warn-bg{border-color:var(--muted);color:var(--muted)}.bad-bg{border-color:var(--down);color:var(--down)}
.barwrap{flex:1;height:var(--bar-h);background:var(--line);border-radius:var(--r-pill);margin:0 var(--s3);align-self:center;min-width:40px}.barwrap i{display:block;height:var(--bar-h);border-radius:var(--r-pill);background:var(--muted)}.barwrap.wide{margin:2px 0 var(--s2)}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(95px,1fr));gap:var(--s3);text-align:center}.strip b{display:block;font-size:var(--fs-xl);margin-top:2px}
.filters{display:flex;gap:var(--s2);flex-wrap:wrap}.filters select{padding:0 var(--s2)}.tcard{display:block;text-decoration:none;color:inherit;padding:var(--s3) var(--s2);border-bottom:1px solid var(--line);border-radius:var(--r-ctl)}.tcard:hover,.tcard.on{background:var(--hover)}
.chip{display:inline-block;padding:var(--s1) var(--s3);border:1px solid var(--input);border-radius:var(--r-pill);margin:0 var(--s1) var(--s2) 0;text-decoration:none;color:var(--muted);font-size:var(--fs-sm)}.chip.on{background:var(--line);color:var(--text);border-color:var(--muted)}
.tl{border-left:2px solid var(--line);padding:2px 0 var(--s3) var(--s3);margin-left:var(--s1)}details summary{cursor:pointer;color:var(--muted);margin:var(--s2) 0}
svg .up{fill:var(--up);stroke:var(--up)}svg .dn{fill:var(--down);stroke:var(--down)}svg .ln{fill:none;stroke:var(--muted)}svg .mark{fill:var(--text)}
.chartbox{height:var(--chart-h)}#chartcard:fullscreen{margin:0;border-radius:0;display:flex;flex-direction:column;background:var(--navy)}#chartcard:fullscreen .chartbox{flex:1;height:auto}#tfs{flex-wrap:wrap}.legend{min-height:var(--s5);font-size:var(--fs-sm);margin:0 0 var(--s2);font-variant-numeric:tabular-nums}.legend b{margin-right:var(--s2);font-weight:600}@media(max-width:600px){.chartbox{height:var(--chart-h-mobile)}}'''

# Shared page JS: JSON POST helper and a live-refreshing health badge.
JS='''async function post(u,d){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d||{})});let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw Error(j.error||("HTTP "+r.status));return j}
async function badge(){try{const h=(await (await fetch("/api/health")).json()).health,now=Date.now()/1000;document.getElementById("badge").innerHTML=["manage","think","trade","ui"].map(k=>{const x=h[k]||{},s=(now-(x.updated_at||0))>15?"STALE":(x.state||"?");return k[0].toUpperCase()+k.slice(1)+' <b class="'+({HEALTHY:"ok",PAUSED:"warn",DEGRADED:"warn"}[s]||"bad")+'">'+s+"</b>"}).join(" · ")}catch(e){}}
badge();setInterval(badge,5000);
setInterval(async()=>{const a=document.activeElement;if(document.hidden||!document.querySelector("[data-live]")||(a&&["INPUT","TEXTAREA","SELECT"].includes(a.tagName)))return;
try{const d=new DOMParser().parseFromString(await (await fetch(location.href)).text(),"text/html");document.querySelectorAll("[data-live]").forEach(el=>{const n=d.getElementById(el.id);if(n){const o=[...el.querySelectorAll("details")].map(x=>x.open);el.innerHTML=n.innerHTML;el.querySelectorAll("details").forEach((x,i)=>{if(o[i]!==undefined)x.open=o[i]})}})}catch(e){}},10000);'''

# Trade > Overview chart: TradingView Lightweight Charts (ui/static, vendored) fed by /api/trade/candles (Bitfinex candles,
# older history loaded when you scroll left); crosshair legend shows OHLC + volume; trade entries/exits as markers.
STATIC_FILES={'lightweight-charts.standalone.production.js':'application/javascript; charset=utf-8'}
CANDLE_TFS={'1m':60_000,'5m':300_000,'15m':900_000,'30m':1_800_000,'1h':3_600_000,'3h':10_800_000,'6h':21_600_000,'12h':43_200_000,'1D':86_400_000,'1W':604_800_000}
CHART_JS='''const CV=n=>getComputedStyle(document.documentElement).getPropertyValue("--"+n).trim();
let TF="1m",chart,cs,vs,mk,bars=[],vols=[],marks=[],step=60000,gen=0,loading=0,ob=0,done=0,hov=0,spread=null;
const fmt=(v,d)=>v==null?"-":v.toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
function initChart(){const el=document.getElementById("chart");if(!window.LightweightCharts){el.innerHTML='<p class="muted">Chart library not loaded.</p>';return false}const L=LightweightCharts;
chart=L.createChart(el,{autoSize:true,layout:{background:{type:"solid",color:CV("navy")},textColor:CV("muted"),fontFamily:CV("sans")},grid:{vertLines:{color:CV("line")},horzLines:{color:CV("line")}},
rightPriceScale:{borderColor:CV("line")},crosshair:{mode:L.CrosshairMode.Normal},localization:{timeFormatter:t=>new Date(t*1000).toLocaleString([],{dateStyle:"medium",timeStyle:"short"})},
timeScale:{borderColor:CV("line"),timeVisible:true,secondsVisible:false,tickMarkFormatter:(t,k)=>{const d=new Date(t*1000);return k==0?String(d.getFullYear()):k==1?d.toLocaleDateString([],{month:"short"}):k==2?d.toLocaleDateString([],{day:"numeric",month:"short"}):d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}}});
cs=chart.addSeries(L.CandlestickSeries,{upColor:CV("up"),downColor:CV("down"),wickUpColor:CV("up"),wickDownColor:CV("down"),borderVisible:false,priceFormat:{type:"price",precision:0,minMove:1}});
vs=chart.addSeries(L.HistogramSeries,{priceFormat:{type:"volume"},priceScaleId:"vol",lastValueVisible:false,priceLineVisible:false});
chart.priceScale("vol").applyOptions({scaleMargins:{top:0.82,bottom:0}});cs.priceScale().applyOptions({scaleMargins:{top:0.06,bottom:0.22}});mk=L.createSeriesMarkers(cs,[]);
chart.subscribeCrosshairMove(p=>{hov=!!(p&&p.time);const c=hov?p.seriesData.get(cs):null,v=hov?p.seriesData.get(vs):null;c?legend(c,v&&v.value):legendLast()});
chart.timeScale().subscribeVisibleLogicalRangeChange(r=>{head();if(r&&r.from<15)older()});return true}
function legendLast(){if(bars.length)legend(bars[bars.length-1],vols.length?vols[vols.length-1].value:null)}
function legend(c,v){const i=bars.findIndex(x=>x.time===c.time),prev=i>0?bars[i-1].close:c.open,ch=(c.close-prev)/prev*100,cl=ch>=0?"ok":"bad",L=s=>'<span class="muted">'+s+"</span> ";
document.getElementById("legend").innerHTML=L(new Date(c.time*1000).toLocaleString([],{dateStyle:"medium",timeStyle:"short"}))+L("O")+"<b>"+fmt(c.open,0)+"</b>"+L("H")+"<b>"+fmt(c.high,0)+"</b>"+L("L")+"<b>"+fmt(c.low,0)+"</b>"
+L("C")+'<b class="'+cl+'">'+fmt(c.close,0)+'</b><span class="'+cl+'">'+(ch>=0?"▲ +":"▼ ")+ch.toFixed(2)+"%</span> "+L("Vol")+"<b>"+(v==null?"-":fmt(v,2))+"</b>"}
const toBars=r=>r.map(k=>({time:k[0]/1000,open:k[1],close:k[2],high:k[3],low:k[4]})),toVols=r=>r.map(k=>({time:k[0]/1000,value:k[5],color:CV(k[2]>=k[1]?"up_fade":"down_fade")}));
function merge(a,b){const m=new Map();a.concat(b).forEach(x=>m.set(x.time,x));return[...m.values()].sort((x,y)=>x.time-y.time)}
function setMarks(){const t0=bars.length?bars[0].time:0,seen=new Map();marks.forEach(m=>{const t=Math.floor(m.t/step)*step/1000;if(t>=t0)seen.set(t+m.k,{time:t,position:m.k=="buy"?"belowBar":"aboveBar",color:CV(m.k=="buy"?"up":"down"),shape:m.k=="buy"?"arrowUp":"arrowDown"})});
mk.setMarkers([...seen.values()].sort((a,b)=>a.time-b.time))}
async function get(end){const t=TF,j=await(await fetch("/api/trade/candles?tf="+t+(end?"&end="+end:""))).json();if(j.tf!==TF)throw Error("stale");if(j.error&&!(j.candles||[]).length)throw Error(j.error);step=j.step;if(j.spread!=null)spread=j.spread;return j}
function head(){const n=bars.length;if(!n)return;document.getElementById("px").textContent="BTCUSD "+fmt(bars[n-1].close,0);const r=chart.timeScale().getVisibleLogicalRange();
let i=0,k=n-1;if(r){i=Math.min(n-1,Math.max(0,Math.ceil(r.from)));k=Math.max(i,Math.min(n-1,Math.floor(r.to)))}const ch=(bars[k].close-bars[i].open)/bars[i].open*100,h=(bars[k].time-bars[i].time)/3600+step/3600000;
document.getElementById("mk").innerHTML='<span class="'+(ch>=0?"ok":"bad")+'">'+(ch>=0?"+":"")+ch.toFixed(2)+"%</span> over the visible "+(h<48?h.toFixed(1)+" h":(h/24).toFixed(1)+" d")+(spread!=null?" · spread "+spread.toFixed(2)+" bps":"")+" · ▲ buy ▼ sell"}
async function load(){const g=++gen;loading=1;ob=0;done=0;try{const j=await get();if(g!==gen)return;bars=toBars(j.candles);vols=toVols(j.candles);marks=j.marks||[];cs.setData(bars);vs.setData(vols);setMarks();
const n=bars.length;chart.timeScale().setVisibleLogicalRange({from:Math.max(0,n-150),to:n+5});legendLast();head()}catch(e){if(g===gen&&e.message!=="stale")document.getElementById("legend").textContent="Candles unavailable ("+e.message+")"}finally{if(g===gen)loading=0}}
async function older(){if(loading||ob||done||!bars.length)return;const g=gen;ob=1;try{const j=await get(bars[0].time*1000);if(g!==gen)return;const nb=toBars(j.candles);
if(!nb.length||nb[0].time>=bars[0].time)done=1;else{const r=chart.timeScale().getVisibleLogicalRange(),n0=bars.length;bars=merge(nb,bars);vols=merge(toVols(j.candles),vols);marks=marks.concat(j.marks||[]);
cs.setData(bars);vs.setData(vols);setMarks();if(r){const d=bars.length-n0;chart.timeScale().setVisibleLogicalRange({from:r.from+d,to:r.to+d})}}}catch(e){}finally{if(g===gen)ob=0}}
async function refresh(){if(loading||!bars.length)return;const g=gen;try{const j=await get();if(g!==gen)return;const nb=toBars(j.candles),nv=toVols(j.candles),lt=bars[bars.length-1].time;
nb.forEach((c,i)=>{if(c.time>=lt){cs.update(c);vs.update(nv[i])}});bars=merge(bars,nb.filter(c=>c.time>=lt));vols=merge(vols,nv.filter(v=>v.time>=lt));marks=marks.concat(j.marks||[]);setMarks();if(!hov)legendLast();head()}catch(e){}}
function tf(x,el){TF=x;document.querySelectorAll("#tfs button").forEach(b=>b.classList.toggle("on",b===el));load()}
function fs(){const c=document.getElementById("chartcard");document.fullscreenElement?document.exitFullscreen():c.requestFullscreen()}
if(initChart()){load();setInterval(refresh,30000)}'''

CHAT_JS='''const log=document.getElementById("log"),m=document.getElementById("m"),b=document.getElementById("b"),st=document.getElementById("st");let busy=0;
function esc(s){return s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function md(s){const code=[];s=esc(s).replace(/```[^\\n]*\\n([\\s\\S]*?)```/g,(_,c)=>{code.push("<pre>"+c+"</pre>");return "\\u0000"+(code.length-1)+"\\u0000"});
s=s.replace(/`([^`\\n]+)`/g,"<code>$1</code>").replace(/\\*\\*([^*\\n]+)\\*\\*/g,"<b>$1</b>").replace(/^#{3,} (.*)$/gm,"<h4>$1</h4>").replace(/^#{1,2} (.*)$/gm,"<h3>$1</h3>").replace(/^\\s*(?:[-*]|\\d+\\.) (.*)$/gm,"<li>$1</li>").replace(/(<li>.*<\\/li>\\n?)+/g,x=>"<ul>"+x.replace(/\\n/g,"")+"</ul>");
return s.replace(/\\n+(?=<(h3|h4|ul|pre))/g,"").replace(/(<\\/(h3|h4|ul)>)\\n+/g,"$1").replace(/\\n/g,"<br>").replace(/\\u0000(\\d+)\\u0000/g,(_,i)=>code[i])}
function add(role,text){const d=document.createElement("div");d.className="msg "+role;if(role=="user")d.textContent=text;else d.innerHTML=md(text);log.appendChild(d);log.scrollTop=log.scrollHeight}
HISTORY.forEach(t=>add(t.role,t.text));if(!HISTORY.length)log.innerHTML='<p class="muted">Ask Manage about system health, logs, code, or to propose a change.</p>';
async function send(){const t=m.value.trim();if(!t||busy)return;if(!log.querySelector(".msg"))log.innerHTML="";busy=1;b.disabled=1;add("user",t);m.value="";st.textContent="Thinking… (may take a minute)";
const c=new AbortController(),to=setTimeout(()=>c.abort(),300000);
try{const r=await fetch("/api/manage/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:t,chat_id:CHAT_ID}),signal:c.signal});const j=await r.json();if(!r.ok)throw Error(j.error||("HTTP "+r.status));if(!CHAT_ID){location.href="/manage/overview?chat="+j.chat_id;return}add("assistant",j.answer);
st.innerHTML=j.pending?'<a href="/manage/actions">'+j.pending+" action(s) awaiting your approval →</a>":""}
catch(e){add("error",e.name=="AbortError"?"No reply after 5 minutes; request cancelled.":"Error: "+e.message);st.textContent=""}finally{clearTimeout(to);busy=0;b.disabled=0;m.focus()}}
m.addEventListener("keydown",e=>{if(e.key=="Enter"&&!e.shiftKey){e.preventDefault();send()}});
async function del(){if(confirm("Delete this chat?")){try{await post("/api/manage/chats/delete",{chat_id:CHAT_ID});location.href="/manage/overview"}catch(e){alert(e.message)}}}'''

def esc(x)->str: return html.escape(str(x))
def ago(ts)->str:
    s=max(0,time.time()-float(ts or 0))
    return f'{s:.0f}s ago' if s<120 else (f'{s/60:.0f}m ago' if s<7200 else f'{s/3600:.1f}h ago')

class App:
    def __init__(self,root:Path): self.root=root; self.cfg=load_config(root); self.store=Store(resolve(root,self.cfg["runtime"]["database"])); self.manage=ManageEngine(root); self.think=ThinkEngine(root)
    def shell(self,section,sub,body):
        tabs=''.join(f'<a class="{"active" if section==x else ""}" href="/{x}/overview">{x.upper()}</a>' for x in ('manage','think','trade'))
        subs={"manage":["overview","actions","system","settings"],"think":["overview","candidates","notebook","settings"],"trade":["overview","trades","settings"]}[section]
        sn=''.join(f'<a class="{"active" if sub==x else ""}" href="/{section}/{x}">{x.title()}{self.pending_badge() if (section,x)==("manage","actions") else ""}</a>' for x in subs)
        name=esc(self.cfg["project"]["name"])
        return f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="TT Paper"><meta name="theme-color" content="{THEME["colors"]["navy"]["hex"]}"><title>{section.title()} · {name}</title><style>{THEME_VARS}{CSS}</style></head><body><div class="wrap"><div class="top"><b>{name}</b><small id="badge"></small></div><div class="tabs">{tabs}</div><div class="sub">{sn}</div>{body}</div><script>{JS}</script></body></html>'
    def pending_badge(self):
        n=len(self.store.actions("PENDING")); return f' <b class="warn">({n})</b>' if n else ''
    def render(self,path,query=None):
        parts=[p for p in path.split('/') if p]; section=parts[0] if parts else 'manage'; sub=parts[1] if len(parts)>1 else 'overview'; q=query or {}
        sub={'pool':'candidates','champion':'candidates','market':'overview','performance':'trades'}.get(sub,sub)
        if section=='manage':
            body={'actions':self.actions_page,'system':self.system_page,'settings':lambda:self.settings('manage')}.get(sub,lambda:self.chat_page(q.get('chat',[''])[0]))()
        elif section=='think':
            body={'candidates':self.think_candidates,'notebook':lambda:self.think_notebook(q),'settings':lambda:self.think_controls()+self.settings('think')}.get(sub,self.think_overview)()
        else:
            body={'trades':lambda:self.trade_trades(q),'settings':self.trade_settings}.get(sub,self.trade_overview)()
        return self.shell(section,sub,body)
    def chat_page(self,chat_id=''):
        s=self.store.get('settings','manage_agent',{}) or {}
        chats=self.manage.chats(); meta=next((c for c in chats if c["id"]==chat_id),None)
        if meta is None: chat_id=''
        hist=json.dumps([{"role":t["role"],"text":t["text"]} for t in (self.manage.history(chat_id) if chat_id else [])]).replace('</','<\\/')
        side=('<div class="card chats"><button onclick="location.href=\'/manage/overview\'">+ New chat</button>'
              +''.join(f'<a class="{"active" if c["id"]==chat_id else ""}" href="/manage/overview?chat={esc(c["id"])}">{esc(c["title"])}<small>{ago(c["updated_at"])}</small></a>' for c in chats)
              +('' if chats else '<p class="muted">No chats yet.</p>')+'</div>')
        title=esc(meta["title"]) if meta else 'New chat'
        return (f'<div class="chatwrap">{side}<div class="card"><div class="row"><h2 style="font-size:18px">{title}</h2><span><small class="muted">{esc(s.get("provider","mock"))} · {esc(s.get("model",""))}</small> '
                +(f'<button class="ghost small" onclick="del()">Delete chat</button>' if meta else '')+'</span></div><div id="log"></div>'
                '<textarea id="m" placeholder="Ask Manage… (Enter to send, Shift+Enter for a new line)"></textarea>'
                '<div class="row"><small id="st" class="muted"></small><button id="b" onclick="send()">Send</button></div></div></div>'
                f'<script>const HISTORY={hist},CHAT_ID={json.dumps(chat_id or None)};{CHAT_JS}</script>')
    def actions_page(self):
        acts=self.store.actions()[:30]
        if not acts: return '<div class="card"><h2>Actions</h2><p class="muted">No actions yet. When Manage proposes a file edit or restart, it appears here for your approval.</p></div>'
        cls={"PENDING":"warn","RUNNING":"warn","COMPLETED":"ok"}
        cards=[]
        for a in acts:
            p=a["payload"]; target=p.get("path") or p.get("worker","")
            detail=''
            if a["kind"]=="EDIT_FILE":
                detail='<pre class="diff">'+''.join(f'<span class="{"add" if l.startswith("+") and not l.startswith("+++") else "del" if l.startswith("-") and not l.startswith("---") else ""}">{esc(l)}</span>\n' for l in edit_diff(p).splitlines())+'</pre>'
            r=a["result"] or {}
            res='' if not r else f'<details><summary>{"Result" if r.get("ok") else esc(r.get("error","Result"))}</summary><pre>{esc(json.dumps(r,indent=1)[:6000])}</pre></details>'
            btn=(f'<div class="row" style="justify-content:flex-end"><button class="ghost reject" onclick="act({a["id"]},\'reject\',this)">Reject</button> <button onclick="act({a["id"]},\'approve\',this)">Approve</button></div>' if a["status"]=="PENDING" else '')
            cards.append(f'<div class="card"><div class="row"><b>#{a["id"]} {esc(a["kind"])} · {esc(target)}</b><span><b class="{cls.get(a["status"],"bad")}">{esc(a["status"])}</b> <small class="muted">{ago(a["created_at"])}</small></span></div><p>{esc(p.get("reason",""))}</p>{detail}{res}{btn}</div>')
        js='''async function act(id,d,el){el.parentNode.querySelectorAll("button").forEach(x=>x.disabled=1);el.textContent=d=="approve"?"Validating & testing…":"Rejecting…";try{const r=await post("/api/manage/actions",{id:id,decision:d});if(d=="approve"&&!r.ok)alert("Not applied: "+(r.error||"see result"))}catch(e){alert(e.message)}location.reload()}'''
        return ''.join(cards)+f'<script>{js}</script>'
    def system_page(self):
        rows=''.join(f'<tr><td><b>{esc(k.title())}</b></td><td><b class="{"bad" if v["stale"] else {"HEALTHY":"ok","PAUSED":"warn","DEGRADED":"warn"}.get(v["state"],"bad")}">{"STALE" if v["stale"] else esc(v["state"])}</b></td><td>{esc(v["reason"])}</td><td class="muted">{ago(v["updated_at"])} · pid {esc(v["pid"])}</td><td><button class="ghost small" onclick="rs(\'{esc(k)}\',this)">Restart</button></td></tr>' for k,v in self.manage.health().items() if k in WORKERS)
        errs='\n'.join(self.manage.log_errors(20)) or 'No recent errors.'
        js='''async function rs(w,el){if(!confirm("Restart "+w+"?"+(w=="all"?" Trading pauses for a few seconds.":"")))return;el.disabled=1;try{await post("/api/manage/restart",{worker:w});setTimeout(()=>location.reload(),4000)}catch(e){alert(e.message);el.disabled=0}}'''
        return (f'<div class="card"><div class="row"><h2>System</h2><button class="ghost small" onclick="rs(\'all\',this)">Restart all</button></div><table>{rows}</table></div>'
                f'<div class="card"><h2>Recent errors</h2><pre>{esc(errs)}</pre></div><script>{js}</script>')
    # ---------------- Think ----------------
    # ---------------- shared ----------------
    DAILY_LABELS=[('health','System health'),('pnl','Net P&amp;L today / total'),('trades','Trades today'),('edge','Edge per trade (last 30)'),('dd','Drawdown now'),('streak','Losing streak'),('position','Open position')]
    def daily_table(self,cols):
        head=''.join(f'<th>{esc(c["name"])}</th>' for c in cols)
        body=''.join('<tr><td class="muted">'+lbl+'</td>'+''.join(f'<td class="lv-{c["rows"][k][2]}"><b>{esc(c["rows"][k][0])}</b><br><small class="muted">{esc(c["rows"][k][1])}</small></td>' for c in cols)+'</tr>' for k,lbl in self.DAILY_LABELS)
        verdict=''.join('<td>'+(lambda r,a:'<b class="ok">All green</b>' if not r and not a else f'<b class="{"bad" if r else "warn"}">{r} red · {a} amber</b>')(sum(x[2]=="r" for x in c["rows"].values()),sum(x[2]=="a" for x in c["rows"].values()))+'</td>' for c in cols)
        return (f'<div class="card"><div class="row"><h2>Daily check</h2><small class="muted">{"swipe for candidates ›" if len(cols)>2 else ""}</small></div><div class="scroll"><table class="daily" style="min-width:{170+140*len(cols)}px">'
                f'<tr><th></th>{head}</tr>{body}<tr><td class="muted">Verdict</td>{verdict}</tr></table></div></div>')
    @staticmethod
    def kvrow(label,value,cls=''): return f'<div class="kv"><span>{label}</span><b class="{cls}">{value}</b></div>'

    # ---------------- Think ----------------
    def think_overview(self):
        st=self.think.status(); c=st['control']; now=time.time(); pack=self.store.get('think','analysis',{}) or {}
        state_cls={'IDLE':'ok','RUNNING':'ok','PAUSED':'warn','ERROR':'bad'}.get(st['state'],'muted')
        nxt=st.get('next_cycle'); nxt_txt='—' if not nxt else (f'in {max(0,(nxt-now)/60):.0f} min' if nxt>now else 'due now')
        rec=st['recordings']; prov=st['provider']
        status=(f'<div class="card"><div class="row"><h2>Scientist</h2><b class="{state_cls}">{esc(st["state"])}</b></div>'
                +self.kvrow('Last cycle',ago(st["last_cycle"]) if st.get("last_cycle") else "never")+self.kvrow('Next cycle',nxt_txt)
                +self.kvrow('AI calls today',f'{st["llm_calls_today"]} / {st["llm_cap"]} · {esc(prov)}')
                +self.kvrow('Recorded market data',f'{rec["hours"]/24:.1f} days ({rec["bytes"]/1e6:.0f} MB)' if rec["hours"]>=24 else f'{rec["hours"]} h ({rec["bytes"]/1e6:.0f} MB)')
                +self.kvrow('Live candidates',f'{len(st["pool"])} / {c["pool_slots"]}')
                +(self.kvrow('Last error',esc(st["last_error"]),'bad') if st.get('last_error') else '')
                +('<p class="warn">AI provider is mock: analysis runs, but no experiments are proposed. Set Provider to openai in Settings.</p>' if prov=='mock' else '')
                +f'<div class="row" style="justify-content:flex-end;margin-top:10px"><button class="ghost" onclick="tc({{paused:{"false" if c["paused"] else "true"}}},this)">{"Resume" if c["paused"] else "Pause"}</button> <button onclick="tc({{run_now:true}},this)">Run cycle now</button></div></div>')
        notes=self.think.notebook(300)
        active=sorted(st['pool'],key=lambda p:-float(p.get('created_at') or 0))
        last_exp=next((n for n in reversed(notes) if n['type']=='experiment' and n.get('hypothesis')),None)
        prev=next((n for n in reversed(notes) if n['type'] in ('verdict','screen') and n.get('verdict')),None)
        cur=active[0] if active else last_exp
        if cur:
            research=(f'<p style="margin:4px 0 8px">{esc(cur.get("hypothesis"))}</p>'
                      +self.kvrow('Evidence',esc(cur.get('evidence') or cur.get('why') or '—'))+self.kvrow('Expected effect',esc(cur.get('expected_effect') or '—'))
                      +self.kvrow('Status',esc(cur.get('status') or 'proposed')+(f' · screen {esc(cur.get("screen",{}).get("status",""))}' if cur.get('screen') else ''))
                      +(self.kvrow('Previous outcome',f'{esc(prev.get("candidate") or "")} {esc(prev["verdict"])} · {esc((prev.get("why") or "")[:90])}') if prev else ''))
        else: research='<p class="muted">No experiment yet. Think proposes one each cycle once the AI provider is available.</p>'
        research=f'<div class="card"><div class="row"><h2>Current research</h2><small class="muted">experiments tested: {int(self.store.get("think","tested",0) or 0)}</small></div>{research}</div>'
        try:  # with the Lab loop on, research runs as Lab studies (run_cycle skips the experiments above): show those instead
            from think.loop import Lab, ACTIVE
            lab=Lab(self.think); lc=lab.cfg
            if lc.get('auto'):
                ll=self.think.state().get('lab_last') or {}; studies=lab.studies(3); spent=lab.spent_today(); gap=float(lc['min_hours_between_studies'])
                now_txt=esc(ll.get('step','-'))+(f' · {esc(ll["why"])}' if ll.get('why') else '')+(f' · since {ago(ll["since"])}' if ll.get('since') else '')
                running=any(s['stage'] in ACTIVE and s['status']!='failed' for s in studies); nxt=(studies[0]['created'] if studies else 0)+3600*gap
                nxt_txt=('a study is running' if running else 'after the daily budget resets (00:00 UTC)' if spent>=float(lc['budget_usd_per_day'])
                         else f'in {(nxt-now)/3600:.1f} h ({time.strftime("%H:%M",time.gmtime(nxt))} UTC)' if nxt>now else f'at the next Lab step (every {float(lc["step_minutes"]):g} min)')
                seats=''.join(self.kvrow(g.title(),' · '.join(esc(f'{m["provider"]} {m["model"]}') for m in lc['groups'][g]['members'])) for g in ('scientists','workers','evaluators','supervisor'))
                rows=''
                for s in studies:
                    p=lab.dir/s['id']/'study.json'; m=json.loads(p.read_text()) if p.is_file() else {}   # read only (Lab._manifest writes)
                    out=(esc(s['error'][:160]) if s.get('error') else f'{len(m.get("hypotheses") or [])} hypotheses · {len(m.get("builds") or {})} builds · {esc((m.get("evaluation") or {}).get("note") or "")}')
                    rows+=self.kvrow(f'{time.strftime("%d %b %H:%M",time.gmtime(s["created"]))} · {esc(s["stage"])} · {esc(s["status"])} · ${lab.cost(s["id"])["usd"]:.2f}',out,'bad' if s['status']=='failed' else '')
                research=(f'<div class="card"><div class="row"><h2>Lab research</h2><small class="muted">loop on · ${spent:.2f} / ${float(lc["budget_usd_per_day"]):.2f} today</small></div>'
                          +self.kvrow('Now',now_txt)+self.kvrow('Next study',nxt_txt)+seats+(rows or '<p class="muted">No study yet.</p>')
                          +'<p class="muted">Think &gt; Settings (provider, model) is for the one-agent mode, which is off while the Lab loop is on. Lab seats change through Manage (a proposal, then your approval).</p></div>')
        except Exception as exc: research+=f'<p class="muted">Lab status unavailable: {esc(type(exc).__name__)}: {esc(str(exc)[:120])}</p>'
        funnel=pack.get('block_funnel_24h') or {}
        fun=''.join(f'<div class="kv"><span>{esc(r.replace("_"," "))}</span><span class="barwrap"><i style="width:{v["pct"]}%"></i></span><b>{v["pct"]:.0f}%</b></div>' for r,v in list(funnel.items())[:6]) or '<p class="muted">No decisions recorded yet.</p>'
        s=(pack.get('champion') or {}).get('stats') or {}
        if s.get('n'):
            exc=(pack.get('champion') or {}).get('excursions') or []; cap=[e['captured_pct'] for e in exc if e.get('captured_pct') is not None]
            by=' · '.join(f'{esc(k)} <b class="{"ok" if v["avg_bps"]>0 else "bad"}">{v["avg_bps"]:+.1f}</b>' for k,v in (s.get('by_setup') or {}).items())
            ana=(self.kvrow('Trades · net',f'{s["n"]} · ${s["net"]:+,.2f}','ok' if s['net']>0 else 'bad')+self.kvrow('Edge per trade',f'{s["expectancy_bps"]:+.2f} bps','ok' if s['expectancy_bps']>0 else 'bad')
                 +self.kvrow('t-stat · profit factor',f'{s.get("t_stat")} · {s.get("profit_factor")}')+self.kvrow('Payoff · breakeven win',f'{s.get("payoff")} · {s.get("breakeven_win_rate")}% (actual {s["win_rate"]}%)')
                 +(self.kvrow('Exit efficiency',f'{sum(cap)/len(cap):.0f}% of best move captured') if cap else '')+(self.kvrow('Edge by setup (bps)',by) if by else ''))
        else: ana='<p class="muted">No champion trades yet.</p>'
        return (status+research+f'<div class="grid"><div class="card" id="tl1" data-live><h2>What blocks trades · 24 h</h2>{fun}</div>'
                f'<div class="card"><h2>Champion analysis</h2><small class="muted">updated {ago(pack.get("generated_at")) if pack else "never"}</small>{ana}</div></div>'+self.think_js())
    def think_js(self):
        return '<script>async function tc(d,el){if(el)el.disabled=1;try{await post("/api/think/control",d);setTimeout(()=>location.reload(),600)}catch(e){alert(e.message);if(el)el.disabled=0}}</script>'
    def think_candidates(self):
        active=[p for p in self.think.pool() if p.get('status') in ('SHADOW','PASS_PENDING')]
        evs={p['id']:self.think.evaluate(p) for p in active}
        cols=[('Champion',stats_of(self.think.champion_trades()),None)]+[(p['id'],evs[p['id']]['candidate'],evs[p['id']]['p_beats']) for p in active]
        groups=[('Edge',[('expectancy_bps','bps / trade','bps'),('net','Net $','usd'),('n','Trades','')]),('Luck',[('t_stat','t-stat',''),('p_beats','P(beats champion)','pct')]),
                ('Risk',[('max_dd_pct','Max drawdown %',''),('max_losing_streak','Max losing streak','')]),('Robustness',[('stress_net','Net at 1.5× costs $','usd'),('net_without_best2','Net w/o best 2 $','usd'),('profit_factor','Profit factor','')])]
        def cell(s,p,key,kind):
            v=p if key=='p_beats' else s.get(key)
            if v is None: return '<td class="muted">—</td>'
            txt=f'{v*100:.0f}%' if kind=='pct' else (f'{v:+,.2f}' if kind=='usd' else (f'{v:+.2f}' if kind=='bps' else esc(v)))
            cls='ok' if kind in ('usd','bps') and v>0 else ('bad' if kind in ('usd','bps') and v<0 else '')
            return f'<td class="{cls}">{txt}</td>'
        rows=''.join(f'<tr class="grp"><td colspan="{len(cols)+1}">{g}</td></tr>'+''.join('<tr><td>'+lbl+'</td>'+''.join(cell(s,p,k,kind) for _,s,p in cols)+'</tr>' for k,lbl,kind in items) for g,items in groups)
        comp=(f'<div class="card"><h2>Comparison</h2><small class="muted">Each strategy since it started; P(beats) uses the same hours as the champion.</small>'
              f'<div class="scroll"><table style="min-width:{170+110*len(cols)}px"><tr><th></th>'+''.join(f'<th>{esc(n)}</th>' for n,_,_ in cols)+f'</tr>{rows}</table></div></div>')
        cards=[]
        for p in active:
            ev=evs[p['id']]; vcls={'PASS':'ok','FAIL':'bad'}.get(ev['verdict'],'warn'); n=ev['candidate'].get('n',0)
            rows=''.join(f'<tr><td>{"✓" if x["ok"] else "✗"} {esc(x["name"])}</td><td>{esc(x["value"])}</td><td class="muted">{esc(x["need"])}</td></tr>' for x in ev['checks'])
            goal=''.join(f'<tr><td>{"✓" if x["ok"] else "✗"} {esc(x["name"])}</td><td>{esc(x["value"])}</td><td class="muted">{esc(x["need"])}</td></tr>' for x in ev['goal'])
            cards.append(f'<div class="card"><div class="row"><h2 style="font-size:17px">{esc(p["id"])} · {esc(p["kind"])}</h2><b class="{vcls}">{esc(ev["verdict"])}</b></div><p>{esc(p["hypothesis"])}</p>'
                         +(f'<small class="muted">Params: {esc(json.dumps(p.get("overrides")))}</small>' if p.get('overrides') else '')
                         +f'<div class="kv"><span>Test progress</span><b>{ev["age_h"]:.0f} h / 24 h · {n} / 30 trades</b></div><div class="barwrap wide"><i style="width:{min(100,min(ev["age_h"]/24,n/30)*100):.0f}%"></i></div>'
                         f'<small class="muted">{esc(ev["why"])}</small>'+(f'<p>Promotion action #{p["action_id"]} is waiting in <a href="/manage/actions">Actions</a>.</p>' if p.get('action_id') else '')
                         +f'<details open><summary>Promotion rules</summary><table>{rows}</table></details><details><summary>Ultimate goal</summary><table>{goal}</table></details></div>')
        if not cards: cards=['<div class="card"><p class="muted">No live candidates. Think starts up to the configured number after screening its experiments.</p></div>']
        ch=self.store.get('think','champion',{}) or {}; guard='rolled back' if ch.get('rolled_back') else ('armed' if ch.get('previous_sha') else 'inactive until the first promotion')
        hist=[n for n in self.think.notebook(400) if n['type'] in ('champion_changed','promotion_proposed','rollback')]
        champ=(f'<div class="card"><h2>Champion</h2>'+self.kvrow('Strategy',esc(ch.get("id","baseline")))+self.kvrow('Champion since',ago((ch.get("since_ms") or 0)/1000) if ch.get("since_ms") else "—")
               +self.kvrow('Rollback guard',guard)+''.join(self.kvrow(f'{ago(n["ts"])} · {esc(n["type"].replace("_"," "))}',esc(n.get("champion") or n.get("candidate") or "")) for n in reversed(hist[-8:]))
               +'<small class="muted">After a promotion, 20+ trades with negative edge (t &lt; −1) or drawdown above 1.5× the shadow drawdown reverts to the previous champion automatically.</small></div>')
        return comp+''.join(cards)+champ
    def think_notebook(self,q):
        f=(q.get('f',['all']) or ['all'])[0]
        kinds={'all':None,'experiments':('experiment','screen','shadow_started'),'verdicts':('verdict','promotion_proposed','champion_changed','rollback'),'reports':('report',),'errors':('error',)}
        sel=kinds.get(f); rows=[n for n in self.think.notebook(400) if n['type']!='cycle' and (sel is None or n['type'] in sel)]
        tl={'experiment':'warn','screen':'muted','shadow_started':'ok','verdict':'bad','promotion_proposed':'ok','champion_changed':'ok','rollback':'bad','error':'bad','report':'ok'}
        chips=''.join(f'<a class="chip{" on" if f==k else ""}" href="/think/notebook?f={k}">{k.title()}</a>' for k in kinds)
        items=''.join(f'<div class="tl"><small class="muted">{ago(n["ts"])}</small> <b class="{tl.get(n["type"],"muted")}">{esc(n["type"].replace("_"," "))}</b> {esc(n.get("candidate") or "")}'
                      +(f'<div>{esc(n.get("hypothesis"))}</div>' if n.get('hypothesis') else '')
                      +(f'<small class="muted">{esc(n.get("verdict") or "")} {esc(n.get("why") or "")} {esc(n.get("lesson") or "")}</small>' if (n.get('why') or n.get('verdict') or n.get('lesson')) else '')
                      +(f'<div class="msg assistant">{esc(n["text"])}</div>' if n.get('text') else '')+'</div>' for n in reversed(rows[-80:])) or '<p class="muted">Nothing here yet.</p>'
        return f'<div class="card"><h2>Lab notebook</h2><div style="margin-bottom:10px">{chips}</div>{items}</div>'

    # ---------------- Trade ----------------
    def trade_overview(self):
        live1=f'<div id="tr1" data-live>{self.daily_table(self.think.daily_check())}</div>'
        tfs=''.join(f'<button class="{"on" if x=="1m" else ""}" onclick="tf(\'{x}\',this)">{x}</button>' for x in CANDLE_TFS)
        market=('<div class="card" id="chartcard"><div class="row"><div><h2 id="px" style="margin:0;display:inline">BTCUSD</h2> <small class="muted" id="mk"></small></div>'
                f'<div class="row" style="gap:6px"><span class="seg" id="tfs">{tfs}</span><button class="small ghost" onclick="fs()" title="Full screen" aria-label="Full screen">&#x26F6;</button></div></div>'
                '<div id="legend" class="legend"></div><div id="chart" class="chartbox"></div></div>')
        live2=f'<div id="tr2" data-live><div class="grid">{self.screening_card()}{self.position_card()}</div>{self.diagnostics_card()}</div>'
        return market+live1+live2+'<script src="/static/lightweight-charts.standalone.production.js"></script>'+f'<script>{CHART_JS}</script>'
    def screening_card(self):
        s=self.store.get('trade','screen') or {}; f=s.get('f') or {}; cfg=s.get('cfg') or {}
        if not s: return '<div class="card"><h2>Screening</h2><p class="muted">Waiting for the first strategy decision.</p></div>'
        if not cfg.get('min_confirmations'):  # another strategy: its latest decision and the live metrics it publishes (TRADE_UI_SPEC)
            cm=(self.store.get('trade','snapshot',{}) or {}).get('custom_metrics') or {}; spread=s.get('spread')
            fmt=lambda v: '—' if v is None else (f'{v:,.2f}' if isinstance(v,float) else str(v))
            rows=[('Latest decision',(s.get('reason') or '—').replace('_',' ')),('Spread','—' if spread is None else f'{spread:.2f} bps'),
                  ('Cooldown','none' if not s.get('cooldown_s') else f'{s["cooldown_s"]:.0f} s left')]+[(str(k).replace('_',' ').capitalize(),fmt(v)) for k,v in cm.items()]
            body=''.join(f'<div class="kv"><span>{esc(n)}</span><b>{esc(v)}</b></div>' for n,v in rows)
            return f'<div class="card"><h2>Screening</h2>{body}<p class="muted" style="margin:8px 0 0">updated {ago(s.get("ts"))}</p></div>'
        mid=s.get('mid') or 0; spread=s.get('spread'); warm=float(cfg.get('warmup_seconds') or 0); el=float(s.get('warm_elapsed_s') or 0)
        ret15=f.get('ret_15m_bps'); trend=float(cfg.get('trend_15m_bps') or 0); min_move=float(f.get('min_move_bps') or 0); minc=int(cfg['min_confirmations'])
        pull=0.0; which='none'
        if mid and ret15 is not None and ret15>=trend and f.get('high_10m'): pull=(f['high_10m']-mid)/f['high_10m']*1e4; which='pullback from 10m high'
        elif mid and ret15 is not None and ret15<=-trend and f.get('low_10m'): pull=(mid-f['low_10m'])/f['low_10m']*1e4; which='bounce from 10m low'
        room=((f['reference_high']-f['reference_low'])/f['reference_high']*1e4/2) if f.get('reference_high') and f.get('reference_low') else 0.0
        setup,label=(pull,which) if pull>=room else (room,'reversal room (half 30m range)')
        side='long' if (f.get('long_score') or 0)>=(f.get('short_score') or 0) else 'short'; sg=1 if side=='long' else -1
        flow=[('Near OBI',f.get('obi_near'),cfg.get('near_obi_threshold')),('Persistent OBI',f.get('persistent_obi'),cfg.get('persistent_obi_threshold')),('Microprice',f.get('microprice_bias'),cfg.get('microprice_bias_threshold')),
              ('Book pressure',f.get('book_pressure'),None),('Trade flow',f.get('tfi_fast'),cfg.get('fast_tfi_threshold'))]
        score=int(f.get('long_score') if side=='long' else f.get('short_score') or 0)
        rows=[('Warm-up',f'{min(el,warm)/60:.0f} / {warm/60:.0f} min',el>=warm),('Spread ≤ '+f'{cfg.get("max_spread_bps")} bps','—' if spread is None else f'{spread:.2f}',spread is not None and spread<=float(cfg.get('max_spread_bps') or 0)),
              ('Cooldown','none' if not s.get('cooldown_s') else f'{s["cooldown_s"]:.0f} s left',not s.get('cooldown_s')),
              (f'15m trend ≥ ±{trend:g} bps','—' if ret15 is None else f'{ret15:+.1f}',ret15 is not None and abs(ret15)>=trend),
              (f'Setup move ≥ {min_move:.1f} bps',f'{setup:.1f} · {label}',setup>=min_move and setup>0),(f'Flow checks ≥ {minc} of 5 ({side})',f'{score} of 5',score>=minc)]
        body=''.join(f'<div class="kv"><span>{esc(n)}</span><b class="{"ok" if ok else "bad"}">{esc(v)} {"✓" if ok else "✗"}</b></div>' for n,v,ok in rows)
        chk=' · '.join(f'{n} {"✓" if (v is not None and ((th is None and sg*v>=0.1) or (th is not None and sg*v>=float(th)))) else "✗"}' for n,v,th in flow)
        reason=(s.get('reason') or '').replace('_',' '); blocked=reason not in ('manage open position',) and not reason.startswith(('reversal','continuation'))
        return (f'<div class="card"><div class="row"><h2>Screening</h2><b class="{"warn" if blocked else "ok"}">{"Blocked" if blocked else "Active"}</b></div>{body}'
                f'<small class="muted">{chk}</small><p class="muted" style="margin:8px 0 0">Now: {esc(reason or "—")} · cost {f.get("cost_bps") or 0:.1f} bps · updated {ago(s.get("ts"))}</p></div>')
    def position_card(self):
        snap=self.store.get('trade','snapshot',{}) or {}; pos=snap.get('positions') or []; mid=(self.store.get('trade','screen') or {}).get('mid')
        if not pos: return '<div class="card"><h2>Open position</h2><p class="muted">No open position.</p></div>'
        p=pos[0]; notion=p['size']*p['entry_price']; bps=p.get('unrealized_pnl',0)/notion*1e4 if notion else 0
        dist=lambda x:'—' if not (x and mid) else f'{abs(x-mid)/mid*1e4:.1f} bps away'
        return (f'<div class="card"><div class="row"><h2>Open position</h2><b class="{"ok" if p.get("unrealized_pnl",0)>=0 else "bad"}">${p.get("unrealized_pnl",0):+,.2f} · {bps:+.1f} bps</b></div>'
                +self.kvrow('Side · size',f'{esc(p["side"]).title()} · {p["size"]:.3f} BTC')+self.kvrow('Entry',f'{p["entry_price"]:,.2f} · {ago(p["entry_time_ms"]/1000)}')
                +self.kvrow('Stop',f'{(p.get("stop_price") or 0):,.2f} · {dist(p.get("stop_price"))}')+self.kvrow('Target',f'{(p.get("target_price") or 0):,.2f} · {dist(p.get("target_price"))}')+'</div>')
    def diagnostics_card(self):
        snap=self.store.get('trade','snapshot',{}) or {}; i=(snap.get('health') or {}).get('market_integrity') or {}; m=snap.get('metrics') or {}
        cp=self.store.get('trade_state','checkpoint') or {}; b=cp.get('broker') or {}; now=time.time()
        rc=(self.store.get('trade','reconnects',{}) or {}).get(time.strftime('%Y-%m-%d',time.gmtime(now)),0)
        try:
            with self.store.connect() as db: jrows=db.execute('SELECT COUNT(*) FROM trade_journal').fetchone()[0]
        except Exception: jrows=0
        unreal=sum(float(p.get('unrealized_pnl') or 0) for p in snap.get('positions') or [])
        expect=float(m.get('starting_equity_usd') or 10000)+float(b.get('realized_pnl_usd') or 0)+unreal-sum(float(x) for x in (b.get('entry_fees') or {}).values())
        diff=abs(float(m.get('equity_usd') or expect)-expect) if b else 0.0; pnl_ok=abs(sum(b.get('trade_pnls') or [])-float(b.get('realized_pnl_usd') or 0))<0.01
        h=self.manage.health(); workers_ok=all(not v['stale'] and v['state'] in ('HEALTHY','PAUSED') for k,v in h.items() if k in ('manage','trade','ui'))
        th=h.get('think',{}); rec=self.store.get('trade','recorder') or {}; rec_ok=not rec.get('error') and now-float(rec.get('updated_at') or 0)<180
        errs=self.manage.log_errors(1); last_err=errs[-1] if errs else ''
        try: err_age=now-time.mktime(time.strptime(last_err[:19],'%Y-%m-%d %H:%M:%S'))  # log stamps are local time
        except Exception: err_age=1e9
        dbsize=self.store.db_path.stat().st_size/1e6 if self.store.db_path.exists() else 0
        checks=[('Feed connected and verified','g' if i.get('connected') and i.get('verified') and i.get('valid') else 'r',f'{i.get("checksum_count",0):,} checksums'),
                ('Sequence gaps / checksum mismatches','g' if not i.get('sequence_gaps') and not i.get('checksum_mismatches') else 'r',f'{i.get("sequence_gaps",0)} / {i.get("checksum_mismatches",0)}'),
                ('Fresh data (not stale)','g' if not i.get('stale') else 'r',f'pending {i.get("pending_evidence",0)}'),
                ('Reconnects today','g' if rc<=3 else ('a' if rc<=10 else 'r'),str(rc)),
                ('Accounting reconciles','g' if diff<0.01 and pnl_ok else 'r',f'${diff:.2f} difference'),
                ('Trade journal matches state','g' if jrows==len(b.get('executions') or []) else 'r',f'{jrows} = {len(b.get("executions") or [])}'),
                ('Workers alive','g' if workers_ok else 'r',' · '.join(f'{k} {v["state"].lower()}' for k,v in h.items() if k in ('manage','trade','ui'))),
                ('Think scientist','g' if th.get('state')=='HEALTHY' and not th.get('stale') else ('a' if th.get('state') in ('PAUSED','DEGRADED') else 'r'),esc((th.get('reason') or '')[:40])),
                ('Market recorder','g' if rec_ok else 'r',f'{rec.get("events",0):,} events this run'),
                ('Database size','g' if dbsize<500 else 'a',f'{dbsize:.1f} MB'),
                ('Last error in log','g' if err_age>3600 else 'a',esc(last_err[:19] or 'none'))]
        bad=[c for c in checks if c[1]!='g']
        head=f'<div class="banner {"ok-bg" if not bad else ("warn-bg" if all(c[1]=="a" for c in bad) else "bad-bg")}">{"All "+str(len(checks))+" checks healthy" if not bad else str(len(bad))+" need attention: "+esc(", ".join(c[0] for c in bad))}</div>'
        rows=''.join(f'<div class="kv lv-{l}" style="padding-left:8px"><span>{n}</span><b>{v}</b></div>' for n,l,v in checks)
        return f'<div class="card"><div class="row"><h2>Diagnostics</h2><small class="muted">checked {ago(float(h.get("trade",{}).get("updated_at") or now))}</small></div>{head}<details><summary>Show all {len(checks)} checks</summary>{rows}</details></div>'
    def _strategy_trades(self,sel):
        return self.think.champion_trades() if sel=='champion' else self.think.shadow_trades(sel)
    def _filtered(self,q):
        g=lambda k,d:(q.get(k,[d]) or [d])[0]
        sel,days,side,setup,res=g('s','champion'),g('d','7'),g('side','all'),g('setup','all'),g('r','all')
        tr=self._strategy_trades(sel)
        if days!='all': tr=[t for t in tr if t['exit_ms']>=(time.time()-float(days)*86400)*1000]
        if side!='all': tr=[t for t in tr if t['side']==side]
        if setup!='all': tr=[t for t in tr if t['setup']==setup]
        if res!='all': tr=[t for t in tr if (t['net']>0)==(res=='win')]
        return (sel,days,side,setup,res),tr
    def trade_trades(self,q):
        from urllib.parse import urlencode
        (sel,days,side,setup,res),tr=self._filtered(q)
        strategies=['champion']+[p['id'] for p in self.think.pool() if p.get('status') in ('SHADOW','PASS_PENDING')]
        opt=lambda name,cur,vals:f'<select name="{name}" onchange="this.form.submit()">'+''.join(f'<option value="{v}"{" selected" if v==cur else ""}>{esc(l)}</option>' for v,l in vals)+'</select>'
        form=('<form method="get" class="filters">'+opt('s',sel,[(x,x.title() if x=='champion' else x) for x in strategies])+opt('d',days,[('1','Today'),('7','7 days'),('30','30 days'),('all','All time')])
              +opt('side',side,[('all','All sides'),('long','Long'),('short','Short')])+opt('setup',setup,[('all','All setups')]+[(x,x.title()) for x in sorted({t['setup'] for t in self._strategy_trades(sel)})])
              +opt('r',res,[('all','All results'),('win','Wins'),('loss','Losses')])+'</form>')
        s=stats_of(tr)
        strip=('<div class="card strip">'+''.join(f'<div><small class="muted">{l}</small><b class="{c}">{v}</b></div>' for l,v,c in [
               ('Trades',s.get('n',0),''),('Net',f'${s.get("net",0):+,.2f}','ok' if s.get('net',0)>0 else ('bad' if s.get('net',0)<0 else '')),('Win rate',f'{s.get("win_rate","—")}%' if s.get('n') else '—',''),
               ('Avg',f'{s.get("expectancy_bps",0):+.1f} bps' if s.get('n') else '—',''),('Profit factor',s.get('profit_factor') or '—',''),('Fees',f'${s.get("fees",0):,.2f}' if s.get('n') else '—','')])+'</div>')
        base={'s':sel,'d':days,'side':side,'setup':setup,'r':res}; sel_t=(q.get('t',[''])[0] or '')
        detail=''
        if sel_t:
            t=next((x for x in tr if str(x['entry_ms'])==sel_t),None)
            if t: detail=self.trade_detail(t,sel)
        items=''.join(f'<a class="tcard{" on" if str(t["entry_ms"])==sel_t else ""}" href="/trade/trades?{urlencode({**base,"t":t["entry_ms"]})}"><div class="row"><b>{t["side"].title()} · {esc(t["setup"])}</b><b class="{"ok" if t["net"]>0 else "bad"}">${t["net"]:+,.2f}</b></div>'
                      f'<small class="muted">{time.strftime("%d %b %H:%M",time.gmtime(t["entry_ms"]/1000))} → {time.strftime("%H:%M",time.gmtime(t["exit_ms"]/1000))} · {t["hold_s"]/60:.0f} min · {esc(t["exit_reason"].replace("_"," "))}<br>'
                      f'{t["entry"]:,.2f} → {t["exit"]:,.2f} · {t["net_bps"]:+.1f} bps · fees ${t["fees"]:.2f}</small></a>' for t in reversed(tr[-200:])) or '<p class="muted">No trades match these filters yet.</p>'
        return (form+strip+detail+f'<div class="card"><div class="row"><h2>Trades</h2><a href="/api/trade/trades.csv?{urlencode(base)}">Export CSV</a></div>{items}</div>')
    def trade_detail(self,t,sel):
        from think.replay import excursions, iter_rows
        ctx=(self.store.get('trade','entries',{}) or {}).get(str(t['entry_ms'])) if sel=='champion' else None
        ex=(excursions(self.think.rec_dir,[t]) or [{}])[0]
        pts=[(r['t'],float(r['m'])) for r in iter_rows(self.think.rec_dir,t['entry_ms']-120_000,t['exit_ms']+120_000) if r.get('m')]
        chart=''
        if len(pts)>2:
            step=max(1,len(pts)//160); pts=pts[::step]; t0,t1=pts[0][0],pts[-1][0]; vals=[p for _,p in pts]+[t['entry'],t['exit']]+([ctx['stop'],ctx['target']] if ctx and ctx.get('stop') and ctx.get('target') else [])
            hi,lo=max(vals),min(vals); X=lambda x:(x-t0)/max(1,t1-t0)*600; Y=lambda v:6+(hi-v)/max(1e-9,hi-lo)*108
            line=' '.join(f'{X(a):.1f},{Y(b):.1f}' for a,b in pts)
            lines=''.join(f'<line x1="0" x2="600" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="{c}" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>' for v,c in (([(ctx["stop"],"dn"),(ctx["target"],"up")]) if ctx and ctx.get("stop") and ctx.get("target") else []))
            chart=(f'<svg viewBox="0 0 600 120" preserveAspectRatio="none" style="width:100%;height:120px">{lines}<polyline class="ln" points="{line}" vector-effect="non-scaling-stroke"/>'
                   f'<circle cx="{X(t["entry_ms"]):.1f}" cy="{Y(t["entry"]):.1f}" r="4" class="mark"/><circle cx="{X(t["exit_ms"]):.1f}" cy="{Y(t["exit"]):.1f}" r="4" class="{"up" if t["net"]>0 else "dn"}"/></svg>'
                   '<small class="muted">● entry ● exit · dashed: stop (red) and target (green)</small>')
        why=''
        if ctx:
            f=ctx.get('features') or {}; sc=f.get('long_score') if t['side']=='long' else f.get('short_score')
            why=('<h3>Why it entered</h3>'+self.kvrow('Signal',esc((ctx.get('reason') or '').replace('_',' ')))+self.kvrow('Flow checks passed',f'{sc} of 5')
                 +self.kvrow('Required move · cost',f'{(f.get("min_move_bps") or 0):.1f} · {(f.get("cost_bps") or 0):.1f} bps')+self.kvrow('15m trend · regime',f'{(f.get("ret_15m_bps") or 0):+.1f} bps · {esc(f.get("regime") or "")}')
                 +self.kvrow('Spread at entry',f'{(ctx.get("spread_bps") or 0):.2f} bps'))
        plan=('<h3>Plan vs outcome</h3>'+self.kvrow('Entry',f'{t["entry"]:,.2f}')+(self.kvrow('Stop / target',f'{ctx["stop"]:,.2f} / {ctx["target"]:,.2f}') if ctx and ctx.get('stop') and ctx.get('target') else '')
              +self.kvrow('Exit',f'{t["exit"]:,.2f} · {esc(t["exit_reason"].replace("_"," "))}')
              +(self.kvrow('Best / worst during trade',f'<span class="ok">{ex["mfe_bps"]:+.1f}</span> / <span class="bad">{ex["mae_bps"]:+.1f}</span> bps') if ex.get('mfe_bps') is not None and pts else self.kvrow('Best / worst during trade','not recorded'))
              +(self.kvrow('Captured of best move',f'{ex["captured_pct"]:.0f}%') if ex.get('captured_pct') is not None and pts else ''))
        costs='<h3>Costs</h3>'+self.kvrow('Fees in + out',f'${t["fees"]:.2f}')+self.kvrow('Slippage (paper)',f'{t.get("slippage_bps",1.0):g} bps each side')
        return (f'<div class="card"><div class="row"><h2>{t["side"].title()} · {esc(t["setup"])}</h2><b class="{"ok" if t["net"]>0 else "bad"}">${t["net"]:+,.2f} · {t["net_bps"]:+.1f} bps</b></div>'
                f'<small class="muted">{esc(sel)} · {time.strftime("%d %b %H:%M:%S",time.gmtime(t["entry_ms"]/1000))} → {time.strftime("%H:%M:%S",time.gmtime(t["exit_ms"]/1000))} · {t["hold_s"]/60:.1f} min · {t["size"]:.3f} BTC</small>'
                f'{chart}<div class="grid">{("<div>"+why+"</div>") if why else ""}<div>{plan}</div><div>{costs}</div></div></div>')
    def trades_csv(self,q):
        _,tr=self._filtered(q); cols=['entry_ms','exit_ms','side','setup','size','entry','exit','exit_reason','fees','net','net_bps','hold_s']
        return '\n'.join([','.join(cols)]+[','.join(str(t[c]) for c in cols) for t in tr])+'\n'
    def trade_settings(self):
        mod=self.think.champion_module(); spec={x['key']:x for x in getattr(mod,'TRADE_UI_SPEC',{}).get('settings',[])}; tun=self.think.tunables()
        order=[k for k in spec if k in tun]+[k for k in tun if k not in spec]
        rows=''.join(f'<div class="kv"><span>{esc(spec.get(k,{}).get("label") or k.replace("_"," "))}<br><small class="muted">{esc(k)}</small></span>'
                     f'<span><input value="{esc(v)}" id="p_{esc(k)}" style="width:90px"> <button class="ghost small" onclick="prop(\'{esc(k)}\',this)">Propose</button></span></div>' for k,v in ((k,tun[k]) for k in order))
        js='''async function prop(k,el){const v=document.getElementById("p_"+k).value;el.disabled=1;try{const r=await post("/api/trade/propose",{key:k,value:v});el.outerHTML='<a href="/manage/actions">Action #'+r.action_id+' ›</a>'}catch(e){alert(e.message);el.disabled=0}}'''
        return (f'<div class="card"><h2>Strategy parameters</h2><p class="muted">Changes are proposed to Manage → Actions, validated, tested and applied only after you approve. Trading stays paper-only.</p>{rows}</div><script>{js}</script>')
    def propose_setting(self,key,value):
        from think.think import set_config_defaults
        tun=self.think.tunables()
        if key not in tun: raise ValueError(f'unknown parameter {key}')
        cur=self.think.trade_file.read_text(encoding='utf-8'); new=set_config_defaults(cur,{key:value})
        if new==cur: raise ValueError('value unchanged')
        return {"action_id":self.manage.propose_file_edit('trade/trade.py',cur,new,f'Settings: {key} {tun[key]} → {value}')}
    _candles={}
    def candles(self,tf,end=None):
        """Bitfinex public candles, newest 500 (cached 15 s) or the 500 before `end` (ms) for scrolling back; trade marks in range."""
        tf=tf if tf in CANDLE_TFS else '1m'; step=CANDLE_TFS[tf]; now=time.time(); err=None
        try: end=int(end) if end else None
        except (TypeError,ValueError): end=None
        c=None if end else self._candles.get(tf)
        if not c or now-c[0]>15:
            try:
                import urllib.request
                url=f'https://api-pub.bitfinex.com/v2/candles/trade:{tf}:{self.cfg["market"]["symbol"]}/hist?limit=500'+(f'&end={end-1}' if end else '')
                rows=json.load(urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=6))[::-1]; c=(now,rows)
                if not end: self._candles[tf]=c
            except Exception as e: err=f'{type(e).__name__}'
        rows=c[1] if c else []; start=rows[0][0] if rows else 0; stop=(rows[-1][0]+step) if rows else 0; marks=[]
        for t in self.think.champion_trades():
            buy_in=t['side']=='long'
            if start<=t['entry_ms']<stop or (not end and t['entry_ms']>=start): marks.append({'t':t['entry_ms'],'p':t['entry'],'k':'buy' if buy_in else 'sell'})
            if start<=t['exit_ms']<stop or (not end and t['exit_ms']>=start): marks.append({'t':t['exit_ms'],'p':t['exit'],'k':'sell' if buy_in else 'buy'})
        if not end:
            for p in (self.store.get('trade','snapshot',{}) or {}).get('positions') or []:
                if p['entry_time_ms']>=start: marks.append({'t':p['entry_time_ms'],'p':p['entry_price'],'k':'buy' if p['side']=='long' else 'sell'})
        spread=(self.store.get('trade','screen') or {}).get('spread')
        return {'tf':tf,'candles':rows,'step':step,'marks':marks,'error':err,'spread':spread}
    def think_controls(self):
        c=self.think.control(); fields=[('paused','Paused'),('interval_hours','Cycle every (hours)'),('pool_slots','Live candidate slots'),('max_llm_calls_per_day','AI calls per day (cap)'),('replay_budget_minutes','Replay budget (min)'),('shadow_max_days','Max candidate test (days)')]
        def ctl(k,v):
            if isinstance(v,bool): return f'<select name="{k}"><option{" selected" if v else ""}>true</option><option{" selected" if not v else ""}>false</option></select>'
            return f'<input name="{k}" value="{esc(v)}" style="width:90px">'
        js='''async function tcs(f){const d={};new FormData(f).forEach((v,k)=>d[k]=v);const s=document.getElementById("tcs");try{await post("/api/think/control",d);s.textContent="Saved."}catch(e){s.textContent="Error: "+e.message}return false}'''
        return (f'<div class="card"><h2>Scientist controls</h2><form onsubmit="return tcs(this)">'+''.join(f'<div class="kv"><span>{lbl}</span>{ctl(k,c[k])}</div>' for k,lbl in fields)
                +f'<div class="row" style="margin-top:10px"><small id="tcs" class="muted"></small><button>Save</button></div></form></div><script>{js}</script>')
    def settings(self,who):
        s=self.store.get('settings',f'{who}_agent',{}) or {}
        def field(k,v):
            if k in AGENT_OPTIONS: ctl=f'<select name="{esc(k)}">'+''.join(f'<option{" selected" if o==v else ""}>{esc(o)}</option>' for o in AGENT_OPTIONS[k])+'</select>'
            elif k in ('model','fallback_model'):
                known=[m for ms in MODEL_CHOICES.values() for m in ms]
                groups=([('Current',[v])] if v and v not in known else [])+[('Claude',MODEL_CHOICES['anthropic']),('OpenAI',MODEL_CHOICES['openai'])]
                ctl=f'<select name="{esc(k)}">'+''.join(f'<optgroup label="{g}">'+''.join(f'<option{" selected" if m==v else ""}>{esc(m)}</option>' for m in ms)+'</optgroup>' for g,ms in groups)+'</select>'
            else: ctl=f'<input name="{esc(k)}" value="{esc(v)}">'
            return f'<div class="kv"><span>{esc(k.replace("_"," ").title())}</span>{ctl}</div>'
        js=f'''async function save(f){{const d={{}};new FormData(f).forEach((v,k)=>d[k]=v);const s=document.getElementById("saved");try{{await post("/api/{who}/settings",d);s.textContent="Saved."}}catch(e){{s.textContent="Error: "+e.message}}return false}}'''
        return f'<div class="card"><h2>Agent Settings</h2><form onsubmit="return save(this)">{"".join(field(k,v) for k,v in s.items())}<div class="row" style="margin-top:10px"><small id="saved" class="muted"></small><button>Save</button></div></form></div><script>{js}</script>'

LOCAL_HOSTS={'127.0.0.1','localhost','[::1]','::1'}
class Handler(BaseHTTPRequestHandler):
    app:App
    def out(self,code,data,ctype='application/json'):
        b=data if isinstance(data,bytes) else (data if isinstance(data,str) else json.dumps(data)).encode(); self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def local(self)->bool:
        # Reject DNS-rebinding requests: the Host header must name this machine.
        h=self.headers.get('Host') or ''
        return (h.split(']')[0]+']' if h.startswith('[') else h.split(':')[0]) in LOCAL_HOSTS
    def do_GET(self):
        if not self.local(): return self.out(403,{"error":"forbidden host"})
        path=urlparse(self.path).path
        if path=='/api/health': return self.out(200,{"live":True,"health":self.app.store.health()})
        if path=='/api/ready':
            health=self.app.store.health(); now=time.time()
            required=('manage','trade')
            worker_ok=all(x in health and now-float(health[x]['updated_at'])<10 and health[x]['state']=='HEALTHY' for x in required)
            snap=self.app.store.get('trade','snapshot',{}) or {}
            market=snap.get('health',{}).get('market_integrity',{})
            fresh=market.get('valid') and market.get('verified') and now*1000-float(market.get('last_update_ms',0))<5000
            ready=bool(worker_ok and fresh)
            return self.out(200 if ready else 503,{"ready":ready,"workers":health,"market":market})
        q=parse_qs(urlparse(self.path).query)
        if path=='/api/trade/candles': return self.out(200,self.app.candles((q.get('tf',['1m']) or ['1m'])[0],(q.get('end',['']) or [''])[0]))
        if path.startswith('/static/'):
            name=path[len('/static/'):]
            if name not in STATIC_FILES: return self.out(404,{"error":"not found"})
            return self.out(200,(Path(__file__).with_name('static')/name).read_bytes(),STATIC_FILES[name])
        if path=='/api/trade/trades.csv':
            b=self.app.trades_csv(q).encode(); self.send_response(200); self.send_header('Content-Type','text/csv'); self.send_header('Content-Disposition','attachment; filename="trades.csv"')
            self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if path not in {'/','/manage/overview','/manage/actions','/manage/system','/manage/settings','/think/overview','/think/candidates','/think/notebook','/think/pool','/think/champion','/think/settings',
                        '/trade/overview','/trade/trades','/trade/market','/trade/performance','/trade/settings'}:
            return self.out(404,{"error":"not found"})
        return self.out(200,self.app.render(path,q),'text/html; charset=utf-8')
    def do_POST(self):
        path=urlparse(self.path).path
        # JSON content type forces a CORS preflight, so other websites cannot post here.
        if not self.local() or not (self.headers.get('Content-Type') or '').startswith('application/json'): return self.out(403,{"error":"forbidden"})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<0 or n>16_384: raise ValueError('body size must be 0..16384 bytes')
            body=json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(body,dict): raise ValueError('expected JSON object')
        except (ValueError,json.JSONDecodeError) as exc:
            return self.out(400,{"error":str(exc)})
        m=self.app.manage
        try:
            if path=='/api/manage/chat':
                try: r=m.chat(str(body.get('message','')),body.get('chat_id') or None)
                except ValueError: raise
                except Exception as e: return self.out(503,{"error":f"Manage provider unavailable: {type(e).__name__}: {e}"})
                return self.out(200,{**r,"pending":len(self.app.store.actions("PENDING"))})
            if path=='/api/manage/chats/delete': return self.out(200,m.delete_chat(str(body.get('chat_id',''))))
            if path=='/api/manage/actions':
                d=body.get('decision')
                if d not in ('approve','reject'): raise ValueError('decision must be approve or reject')
                return self.out(200,m.approve_action(int(body.get('id',0))) if d=='approve' else m.reject_action(int(body.get('id',0))))
            if path=='/api/manage/restart': m.request_restart([str(body.get('worker',''))]); return self.out(200,{"ok":True})
            if path in ('/api/manage/settings','/api/think/settings'): return self.out(200,m.update_settings(path.split('/')[2],body))
        except (ValueError,PermissionError,FileNotFoundError) as e: return self.out(400,{"error":str(e)})
        if path=='/api/trade/propose':
            try: return self.out(200,self.app.propose_setting(str(body.get('key','')),body.get('value')))
            except (ValueError,PermissionError,FileNotFoundError) as e: return self.out(400,{"error":str(e)})
        if path=='/api/think/control':
            try: return self.out(200,self.app.think.set_control(body))
            except (ValueError,TypeError) as e: return self.out(400,{"error":str(e)})
        return self.out(404,{"error":"not found"})
    def log_message(self,*a): pass

def serve(root:Path,host:str,port:int):
    if host not in {'127.0.0.1','::1','localhost'}: raise ValueError('UI must bind to loopback')
    app=App(root); Handler.app=app; server=ThreadingHTTPServer((host,port),Handler)
    import threading
    def beat():
        while True: app.store.set_health(Health('ui','HEALTHY','ui running',os.getpid())); time.sleep(5)
    threading.Thread(target=beat,daemon=True).start(); server.serve_forever()
if __name__=='__main__':
    cfg=load_config(ROOT); serve(ROOT,cfg['ui']['host'],int(cfg['ui']['port']))
