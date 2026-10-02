from __future__ import annotations
from decimal import Decimal
from datetime import datetime,timezone
from typing import Any
from app.domain.models import OrderIntent
from app.domain.states import OrderState
D=Decimal
class TradingAuthority:
    """The only real-order authority. No other subsystem receives a write-capable gateway."""
    def __init__(self,config:Any,gateway:Any,db:Any,audit:Any,policy:Any,reconciler:Any)->None:
        self.config=config;self.gateway=gateway;self.db=db;self.audit=audit;self.policy=policy;self.reconciler=reconciler
    def submit(self,intent:OrderIntent,market:Any)->dict[str,Any]:
        self.db.save_order_intent(intent)
        checks=self._preflight(intent,market)
        if not checks["allowed"]:
            self.db.update_order_state(intent.client_order_id,OrderState.REJECTED.value,last_error=checks["reason"])
            self.audit.emit("ORDER_BLOCKED","WARNING",intent_id=intent.intent_id,reason=checks["reason"])
            return {"state":OrderState.REJECTED.value,"reason":checks["reason"]}
        if not (self.config.live_enabled and not self.config.kill_switch):
            self.db.update_order_state(intent.client_order_id,OrderState.REJECTED.value,last_error="LIVE_TRADING_DISABLED")
            self.audit.emit("ORDER_BLOCKED","INFO",intent_id=intent.intent_id,reason="LIVE_TRADING_DISABLED")
            return {"state":OrderState.REJECTED.value,"reason":"LIVE_TRADING_DISABLED"}
        self.db.update_order_state(intent.client_order_id,OrderState.SUBMITTING.value)
        try:
            if intent.instrument.product_type.value=="DERIVATIVE":
                response=self.gateway.submit_futures_order(
                    instrument_id=intent.instrument.instrument_id,side=intent.side,order_type=intent.order_type,
                    quantity=intent.quantity,price=intent.limit_price,client_order_id=intent.client_order_id,
                    reduce_only=intent.reduce_only,post_only=intent.post_only)
                status=response.get("sendStatus",{}) if isinstance(response,dict) else {}
                order_id=status.get("order_id") or status.get("orderId")
            else:
                response=self.gateway.submit_spot_order(
                    instrument_id=intent.instrument.instrument_id,side=intent.side,order_type=intent.order_type,
                    quantity=intent.quantity,price=intent.limit_price,client_order_id=intent.client_order_id,
                    leverage=intent.leverage,margin=intent.margin,reduce_only=intent.reduce_only,post_only=intent.post_only)
                ids=response.get("txid",[]) if isinstance(response,dict) else []
                order_id=ids[0] if ids else response.get("order_id") if isinstance(response,dict) else None
            self.db.update_order_state(intent.client_order_id,OrderState.ACKNOWLEDGED.value,kraken_order_id=order_id)
            self.audit.emit("ORDER_ACKNOWLEDGED","INFO",intent_id=intent.intent_id)
            return {"state":OrderState.ACKNOWLEDGED.value,"kraken_order_id":order_id,"response":response}
        except Exception as exc:
            self.db.update_order_state(intent.client_order_id,OrderState.UNKNOWN_RECONCILING.value,last_error=type(exc).__name__)
            self.audit.emit("ORDER_RECONCILING","ERROR",intent_id=intent.intent_id,error=type(exc).__name__)
            try:
                found=self.gateway.lookup_order(client_order_id=intent.client_order_id,instrument=intent.instrument)
                state,order_id=self.reconciler.reconcile(found)
                self.db.update_order_state(intent.client_order_id,state.value,kraken_order_id=order_id)
                return {"state":state.value,"kraken_order_id":order_id,"reconciled":True}
            except Exception as rex:
                self.db.event("ORDER_RECONCILIATION_FAILED","ERROR",{"client_order_id":intent.client_order_id,"error":type(rex).__name__})
                return {"state":OrderState.UNKNOWN_RECONCILING.value,"reconciled":False}
    def _preflight(self,intent:OrderIntent,market:Any)->dict[str,Any]:
        if not self.config.kraken_enabled:return {"allowed":False,"reason":"KRAKEN_DISABLED"}
        if not intent.instrument.tradeable:return {"allowed":False,"reason":"INSTRUMENT_NOT_TRADEABLE"}
        if intent.direction.value=="SHORT" and not intent.instrument.short_available:return {"allowed":False,"reason":"SHORT_NOT_AVAILABLE"}
        if intent.quantity<intent.instrument.min_order_qty:return {"allowed":False,"reason":"MIN_ORDER_QTY"}
        if intent.limit_price and intent.quantity*intent.limit_price<intent.instrument.min_cost:return {"allowed":False,"reason":"MIN_ORDER_COST"}
        if intent.leverage>intent.instrument.max_leverage:return {"allowed":False,"reason":"LEVERAGE_INSTRUMENT_LIMIT"}
        open_orders=self.db.query("SELECT client_order_id FROM orders WHERE symbol=? AND state IN ('SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED')",(intent.instrument.symbol,))
        if open_orders:return {"allowed":False,"reason":"DUPLICATE_OPEN_ORDER"}
        count=self.db.one("SELECT COUNT(*) AS n FROM orders WHERE created_at>=strftime('%s','now','start of day')")
        if count and int(count["n"])>=self.config.execution_max_orders_per_day:return {"allowed":False,"reason":"DAILY_ORDER_LIMIT"}
        recent=self.db.query("""SELECT created_at FROM orders WHERE symbol=? AND direction=? ORDER BY created_at DESC LIMIT 1""",
                             (intent.instrument.symbol,intent.direction.value))
        if recent and time_since(recent[0]["created_at"])<60:return {"allowed":False,"reason":"ORDER_COOLDOWN"}
        chosen=self.policy.choose(market.spread_bps,intent.expected_edge_bps,self._volatility(market))
        estimated=self._volatility(market)*D(2)
        ok,reason=self.policy.validate(chosen,intent.expected_edge_bps,estimated)
        return {"allowed":ok,"reason":reason if not ok else "PRECHECK_OK","method":chosen["method"]}
    @staticmethod
    def _volatility(market:Any)->D:
        c=getattr(market,"closes",())
        if len(c)<2:return D(999)
        x=[abs(c[i]/c[i-1]-1)*100 for i in range(1,len(c)) if c[i-1]]
        return sum(x,D(0))/D(max(1,len(x)))

def time_since(ts:Any)->float:
    try:return max(0.0,datetime.now(timezone.utc).timestamp()-float(ts))
    except (TypeError,ValueError):return 999999.0
