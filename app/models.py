"""Pydantic models for request/response validation."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"


class WebhookSignal(BaseModel):
    secret: str
    signal_type: SignalType
    symbol: str
    entry_price_estimate: float
    confidence_score: int = Field(ge=0, le=100)
    suggested_leverage: int = Field(ge=1, le=20)
    suggested_position_size_percent: float = Field(ge=0, le=100)
    bull_score: Optional[int] = None
    bear_score: Optional[int] = None
    rsi: Optional[float] = None
    atr: Optional[float] = None
    timeframe: Optional[str] = None


class ClaudeDecision(str, Enum):
    EXECUTE = "EXECUTE"
    REJECT = "REJECT"
    MODIFY = "MODIFY"


class ClaudeResponse(BaseModel):
    decision: ClaudeDecision
    reasoning: str
    modified_size_percent: Optional[float] = None
    modified_leverage: Optional[int] = None
    modified_order_type: Optional[str] = None  # "market" or "limit"
    limit_price: Optional[float] = None
    risk_score: Optional[int] = None  # 1-10
    geo_risk_note: Optional[str] = None


class TradeRecord(BaseModel):
    id: Optional[int] = None
    timestamp: datetime
    tx_id: Optional[str] = None
    signal_type: str
    symbol: str
    action: str  # EXECUTE, REJECT, MODIFY
    amount_sol: float
    price_usd: float
    fees_sol: float = 0.0
    leverage: int = 1
    wallet_address: str = ""
    confidence_score: int = 0
    claude_reasoning: str = ""
    pnl_usd: Optional[float] = None
    notes: str = ""


class DashboardStats(BaseModel):
    wallet_balance_sol: float = 0.0
    wallet_balance_usdc: float = 0.0
    wallet_balance_usd: float = 0.0   # total: SOL value + USDC
    sol_price_usd: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl_usd: float = 0.0
    total_pnl_percent: float = 0.0
    today_pnl_usd: float = 0.0
    max_drawdown_percent: float = 0.0
    avg_trade_size_sol: float = 0.0
    last_signal_time: Optional[str] = None
    bot_status: str = "running"


class SettingsUpdate(BaseModel):
    max_purchase_sol: Optional[float] = None
    max_purchase_usd: Optional[float] = None
    max_leverage: Optional[int] = None
    max_position_size_percent: Optional[float] = None
    risk_per_trade_percent: Optional[float] = None
    low_balance_shutdown_sol: Optional[float] = None
    low_balance_shutdown_usd: Optional[float] = None
    daily_loss_limit_percent: Optional[float] = None
    max_open_positions: Optional[int] = None
    cooldown_between_trades_seconds: Optional[int] = None
    slippage_bps: Optional[int] = None
    priority_fee_lamports: Optional[int] = None
    geo_risk_weight: Optional[float] = None
