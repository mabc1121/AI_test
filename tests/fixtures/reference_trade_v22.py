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
    "name": "L3 Structural Flow Hybrid",
    "version": "1.1.0",
    "description": "BTCUSD long/short paper strategy using verified Bitfinex R0 lifecycle and public-trade flow with structural continuation and failed-breakout logic. v1.1: entry thresholds sized to observed volatility, history kept across short reconnects, target exits.",
    "symbols": ["tBTCUSD"],
    "paper_only": True,
    "expected_horizon": "seconds_to_2_hours",
    "supports_multi_symbol": False,
    "supports_multi_position": False,
}

TRADE_UI_SPEC: dict[str, Any] = {
    "custom_metrics": [
        {"key": "strategy_state", "label": "Strategy State", "format": "text"},
        {"key": "regime", "label": "Regime", "format": "text"},
        {"key": "signal_score", "label": "Signal Score", "format": "number", "precision": 2},
        {"key": "obi_near", "label": "Near OBI", "format": "number", "precision": 3},
        {"key": "persistent_obi", "label": "Persistent OBI", "format": "number", "precision": 3},
        {"key": "microprice_bias", "label": "Microprice Bias", "format": "number", "precision": 3},
        {"key": "book_pressure", "label": "Book Pressure", "format": "number", "precision": 3},
        {"key": "tfi_fast", "label": "Fast Trade Flow", "format": "number", "precision": 3},
        {"key": "tfi_prior", "label": "Prior Trade Flow", "format": "number", "precision": 3},
        {"key": "estimated_net_bps", "label": "Est. Net PnL", "format": "number", "precision": 2},
        {"key": "runner", "label": "Runner", "format": "boolean"},
    ],
    "settings": [
        {"key": "base_size", "label": "Max BTC Size", "type": "number", "min": 0.001, "step": 0.001},
        {"key": "risk_per_trade_pct", "label": "Risk / Trade %", "type": "number", "min": 0.05, "max": 1.0, "step": 0.05},
        {"key": "max_spread_bps", "label": "Max Spread (bps)", "type": "number", "min": 0.5, "max": 10.0, "step": 0.1},
        {"key": "min_expected_move_bps", "label": "Min Expected Move (bps)", "type": "number", "min": 10.0, "max": 100.0, "step": 1.0},
        {"key": "profit_trigger_net_bps", "label": "Profit-Enough Net (bps)", "type": "number", "min": 5.0, "max": 100.0, "step": 1.0},
        {"key": "runner_giveback_bps", "label": "Runner Giveback (bps)", "type": "number", "min": 2.0, "max": 50.0, "step": 1.0},
        {"key": "max_stop_bps", "label": "Max Structural Stop (bps)", "type": "number", "min": 4.0, "max": 30.0, "step": 1.0},
        {"key": "continuation_max_hold_seconds", "label": "Continuation Max Hold (s)", "type": "number", "min": 300, "max": 7200, "step": 60},
        {"key": "reversal_max_hold_seconds", "label": "Reversal Max Hold (s)", "type": "number", "min": 120, "max": 3600, "step": 60},
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

@dataclass
class TradeConfig:
    # Position/risk defaults. base_size is a hard cap; sizing also respects the
    # approximate loss implied by the structural stop and protected paper costs.
    base_size: float = 0.01
    risk_per_trade_pct: float = 0.25
    assumed_equity_usd: float = 10_000.0

    # Execution-quality / opportunity filters.
    max_spread_bps: float = 2.0
    # Sized from 3 days of 1m BTCUSD data: 38 bps / 2.5x cost produced zero
    # setups (10m range median ~18 bps); 22 bps / 1.7x gives ~25 price setups/day
    # before flow confirmation, while still clearing the ~13 bps round-trip cost.
    min_expected_move_bps: float = 22.0
    cost_multiple: float = 1.7
    paper_fee_bps_each_side: float = 5.0
    paper_slippage_bps_each_side: float = 1.0

    # Book feature geometry.
    near_book_bps: float = 8.0
    far_book_bps: float = 20.0
    persistent_order_age_seconds: float = 2.5
    pressure_window_seconds: float = 2.0
    refill_window_seconds: float = 4.0

    # Entry thresholds. Kept deliberately permissive enough to support the
    # user's preference for frequent trades, while still requiring confluence.
    near_obi_threshold: float = 0.12
    persistent_obi_threshold: float = 0.08
    microprice_bias_threshold: float = 0.15
    book_pressure_threshold: float = 0.10
    fast_tfi_threshold: float = 0.10
    prior_tfi_threshold: float = 0.15
    min_confirmations: int = 3

    # Structure and regime.
    warmup_seconds: int = 900
    trend_5m_bps: float = 8.0
    trend_15m_bps: float = 12.0
    continuation_pullback_min_bps: float = 8.0
    continuation_pullback_max_bps: float = 70.0
    breakout_min_bps: float = 2.0
    breakout_max_seconds: int = 20
    breakout_reference_exclusion_seconds: int = 30
    breakout_reference_seconds: int = 1800
    continuation_high_seconds: int = 600
    # Keep price/trade history across a reconnect shorter than this, so a brief
    # feed drop does not restart the full warm-up.
    reconnect_keep_history_seconds: int = 120

    # Stops / exits.
    min_stop_bps: float = 5.0
    max_stop_bps: float = 12.0
    stop_buffer_bps: float = 1.5
    profit_trigger_net_bps: float = 22.0
    runner_giveback_bps: float = 10.0
    reversal_progress_check_seconds: int = 180
    continuation_progress_check_seconds: int = 300
    minimum_progress_r: float = 0.20
    reversal_max_hold_seconds: int = 1200
    continuation_max_hold_seconds: int = 3600
    runner_max_hold_seconds: int = 7200
    normal_cooldown_seconds: int = 30
    loss_cooldown_seconds: int = 90


class ScientificCore:
    """Event-driven L3 + public-trade research strategy.

    The protected layer proves market-data integrity. This editable layer keeps
    its own scientific state and intentionally treats order removals as book
    lifecycle evidence, not guaranteed executions; actual aggressor flow comes
    from the protected public-trades channel.
    """

    def __init__(self, config: Optional[TradeConfig] = None) -> None:
        self.config = config or TradeConfig()
        self.event_history: Deque[CleanMarketEvent] = deque(maxlen=2_000)

        # Independent scientific reconstruction/state derived only from clean
        # evidence. order_id -> (price, signed_amount, first_seen_ms, last_seen_ms)
        self.book: dict[int, tuple[float, float, int, int]] = {}
        # One observation per second retains the full 15 minute horizon even
        # when the R0 feed produces many messages in a single second.
        self.price_history: Deque[tuple[int, float]] = deque(maxlen=7_200)
        self.trades: Deque[tuple[int, int, float, float]] = deque(maxlen=40_000)
        self.pressure_events: Deque[tuple[int, float, float]] = deque(maxlen=40_000)
        self.refill_events: Deque[tuple[int, float, float]] = deque(maxlen=20_000)
        self.recent_removals: Deque[tuple[int, str, float, float]] = deque(maxlen=10_000)
        self.seen_trade_ids: set[int] = set()

        self.first_event_ms: Optional[int] = None
        self.last_event_ms: Optional[int] = None
        self.cooldown_until_ms = 0
        self.pending_breakout: Optional[dict[str, Any]] = None
        self.position_state: dict[str, dict[str, Any]] = {}
        self.last_signal = 0.0
        self.last_features: dict[str, Any] = {}

    # ------------------------------ evidence ------------------------------

    def _apply_event(self, event: CleanMarketEvent) -> None:
        self.event_history.append(event)
        now = int(event.recv_time_ms)
        if self.first_event_ms is None:
            self.first_event_ms = now

        if event.kind == "l3_snapshot":
            # A new connection is a new evidence generation. Order-lifecycle
            # buffers always reset; price/trade history survives a short gap.
            # Account/position state lives outside these buffers and is retained.
            short_gap = self.last_event_ms is not None and now - self.last_event_ms <= self.config.reconnect_keep_history_seconds * 1000
            self.pressure_events.clear(); self.refill_events.clear(); self.recent_removals.clear()
            self.pending_breakout = None
            if not short_gap:
                self.price_history.clear(); self.trades.clear(); self.seen_trade_ids.clear()
                self.first_event_ms = now
            rebuilt: dict[int, tuple[float, float, int, int]] = {}
            for row in list(event.payload or []):
                if not isinstance(row, (list, tuple)) or len(row) < 3:
                    continue
                oid = int(row[0]); price = float(row[1]); amount = float(row[2])
                if price > 0 and amount != 0:
                    rebuilt[oid] = (price, amount, now, now)
            self.book = rebuilt

        elif event.kind == "l3_update":
            row = event.payload
            if isinstance(row, (list, tuple)) and len(row) >= 3:
                oid = int(row[0]); price = float(row[1]); amount = float(row[2])
                old = self.book.get(oid)
                if price == 0:
                    self.book.pop(oid, None)
                elif amount != 0:
                    if old is not None and abs(old[0] - price) < 1e-12 and (old[1] > 0) == (amount > 0):
                        first_seen = old[2]
                    else:
                        first_seen = now
                    self.book[oid] = (price, amount, first_seen, now)

            for life in list(event.protected_context.get("lifecycle", []) or []):
                self._record_lifecycle(now, life)

        elif event.kind == "public_trade_snapshot":
            for row in list(event.payload or []):
                self._record_trade(row, now)

        elif event.kind == "public_trade":
            self._record_trade(event.payload, now)

        if event.mid is not None and math.isfinite(float(event.mid)):
            mid = float(event.mid)
            # Keep event-time shape without storing duplicate identical timestamp/mid rows.
            bucket = now // 1000
            if not self.price_history or self.price_history[-1][0] // 1000 != bucket:
                self.price_history.append((bucket * 1000, mid))
            else:
                self.price_history[-1] = (bucket * 1000, mid)

        self.last_event_ms = now
        self._prune(now)

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
        # Bitfinex public trade amount is signed; preserve that evidence directly.
        ts = exchange_ms if exchange_ms > 0 else recv_ms
        self.trades.append((ts, trade_id, amount, price))
        if len(self.seen_trade_ids) > 80_000:
            # Bound in-process state. Duplicate protection over the recent stream is enough.
            self.seen_trade_ids = {x[1] for x in list(self.trades)[-30_000:]}

    def _record_lifecycle(self, now: int, life: Any) -> None:
        if not isinstance(life, dict):
            return
        kind = str(life.get("kind") or "")
        side = str(life.get("side") or "")
        try:
            qty = abs(float(life.get("qty") or 0.0)); price = float(life.get("price") or 0.0)
        except (TypeError, ValueError):
            return
        if qty <= 0 or side not in {"bid", "ask"}:
            return

        if kind in {"add", "resize_up"}:
            signed = qty if side == "bid" else -qty
        elif kind in {"remove", "resize_down"}:
            signed = -qty if side == "bid" else qty
        else:
            return
        self.pressure_events.append((now, signed, qty))

        cutoff = now - int(self.config.refill_window_seconds * 1000)
        while self.recent_removals and self.recent_removals[0][0] < cutoff:
            self.recent_removals.popleft()
        if kind in {"remove", "resize_down"}:
            self.recent_removals.append((now, side, price, qty))
        elif kind in {"add", "resize_up"}:
            matched = 0.0
            for index in range(len(self.recent_removals) - 1, -1, -1):
                ts, rside, rprice, rqty = self.recent_removals[index]
                if rside == side and abs(rprice - price) < 1e-9:
                    matched = min(qty, rqty)
                    if matched >= rqty: del self.recent_removals[index]
                    else: self.recent_removals[index] = (ts, rside, rprice, rqty - matched)
                    break
            if matched > 0:
                self.refill_events.append((now, matched if side == "bid" else -matched, matched))

    def _prune(self, now: int) -> None:
        two_hours = now - 7_200_000
        while self.price_history and self.price_history[0][0] < two_hours:
            self.price_history.popleft()
        while self.trades and self.trades[0][0] < two_hours:
            old = self.trades.popleft()
            self.seen_trade_ids.discard(old[1])
        for dq in (self.pressure_events, self.refill_events):
            while dq and dq[0][0] < now - 120_000:
                dq.popleft()
        while self.recent_removals and self.recent_removals[0][0] < now - 10_000:
            self.recent_removals.popleft()

    # ------------------------------ features ------------------------------

    @staticmethod
    def _ratio(signed: float, total: float) -> float:
        return signed / total if total > 1e-12 else 0.0

    def _price_before(self, target_ms: int) -> Optional[float]:
        for ts, price in reversed(self.price_history):
            if ts <= target_ms:
                return price
        return None

    def _return_bps(self, now: int, seconds: float, current: float) -> Optional[float]:
        old = self._price_before(now - int(seconds * 1000))
        if old is None or old <= 0:
            return None
        return (current - old) / old * 10_000.0

    def _range_prices(self, now: int, lookback_seconds: float, exclude_recent_seconds: float = 0.0) -> tuple[Optional[float], Optional[float]]:
        start = now - int(lookback_seconds * 1000)
        end = now - int(exclude_recent_seconds * 1000)
        vals = []
        for ts, p in reversed(self.price_history):  # time-ordered; stop at the window start
            if ts < start:
                break
            if ts <= end:
                vals.append(p)
        return (max(vals), min(vals)) if vals else (None, None)

    def _trade_flow(self, now: int, start_age_s: float, end_age_s: float = 0.0) -> tuple[float, int, float]:
        newer = now - int(end_age_s * 1000)
        older = now - int(start_age_s * 1000)
        signed = 0.0; total = 0.0; count = 0
        for ts, _, amount, _ in reversed(self.trades):
            # Trades arrive nearly time-ordered; stop once well past the window
            # (10 s slack tolerates out-of-order exchange timestamps).
            if ts < older - 10_000:
                break
            if ts < older:
                continue
            if ts > newer:
                continue
            signed += amount
            total += abs(amount)
            count += 1
        return self._ratio(signed, total), count, total

    def _event_pressure(self, now: int, window_s: float, source: Deque[tuple[int, float, float]]) -> float:
        cutoff = now - int(window_s * 1000)
        signed = 0.0; total = 0.0
        for ts, value, magnitude in reversed(source):
            if ts < cutoff:
                break
            signed += value; total += magnitude
        return self._ratio(signed, total)

    def _book_features(self, now: int, event: CleanMarketEvent) -> dict[str, float]:
        mid = float(event.mid or 0.0)
        bid = float(event.best_bid or 0.0)
        ask = float(event.best_ask or 0.0)
        if mid <= 0 or bid <= 0 or ask <= 0:
            return {"obi_near": 0.0, "obi_far": 0.0, "persistent_obi": 0.0, "microprice_bias": 0.0}

        near_bid = near_ask = far_bid = far_ask = persistent_bid = persistent_ask = 0.0
        persist_ms = int(self.config.persistent_order_age_seconds * 1000)
        best_bid_qty = best_ask_qty = 0.0
        for price, amount, first_seen, _ in self.book.values():
            qty = abs(amount)
            distance = abs(price - mid) / mid * 10_000.0
            is_bid = amount > 0
            if distance <= self.config.far_book_bps:
                if is_bid: far_bid += qty
                else: far_ask += qty
            if distance <= self.config.near_book_bps:
                if is_bid: near_bid += qty
                else: near_ask += qty
                if now - first_seen >= persist_ms:
                    if is_bid: persistent_bid += qty
                    else: persistent_ask += qty
            if is_bid and abs(price - bid) < 1e-9:
                best_bid_qty += qty
            if (not is_bid) and abs(price - ask) < 1e-9:
                best_ask_qty += qty

        obi_near = self._ratio(near_bid - near_ask, near_bid + near_ask)
        obi_far = self._ratio(far_bid - far_ask, far_bid + far_ask)
        persistent_obi = self._ratio(persistent_bid - persistent_ask, persistent_bid + persistent_ask)
        micro_bias = 0.0
        if ask > bid and best_bid_qty + best_ask_qty > 1e-12:
            microprice = (ask * best_bid_qty + bid * best_ask_qty) / (best_bid_qty + best_ask_qty)
            micro_bias = max(-1.0, min(1.0, (microprice - mid) / ((ask - bid) / 2.0)))
        return {"obi_near": obi_near, "obi_far": obi_far, "persistent_obi": persistent_obi, "microprice_bias": micro_bias}

    def _features(self, event: CleanMarketEvent) -> dict[str, Any]:
        now = int(event.recv_time_ms)
        mid = float(event.mid or 0.0)
        book = self._book_features(now, event)
        tfi_fast, fast_count, fast_qty = self._trade_flow(now, 2.0, 0.0)
        tfi_prior, prior_count, prior_qty = self._trade_flow(now, 8.0, 2.0)
        tfi_30, _, _ = self._trade_flow(now, 30.0, 0.0)
        pressure = self._event_pressure(now, self.config.pressure_window_seconds, self.pressure_events)
        refill = self._event_pressure(now, self.config.refill_window_seconds, self.refill_events)
        high_2m, low_2m = self._range_prices(now, 120.0)
        high_10m, low_10m = self._range_prices(now, float(self.config.continuation_high_seconds))
        ref_high, ref_low = self._range_prices(now, self.config.breakout_reference_seconds, self.config.breakout_reference_exclusion_seconds)

        ret_2s = self._return_bps(now, 2.0, mid) if mid > 0 else None
        ret_8s = self._return_bps(now, 8.0, mid) if mid > 0 else None
        ret_1m = self._return_bps(now, 60.0, mid) if mid > 0 else None
        ret_5m = self._return_bps(now, 300.0, mid) if mid > 0 else None
        ret_15m = self._return_bps(now, 900.0, mid) if mid > 0 else None

        long_checks = [
            book["obi_near"] >= self.config.near_obi_threshold,
            book["persistent_obi"] >= self.config.persistent_obi_threshold,
            book["microprice_bias"] >= self.config.microprice_bias_threshold,
            pressure >= self.config.book_pressure_threshold,
            tfi_fast >= self.config.fast_tfi_threshold,
        ]
        short_checks = [
            book["obi_near"] <= -self.config.near_obi_threshold,
            book["persistent_obi"] <= -self.config.persistent_obi_threshold,
            book["microprice_bias"] <= -self.config.microprice_bias_threshold,
            pressure <= -self.config.book_pressure_threshold,
            tfi_fast <= -self.config.fast_tfi_threshold,
        ]
        long_score = sum(1 for x in long_checks if x)
        short_score = sum(1 for x in short_checks if x)

        if ret_5m is not None and ret_15m is not None and ret_5m >= self.config.trend_5m_bps and ret_15m >= self.config.trend_15m_bps:
            regime = "trend_up"
        elif ret_5m is not None and ret_15m is not None and ret_5m <= -self.config.trend_5m_bps and ret_15m <= -self.config.trend_15m_bps:
            regime = "trend_down"
        else:
            regime = "balanced_or_mixed"

        cost_bps = 2.0 * (self.config.paper_fee_bps_each_side + self.config.paper_slippage_bps_each_side) + float(event.spread_bps or 0.0)
        min_move = max(self.config.min_expected_move_bps, self.config.cost_multiple * cost_bps)
        directional = (long_score - short_score) / 5.0
        self.last_signal = max(-1.0, min(1.0, directional))

        return {
            **book,
            "book_pressure": pressure,
            "refill_bias": refill,
            "tfi_fast": tfi_fast,
            "tfi_prior": tfi_prior,
            "tfi_30": tfi_30,
            "fast_trade_count": fast_count,
            "prior_trade_count": prior_count,
            "fast_trade_qty": fast_qty,
            "prior_trade_qty": prior_qty,
            "ret_2s_bps": ret_2s,
            "ret_8s_bps": ret_8s,
            "ret_1m_bps": ret_1m,
            "ret_5m_bps": ret_5m,
            "ret_15m_bps": ret_15m,
            "high_2m": high_2m,
            "low_2m": low_2m,
            "high_10m": high_10m,
            "low_10m": low_10m,
            "reference_high": ref_high,
            "reference_low": ref_low,
            "long_score": long_score,
            "short_score": short_score,
            "regime": regime,
            "cost_bps": cost_bps,
            "min_move_bps": min_move,
            "signal_score": self.last_signal,
        }

    # ------------------------------- entries ------------------------------

    def _warm(self, now: int) -> bool:
        target = now - self.config.warmup_seconds * 1000
        return (self.first_event_ms is not None and self.first_event_ms <= target
                and self._price_before(target) is not None)

    def _size_for_stop(self, entry: float, stop_distance_bps: float, cost_bps: float) -> float:
        if entry <= 0:
            return 0.0
        risk_budget = self.config.assumed_equity_usd * self.config.risk_per_trade_pct / 100.0
        estimated_loss_per_btc = entry * (stop_distance_bps + cost_bps) / 10_000.0
        risk_size = risk_budget / estimated_loss_per_btc if estimated_loss_per_btc > 0 else 0.0
        size = min(float(self.config.base_size), risk_size)
        return max(0.0, math.floor(size * 1000.0) / 1000.0)

    def _entry_fill(self, side: str, event: CleanMarketEvent) -> Optional[float]:
        slip = self.config.paper_slippage_bps_each_side / 10_000.0
        if side == "long" and event.best_ask is not None:
            return float(event.best_ask) * (1.0 + slip)
        if side == "short" and event.best_bid is not None:
            return float(event.best_bid) * (1.0 - slip)
        return None

    def _make_entry(self, event: CleanMarketEvent, side: str, strategy: str, stop_distance_bps: float, expected_move_bps: float, f: dict[str, Any]) -> Optional[TradeDecision]:
        entry = self._entry_fill(side, event)
        if entry is None or stop_distance_bps < self.config.min_stop_bps or stop_distance_bps > self.config.max_stop_bps:
            return None
        size = self._size_for_stop(entry, stop_distance_bps, float(f["cost_bps"]))
        if size <= 0:
            return None
        if side == "long":
            stop = entry * (1.0 - stop_distance_bps / 10_000.0)
            target = entry * (1.0 + expected_move_bps / 10_000.0)
            action = "ENTER_LONG"
        else:
            stop = entry * (1.0 + stop_distance_bps / 10_000.0)
            target = entry * (1.0 - expected_move_bps / 10_000.0)
            action = "ENTER_SHORT"

        max_hold = self.config.continuation_max_hold_seconds if strategy == "continuation" else self.config.reversal_max_hold_seconds
        self.position_state[event.symbol] = {
            "side": side,
            "strategy": strategy,
            "entry_fill": entry,
            "entry_ms": int(event.recv_time_ms),
            "entry_spread_bps": float(event.spread_bps or 0.0),
            "stop_distance_bps": stop_distance_bps,
            "peak_net_bps": -1e9,
            "runner": False,
        }
        confidence = min(0.95, 0.50 + 0.07 * (f["long_score"] if side == "long" else f["short_score"]))
        return TradeDecision(
            action=action,
            symbol=event.symbol,
            requested_size=size,
            confidence=confidence,
            reason=f"{strategy}_{side}_confirmed",
            stop_price=stop,
            target_price=target,
            max_hold_seconds=max_hold,
            metadata=self._metadata(f, f"enter_{strategy}_{side}", 0.0, False),
        )

    def _update_breakout(self, event: CleanMarketEvent, f: dict[str, Any]) -> None:
        mid = float(event.mid or 0.0); now = int(event.recv_time_ms)
        if mid <= 0:
            return
        if self.pending_breakout is not None:
            if now - int(self.pending_breakout["start_ms"]) > self.config.breakout_max_seconds * 1000:
                self.pending_breakout = None
            else:
                if self.pending_breakout["side"] == "up":
                    self.pending_breakout["extreme"] = max(float(self.pending_breakout["extreme"]), mid)
                else:
                    self.pending_breakout["extreme"] = min(float(self.pending_breakout["extreme"]), mid)
                return

        ref_high = f.get("reference_high"); ref_low = f.get("reference_low")
        trend15 = f.get("ret_15m_bps")
        # Avoid fading an exceptionally directional market.
        if trend15 is not None and abs(float(trend15)) > 45.0:
            return
        if ref_high and mid > float(ref_high) * (1.0 + self.config.breakout_min_bps / 10_000.0):
            self.pending_breakout = {"side": "up", "level": float(ref_high), "extreme": mid, "start_ms": now}
        elif ref_low and mid < float(ref_low) * (1.0 - self.config.breakout_min_bps / 10_000.0):
            self.pending_breakout = {"side": "down", "level": float(ref_low), "extreme": mid, "start_ms": now}

    def _reversal_entry(self, event: CleanMarketEvent, f: dict[str, Any]) -> Optional[TradeDecision]:
        p = self.pending_breakout
        if p is None or event.mid is None:
            return None
        mid = float(event.mid); level = float(p["level"]); extreme = float(p["extreme"])
        ref_hi = f.get("reference_high"); ref_lo = f.get("reference_low")
        if p["side"] == "up":
            reclaimed = mid < level * (1.0 - 0.5 / 10_000.0)
            confirmed = f["short_score"] >= self.config.min_confirmations and f["tfi_fast"] <= -self.config.fast_tfi_threshold
            room = ((level - float(ref_lo)) / level * 10_000.0) / 2.0 if ref_lo else 0.0
            stop_dist = (extreme - mid) / mid * 10_000.0 + self.config.stop_buffer_bps
            if reclaimed and confirmed and room >= f["min_move_bps"]:
                self.pending_breakout = None
                return self._make_entry(event, "short", "reversal", max(self.config.min_stop_bps, stop_dist), room, f)
        else:
            reclaimed = mid > level * (1.0 + 0.5 / 10_000.0)
            confirmed = f["long_score"] >= self.config.min_confirmations and f["tfi_fast"] >= self.config.fast_tfi_threshold
            room = ((float(ref_hi) - level) / level * 10_000.0) / 2.0 if ref_hi else 0.0
            stop_dist = (mid - extreme) / mid * 10_000.0 + self.config.stop_buffer_bps
            if reclaimed and confirmed and room >= f["min_move_bps"]:
                self.pending_breakout = None
                return self._make_entry(event, "long", "reversal", max(self.config.min_stop_bps, stop_dist), room, f)
        return None

    def _continuation_entry(self, event: CleanMarketEvent, f: dict[str, Any]) -> Optional[TradeDecision]:
        if event.mid is None:
            return None
        mid = float(event.mid); ret2 = f.get("ret_2s_bps") or 0.0; ret8 = f.get("ret_8s_bps") or 0.0
        # Trend is judged on the 15m return only: requiring the 5m return to stay
        # positive contradicted requiring a meaningful pullback. The pullback is
        # measured from the 10m extreme and its size is the expected move back.
        ret15 = f.get("ret_15m_bps")
        up = ret15 is not None and ret15 >= self.config.trend_15m_bps
        down = ret15 is not None and ret15 <= -self.config.trend_15m_bps

        if up and f.get("high_10m"):
            pullback = (float(f["high_10m"]) - mid) / float(f["high_10m"]) * 10_000.0
            absorption_flip = (
                f["prior_trade_count"] >= 2
                and f["tfi_prior"] <= -self.config.prior_tfi_threshold
                and f["tfi_fast"] >= self.config.fast_tfi_threshold
                and ret8 >= -5.0
                and ret2 > 0.25
            )
            if (
                self.config.continuation_pullback_min_bps <= pullback <= self.config.continuation_pullback_max_bps
                and pullback >= f["min_move_bps"]
                and absorption_flip
                and f["long_score"] >= self.config.min_confirmations
            ):
                recent_low = f.get("low_2m") or mid
                stop_dist = (mid - float(recent_low)) / mid * 10_000.0 + self.config.stop_buffer_bps
                return self._make_entry(event, "long", "continuation", max(self.config.min_stop_bps, stop_dist), pullback, f)

        if down and f.get("low_10m"):
            pullback = (mid - float(f["low_10m"])) / float(f["low_10m"]) * 10_000.0
            absorption_flip = (
                f["prior_trade_count"] >= 2
                and f["tfi_prior"] >= self.config.prior_tfi_threshold
                and f["tfi_fast"] <= -self.config.fast_tfi_threshold
                and ret8 <= 5.0
                and ret2 < -0.25
            )
            if (
                self.config.continuation_pullback_min_bps <= pullback <= self.config.continuation_pullback_max_bps
                and pullback >= f["min_move_bps"]
                and absorption_flip
                and f["short_score"] >= self.config.min_confirmations
            ):
                recent_high = f.get("high_2m") or mid
                stop_dist = (float(recent_high) - mid) / mid * 10_000.0 + self.config.stop_buffer_bps
                return self._make_entry(event, "short", "continuation", max(self.config.min_stop_bps, stop_dist), pullback, f)
        return None

    # ------------------------------- exits --------------------------------

    def _estimated_net_bps(self, event: CleanMarketEvent, pos: PositionState, state: dict[str, Any]) -> float:
        entry = float(state.get("entry_fill") or pos.entry_price)
        slip = self.config.paper_slippage_bps_each_side / 10_000.0
        if pos.side == "long":
            if event.best_bid is None: return -1e9
            exit_fill = float(event.best_bid) * (1.0 - slip)
            gross = (exit_fill - entry) / entry * 10_000.0
        else:
            if event.best_ask is None: return -1e9
            exit_fill = float(event.best_ask) * (1.0 + slip)
            gross = (entry - exit_fill) / entry * 10_000.0
        return gross - 2.0 * self.config.paper_fee_bps_each_side

    def _runner_strong(self, side: str, f: dict[str, Any]) -> bool:
        if side == "long":
            return f["long_score"] >= 4 and f["tfi_fast"] >= 0.05 and f["book_pressure"] >= 0.0 and f["regime"] == "trend_up"
        return f["short_score"] >= 4 and f["tfi_fast"] <= -0.05 and f["book_pressure"] <= 0.0 and f["regime"] == "trend_down"

    def _exit(self, event: CleanMarketEvent, f: dict[str, Any], reason: str, net_bps: float, runner: bool) -> TradeDecision:
        cooldown = self.config.normal_cooldown_seconds if net_bps > 0 else self.config.loss_cooldown_seconds
        self.cooldown_until_ms = int(event.recv_time_ms) + cooldown * 1000
        self.position_state.pop(event.symbol, None)
        self.pending_breakout = None
        return TradeDecision(
            action="EXIT",
            symbol=event.symbol,
            reason=reason,
            confidence=1.0,
            metadata=self._metadata(f, reason, net_bps, runner),
        )

    def _manage_position(self, event: CleanMarketEvent, pos: PositionState, f: dict[str, Any]) -> TradeDecision:
        now = int(event.recv_time_ms)
        state = self.position_state.setdefault(event.symbol, {
            "side": pos.side,
            "strategy": pos.strategy_tag or "unknown",
            "entry_fill": pos.entry_price,
            "entry_ms": pos.entry_time_ms,
            "entry_spread_bps": float(event.spread_bps or 0.0),
            "stop_distance_bps": self.config.max_stop_bps,
            "peak_net_bps": -1e9,
            "runner": False,
        })
        net = self._estimated_net_bps(event, pos, state)
        state["peak_net_bps"] = max(float(state.get("peak_net_bps", -1e9)), net)
        age_s = max(0.0, (now - int(pos.entry_time_ms)) / 1000.0)
        runner = bool(state.get("runner", False))
        side = pos.side

        # Structural stop gets first priority.
        if side == "long" and pos.stop_price is not None and event.best_bid is not None and float(event.best_bid) <= float(pos.stop_price):
            return self._exit(event, f, "structural_stop", net, runner)
        if side == "short" and pos.stop_price is not None and event.best_ask is not None and float(event.best_ask) >= float(pos.stop_price):
            return self._exit(event, f, "structural_stop", net, runner)

        # Strong opposite evidence invalidates the premise before the hard stop.
        if age_s >= 5.0:
            opposite = f["short_score"] >= 4 if side == "long" else f["long_score"] >= 4
            flow_failed = (f["tfi_fast"] <= -0.25 and f["book_pressure"] <= -0.15) if side == "long" else (f["tfi_fast"] >= 0.25 and f["book_pressure"] >= 0.15)
            if opposite and flow_failed:
                return self._exit(event, f, "flow_thesis_failed", net, runner)

        # Target reached (the structural expected move): bank it unless continuation
        # evidence is strong enough to run.
        target_hit = pos.target_price is not None and (
            (side == "long" and event.best_bid is not None and float(event.best_bid) >= float(pos.target_price)) or
            (side == "short" and event.best_ask is not None and float(event.best_ask) <= float(pos.target_price)))
        if target_hit and not runner:
            if self._runner_strong(side, f) and state.get("strategy") == "continuation":
                state["runner"] = True; runner = True
            else:
                return self._exit(event, f, "target_reached", net, runner)

        # Once profit is sufficient, bank it unless continuation evidence is unusually strong.
        if net >= self.config.profit_trigger_net_bps:
            if self._runner_strong(side, f) and state.get("strategy") == "continuation":
                state["runner"] = True; runner = True
            else:
                return self._exit(event, f, "profit_enough", net, runner)

        if runner:
            giveback = float(state["peak_net_bps"]) - net
            if giveback >= self.config.runner_giveback_bps:
                return self._exit(event, f, "runner_giveback", net, True)
            if net > 0 and not self._runner_strong(side, f):
                return self._exit(event, f, "runner_flow_faded", net, True)

        strategy = str(state.get("strategy") or "unknown")
        progress_check = self.config.continuation_progress_check_seconds if strategy == "continuation" else self.config.reversal_progress_check_seconds
        r_bps = float(state.get("stop_distance_bps", self.config.max_stop_bps)) + float(f["cost_bps"])
        required_progress = self.config.minimum_progress_r * r_bps
        supportive_score = f["long_score"] if side == "long" else f["short_score"]
        if age_s >= progress_check and float(state["peak_net_bps"]) < required_progress and supportive_score < self.config.min_confirmations:
            return self._exit(event, f, "progress_time_stop", net, runner)

        max_hold = self.config.continuation_max_hold_seconds if strategy == "continuation" else self.config.reversal_max_hold_seconds
        if runner:
            max_hold = self.config.runner_max_hold_seconds
        if age_s >= max_hold:
            return self._exit(event, f, "maximum_hold", net, runner)

        return TradeDecision(
            action="HOLD",
            symbol=event.symbol,
            reason="manage_open_position",
            confidence=0.5,
            metadata=self._metadata(f, "manage_position", net, runner),
        )

    # ------------------------------- output -------------------------------

    def _metadata(self, f: dict[str, Any], strategy_state: str, estimated_net_bps: float = 0.0, runner: bool = False) -> dict[str, Any]:
        return {
            "strategy_tag": strategy_state,
            "strategy_state": strategy_state,
            "regime": f.get("regime"),
            "signal_score": f.get("signal_score", 0.0),
            "obi_near": f.get("obi_near", 0.0),
            "persistent_obi": f.get("persistent_obi", 0.0),
            "microprice_bias": f.get("microprice_bias", 0.0),
            "book_pressure": f.get("book_pressure", 0.0),
            "refill_bias": f.get("refill_bias", 0.0),
            "tfi_fast": f.get("tfi_fast", 0.0),
            "tfi_prior": f.get("tfi_prior", 0.0),
            "estimated_net_bps": estimated_net_bps,
            "runner": runner,
            "estimated_roundtrip_cost_bps": f.get("cost_bps"),
            "required_expected_move_bps": f.get("min_move_bps"),
        }

    def decide(self, event: CleanMarketEvent, positions: list[PositionState]) -> TradeDecision:
        self._apply_event(event)
        f = self._features(event)
        self.last_features = f
        now = int(event.recv_time_ms)
        if self.pending_breakout is not None and now - int(self.pending_breakout["start_ms"]) > self.config.breakout_max_seconds * 1000:
            self.pending_breakout = None

        if positions:
            return self._manage_position(event, positions[0], f)

        if not self._warm(now):
            return TradeDecision(action="HOLD", symbol=event.symbol, reason="warming_up", metadata=self._metadata(f, "warming_up"))
        if event.spread_bps is None or not math.isfinite(float(event.spread_bps)) or not 0 <= float(event.spread_bps) <= self.config.max_spread_bps:
            return TradeDecision(action="HOLD", symbol=event.symbol, reason="spread_filter", metadata=self._metadata(f, "spread_filter"))
        if now < self.cooldown_until_ms:
            return TradeDecision(action="HOLD", symbol=event.symbol, reason="cooldown", metadata=self._metadata(f, "cooldown"))

        # Failed breakout has priority when an active sweep/reclaim state exists.
        reversal = self._reversal_entry(event, f)
        if reversal is not None:
            return reversal
        self._update_breakout(event, f)
        reversal = self._reversal_entry(event, f)
        if reversal is not None:
            return reversal

        continuation = self._continuation_entry(event, f)
        if continuation is not None:
            return continuation

        state = "watch_breakout" if self.pending_breakout is not None else "scanning"
        return TradeDecision(
            action="HOLD",
            symbol=event.symbol,
            requested_size=0.0,
            confidence=0.0,
            reason="no_confirmed_setup",
            metadata=self._metadata(f, state),
        )

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
