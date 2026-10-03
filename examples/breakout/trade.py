"""TRADE AI AUTHORING TEMPLATE v2.0

HOW TO USE THIS FILE
--------------------
After you and an AI have researched/agreed a trading hypothesis, give the AI this
entire file and say: "Create the final app-compatible trade.py using our agreed
strategy. Preserve all PROTECTED blocks exactly and return one complete Python
file."

WHAT IS ALREADY PROVIDED AND MUST REMAIN PROTECTED
--------------------------------------------------
- Real-time Bitfinex public WebSocket connection.
- Raw R0 Level-3 subscription (individual ORDER_ID / PRICE / signed AMOUNT).
- Public trades subscription (ID / MTS / signed AMOUNT / PRICE).
- Decimal-safe JSON parsing.
- Connection sequence checking.
- Individual-order R0 reconstruction.
- CRC32 checksum reconstruction and verification, including the proven tiny-
  Decimal serialization rule (for example 0.00000003 -> 3e-8).
- Staleness/integrity state and fail-closed reconnect/resynchronization.
- A checksum gate: unverified book evidence is buffered and is not released to
  editable science until the reconstructed book passes checksum validation.
- Rich CleanMarketEvent evidence including original/canonical payload rows, exact
  raw WebSocket message text, sequence, receive/exchange timestamps, channel,
  protected lifecycle context, top-of-book context, and integrity status.
- Protected paper accounting/fill truth and the app runtime/UI contract.

SCIENTIFIC FREEDOM
------------------
Everything inside EDITABLE sections may be redesigned. The scientific workspace
may keep any amount of in-process history/state and may derive anything useful
from healthy raw evidence: its own order book, individual-order lifecycle, rolling
windows, depth, imbalance, liquidity/replenishment, microprice, public-trade flow,
statistics, regimes, learned features, or entirely different representations.
Do not assume the example feature is required.

HARD OUTPUT RULES
-----------------
1. Return exactly ONE complete Python file to save as trade/trade.py.
2. Preserve every PROTECTED block byte-for-byte, including marker lines.
3. Keep TRADE_CONTRACT_VERSION and required runtime methods unchanged.
4. Keep paper_only=True. Never add live-money order execution or credential use.
5. Do not bypass sequence/checksum/staleness/reconnect or the checksum evidence gate.
6. Do not alter protected accounting/metrics truth to improve apparent results.
7. Edit TRADE_META/TRADE_UI_SPEC only within their allowed editable values and keep
   the generic UI self-describing.
8. Long-term raw market-data storage is not this Trade file's responsibility.
9. If the strategy needs unavailable evidence, fail visibly/HOLD; never invent it.
10. Run run_contract_self_test() before returning the file.

The app independently verifies protected-block hashes, so changing these written
instructions cannot make an incompatible Trade file acceptable.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import inspect
import json
import logging
import math
import time
from typing import Any, Deque, Iterable, Optional
import zlib


# ============================================================================
# PROTECTED:CONTRACT BEGIN
# ============================================================================

TRADE_CONTRACT_VERSION = "2.1"

REQUIRED_RUNTIME_METHODS = (
    "initialize",
    "run_live_market_data",
    "on_l3_snapshot",
    "on_l3_update",
    "on_l3_checksum",
    "on_public_trade_snapshot",
    "on_public_trade",
    "get_status",
    "get_positions",
    "get_recent_signals",
    "get_recent_executions",
    "get_metrics",
    "health",
    "get_ui_schema",
    "get_ui_snapshot",
)

@dataclass(frozen=True)
class MarketIntegrity:
    valid: bool
    connected: bool
    book_subscribed: bool
    trades_subscribed: bool
    verified: bool
    stale: bool
    last_sequence: Optional[int]
    sequence_gaps: int
    checksum_count: int
    checksum_mismatches: int
    last_verified_checksum: Optional[int]
    last_update_ms: int
    pending_evidence: int
    error: Optional[str] = None

@dataclass(frozen=True)
class CleanMarketEvent:
    """Healthy Bitfinex public-market evidence delivered to editable science."""
    kind: str
    source: str
    symbol: str
    channel: str
    channel_id: Optional[int]
    sequence: Optional[int]
    recv_time_ns: int
    recv_time_ms: int
    exchange_time_ms: Optional[int]
    payload: Any
    raw_message: Optional[str]
    protected_context: dict[str, Any]
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread_bps: Optional[float]
    integrity: MarketIntegrity

# Backward-friendly alias for code that conceptually expects clean L3 events.
CleanL3Event = CleanMarketEvent

@dataclass
class TradeDecision:
    action: str = "HOLD"
    symbol: str = "tBTCUSD"
    requested_size: float = 0.0
    confidence: Optional[float] = None
    reason: str = "no_signal"
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    max_hold_seconds: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class PositionState:
    symbol: str
    side: str
    size: float
    entry_price: float
    entry_time_ms: int
    unrealized_pnl: float = 0.0
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    max_hold_seconds: Optional[int] = None
    strategy_tag: Optional[str] = None
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class ExecutionRecord:
    action: str
    symbol: str
    side: str
    size: float
    fill_price: float
    fee_usd: float
    slippage_bps: float
    realized_pnl: float
    timestamp_ms: int
    reason: str
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class PerformanceMetrics:
    starting_equity_usd: float
    equity_usd: float
    net_pnl_usd: float
    return_pct: float
    gross_profit_usd: float
    gross_loss_usd: float
    profit_factor: Optional[float]
    expectancy_usd: Optional[float]
    win_rate_pct: Optional[float]
    average_win_usd: Optional[float]
    average_loss_usd: Optional[float]
    max_drawdown_pct: float
    closed_trades: int
    executions: int
    accounting_valid: bool
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class HealthStatus:
    ok: bool
    state: str
    message: str
    market_integrity: MarketIntegrity
    def as_dict(self) -> dict[str, Any]:
        data=asdict(self); data["market_integrity"]=asdict(self.market_integrity); return data

class TradeContractError(RuntimeError): pass
class L3IntegrityError(TradeContractError): pass

# ============================================================================
# PROTECTED:CONTRACT END
# ============================================================================


# ============================================================================
# EDITABLE:IDENTITY_AND_UI BEGIN
# AI/Think MAY edit values in this section, but must preserve dictionary shape.
# ============================================================================

TRADE_META: dict[str, Any] = {
    "contract_version": TRADE_CONTRACT_VERSION,
    "name": "BTC Channel Breakout",
    "version": "0.1.0",
    "description": "BTCUSD long/short paper strategy: enters when the mid breaks the high/low of the last 30 completed 1-minute bars with above-normal traded volume leaning the breakout way; exits on an initial stop sized from the channel, a trailing stop once in profit, or a maximum hold.",
    "symbols": ["tBTCUSD"],
    "paper_only": True,
    "expected_horizon": "minutes_to_2_hours",
    "supports_multi_symbol": False,
    "supports_multi_position": False,
}

TRADE_UI_SPEC: dict[str, Any] = {
    "custom_metrics": [
        {"key": "strategy_state", "label": "Strategy State", "format": "text"},
        {"key": "channel_high", "label": "Channel High", "format": "number", "precision": 2},
        {"key": "channel_low", "label": "Channel Low", "format": "number", "precision": 2},
        {"key": "channel_bps", "label": "Channel Width (bps)", "format": "number", "precision": 1},
        {"key": "breakout_bps", "label": "Beyond Channel (bps)", "format": "number", "precision": 1},
        {"key": "volume_ratio", "label": "Volume vs Normal", "format": "number", "precision": 2},
        {"key": "buy_share", "label": "Buy Share", "format": "number", "precision": 2},
        {"key": "trail_stop", "label": "Stop Price", "format": "number", "precision": 2},
        {"key": "estimated_net_bps", "label": "Est. Net PnL", "format": "number", "precision": 2},
    ],
    "settings": [
        {"key": "base_size", "label": "Max BTC Size", "type": "number", "min": 0.001, "step": 0.001},
        {"key": "risk_per_trade_pct", "label": "Risk / Trade %", "type": "number", "min": 0.05, "max": 1.0, "step": 0.05},
        {"key": "max_spread_bps", "label": "Max Spread (bps)", "type": "number", "min": 0.5, "max": 10.0, "step": 0.1},
        {"key": "channel_minutes", "label": "Channel (minutes)", "type": "number", "min": 5, "max": 240, "step": 1},
        {"key": "breakout_buffer_bps", "label": "Breakout Buffer (bps)", "type": "number", "min": 0.0, "max": 20.0, "step": 0.5},
        {"key": "volume_multiple", "label": "Volume Multiple", "type": "number", "min": 0.5, "max": 5.0, "step": 0.1},
        {"key": "trail_activate_bps", "label": "Trail Starts At (bps)", "type": "number", "min": 5.0, "max": 100.0, "step": 1.0},
        {"key": "trail_bps", "label": "Trail Distance (bps)", "type": "number", "min": 3.0, "max": 50.0, "step": 1.0},
        {"key": "max_hold_seconds", "label": "Max Hold (s)", "type": "number", "min": 300, "max": 14400, "step": 60},
    ],
}

# ============================================================================
# EDITABLE:IDENTITY_AND_UI END
# ============================================================================


# ============================================================================
# PROTECTED:MARKET_DATA BEGIN
# Do not modify. Live Bitfinex acquisition + trusted raw evidence boundary.
# ============================================================================

BITFINEX_PUBLIC_WS = "wss://api-pub.bitfinex.com/ws/2"
SEQ_ALL_FLAG = 65536
CHECKSUM_FLAG = 131072
DEFAULT_MARKET_CONFIG = {
    "url": BITFINEX_PUBLIC_WS,
    "symbol": "tBTCUSD",
    "book_precision": "R0",
    "book_frequency": "F0",
    "book_length": 250,
    "stale_seconds": 5.0,
    "reconnect_max_seconds": 30,
}

def _checksum_value(value: Any) -> str:
    # Proven R0 serialization rule, including tiny Decimal scientific notation.
    if not isinstance(value, Decimal): return str(value)
    if value == 0: return "0"
    if value.adjusted() >= -6: return format(value, "f")
    mantissa, exponent = format(value.normalize(), "e").split("e")
    return f"{mantissa}e{int(exponent)}"

@dataclass
class _RawOrder:
    order_id: int
    price: Decimal
    amount: Decimal
    first_seen_ms: int
    last_seen_ms: int
    @property
    def side(self)->str: return "bid" if self.amount>0 else "ask"
    @property
    def size(self)->float: return float(abs(self.amount))

class _ProtectedL3Book:
    def __init__(self)->None:
        self.orders: dict[int,_RawOrder]={}; self.last_update_ms=0; self.last_update_monotonic=0.0
    def clear(self)->None:
        self.orders.clear(); self.last_update_ms=0; self.last_update_monotonic=0.0
    def _touch(self,recv_ms:int)->None:
        self.last_update_ms=int(recv_ms); self.last_update_monotonic=time.monotonic()
    def apply_snapshot(self,entries:Iterable[Any],recv_ms:int)->None:
        new:dict[int,_RawOrder]={}
        for entry in entries:
            if not isinstance(entry,(list,tuple)) or len(entry)<3: raise L3IntegrityError(f"malformed_l3_snapshot_entry:{entry!r}")
            oid=int(entry[0]); price=entry[1] if isinstance(entry[1],Decimal) else Decimal(str(entry[1])); amount=entry[2] if isinstance(entry[2],Decimal) else Decimal(str(entry[2]))
            if price==0 or amount==0: raise L3IntegrityError(f"invalid_active_snapshot_order:{oid}")
            new[oid]=_RawOrder(oid,price,amount,recv_ms,recv_ms)
        if not new: raise L3IntegrityError("empty_l3_snapshot")
        self.orders=new; self._touch(recv_ms)
    def apply_update(self,entry:Any,recv_ms:int)->list[dict[str,Any]]:
        if not isinstance(entry,(list,tuple)) or len(entry)<3: raise L3IntegrityError(f"malformed_l3_entry:{entry!r}")
        oid=int(entry[0]); rp,ra=entry[1],entry[2]; price=rp if isinstance(rp,Decimal) else Decimal(str(rp)); amount=ra if isinstance(ra,Decimal) else Decimal(str(ra)); old=self.orders.get(oid); events=[]
        if price==0:
            if old is not None:
                self.orders.pop(oid,None); events.append({"kind":"remove","order_id":oid,"side":old.side,"price":float(old.price),"qty":old.size,"ts_ms":recv_ms})
            self._touch(recv_ms); return events
        if amount==0: raise L3IntegrityError(f"zero_amount_active_order:{oid}")
        if old is None:
            new=_RawOrder(oid,price,amount,recv_ms,recv_ms); self.orders[oid]=new; events.append({"kind":"add","order_id":oid,"side":new.side,"price":float(price),"qty":new.size,"ts_ms":recv_ms})
        else:
            new_side="bid" if amount>0 else "ask"
            if old.price!=price or old.side!=new_side:
                events.append({"kind":"remove","order_id":oid,"side":old.side,"price":float(old.price),"qty":old.size,"ts_ms":recv_ms})
                new=_RawOrder(oid,price,amount,recv_ms,recv_ms); self.orders[oid]=new; events.append({"kind":"add","order_id":oid,"side":new.side,"price":float(price),"qty":new.size,"ts_ms":recv_ms})
            else:
                new_size=float(abs(amount)); delta=new_size-old.size; self.orders[oid]=_RawOrder(oid,price,amount,old.first_seen_ms,recv_ms)
                if abs(delta)>1e-12: events.append({"kind":"resize_up" if delta>0 else "resize_down","order_id":oid,"side":old.side,"price":float(price),"qty":abs(delta),"new_size":new_size,"ts_ms":recv_ms})
        self._touch(recv_ms); return events
    def bids(self): return sorted((o for o in self.orders.values() if o.amount>0),key=lambda o:(-o.price,o.order_id))
    def asks(self): return sorted((o for o in self.orders.values() if o.amount<0),key=lambda o:(o.price,o.order_id))
    def best_bid(self):
        x=self.bids(); return float(x[0].price) if x else None
    def best_ask(self):
        x=self.asks(); return float(x[0].price) if x else None
    def checksum_payload(self)->str:
        bids=self.bids()[:25]; asks=self.asks()[:25]; vals=[]
        for i in range(max(len(bids),len(asks))):
            if i<len(bids): vals.extend([bids[i].order_id,bids[i].amount])
            if i<len(asks): vals.extend([asks[i].order_id,asks[i].amount])
        return ":".join(_checksum_value(v) for v in vals)
    def checksum(self)->int:
        value=zlib.crc32(self.checksum_payload().encode("utf-8")); return value if value<2**31 else value-2**32

@dataclass
class _PendingEvidence:
    kind:str; channel:str; channel_id:Optional[int]; sequence:Optional[int]; recv_time_ns:int; exchange_time_ms:Optional[int]; payload:Any; raw_message:Optional[str]; protected_context:dict[str,Any]; best_bid:Optional[float]; best_ask:Optional[float]

class _ProtectedMarketInput:
    def __init__(self,symbol:str,stale_after_seconds:float=5.0)->None:
        self.symbol=symbol; self.stale_after_seconds=float(stale_after_seconds); self.book=_ProtectedL3Book(); self.last_sequence=None; self.sequence_gaps=0; self.checksum_count=0; self.checksum_mismatches=0; self.last_verified_checksum=None; self.last_error=None; self.connected=False; self.book_subscribed=False; self.trades_subscribed=False; self.verified=False; self.pending:list[_PendingEvidence]=[]; self.pending_book=False
    def reset_connection(self,error:Optional[str]=None)->None:
        self.book.clear(); self.last_sequence=None; self.connected=False; self.book_subscribed=False; self.trades_subscribed=False; self.verified=False; self.pending.clear(); self.pending_book=False; self.last_error=error
    def mark_connected(self)->None:
        self.connected=True; self.last_error=None
    def mark_subscribed(self,channel:str)->None:
        if channel=="book": self.book_subscribed=True
        elif channel=="trades": self.trades_subscribed=True
    def sequence_only(self,sequence:Optional[int])->None: self._check_sequence(sequence)
    def _check_sequence(self,sequence:Optional[int])->None:
        if sequence is None:
            self.last_error="missing_sequence"; raise L3IntegrityError(self.last_error)
        if self.last_sequence is not None and int(sequence)!=self.last_sequence+1:
            self.sequence_gaps+=1; self.last_error=f"sequence_gap previous={self.last_sequence} current={sequence}"; raise L3IntegrityError(self.last_error)
        self.last_sequence=int(sequence)
    def _stale(self)->bool:
        if self.book.last_update_monotonic<=0:return True
        return (time.monotonic()-self.book.last_update_monotonic)>self.stale_after_seconds
    def integrity(self)->MarketIntegrity:
        bid,ask=self._top()
        crossed=(bid is None or ask is None or not all(math.isfinite(float(x)) and float(x)>0 for x in (bid,ask)) or float(bid)>=float(ask))
        stale=self._stale(); valid=self.connected and self.book_subscribed and self.trades_subscribed and self.verified and not stale and not crossed and self.last_error is None
        return MarketIntegrity(valid,self.connected,self.book_subscribed,self.trades_subscribed,self.verified,stale,self.last_sequence,self.sequence_gaps,self.checksum_count,self.checksum_mismatches,self.last_verified_checksum,self.book.last_update_ms,len(self.pending),self.last_error)
    def _top(self):
        bid=self.book.best_bid(); ask=self.book.best_ask(); return bid,ask
    def _pending(self,kind,channel,channel_id,sequence,recv_ns,exchange_ms,payload,raw_message,context)->_PendingEvidence:
        bid,ask=self._top(); return _PendingEvidence(kind,channel,channel_id,sequence,int(recv_ns),exchange_ms,payload,raw_message,context,bid,ask)
    def _clean(self,p:_PendingEvidence)->CleanMarketEvent:
        integ=self.integrity(); mid=(p.best_bid+p.best_ask)/2.0 if p.best_bid is not None and p.best_ask is not None else None; spread=(p.best_ask-p.best_bid)/mid*10000.0 if mid and p.best_bid is not None and p.best_ask is not None else None
        return CleanMarketEvent(p.kind,"bitfinex_public_ws",self.symbol,p.channel,p.channel_id,p.sequence,p.recv_time_ns,p.recv_time_ns//1_000_000,p.exchange_time_ms,p.payload,p.raw_message,dict(p.protected_context),p.best_bid,p.best_ask,mid,spread,integ)
    def release_ready(self)->list[CleanMarketEvent]:
        if self.pending_book or not self.integrity().valid or not self.pending: return []
        if time.time_ns()-self.pending[0].recv_time_ns > 5_000_000_000:
            self.pending.clear(); self.last_error="checksum_deadline_exceeded"; raise L3IntegrityError(self.last_error)
        batch=self.pending; self.pending=[]; return [self._clean(p) for p in batch]
    def snapshot(self,entries,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); rows=list(entries); self.book.apply_snapshot(rows,recv_ns//1_000_000); self.verified=False; self.pending_book=True; self.pending.clear(); self.pending.append(self._pending("l3_snapshot","book",channel_id,sequence,recv_ns,None,rows,raw_message,{"order_count":len(rows)})); return []
    def update(self,entry,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); lifecycle=self.book.apply_update(entry,recv_ns//1_000_000); self.verified=bool(self.last_verified_checksum is not None); self.pending_book=True; self.pending.append(self._pending("l3_update","book",channel_id,sequence,recv_ns,None,entry,raw_message,{"lifecycle":lifecycle})); self._guard_pending(); return []
    def trade_snapshot(self,trades,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); rows=list(trades); p=self._pending("public_trade_snapshot","trades",channel_id,sequence,recv_ns,None,rows,raw_message,{"trade_count":len(rows)})
        if self.pending_book or not self.integrity().valid: self.pending.append(p); self._guard_pending(); return []
        return [self._clean(p)]
    def trade(self,trade,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); row=list(trade); exchange=int(row[1]) if len(row)>1 else None; p=self._pending("public_trade","trades",channel_id,sequence,recv_ns,exchange,row,raw_message,{})
        if self.pending_book or not self.integrity().valid:
            if self.verified and self._stale(): self.last_error="market_stale_before_trade"; raise L3IntegrityError(self.last_error)
            self.pending.append(p); self._guard_pending(); return []
        return [self._clean(p)]
    def checksum(self,server_checksum,sequence,recv_ns,channel_id=None,raw_message=None)->list[CleanMarketEvent]:
        self._check_sequence(sequence); local=self.book.checksum(); self.checksum_count+=1
        if int(server_checksum)!=int(local):
            self.checksum_mismatches+=1; self.verified=False; self.last_error=f"checksum_mismatch server={server_checksum} local={local}"; self.pending.clear(); self.pending_book=False; raise L3IntegrityError(self.last_error)
        self.last_verified_checksum=int(server_checksum); self.verified=True; self.last_error=None; self.pending_book=False
        if not self.integrity().valid:
            self.last_error="invalid_or_stale_book_at_checksum"; self.pending.clear(); raise L3IntegrityError(self.last_error)
        cs=self._pending("l3_checksum","book",channel_id,sequence,recv_ns,None,{"checksum":int(server_checksum)},raw_message,{"local_checksum":local})
        self.pending.append(cs)
        return self.release_ready()
    def _guard_pending(self)->None:
        if len(self.pending)>10_000 or (self.pending and time.time_ns()-self.pending[0].recv_time_ns>5_000_000_000):
            self.pending.clear(); self.last_error="pending_evidence_limit"; raise L3IntegrityError(self.last_error)

class _BitfinexPublicFeed:
    def __init__(self,runtime:"TradeRuntime",config:dict[str,Any])->None:
        self.runtime=runtime; self.config=dict(DEFAULT_MARKET_CONFIG); self.config.update(config or {}); self.channels:dict[int,str]={}; self.log=logging.getLogger("trade.bitfinex")
    @staticmethod
    def _parse(raw:str): return json.loads(raw,parse_float=Decimal)
    @staticmethod
    def _split_sequence(msg:list)->tuple[list,Optional[int]]:
        if len(msg)>=3 and isinstance(msg[-1],int): return msg[:-1],int(msg[-1])
        return msg,None
    async def run(self)->None:
        try: import websockets
        except ImportError as exc: raise RuntimeError("live market feed requires the 'websockets' package; install config/requirements-market.txt") from exc
        backoff=1
        while True:
            try:
                await self._run_connection(websockets); backoff=1
            except asyncio.CancelledError:
                self.runtime.input.reset_connection("feed_cancelled"); raise
            except Exception as exc:
                self.runtime.input.reset_connection(repr(exc)); self.log.exception("Bitfinex feed failure; reconnecting")
                await asyncio.sleep(backoff); backoff=min(backoff*2,int(self.config.get("reconnect_max_seconds",30)))
    async def _run_connection(self,websockets)->None:
        url=str(self.config.get("url",BITFINEX_PUBLIC_WS)); symbol=str(self.config.get("symbol",self.runtime.symbol))
        async with websockets.connect(url,ping_interval=20,ping_timeout=20,close_timeout=5,max_queue=8192) as ws:
            self.channels.clear(); self.runtime.input.reset_connection(); self.runtime.input.mark_connected()
            await ws.send(json.dumps({"event":"conf","flags":SEQ_ALL_FLAG+CHECKSUM_FLAG}))
            await ws.send(json.dumps({"event":"subscribe","channel":"book","symbol":symbol,"prec":"R0","freq":str(self.config.get("book_frequency","F0")),"len":str(self.config.get("book_length",250))}))
            await ws.send(json.dumps({"event":"subscribe","channel":"trades","symbol":symbol}))
            while True:
                raw=await asyncio.wait_for(ws.recv(),timeout=10)
                recv_ns=time.time_ns(); raw=raw.decode("utf-8") if isinstance(raw,(bytes,bytearray)) else raw; msg=self._parse(raw)
                if isinstance(msg,dict):
                    event=msg.get("event")
                    if event=="subscribed":
                        cid=int(msg["chanId"]); channel=str(msg.get("channel")); self.channels[cid]=channel; self.runtime.input.mark_subscribed(channel); self.runtime._handle_events(self.runtime.input.release_ready())
                    elif event=="error": raise RuntimeError(f"Bitfinex error event: {msg}")
                    elif event=="info" and msg.get("code") in {20051,20060}: raise RuntimeError(f"Bitfinex websocket restart/maintenance code={msg.get('code')}")
                    continue
                if not isinstance(msg,list) or len(msg)<2: continue
                body,seq=self._split_sequence(msg); cid=int(body[0]); channel=self.channels.get(cid)
                if channel is None: self.runtime.input.sequence_only(seq); continue
                if body[1]=="hb":
                    self.runtime.input.sequence_only(seq)
                    if self.runtime.input._stale(): raise L3IntegrityError("heartbeat_only_stale_book")
                    continue
                if channel=="book":
                    if body[1]=="cs": self.runtime.on_l3_checksum(int(body[2]),seq,recv_ns,cid,raw); continue
                    payload=body[1]
                    if isinstance(payload,list) and payload and isinstance(payload[0],list): self.runtime.on_l3_snapshot(payload,seq,recv_ns,cid,raw)
                    elif isinstance(payload,list): self.runtime.on_l3_update(payload,seq,recv_ns,cid,raw)
                    else: raise L3IntegrityError(f"malformed_book_message:{body!r}")
                    continue
                if channel=="trades":
                    payload=body[1]
                    if isinstance(payload,list) and (not payload or isinstance(payload[0],list)):
                        rows=[]
                        for row in payload:
                            if not isinstance(row,list) or len(row)<4: raise L3IntegrityError(f"malformed_trade_snapshot_row:{row!r}")
                            rows.append([int(row[0]),int(row[1]),row[2],row[3]])
                        self.runtime.on_public_trade_snapshot(rows,seq,recv_ns,cid,raw); continue
                    if isinstance(payload,str) and payload in {"te","tu"} and len(body)>=3:
                        if payload=="tu": self.runtime.input.sequence_only(seq); continue
                        row=body[2]
                        if not isinstance(row,list) or len(row)<4: raise L3IntegrityError(f"malformed_trade_row:{row!r}")
                        self.runtime.on_public_trade([int(row[0]),int(row[1]),row[2],row[3]],seq,recv_ns,cid,raw); continue
                    raise L3IntegrityError(f"malformed_trade_message:{body!r}")

# ============================================================================
# PROTECTED:MARKET_DATA END
# ============================================================================


# ============================================================================
# PROTECTED:PAPER_TRUTH BEGIN
# Fixed benchmark accounting/fill assumptions used for objective comparison.
# Think may change trading decisions, not this truth layer.
# ============================================================================

PAPER_DEFAULTS = {
    "starting_equity_usd": 10_000.0,
    "fee_bps": 5.0,
    "slippage_bps": 1.0,
}


class _ProtectedPaperBroker:
    def __init__(self, starting_equity_usd: float = 10_000.0) -> None:
        self.starting_equity_usd = float(starting_equity_usd)
        self.realized_pnl_usd = 0.0
        self.positions: dict[str, PositionState] = {}
        self.entry_fees: dict[str, float] = {}
        self.executions: list[ExecutionRecord] = []
        self.trade_pnls: list[float] = []
        self.peak_equity = self.starting_equity_usd
        self.max_drawdown_pct = 0.0

    @staticmethod
    def _fill_price(side: str, bid: float, ask: float) -> tuple[float, float]:
        slippage_bps = float(PAPER_DEFAULTS["slippage_bps"])
        if side == "buy":
            base = ask
            fill = base * (1.0 + slippage_bps / 10_000.0)
        else:
            base = bid
            fill = base * (1.0 - slippage_bps / 10_000.0)
        return fill, slippage_bps

    @staticmethod
    def _fee(notional: float) -> float:
        return abs(notional) * float(PAPER_DEFAULTS["fee_bps"]) / 10_000.0

    def _unrealized_total(self, event: CleanMarketEvent) -> float:
        total = 0.0
        for position in self.positions.values():
            if position.symbol != event.symbol:
                continue
            mark = event.best_bid if position.side == "long" else event.best_ask
            if mark is None:
                continue
            signed = position.size if position.side == "long" else -position.size
            position.unrealized_pnl = signed * (float(mark) - position.entry_price)
            total += position.unrealized_pnl
        return total

    def equity(self, event: Optional[CleanMarketEvent] = None) -> float:
        unrealized = self._unrealized_total(event) if event is not None else 0.0
        equity = self.starting_equity_usd + self.realized_pnl_usd + unrealized - sum(self.entry_fees.values())
        self.peak_equity = max(self.peak_equity, equity)
        if self.peak_equity > 0:
            dd = max(0.0, (self.peak_equity - equity) / self.peak_equity)
            self.max_drawdown_pct = max(self.max_drawdown_pct, dd)
        return equity

    def execute(self, decision: TradeDecision, event: CleanMarketEvent) -> Optional[ExecutionRecord]:
        action = str(decision.action).upper()
        symbol = decision.symbol or event.symbol
        if action == "HOLD":
            return None
        if event.best_bid is None or event.best_ask is None:
            raise TradeContractError("missing_executable_bid_ask")
        now_ms = event.recv_time_ms
        current = self.positions.get(symbol)

        if action in {"ENTER_LONG", "ENTER_SHORT"}:
            if current is not None or decision.requested_size <= 0:
                return None
            side = "buy" if action == "ENTER_LONG" else "sell"
            fill, slip = self._fill_price(side, event.best_bid, event.best_ask)
            fee = self._fee(fill * decision.requested_size)
            pos_side = "long" if action == "ENTER_LONG" else "short"
            self.positions[symbol] = PositionState(
                symbol=symbol,
                side=pos_side,
                size=float(decision.requested_size),
                entry_price=fill,
                entry_time_ms=now_ms,
                stop_price=decision.stop_price,
                target_price=decision.target_price,
                max_hold_seconds=decision.max_hold_seconds,
                strategy_tag=str(decision.metadata.get("strategy_tag") or "default"),
            )
            self.entry_fees[symbol] = fee
            rec = ExecutionRecord(
                action=action,
                symbol=symbol,
                side=side,
                size=float(decision.requested_size),
                fill_price=fill,
                fee_usd=fee,
                slippage_bps=slip,
                realized_pnl=0.0,
                timestamp_ms=now_ms,
                reason=decision.reason,
            )
            self.executions.append(rec)
            self.equity(event)
            return rec

        if action == "EXIT" and current is not None:
            side = "sell" if current.side == "long" else "buy"
            fill, slip = self._fill_price(side, event.best_bid, event.best_ask)
            exit_fee = self._fee(fill * current.size)
            signed = current.size if current.side == "long" else -current.size
            gross = signed * (fill - current.entry_price)
            realized = gross - float(self.entry_fees.pop(symbol, 0.0)) - exit_fee
            self.realized_pnl_usd += realized
            self.trade_pnls.append(realized)
            self.positions.pop(symbol, None)
            rec = ExecutionRecord(
                action="EXIT",
                symbol=symbol,
                side=side,
                size=current.size,
                fill_price=fill,
                fee_usd=exit_fee,
                slippage_bps=slip,
                realized_pnl=realized,
                timestamp_ms=now_ms,
                reason=decision.reason,
            )
            self.executions.append(rec)
            self.equity(event)
            return rec
        return None

    def metrics(self, event: Optional[CleanMarketEvent] = None) -> PerformanceMetrics:
        equity = self.equity(event)
        wins = [x for x in self.trade_pnls if x > 0]
        losses = [x for x in self.trade_pnls if x < 0]
        gross_profit = sum(wins)
        gross_loss_abs = abs(sum(losses))
        closed = len(self.trade_pnls)
        profit_factor = gross_profit / gross_loss_abs if gross_loss_abs > 0 else None
        expectancy = sum(self.trade_pnls) / closed if closed else None
        win_rate = len(wins) / closed * 100.0 if closed else None
        avg_win = gross_profit / len(wins) if wins else None
        avg_loss = sum(losses) / len(losses) if losses else None
        net = equity - self.starting_equity_usd
        return PerformanceMetrics(
            starting_equity_usd=self.starting_equity_usd,
            equity_usd=equity,
            net_pnl_usd=net,
            return_pct=(net / self.starting_equity_usd * 100.0) if self.starting_equity_usd else 0.0,
            gross_profit_usd=gross_profit,
            gross_loss_usd=sum(losses),
            profit_factor=profit_factor,
            expectancy_usd=expectancy,
            win_rate_pct=win_rate,
            average_win_usd=avg_win,
            average_loss_usd=avg_loss,
            max_drawdown_pct=self.max_drawdown_pct * 100.0,
            closed_trades=closed,
            executions=len(self.executions),
            accounting_valid=math.isfinite(equity),
        )

# ============================================================================
# PROTECTED:PAPER_TRUTH END
# ============================================================================


# ============================================================================
# EDITABLE:SCIENTIFIC_WORKSPACE BEGIN
# AI/Think may replace this entire section. Healthy raw R0 + public-trade
# evidence arrives here without being reduced to a prescribed feature set.
# Maintain any history/state/windows/models you need.
# ============================================================================

@dataclass
class TradeConfig:
    # Position/risk. base_size is a hard cap; sizing also respects the loss implied by the stop plus costs.
    base_size: float = 0.01
    risk_per_trade_pct: float = 0.25
    assumed_equity_usd: float = 10_000.0
    # Mirrors of the protected paper costs (used only for estimates; the protected broker is the truth).
    paper_fee_bps_each_side: float = 5.0
    paper_slippage_bps_each_side: float = 1.0

    # Filters.
    max_spread_bps: float = 3.0
    # Channel: high/low of the last N completed 1-minute bars of the mid price.
    channel_minutes: int = 30
    breakout_buffer_bps: float = 2.0     # mid must clear the channel edge by this much
    min_channel_bps: float = 15.0        # a narrower channel is noise, not a range worth breaking
    max_channel_bps: float = 250.0       # a wider one means the market already moved; skip

    # Volume confirmation from public trades.
    volume_window_seconds: int = 120
    volume_baseline_minutes: int = 60
    volume_multiple: float = 1.5         # recent volume vs the normal volume of an equal window
    min_side_share: float = 0.55         # share of recent volume traded in the breakout direction

    # Stops / exits.
    stop_channel_fraction: float = 0.5   # initial stop = this fraction of the channel width...
    min_stop_bps: float = 8.0            # ...kept within these bounds
    max_stop_bps: float = 25.0
    trail_activate_bps: float = 25.0     # trailing starts once the best price is this far in favour (> costs + trail)
    trail_bps: float = 10.0              # then the stop follows the best price at this distance
    max_hold_seconds: int = 7200
    cooldown_seconds: int = 120
    # Keep price/trade history across a reconnect shorter than this.
    reconnect_keep_history_seconds: int = 120


class ScientificCore:
    """Channel breakout: 1-minute bars of the mid and signed public-trade volume, rebuilt from clean evidence.

    Required by the platform (trade worker, checkpoints, shadows, Think): `config`, `event_history`,
    `position_state` (JSON-safe dict per symbol), `cooldown_until_ms` (int), `last_features` (dict),
    `_apply_event(event)` for history-only events and `decide(event, positions)` for current ones.
    """

    def __init__(self, config: Optional[TradeConfig] = None) -> None:
        self.config = config or TradeConfig()
        self.event_history: Deque[CleanMarketEvent] = deque(maxlen=2_000)
        self.bars: Deque[list] = deque(maxlen=600)                          # [minute_ms, high, low, close]
        self.trades: Deque[tuple[int, int, float]] = deque(maxlen=200_000)  # (ms, trade id, signed amount)
        self.seen_trade_ids: set[int] = set()
        self.first_event_ms: Optional[int] = None
        self.last_event_ms: Optional[int] = None
        self.cooldown_until_ms = 0
        self.position_state: dict[str, dict[str, Any]] = {}
        self.last_features: dict[str, Any] = {}

    # ------------------------------ evidence ------------------------------

    def _apply_event(self, event: CleanMarketEvent) -> None:
        self.event_history.append(event)
        now = int(event.recv_time_ms)
        if self.first_event_ms is None:
            self.first_event_ms = now
        if event.kind == "l3_snapshot":
            # New connection: keep history across a short gap, otherwise start over (warm-up again).
            short_gap = self.last_event_ms is not None and now - self.last_event_ms <= self.config.reconnect_keep_history_seconds * 1000
            if not short_gap:
                self.bars.clear(); self.trades.clear(); self.seen_trade_ids.clear(); self.first_event_ms = now
        elif event.kind == "public_trade_snapshot":
            for row in list(event.payload or []):
                self._record_trade(row, now)
        elif event.kind == "public_trade":
            self._record_trade(event.payload, now)
        if event.mid is not None and math.isfinite(float(event.mid)) and float(event.mid) > 0:
            mid = float(event.mid); minute = now // 60_000 * 60_000
            if self.bars and self.bars[-1][0] == minute:
                b = self.bars[-1]; b[1] = max(b[1], mid); b[2] = min(b[2], mid); b[3] = mid
            else:
                self.bars.append([minute, mid, mid, mid])
        self.last_event_ms = now
        cutoff = now - (self.config.volume_baseline_minutes * 60 + self.config.volume_window_seconds) * 1000
        while self.trades and self.trades[0][0] < cutoff:
            self.seen_trade_ids.discard(self.trades.popleft()[1])

    def _record_trade(self, row: Any, recv_ms: int) -> None:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            return
        try:
            trade_id = int(row[0]); exchange_ms = int(row[1]); amount = float(row[2]); price = float(row[3])
        except (TypeError, ValueError):
            return
        if trade_id in self.seen_trade_ids or amount == 0 or price <= 0:
            return
        self.seen_trade_ids.add(trade_id)
        self.trades.append((exchange_ms if exchange_ms > 0 else recv_ms, trade_id, amount))

    # ------------------------------ features ------------------------------

    def _features(self, event: CleanMarketEvent) -> dict[str, Any]:
        c = self.config; now = int(event.recv_time_ms); mid = float(event.mid or 0.0)
        minute = now // 60_000 * 60_000
        done = [b for b in self.bars if b[0] < minute][-int(c.channel_minutes):]
        # a channel needs the full count of consecutive completed minutes (no gap inside it)
        complete = len(done) == int(c.channel_minutes) and done[-1][0] == minute - 60_000 and done[0][0] == minute - int(c.channel_minutes) * 60_000
        # the volume baseline needs its full history too, or "normal" volume is underestimated
        history_ms = now - (self.first_event_ms if self.first_event_ms is not None else now)
        complete = complete and history_ms >= (c.volume_baseline_minutes * 60 + c.volume_window_seconds) * 1000
        hi = max(b[1] for b in done) if done else None
        lo = min(b[2] for b in done) if done else None
        width = (hi - lo) / mid * 1e4 if hi and lo and mid else None
        win = c.volume_window_seconds * 1000
        recent = [a for t, _, a in self.trades if t >= now - win]
        base = [abs(a) for t, _, a in self.trades if now - win - c.volume_baseline_minutes * 60_000 <= t < now - win]
        vol = sum(abs(a) for a in recent)
        normal = sum(base) / (c.volume_baseline_minutes * 60_000 / win) if base else 0.0
        buy = sum(a for a in recent if a > 0)
        cost = 2.0 * (c.paper_fee_bps_each_side + c.paper_slippage_bps_each_side) + float(event.spread_bps or 0.0)
        up = (mid - hi) / hi * 1e4 if hi and mid else None
        down = (lo - mid) / lo * 1e4 if lo and mid else None
        return {"mid": mid, "channel_ready": complete, "channel_high": hi, "channel_low": lo, "channel_bps": width,
                "breakout_bps": max(up, down) if (up is not None and down is not None) else None,
                "up_bps": up, "down_bps": down, "volume": vol, "normal_volume": normal,
                "volume_ratio": vol / normal if normal > 0 else None, "buy_share": buy / vol if vol > 0 else None,
                "cost_bps": cost,
                # shared names the platform shows in "why it entered"
                "reference_high": hi, "reference_low": lo, "min_move_bps": c.trail_activate_bps,
                "regime": "breakout_up" if up is not None and up >= c.breakout_buffer_bps else ("breakout_down" if down is not None and down >= c.breakout_buffer_bps else "in_range")}

    # ------------------------------ entries -------------------------------

    def _size_for_stop(self, entry: float, stop_bps: float, cost_bps: float) -> float:
        if entry <= 0:
            return 0.0
        risk_budget = self.config.assumed_equity_usd * self.config.risk_per_trade_pct / 100.0
        loss_per_btc = entry * (stop_bps + cost_bps) / 10_000.0
        size = min(float(self.config.base_size), risk_budget / loss_per_btc if loss_per_btc > 0 else 0.0)
        return max(0.0, math.floor(size * 1000.0) / 1000.0)

    def _entry(self, event: CleanMarketEvent, side: str, f: dict[str, Any]) -> Optional[TradeDecision]:
        c = self.config; slip = c.paper_slippage_bps_each_side / 10_000.0
        entry = float(event.best_ask) * (1 + slip) if side == "long" else float(event.best_bid) * (1 - slip)
        stop_bps = min(c.max_stop_bps, max(c.min_stop_bps, float(f["channel_bps"]) * c.stop_channel_fraction))
        size = self._size_for_stop(entry, stop_bps, float(f["cost_bps"]))
        if size <= 0:
            return None
        stop = entry * (1 - stop_bps / 1e4) if side == "long" else entry * (1 + stop_bps / 1e4)
        self.position_state[event.symbol] = {"side": side, "entry_fill": entry, "entry_ms": int(event.recv_time_ms), "stop": stop,
                                             "initial_stop_bps": stop_bps, "best": entry, "trailing": False}
        return TradeDecision(action="ENTER_LONG" if side == "long" else "ENTER_SHORT", symbol=event.symbol, requested_size=size,
                             confidence=min(0.9, 0.5 + 0.1 * float(f.get("volume_ratio") or 0)), reason=f"breakout_{side}_confirmed",
                             stop_price=stop, target_price=None, max_hold_seconds=int(c.max_hold_seconds),
                             metadata=self._metadata(f, f"enter_breakout_{side}", 0.0, stop))

    # ------------------------------ position ------------------------------

    def _exit(self, event: CleanMarketEvent, f: dict[str, Any], reason: str, net_bps: float) -> TradeDecision:
        self.cooldown_until_ms = int(event.recv_time_ms) + int(self.config.cooldown_seconds) * 1000
        self.position_state.pop(event.symbol, None)
        return TradeDecision(action="EXIT", symbol=event.symbol, reason=reason, confidence=1.0, metadata=self._metadata(f, reason, net_bps, None))

    def _manage(self, event: CleanMarketEvent, pos: PositionState, f: dict[str, Any]) -> TradeDecision:
        c = self.config; now = int(event.recv_time_ms); side = pos.side
        st = self.position_state.setdefault(event.symbol, {   # after a restart: rebuild from the protected position
            "side": side, "entry_fill": pos.entry_price, "entry_ms": pos.entry_time_ms,
            "stop": pos.stop_price if pos.stop_price is not None else pos.entry_price * (1 - c.max_stop_bps / 1e4 if side == "long" else 1 + c.max_stop_bps / 1e4),
            "initial_stop_bps": c.max_stop_bps, "best": pos.entry_price, "trailing": False})
        exit_px = float(event.best_bid) if side == "long" else float(event.best_ask)
        sign = 1.0 if side == "long" else -1.0
        net = sign * (exit_px - pos.entry_price) / pos.entry_price * 1e4 - 2 * c.paper_fee_bps_each_side - c.paper_slippage_bps_each_side
        # stop first (initial or trailing), on the price we could actually exit at
        if (side == "long" and exit_px <= st["stop"]) or (side == "short" and exit_px >= st["stop"]):
            return self._exit(event, f, "trailing_stop" if st["trailing"] else "initial_stop", net)
        # trail the best exit price once far enough in favour
        st["best"] = max(st["best"], exit_px) if side == "long" else min(st["best"], exit_px)
        favour = sign * (st["best"] - pos.entry_price) / pos.entry_price * 1e4
        if favour >= c.trail_activate_bps:
            # never trail below break-even after costs: a trailing exit must be a net gain
            be_bps = 2 * (c.paper_fee_bps_each_side + c.paper_slippage_bps_each_side) + float(event.spread_bps or 0.0)
            floor = pos.entry_price * (1 + be_bps / 1e4) if side == "long" else pos.entry_price * (1 - be_bps / 1e4)
            trail = max(st["best"] * (1 - c.trail_bps / 1e4), floor) if side == "long" else min(st["best"] * (1 + c.trail_bps / 1e4), floor)
            st["stop"] = max(st["stop"], trail) if side == "long" else min(st["stop"], trail); st["trailing"] = True
        if now - int(pos.entry_time_ms) >= int(c.max_hold_seconds) * 1000:
            return self._exit(event, f, "maximum_hold", net)
        return TradeDecision(action="HOLD", symbol=event.symbol, reason="manage_open_position", confidence=0.5,
                             metadata=self._metadata(f, "trailing" if st["trailing"] else "initial_stop", net, st["stop"]))

    # ------------------------------- output -------------------------------

    def _metadata(self, f: dict[str, Any], state: str, net_bps: float = 0.0, stop: Optional[float] = None) -> dict[str, Any]:
        return {"strategy_tag": state, "strategy_state": state, "regime": f.get("regime"), "channel_high": f.get("channel_high"),
                "channel_low": f.get("channel_low"), "channel_bps": f.get("channel_bps"), "breakout_bps": f.get("breakout_bps"),
                "volume_ratio": f.get("volume_ratio"), "buy_share": f.get("buy_share"), "trail_stop": stop,
                "estimated_net_bps": net_bps, "estimated_roundtrip_cost_bps": f.get("cost_bps")}

    def _hold(self, event: CleanMarketEvent, f: dict[str, Any], reason: str) -> TradeDecision:
        return TradeDecision(action="HOLD", symbol=event.symbol, reason=reason, metadata=self._metadata(f, reason))

    def decide(self, event: CleanMarketEvent, positions: list[PositionState]) -> TradeDecision:
        self._apply_event(event)
        f = self._features(event); self.last_features = f
        c = self.config; now = int(event.recv_time_ms)
        if positions:
            return self._manage(event, positions[0], f)
        self.position_state.pop(event.symbol, None)   # no protected position: no stale state
        if not f["channel_ready"]:
            return self._hold(event, f, "warming_up")
        if event.spread_bps is None or not math.isfinite(float(event.spread_bps)) or not 0 <= float(event.spread_bps) <= c.max_spread_bps:
            return self._hold(event, f, "spread_filter")
        if now < self.cooldown_until_ms:
            return self._hold(event, f, "cooldown")
        width = f["channel_bps"]
        if width is None or width < c.min_channel_bps:
            return self._hold(event, f, "channel_too_narrow")
        if width > c.max_channel_bps:
            return self._hold(event, f, "channel_too_wide")
        up, down = f["up_bps"], f["down_bps"]
        side = "long" if up is not None and up >= c.breakout_buffer_bps else ("short" if down is not None and down >= c.breakout_buffer_bps else None)
        if side is None:
            return self._hold(event, f, "no_breakout")
        ratio, share = f["volume_ratio"], f["buy_share"]
        if ratio is None or share is None or ratio < c.volume_multiple:
            return self._hold(event, f, "breakout_without_volume")
        if (share if side == "long" else 1 - share) < c.min_side_share:
            return self._hold(event, f, "breakout_flow_against")
        return self._entry(event, side, f) or self._hold(event, f, "size_zero")

# ============================================================================
# EDITABLE:SCIENTIFIC_WORKSPACE END
# ============================================================================


# ============================================================================
# PROTECTED:RUNTIME BEGIN
# Public runtime contract. Do not modify signatures or integrity routing.
# ============================================================================

class TradeRuntime:
    def __init__(self,symbol:Optional[str]=None,starting_equity_usd:Optional[float]=None,config:Optional[TradeConfig]=None,market_config:Optional[dict[str,Any]]=None)->None:
        self.market_config=dict(DEFAULT_MARKET_CONFIG); self.market_config.update(market_config or {}); self.symbol=symbol or str(self.market_config.get("symbol") or TRADE_META["symbols"][0]); self.market_config["symbol"]=self.symbol; self.input=_ProtectedMarketInput(self.symbol,float(self.market_config.get("stale_seconds",5.0))); self.broker=_ProtectedPaperBroker(starting_equity_usd or float(PAPER_DEFAULTS["starting_equity_usd"])); self.core=ScientificCore(config); self.signals:Deque[dict[str,Any]]=deque(maxlen=2000); self.last_event:Optional[CleanMarketEvent]=None; self.initialized=False
    def initialize(self,context:Optional[dict[str,Any]]=None)->None: self.initialized=True
    def _handle_events(self,events:list[CleanMarketEvent])->TradeDecision:
        last=TradeDecision(action="HOLD",symbol=self.symbol,reason="awaiting_verified_market_evidence")
        for event in events:
            if not event.integrity.valid: raise L3IntegrityError("protected layer attempted to release unhealthy evidence")
            if (event.best_bid is None or event.best_ask is None or
                not math.isfinite(float(event.best_bid)) or not math.isfinite(float(event.best_ask)) or
                float(event.best_bid)<=0 or float(event.best_bid)>=float(event.best_ask)):
                raise L3IntegrityError("invalid_executable_quote")
            self.last_event=event
            # Delayed checksum verification may release historical evidence.
            # Reconstruct features, but only a current quote may execute.
            if event is not events[-1] or time.time_ns()-event.recv_time_ns > 2_000_000_000:
                if hasattr(self.core,"_apply_event"): self.core._apply_event(event)
                continue
            if hasattr(getattr(self.core,"config",None),"assumed_equity_usd"):
                self.core.config.assumed_equity_usd=max(0.0,self.broker.equity(event))
            decision=self.core.decide(event,list(self.broker.positions.values()))
            if not isinstance(decision,TradeDecision): raise TradeContractError("ScientificCore.decide must return TradeDecision")
            self.signals.append({"timestamp_ms":event.recv_time_ms,"event_kind":event.kind,"sequence":event.sequence,"decision":decision.as_dict()}); self.broker.execute(decision,event)
            if getattr(self,"persist_callback",None): self.persist_callback(self)
            last=decision
        return last
    async def run_live_market_data(self)->None:
        await _BitfinexPublicFeed(self,self.market_config).run()
    def on_l3_snapshot(self,entries:Iterable[Any],sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.snapshot(entries,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_l3_update(self,entry:Any,sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.update(entry,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_l3_checksum(self,server_checksum:int,sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.checksum(server_checksum,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_public_trade_snapshot(self,trades:Iterable[Any],sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.trade_snapshot(trades,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def on_public_trade(self,trade:Any,sequence:Optional[int],recv_time_ns:Optional[int]=None,channel_id:Optional[int]=None,raw_message:Optional[str]=None)->TradeDecision:
        return self._handle_events(self.input.trade(trade,sequence,recv_time_ns or time.time_ns(),channel_id,raw_message))
    def get_status(self)->dict[str,Any]:
        i=self.input.integrity(); return {"name":TRADE_META["name"],"version":TRADE_META["version"],"contract_version":TRADE_CONTRACT_VERSION,"symbol":self.symbol,"paper_only":True,"initialized":self.initialized,"position_count":len(self.broker.positions),"market_source":"bitfinex_public_ws","live_feed_configured":True,"connected":i.connected,"verified":i.verified}
    def get_positions(self): return [p.as_dict() for p in self.broker.positions.values()]
    def get_recent_signals(self,limit:int=100): return list(self.signals)[-int(limit):]
    def get_recent_executions(self,limit:int=100): return [x.as_dict() for x in self.broker.executions[-int(limit):]]
    def get_metrics(self): return self.broker.metrics(self.last_event).as_dict()
    def health(self)->dict[str,Any]:
        i=self.input.integrity(); state="healthy" if i.valid else ("disconnected" if not i.connected else ("stale" if i.stale else ("verifying" if not i.verified else "integrity_error"))); message="ok" if i.valid else (i.error or state); return HealthStatus(i.valid,state,message,i).as_dict()
    def get_ui_schema(self): return {"meta":dict(TRADE_META),"ui":dict(TRADE_UI_SPEC)}
    def get_ui_snapshot(self):
        custom={}; metadata=self.signals[-1]["decision"].get("metadata",{}) if self.signals else {}
        for item in TRADE_UI_SPEC.get("custom_metrics",[]): custom[item.get("key")]=metadata.get(item.get("key"))
        return {"status":self.get_status(),"health":self.health(),"positions":self.get_positions(),"signals":self.get_recent_signals(20),"executions":self.get_recent_executions(20),"metrics":self.get_metrics(),"custom_metrics":custom}

def create_trade_system(**kwargs:Any)->TradeRuntime: return TradeRuntime(**kwargs)

def run_contract_self_test()->dict[str,Any]:
    r=create_trade_system(); r.initialize({"test":True}); r.input.mark_connected(); r.input.mark_subscribed("book"); r.input.mark_subscribed("trades"); now=time.time_ns(); snapshot=[[101,Decimal("100.00"),Decimal("1.0")],[102,Decimal("100.10"),Decimal("-1.0")]]
    before=len(r.core.event_history); r.on_l3_snapshot(snapshot,1,now,11,'[11,[[101,100.00,1.0],[102,100.10,-1.0]],1]'); assert len(r.core.event_history)==before
    cs=r.input.book.checksum(); r.on_l3_checksum(cs,2,now+1_000_000,11,f'[11,"cs",{cs},2]'); assert len(r.core.event_history)>=2; assert r.health()["ok"]
    r.on_public_trade([9001,now//1_000_000,Decimal("0.01"),Decimal("100.05")],3,now+2_000_000,12,'[12,"te",[9001,0,0.01,100.05],3]'); assert r.core.event_history[-1].kind=="public_trade"
    assert _checksum_value(Decimal("0.00000003"))=="3e-8"
    assert set(REQUIRED_RUNTIME_METHODS).issubset(set(dir(r))); assert r.get_metrics()["accounting_valid"] is True
    return {"ok":True,"contract_version":TRADE_CONTRACT_VERSION,"market_source":"bitfinex_public_ws","checksum_gate":True,"public_trades":True,"runtime_methods":list(REQUIRED_RUNTIME_METHODS)}

# ============================================================================
# PROTECTED:RUNTIME END
# ============================================================================


if __name__ == "__main__":
    print(run_contract_self_test())
