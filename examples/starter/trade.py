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

TRADE_CONTRACT_VERSION = "2.2"

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
    "name": "Strategy Starter",            # TODO: your strategy's name
    "version": "0.1.0",
    "description": "Template: ready-made data, risk and exits; fill in the STRATEGY part. As shipped it never trades.",  # TODO
    "symbols": ["tBTCUSD"],
    "paper_only": True,
    "expected_horizon": "minutes_to_hours",
    "supports_multi_symbol": False,
    "supports_multi_position": False,
}

TRADE_UI_SPEC: dict[str, Any] = {
    # Live numbers on the Trade page: every key here should be in the decision metadata (see _metadata).
    "custom_metrics": [
        {"key": "strategy_state", "label": "Strategy State", "format": "text"},
        {"key": "signal", "label": "Signal", "format": "text"},
        {"key": "ret_15m_bps", "label": "15m Return (bps)", "format": "number", "precision": 1},
        {"key": "volume_ratio", "label": "Volume vs Normal", "format": "number", "precision": 2},
        {"key": "stop_price", "label": "Stop Price", "format": "number", "precision": 2},
        {"key": "estimated_net_bps", "label": "Est. Net PnL", "format": "number", "precision": 2},
        # TODO: add your own feature keys
    ],
    # Settings editable on the Trade page (TradeConfig field names).
    "settings": [
        {"key": "base_size", "label": "Max BTC Size", "type": "number", "min": 0.001, "step": 0.001},
        {"key": "risk_per_trade_pct", "label": "Risk / Trade %", "type": "number", "min": 0.05, "max": 1.0, "step": 0.05},
        {"key": "max_spread_bps", "label": "Max Spread (bps)", "type": "number", "min": 0.5, "max": 10.0, "step": 0.1},
        {"key": "stop_bps", "label": "Stop (bps)", "type": "number", "min": 3.0, "max": 100.0, "step": 1.0},
        {"key": "max_hold_seconds", "label": "Max Hold (s)", "type": "number", "min": 60, "max": 14400, "step": 60},
        # TODO: add your own settings
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
    """Live market data from Bitfinex directly (market source "bitfinex", default) or through tt_input
    (source "tt_input"). Both sources go through the same parser and the same protected checks."""
    def __init__(self,runtime:"TradeRuntime",config:dict[str,Any])->None:
        self.runtime=runtime; self.config=dict(DEFAULT_MARKET_CONFIG); self.config.update(config or {}); self.channels:dict[int,str]={}; self.log=logging.getLogger("trade.bitfinex")
    @staticmethod
    def _parse(raw:str): return json.loads(raw,parse_float=Decimal)
    @staticmethod
    def _split_sequence(msg:list)->tuple[list,Optional[int]]:
        # SEQ_ALL appends the sequence number. The TIMESTAMP flag (used by tt_input's exchange connection) appends
        # the exchange time in ms after it; that is dropped here (sequence numbers never reach 10**12).
        body=list(msg)
        if len(body)>=4 and isinstance(body[-1],int) and not isinstance(body[-1],bool) and body[-1]>10**12 and isinstance(body[-2],int): body=body[:-1]
        if len(body)>=3 and isinstance(body[-1],int): return body[:-1],int(body[-1])
        return body,None
    async def run(self)->None:
        try: import websockets
        except ImportError as exc: raise RuntimeError("live market feed requires the 'websockets' package; install config/requirements-market.txt") from exc
        connect=self._run_tt_input if str(self.config.get("source","bitfinex"))=="tt_input" else self._run_connection
        backoff=1
        while True:
            try:
                await connect(websockets); backoff=1
            except asyncio.CancelledError:
                self.runtime.input.reset_connection("feed_cancelled"); raise
            except Exception as exc:
                self.runtime.input.reset_connection(repr(exc)); self.log.exception("market feed failure; reconnecting")
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
                self._handle_raw(raw,time.time_ns())
    async def _run_tt_input(self,websockets)->None:
        # tt_input raw mode: one tt_input-created starting snapshot (labelled, never presented as an exchange message),
        # then every exchange message exactly as the exchange sent it, parsed and verified here as if received directly.
        url=str(self.config.get("tt_input_url","ws://127.0.0.1:8900/stream")); feed=str(self.config.get("tt_input_feed","bitfinex."+str(self.config.get("symbol",self.runtime.symbol))))
        async with websockets.connect(url,ping_interval=20,ping_timeout=20,close_timeout=5,max_queue=8192,max_size=2**24) as ws:
            self.channels.clear(); self.runtime.input.reset_connection()
            await ws.send(json.dumps({"op":"subscribe","feed":feed,"mode":"raw"}))
            while True:
                msg=json.loads(await asyncio.wait_for(ws.recv(),timeout=10)); kind=msg.get("type"); recv_ns=time.time_ns()
                if kind=="raw": self._handle_raw(msg["m"],recv_ns)
                elif kind=="snapshot":
                    # Verified like any snapshot: nothing is released until the exchange's next checksum matches it.
                    self.channels={int(k):str(v) for k,v in msg["channels"].items()}
                    self.runtime.input.reset_connection(); self.runtime.input.mark_connected()
                    for channel in set(self.channels.values()): self.runtime.input.mark_subscribed(channel)
                    book_id=next((k for k,v in self.channels.items() if v=="book"),None)
                    rows=[[int(r[0]),Decimal(str(r[1])),Decimal(str(r[2]))] for r in msg["orders"]]
                    self.runtime.on_l3_snapshot(rows,int(msg["at_seq"]),recv_ns,book_id,None)
                elif kind=="notice":  # tt_input's own exchange connection dropped or reconnected
                    self.channels.clear(); self.runtime.input.reset_connection(f"tt_input_{msg.get('event')}:{msg.get('why')}")
                    if msg.get("event")=="upstream_connected": self.runtime.input.mark_connected()
                elif kind=="error": raise RuntimeError(f"tt_input: {msg.get('error')}")
    def _handle_raw(self,raw,recv_ns:int)->None:
        raw=raw.decode("utf-8") if isinstance(raw,(bytes,bytearray)) else raw; msg=self._parse(raw)
        if isinstance(msg,dict):
            event=msg.get("event")
            if event=="subscribed":
                cid=int(msg["chanId"]); channel=str(msg.get("channel")); self.channels[cid]=channel; self.runtime.input.mark_subscribed(channel); self.runtime._handle_events(self.runtime.input.release_ready())
            elif event=="error": raise RuntimeError(f"Bitfinex error event: {msg}")
            elif event=="info" and msg.get("code") in {20051,20060}: raise RuntimeError(f"Bitfinex websocket restart/maintenance code={msg.get('code')}")
            return
        if not isinstance(msg,list) or len(msg)<2: return
        body,seq=self._split_sequence(msg); cid=int(body[0]); channel=self.channels.get(cid)
        if channel is None: self.runtime.input.sequence_only(seq); return
        if body[1]=="hb":
            self.runtime.input.sequence_only(seq)
            if self.runtime.input._stale(): raise L3IntegrityError("heartbeat_only_stale_book")
            return
        if channel=="book":
            if body[1]=="cs": self.runtime.on_l3_checksum(int(body[2]),seq,recv_ns,cid,raw); return
            payload=body[1]
            if isinstance(payload,list) and payload and isinstance(payload[0],list): self.runtime.on_l3_snapshot(payload,seq,recv_ns,cid,raw)
            elif isinstance(payload,list): self.runtime.on_l3_update(payload,seq,recv_ns,cid,raw)
            else: raise L3IntegrityError(f"malformed_book_message:{body!r}")
            return
        if channel=="trades":
            payload=body[1]
            if isinstance(payload,list) and (not payload or isinstance(payload[0],list)):
                rows=[]
                for row in payload:
                    if not isinstance(row,list) or len(row)<4: raise L3IntegrityError(f"malformed_trade_snapshot_row:{row!r}")
                    rows.append([int(row[0]),int(row[1]),row[2],row[3]])
                self.runtime.on_public_trade_snapshot(rows,seq,recv_ns,cid,raw); return
            if isinstance(payload,str) and payload in {"te","tu"} and len(body)>=3:
                if payload=="tu": self.runtime.input.sequence_only(seq); return
                row=body[2]
                if not isinstance(row,list) or len(row)<4: raise L3IntegrityError(f"malformed_trade_row:{row!r}")
                self.runtime.on_public_trade([int(row[0]),int(row[1]),row[2],row[3]],seq,recv_ns,cid,raw); return
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

# HOW THIS PART WORKS
# Every verified market event (from tt_input, or Bitfinex directly) reaches ScientificCore:
#   - history-only events (replays after a reconnect)  -> _apply_event(event)       : DATA only
#   - current events                                     -> decide(event, positions) : DATA, then RISK/exits or STRATEGY/entries
# A decision is HOLD / ENTER_LONG / ENTER_SHORT / EXIT. The protected paper broker executes it (fees, slippage, account).
# The broker never closes a position by itself: the RISK part below sends EXIT for stops, targets, trails and time.
#
# EVENTS (CleanMarketEvent): kind is one of
#   "l3_snapshot"            payload = [[order_id, price, amount], ...]   full order book (amount > 0 bid, < 0 ask)
#   "l3_update"              payload = [order_id, price, amount]          price == 0 means the order was removed
#   "l3_checksum"            payload = checksum                            (the protected layer verified the book)
#   "public_trade_snapshot"  payload = [[trade_id, time_ms, amount, price], ...]  recent trades after (re)connect
#   "public_trade"           payload = [trade_id, time_ms, amount, price]         amount > 0: buyer took the ask
# Every event also carries the verified top of book: best_bid, best_ask, mid, spread_bps, and recv_time_ms.
#
# THE FOUR PARTS - write only what is marked TODO:
#   SETTINGS  TradeConfig: every tunable number (Think / the Lab tunes these; UI settings list them by name).
#   DATA      ready: 1-minute mid bars, public trades (volume, buy share), the order book by order id. Add features in features().
#   STRATEGY  TODO: entry_signal() returns "long" / "short" / None with a reason; optional exit_signal().
#   RISK      ready: size from the stop, stop / take-profit / trailing stop (never below break-even) / max hold / cooldown.


# ================================ SETTINGS ================================

@dataclass
class TradeConfig:
    # Position and risk. base_size is a hard cap; the size also keeps the loss at the stop within risk_per_trade_pct.
    base_size: float = 0.01
    risk_per_trade_pct: float = 0.25
    assumed_equity_usd: float = 10_000.0            # set by the runtime to the live paper equity
    # Mirrors of the protected paper costs (estimates only; the protected broker is the truth).
    paper_fee_bps_each_side: float = 5.0
    paper_slippage_bps_each_side: float = 1.0

    # Filters before any entry.
    warmup_minutes: int = 30                        # history needed before trading (must cover what features() uses)
    max_spread_bps: float = 3.0
    cooldown_seconds: int = 120                     # after every exit

    # Exits (RISK). 0 disables target / trailing.
    stop_bps: float = 15.0
    target_bps: float = 0.0
    trail_activate_bps: float = 0.0                 # trailing starts when this far in favour (keep > costs + trail_bps)
    trail_bps: float = 10.0
    max_hold_seconds: int = 3600

    # Data windows.
    volume_window_seconds: int = 120
    volume_baseline_minutes: int = 60
    reconnect_keep_history_seconds: int = 120       # keep history across a reconnect shorter than this

    # TODO: your strategy's settings, e.g.
    # channel_minutes: int = 30


class ScientificCore:
    """The strategy. The platform needs: config, event_history, position_state (JSON-safe dict), cooldown_until_ms (int),
    last_features (dict), _apply_event(event), decide(event, positions). core.doctor checks them."""

    def __init__(self, config: Optional[TradeConfig] = None) -> None:
        self.config = config or TradeConfig()
        self.event_history: Deque[CleanMarketEvent] = deque(maxlen=2_000)
        self.bars: Deque[list] = deque(maxlen=600)                          # [minute_ms, open, high, low, close] of the mid
        self.trades: Deque[tuple[int, int, float, float]] = deque(maxlen=200_000)   # (time_ms, trade_id, signed amount, price)
        self.seen_trade_ids: set[int] = set()
        self.book: dict[int, tuple[float, float]] = {}                      # order_id -> (price, signed amount)
        self.first_event_ms: Optional[int] = None
        self.last_event_ms: Optional[int] = None
        self.cooldown_until_ms = 0
        self.position_state: dict[str, dict[str, Any]] = {}
        self.last_features: dict[str, Any] = {}
        # TODO: your own state (keep it JSON-safe if it must survive a restart - put that in position_state)

    # ================================ DATA ================================

    def _apply_event(self, event: CleanMarketEvent) -> None:
        self.event_history.append(event)
        now = int(event.recv_time_ms)
        if self.first_event_ms is None:
            self.first_event_ms = now
        if event.kind == "l3_snapshot":
            short_gap = self.last_event_ms is not None and now - self.last_event_ms <= self.config.reconnect_keep_history_seconds * 1000
            if not short_gap:                        # a long gap: start the history (and the warm-up) again
                self.bars.clear(); self.trades.clear(); self.seen_trade_ids.clear(); self.first_event_ms = now
            self.book = {}
            for row in list(event.payload or []):
                if isinstance(row, (list, tuple)) and len(row) >= 3 and float(row[1]) > 0 and float(row[2]) != 0:
                    self.book[int(row[0])] = (float(row[1]), float(row[2]))
        elif event.kind == "l3_update":
            row = event.payload
            if isinstance(row, (list, tuple)) and len(row) >= 3:
                if float(row[1]) == 0: self.book.pop(int(row[0]), None)
                else: self.book[int(row[0])] = (float(row[1]), float(row[2]))
        elif event.kind == "public_trade_snapshot":
            for row in list(event.payload or []):
                self._record_trade(row, now)
        elif event.kind == "public_trade":
            self._record_trade(event.payload, now)
        if event.mid is not None and math.isfinite(float(event.mid)) and float(event.mid) > 0:
            mid = float(event.mid); minute = now // 60_000 * 60_000
            if self.bars and self.bars[-1][0] == minute:
                b = self.bars[-1]; b[2] = max(b[2], mid); b[3] = min(b[3], mid); b[4] = mid
            else:
                self.bars.append([minute, mid, mid, mid, mid])
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
        self.trades.append((exchange_ms if exchange_ms > 0 else recv_ms, trade_id, amount, price))

    # ---- helpers for features() ----
    def completed_bars(self, now: int, minutes: int) -> list:
        """The last `minutes` completed 1-minute bars, or [] if any minute in that window is missing."""
        minute = now // 60_000 * 60_000
        done = [b for b in self.bars if b[0] < minute][-int(minutes):]
        ok = len(done) == int(minutes) and done and done[-1][0] == minute - 60_000 and done[0][0] == minute - int(minutes) * 60_000
        return done if ok else []

    def return_bps(self, now: int, mid: float, minutes: int) -> Optional[float]:
        bars = self.completed_bars(now, minutes)
        return (mid - bars[0][1]) / bars[0][1] * 1e4 if bars and mid else None

    def volume(self, now: int, seconds: float) -> tuple[float, float]:
        """(total traded BTC, share bought by takers) over the last `seconds`."""
        recent = [a for t, _, a, _ in self.trades if t >= now - seconds * 1000]
        vol = sum(abs(a) for a in recent)
        return vol, (sum(a for a in recent if a > 0) / vol if vol > 0 else 0.5)

    def volume_ratio(self, now: int) -> Optional[float]:
        """Volume of the last window vs the normal volume of an equal window over the baseline."""
        c = self.config; win = c.volume_window_seconds * 1000
        base = sum(abs(a) for t, _, a, _ in self.trades if now - win - c.volume_baseline_minutes * 60_000 <= t < now - win)
        normal = base / (c.volume_baseline_minutes * 60_000 / win)
        return self.volume(now, c.volume_window_seconds)[0] / normal if normal > 0 else None

    def depth(self, mid: float, within_bps: float) -> tuple[float, float]:
        """(bid BTC, ask BTC) resting within `within_bps` of the mid."""
        lo, hi = mid * (1 - within_bps / 1e4), mid * (1 + within_bps / 1e4)
        bid = sum(a for p, a in self.book.values() if a > 0 and p >= lo); ask = sum(-a for p, a in self.book.values() if a < 0 and p <= hi)
        return bid, ask

    def features(self, event: CleanMarketEvent) -> dict[str, Any]:
        """Everything the STRATEGY needs, computed from the data above. TODO: add your features here."""
        c = self.config; now = int(event.recv_time_ms); mid = float(event.mid or 0.0)
        history_ms = now - (self.first_event_ms if self.first_event_ms is not None else now)
        f = {"mid": mid, "spread_bps": event.spread_bps,
             "warm": history_ms >= max(c.warmup_minutes * 60, c.volume_baseline_minutes * 60 + c.volume_window_seconds) * 1000,
             "ret_15m_bps": self.return_bps(now, mid, 15), "volume_ratio": self.volume_ratio(now),
             "buy_share": self.volume(now, c.volume_window_seconds)[1],
             "cost_bps": 2 * (c.paper_fee_bps_each_side + c.paper_slippage_bps_each_side) + float(event.spread_bps or 0.0)}
        # TODO: e.g. bars = self.completed_bars(now, c.channel_minutes); f["channel_high"] = max(b[2] for b in bars) if bars else None
        return f

    # ================================ STRATEGY ================================

    def entry_signal(self, f: dict[str, Any]) -> tuple[Optional[str], str]:
        """TODO: return ("long" | "short", reason) to enter, or (None, reason) to wait. Called only when flat, warm,
        the spread is acceptable and there is no cooldown. The reason shows in the UI and the blocked-trades statistics."""
        return None, "no_signal"

    def exit_signal(self, f: dict[str, Any], side: str, state: dict[str, Any]) -> Optional[str]:
        """Optional: return a reason to close the open position early (the RISK exits run first). None = keep it."""
        return None

    # ================================ RISK ================================

    def stop_distance_bps(self, f: dict[str, Any], side: str) -> float:
        """Initial stop distance for a new position. Override to size it from the market (e.g. a fraction of a range)."""
        return float(self.config.stop_bps)

    def _size(self, entry: float, stop_bps: float, cost_bps: float) -> float:
        if entry <= 0:
            return 0.0
        risk_usd = self.config.assumed_equity_usd * self.config.risk_per_trade_pct / 100.0
        loss_per_btc = entry * (stop_bps + cost_bps) / 10_000.0
        size = min(float(self.config.base_size), risk_usd / loss_per_btc if loss_per_btc > 0 else 0.0)
        return max(0.0, math.floor(size * 1000.0) / 1000.0)

    def _enter(self, event: CleanMarketEvent, side: str, reason: str, f: dict[str, Any]) -> Optional[TradeDecision]:
        c = self.config; slip = c.paper_slippage_bps_each_side / 10_000.0
        entry = float(event.best_ask) * (1 + slip) if side == "long" else float(event.best_bid) * (1 - slip)
        stop_bps = float(self.stop_distance_bps(f, side)); size = self._size(entry, stop_bps, float(f["cost_bps"]))
        if size <= 0 or stop_bps <= 0:
            return None
        sign = 1.0 if side == "long" else -1.0
        stop = entry * (1 - sign * stop_bps / 1e4)
        target = entry * (1 + sign * c.target_bps / 1e4) if c.target_bps > 0 else None
        self.position_state[event.symbol] = {"side": side, "entry_fill": entry, "entry_ms": int(event.recv_time_ms), "stop": stop,
                                             "target": target, "best": entry, "trailing": False, "reason": reason}
        return TradeDecision(action="ENTER_LONG" if side == "long" else "ENTER_SHORT", symbol=event.symbol, requested_size=size,
                             confidence=0.5, reason=reason, stop_price=stop, target_price=target, max_hold_seconds=int(c.max_hold_seconds),
                             metadata=self._metadata(f, f"enter_{side}", reason, 0.0, stop))

    def _exit(self, event: CleanMarketEvent, f: dict[str, Any], reason: str, net_bps: float) -> TradeDecision:
        self.cooldown_until_ms = int(event.recv_time_ms) + int(self.config.cooldown_seconds) * 1000
        self.position_state.pop(event.symbol, None)
        return TradeDecision(action="EXIT", symbol=event.symbol, reason=reason, confidence=1.0, metadata=self._metadata(f, reason, reason, net_bps, None))

    def _manage(self, event: CleanMarketEvent, pos: PositionState, f: dict[str, Any]) -> TradeDecision:
        c = self.config; now = int(event.recv_time_ms); side = pos.side; sign = 1.0 if side == "long" else -1.0
        st = self.position_state.setdefault(event.symbol, {   # after a restart: rebuild from the protected position
            "side": side, "entry_fill": pos.entry_price, "entry_ms": pos.entry_time_ms,
            "stop": pos.stop_price if pos.stop_price is not None else pos.entry_price * (1 - sign * c.stop_bps / 1e4),
            "target": pos.target_price, "best": pos.entry_price, "trailing": False, "reason": "restored"})
        exit_px = float(event.best_bid) if side == "long" else float(event.best_ask)   # the price we could exit at
        net = sign * (exit_px - pos.entry_price) / pos.entry_price * 1e4 - 2 * c.paper_fee_bps_each_side - c.paper_slippage_bps_each_side
        if sign * (exit_px - st["stop"]) <= 0:
            return self._exit(event, f, "trailing_stop" if st["trailing"] else "stop_loss", net)
        if st.get("target") is not None and sign * (exit_px - st["target"]) >= 0:
            return self._exit(event, f, "take_profit", net)
        st["best"] = max(st["best"], exit_px) if side == "long" else min(st["best"], exit_px)
        if c.trail_activate_bps > 0 and sign * (st["best"] - pos.entry_price) / pos.entry_price * 1e4 >= c.trail_activate_bps:
            floor = pos.entry_price * (1 + sign * float(f["cost_bps"]) / 1e4)           # never trail below break-even after costs
            trail = st["best"] * (1 - sign * c.trail_bps / 1e4)
            new = max(trail, floor) if side == "long" else min(trail, floor)
            st["stop"] = max(st["stop"], new) if side == "long" else min(st["stop"], new); st["trailing"] = True
        if now - int(pos.entry_time_ms) >= int(c.max_hold_seconds) * 1000:
            return self._exit(event, f, "maximum_hold", net)
        why = self.exit_signal(f, side, st)
        if why:
            return self._exit(event, f, why, net)
        return TradeDecision(action="HOLD", symbol=event.symbol, reason="manage_open_position", confidence=0.5,
                             metadata=self._metadata(f, "trailing" if st["trailing"] else "in_position", st.get("reason", ""), net, st["stop"]))

    # ================================ OUTPUT ================================

    def _metadata(self, f: dict[str, Any], state: str, signal: str = "", net_bps: float = 0.0, stop: Optional[float] = None) -> dict[str, Any]:
        """Shown on the Trade page (TRADE_UI_SPEC custom_metrics) and stored with each decision."""
        return {"strategy_tag": state, "strategy_state": state, "signal": signal, "ret_15m_bps": f.get("ret_15m_bps"),
                "volume_ratio": f.get("volume_ratio"), "stop_price": stop, "estimated_net_bps": net_bps,
                "estimated_roundtrip_cost_bps": f.get("cost_bps")}   # TODO: add your feature keys

    def _hold(self, event: CleanMarketEvent, f: dict[str, Any], reason: str) -> TradeDecision:
        return TradeDecision(action="HOLD", symbol=event.symbol, reason=reason, metadata=self._metadata(f, reason, reason))

    def decide(self, event: CleanMarketEvent, positions: list[PositionState]) -> TradeDecision:
        self._apply_event(event)
        f = self.features(event); self.last_features = f
        if positions:
            return self._manage(event, positions[0], f)
        self.position_state.pop(event.symbol, None)            # flat: no stale position state
        if not f["warm"]:
            return self._hold(event, f, "warming_up")
        if event.spread_bps is None or not math.isfinite(float(event.spread_bps)) or not 0 <= float(event.spread_bps) <= self.config.max_spread_bps:
            return self._hold(event, f, "spread_filter")
        if int(event.recv_time_ms) < self.cooldown_until_ms:
            return self._hold(event, f, "cooldown")
        side, reason = self.entry_signal(f)
        if side not in ("long", "short"):
            return self._hold(event, f, reason or "no_signal")
        return self._enter(event, side, reason or f"{side}_signal", f) or self._hold(event, f, "size_zero")

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
