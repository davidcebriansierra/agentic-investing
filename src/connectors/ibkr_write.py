"""Conector de escritura a IBKR (spec v2.0, secciones 3.7 y 12.2).

Define la interfaz `BrokerClient` que usa el agente Ejecutor y dos implementaciones:

- `MockBrokerClient`: simula fills deterministicos para tests / paper local / shadow mode.
- `IBKRBrokerClient`: implementacion real con `ib_async` directo a IB Gateway. Solo se
  activa si el extra `connectors` esta instalado (import perezoso); en otro caso lanza
  un error descriptivo al instanciarse.

La validacion pre-trade NO vive aqui: el Ejecutor la aplica antes de llamar al broker.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from src.schemas.enums import ExecutionStatus, OrderAction
from src.schemas.order import ExecutionResult, Order


class BrokerClient(Protocol):
    """Contrato minimo que debe cumplir cualquier conector de ejecucion."""

    async def connect(self) -> None: ...

    async def place_bracket_order(self, order: Order) -> ExecutionResult: ...


class MockBrokerClient:
    """Broker simulado: rellena la orden al precio de entrada con comision fija."""

    def __init__(self, commission_per_order: float = 1.20, next_order_id: int = 1000) -> None:
        self.commission = commission_per_order
        self._next_id = next_order_id
        self.placed_orders: list[Order] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def place_bracket_order(self, order: Order) -> ExecutionResult:
        self.placed_orders.append(order)
        order_id = self._next_id
        self._next_id += 1
        return ExecutionResult(
            order_id_internal=order.order_id_internal,
            ibkr_order_id=order_id,
            status=ExecutionStatus.FILLED,
            fill_price=order.entry.price,
            fill_quantity=order.quantity,
            commission=self.commission,
            raw_ibkr_response="mock_fill",
        )


class IBKRBrokerClient:
    """Conector real con ib_async (IB Gateway: 4002 paper / 4001 live)."""

    def __init__(self, host: str, port: int, client_id: int, account: str) -> None:
        try:  # pragma: no cover - depende del extra opcional
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "ib_async no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc
        self._IB = IB
        self.ib = IB()
        self.host = host
        self.port = port
        self.client_id = client_id
        self.account = account

    async def connect(self) -> None:  # pragma: no cover - requiere IB Gateway
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)

    async def place_bracket_order(self, order: Order) -> ExecutionResult:  # pragma: no cover
        from ib_async import LimitOrder, StopOrder, Stock

        _parts = order.ticker.split(".")
        _EXCHANGE_SUFFIXES = {"MC", "L", "PA", "DE", "AS", "MI", "SW", "HK", "TO", "AX"}
        if len(_parts) == 2 and _parts[1].upper() not in _EXCHANGE_SUFFIXES:
            # Clase de accion (BRK.B, BF.B): en IBKR el symbol es 'BRK B' (con espacio)
            contract = Stock(f"{_parts[0]} {_parts[1]}", "SMART", "USD")
        else:
            contract = Stock(_parts[0], "SMART", "USD")
        close_action = "SELL" if order.action == OrderAction.BUY else "BUY"

        # tif="GTC" + outsideRth=True evitan la cancelacion (Error 10349) cuando
        # se envia fuera del horario regular de mercado (RTH).
        parent = LimitOrder(
            order.action.value, order.quantity, order.entry.price,
            transmit=False, account=self.account, tif="GTC", outsideRth=True,
        )
        tp = LimitOrder(
            close_action, order.quantity, order.take_profit.price,
            parentId=parent.orderId, transmit=False, tif="GTC", outsideRth=True,
        )
        sl = StopOrder(
            close_action, order.quantity, order.stop_loss.price,
            parentId=parent.orderId, transmit=True, tif="GTC", outsideRth=True,
        )
        await self.ib.qualifyContractsAsync(contract)
        trades = [self.ib.placeOrder(contract, o) for o in (parent, tp, sl)]
        await asyncio.sleep(1)
        parent_trade = trades[0]
        fills = parent_trade.fills
        fill_price = fills[0].execution.price if fills else order.entry.price
        commission = sum(f.commissionReport.commission for f in fills if f.commissionReport)
        return ExecutionResult(
            order_id_internal=order.order_id_internal,
            ibkr_order_id=parent_trade.order.orderId,
            status=self._map_status(parent_trade.orderStatus.status, fills),
            fill_price=fill_price,
            fill_quantity=sum(f.execution.shares for f in fills) if fills else 0,
            commission=commission or None,
            raw_ibkr_response=str(parent_trade.orderStatus),
        )

    @staticmethod
    def _map_status(ib_status: str, fills: list) -> ExecutionStatus:  # pragma: no cover
        """Traduce el estado de IBKR al ExecutionStatus del sistema."""
        filled_qty = sum(f.execution.shares for f in fills) if fills else 0
        if ib_status == "Filled":
            return ExecutionStatus.FILLED
        if ib_status in ("Cancelled", "ApiCancelled", "Inactive"):
            return ExecutionStatus.CANCELLED
        if ib_status in ("Submitted", "PreSubmitted", "PendingSubmit"):
            # Ejecutada en parte, o aceptada y en reposo (p. ej. fuera de RTH)
            return ExecutionStatus.PARTIAL if filled_qty else ExecutionStatus.SUBMITTED
        return ExecutionStatus.REJECTED
