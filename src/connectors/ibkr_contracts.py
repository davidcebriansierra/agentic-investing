"""Construccion de contratos IBKR compartida entre los conectores de lectura y escritura.

Centraliza el mapeo ticker-de-watchlist -> contrato `ib_async.Stock` para que las ordenes
(write) usen exactamente el mismo contrato que los datos de mercado (read). En particular,
los tickers BME (*.MC) se resuelven en EUR con primaryExchange 'BM' (salvo overrides), no
en USD/SMART como un ticker US: enviar la orden con el contrato equivocado provocaba
resoluciones incorrectas o rechazos en IBEX.
"""
from __future__ import annotations

# Sufijos de bolsa reconocidos en los tickers de la watchlist (formato ticker.SUFIJO).
_EXCHANGE_SUFFIXES = {"MC", "L", "PA", "DE", "AS", "MI", "SW", "HK", "TO", "AX"}

# Tickers *.MC cuyo contrato en IBKR difiere del codigo BME usado en config.
# Formato: "BASE" -> (IBKR_SYMBOL, PRIMARY_EXCHANGE). Por defecto el resto de tickers *.MC
# usan (base, "BM"). ArcelorMittal resuelve por su listado primario en Euronext Amsterdam
# (symbol MT, exchange AEB), no por la linea de Madrid. Indra ya es IDR en BME (= watchlist
# IDR.MC), asi que no necesita override.
BME_CONTRACT_OVERRIDES: dict[str, tuple[str, str]] = {
    "MTS": ("MT", "AEB"),   # ArcelorMittal: listado primario en Amsterdam (MT.AS)
}
# Inverso de BME_CONTRACT_OVERRIDES (symbol IBKR -> base watchlist) para reconstruir el
# ticker desde un contrato al leer posiciones (_contract_to_ticker).
BME_SYMBOL_TO_BASE: dict[str, str] = {
    ibkr: base for base, (ibkr, _pex) in BME_CONTRACT_OVERRIDES.items()
}


def make_stock_contract(ticker: str, Stock):
    """Construye el contrato IBKR (`ib_async.Stock`) correcto segun el mercado del ticker.

    - Tickers BME (*.MC): exchange SMART, moneda EUR, primaryExchange='BM' (salvo overrides
      en BME_CONTRACT_OVERRIDES).
    - Tickers multi-clase (BRK.B, BF.B): symbol='BRK B' (con espacio), USD.
    - Resto USD: symbol directo.

    `Stock` (la clase de ib_async) se inyecta para no acoplar este modulo al import opcional.
    """
    parts = ticker.split(".")
    suffix = parts[1].upper() if len(parts) == 2 else ""
    is_bme = suffix == "MC"
    is_exchange_suffix = suffix in _EXCHANGE_SUFFIXES
    if is_bme:
        base = parts[0]
        ibkr_sym, primary = BME_CONTRACT_OVERRIDES.get(base, (base, "BM"))
        return Stock(ibkr_sym, "SMART", "EUR", primaryExchange=primary)
    if len(parts) == 2 and not is_exchange_suffix:
        # Clase de accion (BRK.B, BF.B): en IBKR el symbol es 'BRK B' (con espacio)
        return Stock(f"{parts[0]} {parts[1]}", "SMART", "USD")
    return Stock(ticker if is_exchange_suffix else parts[0], "SMART", "USD")
