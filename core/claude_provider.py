"""Claude backend for core.common.AgentProvider (Anthropic Messages API, manual tool-use loop).

Tools arrive in the shared function-tool shape ({"type":"function","name","description","parameters"})
and are converted to Claude tool definitions; web research uses Claude's server-side web search.
"""
from __future__ import annotations
import json, os
from typing import Any, Callable

EFFORT={"minimal":"low","low":"low","medium":"medium","high":"high","xhigh":"xhigh","max":"max"}
MAX_NON_STREAMING_TOKENS=16000  # keeps non-streaming requests well inside HTTP timeouts

def claude_tools(tools: list[dict] | None, web: bool) -> list[dict]:
    out=[{"name":t["name"],"description":t.get("description",""),"input_schema":t.get("parameters") or {"type":"object","properties":{}}}
         for t in tools or [] if t.get("type")=="function"]
    if web: out.append({"type":"web_search_20260209","name":"web_search","max_uses":5})
    return out

def run_claude(settings: dict, prompt: str, system: str, web: bool, tools: list[dict] | None,
               handler: Callable[[str,dict],Any] | None, max_tokens: int, steps: int, timeout: float, attempts: int,
               usage: dict | None = None) -> str:
    try:
        import anthropic
    except Exception as exc:
        raise RuntimeError("Anthropic SDK not installed; run pip install -r config/requirements-openai.txt") from exc
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    client=anthropic.Anthropic(timeout=timeout,max_retries=max(0,attempts-1))  # SDK retries 429/5xx/connection errors
    model=str(settings.get("model"))
    params:dict[str,Any]={"model":model,"max_tokens":min(int(max_tokens),MAX_NON_STREAMING_TOKENS),"system":system}
    if not model.startswith("claude-haiku"):  # Haiku 4.5 predates adaptive thinking and effort
        params["thinking"]={"type":"adaptive"}
        params["output_config"]={"effort":EFFORT.get(str(settings.get("reasoning","medium")),"medium")}
    ctools=claude_tools(tools,web)
    if ctools: params["tools"]=ctools
    messages:list[dict]=[{"role":"user","content":prompt}]; text=""
    for _ in range(steps+1):
        response=client.messages.create(messages=messages,**params)
        if usage is not None:
            u=getattr(response,"usage",None); usage["calls"]=usage.get("calls",0)+1
            if u is not None:
                usage["input_tokens"]=usage.get("input_tokens",0)+int(getattr(u,"input_tokens",0) or 0)+int(getattr(u,"cache_read_input_tokens",0) or 0)+int(getattr(u,"cache_creation_input_tokens",0) or 0)
                usage["output_tokens"]=usage.get("output_tokens",0)+int(getattr(u,"output_tokens",0) or 0)
        if response.stop_reason=="refusal":
            raise RuntimeError(f"Claude declined the request (category: {getattr(response.stop_details,'category',None)})")
        text="".join(b.text for b in response.content if b.type=="text")
        messages.append({"role":"assistant","content":response.content})  # full content: keeps thinking/tool blocks intact
        if response.stop_reason=="pause_turn":  # server-side web search paused; re-send to continue
            continue
        calls=[b for b in response.content if b.type=="tool_use"]
        if response.stop_reason!="tool_use" or not calls or handler is None:
            return text+("\n\n_(Reply cut off at the output limit. Raise Budget in Settings if needed.)_" if response.stop_reason=="max_tokens" else "")
        results=[]
        for c in calls:
            try: result=handler(c.name,c.input if isinstance(c.input,dict) else json.loads(c.input or "{}")); failed=False
            except Exception as exc: result={"error":f"{type(exc).__name__}: {exc}"}; failed=True
            results.append({"type":"tool_result","tool_use_id":c.id,"content":json.dumps(result,default=str)[:30000],"is_error":failed})
        messages.append({"role":"user","content":results})  # all results for this turn in one message
    return text+"\n\n_(Stopped: tool step limit reached. Raise Tool Autonomy in Settings if needed.)_"
