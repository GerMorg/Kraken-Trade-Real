from __future__ import annotations
from decimal import Decimal
from typing import Any
from app.domain.models import Decision,OrderIntent,new_id
from app.domain.states import Direction,OrderState,ProductType
D=Decimal
class OrderIntentBuilder:
    def __init__(self,max_slippage_bps:float,timeout_seconds:int)->None:
        self.max_slippage=D(str(max_slippage_bps)); self.timeout_seconds=timeout_seconds
    def build(self,decision:Decision,leverage:D,order_type:str,quantity:D,limit_price:D|None,
              reduce_only:bool=False,post_only:bool=False)->OrderIntent:
        return OrderIntent(new_id("intent"),new_id("client"),decision.decision_id,decision.instrument,
            decision.signal.direction,"buy" if decision.signal.direction==Direction.LONG else "sell",
            order_type,quantity,limit_price,leverage,decision.instrument.product_type!=ProductType.SPOT,
            reduce_only,decision.signal.net_edge_bps,self.max_slippage,self.timeout_seconds,post_only)
