from __future__ import annotations

from decimal import Decimal

from app.domain.states import OrderState


class ExecutionReconciler:
    TERMINAL = {
        "filled": OrderState.FILLED,
        "canceled": OrderState.CANCELED,
        "cancelled": OrderState.CANCELED,
        "expired": OrderState.EXPIRED,
        "rejected": OrderState.REJECTED,
    }

    def state_from_exchange(self, payload: object) -> OrderState:
        if isinstance(payload, dict):
            status=str(payload.get("status") or payload.get("state") or "").lower()
            if status == "closed":
                try:
                    requested = Decimal(str(payload.get("vol") or "0"))
                    executed = Decimal(str(payload.get("vol_exec") or "0"))
                except (TypeError, ValueError):
                    requested = executed = Decimal("0")
                if requested <= 0:
                    return OrderState.UNKNOWN_RECONCILING
                if executed <= 0:
                    return OrderState.CANCELED
                if executed < requested:
                    # A closed partial fill is terminal. Its executed quantity is
                    # separately accounted for by consumers that track cumulative fills.
                    return OrderState.CANCELED
                return OrderState.FILLED
            if status in {"filled", "complete", "completed"}:
                return OrderState.FILLED
            if status in {"partialfilled", "partiallyfilled", "partially_filled", "partial"}:
                return OrderState.PARTIALLY_FILLED
            if status in self.TERMINAL:
                return self.TERMINAL[status]
            if status in {"partially_filled","partial"}:
                return OrderState.PARTIALLY_FILLED
            if status in {"open","new","placed","resting"}:
                return OrderState.LIVE
            if status in {"pending","received","acknowledged"}:
                return OrderState.ACKNOWLEDGED
        return OrderState.UNKNOWN_RECONCILING

    def reconcile(self, lookup_results: list[dict]) -> tuple[OrderState,str|None]:
        if not lookup_results:
            return OrderState.UNKNOWN_RECONCILING,None
        result=lookup_results[0]
        return self.state_from_exchange(result), str(
            result.get("order_id") or result.get("txid") or result.get("id") or ""
        ) or None
