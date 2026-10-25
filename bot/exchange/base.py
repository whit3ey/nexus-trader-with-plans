"""Exchange interface shared by Bybit (testnet/live) and the paper-trading simulator."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..risk.position_sizing import InstrumentInfo
from ..strategy.types import Candle

# Bybit retCodes the bot treats specially
CODE_DUPLICATE_LINK_ID = 110072
CODE_INSUFFICIENT_MARGIN = (110007, 110045, 110012)
CODE_RATE_LIMIT = (10006, 10018)
CODE_LEVERAGE_UNCHANGED = 110043


class ExchangeError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    @property
    def is_duplicate(self) -> bool:
        return self.code == CODE_DUPLICATE_LINK_ID

    @property
    def is_margin(self) -> bool:
        return self.code in CODE_INSUFFICIENT_MARGIN


def flat_position(symbol: str) -> Dict[str, Any]:
    return {"symbol": symbol, "side": "", "size": 0.0, "avgPrice": 0.0, "stopLoss": 0.0,
            "takeProfit": 0.0, "unrealisedPnl": 0.0, "leverage": 0.0, "markPrice": 0.0}


class Exchange:
    """All methods return plain dicts/lists. Implementations: BybitExchange, PaperExchange."""
    name = "abstract"
    simulated = False

    def get_instrument(self, symbol: str) -> InstrumentInfo: raise NotImplementedError
    def get_ticker(self, symbol: str) -> Dict[str, Any]: raise NotImplementedError          # price,bid,ask,mark,index,funding_rate,open_interest
    def get_klines(self, symbol: str, interval: str, limit: int) -> List[Candle]: raise NotImplementedError   # oldest -> newest, CLOSED + forming candle
    def get_wallet(self) -> Dict[str, float]: raise NotImplementedError                     # equity, available
    def get_positions(self, symbol: str) -> List[Dict[str, Any]]: raise NotImplementedError   # only non-zero positions
    def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]: raise NotImplementedError
    def get_order(self, symbol: str, order_link_id: str) -> Optional[Dict[str, Any]]: raise NotImplementedError
    def place_order(self, **params: Any) -> Dict[str, Any]: raise NotImplementedError
    def cancel_order(self, symbol: str, order_link_id: str) -> None: raise NotImplementedError
    def cancel_all_orders(self, symbol: str) -> None: raise NotImplementedError
    def set_leverage(self, symbol: str, leverage: int) -> None: raise NotImplementedError
    def set_trading_stop(self, symbol: str, stop_loss: Optional[float] = None,
                         take_profit: Optional[float] = None) -> None: raise NotImplementedError
    def get_executions(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]: raise NotImplementedError
    def get_closed_pnl(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]: raise NotImplementedError
    def check_api_permissions(self) -> Dict[str, Any]: raise NotImplementedError
