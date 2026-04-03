"""Pydantic models for request/response validation."""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


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


# ─── Kalshi Config Validation ────────────────────────────────────────────────


class ArbitrageConfig(BaseModel):
    enabled: bool = False
    scan_interval_seconds: int = Field(default=120, ge=10)
    min_spread_cents: int = Field(default=3, ge=1)
    min_profit_cents: int = Field(default=5, ge=1)
    fee_per_contract_cents: int = Field(default=2, ge=0)
    auto_execute: bool = False
    max_auto_cost_cents: int = Field(default=500, ge=1)
    telegram_alerts: bool = True


class SpreadBotConfig(BaseModel):
    enabled: bool = False
    poll_interval_seconds: int = Field(default=15, ge=5)
    default_spread_cents: int = Field(default=4, ge=1)
    min_spread_cents: int = Field(default=2, ge=1)
    contracts_per_side: int = Field(default=5, ge=1)
    max_inventory_per_market: int = Field(default=20, ge=1)
    max_total_exposure_cents: int = Field(default=2000, ge=100)
    inventory_skew_cents: int = Field(default=2, ge=0)
    stale_order_threshold_cents: int = Field(default=3, ge=1)
    flatten_minutes_before_close: int = Field(default=30, ge=0)
    fee_per_contract_cents: int = Field(default=2, ge=0)
    telegram_alerts: bool = True
    target_tickers: list = []


class WhaleTrackerConfig(BaseModel):
    enabled: bool = False
    scan_interval_seconds: int = Field(default=60, ge=10)
    min_contract_count: int = Field(default=50, ge=1)
    min_cost_cents: int = Field(default=2500, ge=100)
    max_markets_to_scan: int = Field(default=30, ge=1)
    telegram_alerts: bool = True
    history_limit: int = Field(default=100, ge=10)


class TechnicalBotConfig(BaseModel):
    enabled: bool = False
    scan_interval_seconds: int = Field(default=300, ge=30)
    candle_interval_minutes: int = Field(default=60, ge=1)
    candle_count: int = Field(default=100, ge=30)
    macd_fast: int = Field(default=12, ge=2)
    macd_slow: int = Field(default=26, ge=5)
    macd_signal: int = Field(default=9, ge=2)
    cci_period: int = Field(default=20, ge=5)
    cci_overbought: int = Field(default=100, ge=50)
    cci_oversold: int = Field(default=-100, le=-50)
    auto_trade: bool = False
    contracts_per_trade: int = Field(default=5, ge=1)
    max_positions: int = Field(default=5, ge=1)
    max_cost_per_trade_cents: int = Field(default=500, ge=1)
    telegram_alerts: bool = True
    target_tickers: list = []
    max_markets_to_scan: int = Field(default=20, ge=1)


class SportsScannerConfig(BaseModel):
    enabled: bool = False
    scan_interval_seconds: int = Field(default=120, ge=30)
    leagues: list = ["MLB", "NBA", "NFL", "NHL", "Soccer", "UFC", "WNBA"]
    min_volume: int = Field(default=10, ge=0)
    odds_move_alert_cents: int = Field(default=10, ge=1)
    value_threshold_cents: int = Field(default=5, ge=1)
    max_markets_per_league: int = Field(default=20, ge=1)
    auto_trade: bool = False
    contracts_per_trade: int = Field(default=5, ge=1)
    max_cost_per_trade_cents: int = Field(default=500, ge=1)
    max_positions: int = Field(default=5, ge=1)
    telegram_alerts: bool = True


class MarketMakerConfig(BaseModel):
    enabled: bool = False
    poll_interval_seconds: int = Field(default=10, ge=5)
    default_strategy: str = "midpoint"
    fallback_strategy: str = "volatility"
    base_spread_cents: int = Field(default=4, ge=1)
    min_spread_cents: int = Field(default=2, ge=1)
    max_spread_cents: int = Field(default=12, ge=2)
    dynamic_spread: bool = True
    contracts_per_level: int = Field(default=5, ge=1)
    quote_levels: int = Field(default=1, ge=1)
    max_inventory_per_market: int = Field(default=25, ge=1)
    max_total_inventory: int = Field(default=100, ge=1)
    max_total_exposure_cents: int = Field(default=5000, ge=100)
    inventory_skew_cents: int = Field(default=2, ge=0)
    adverse_selection_trades: int = Field(default=3, ge=1)
    stale_quote_seconds: int = Field(default=60, ge=10)
    stale_price_threshold_cents: int = Field(default=3, ge=1)
    flatten_minutes_before_close: int = Field(default=30, ge=0)
    fee_per_contract_cents: int = Field(default=2, ge=0)
    target_tickers: list = []
    max_markets: int = Field(default=5, ge=1)
    min_market_volume: int = Field(default=100, ge=0)
    rotation_interval_cycles: int = Field(default=50, ge=5)
    telegram_alerts: bool = True


class EsportsScannerConfig(BaseModel):
    enabled: bool = False
    scan_interval_seconds: int = Field(default=120, ge=30)
    games: list = ["CS2", "DotA2", "LoL", "Valorant", "Overwatch"]
    min_volume: int = Field(default=5, ge=0)
    odds_move_alert_cents: int = Field(default=10, ge=1)
    value_threshold_cents: int = Field(default=5, ge=1)
    max_markets_per_game: int = Field(default=20, ge=1)
    auto_trade: bool = False
    contracts_per_trade: int = Field(default=5, ge=1)
    max_cost_per_trade_cents: int = Field(default=500, ge=1)
    max_positions: int = Field(default=5, ge=1)
    telegram_alerts: bool = True


class AIAgentConfig(BaseModel):
    enabled: bool = False
    scan_interval_seconds: int = Field(default=600, ge=60)
    agents: list = ["analyst", "contrarian", "momentum"]
    auto_trade: bool = False
    min_consensus_strength: float = Field(default=0.7, ge=0.0, le=1.0)
    require_unanimous: bool = False
    contracts_per_trade: int = Field(default=5, ge=1)
    max_positions: int = Field(default=3, ge=1)
    max_cost_per_trade_cents: int = Field(default=500, ge=1)
    telegram_alerts: bool = True
    target_tickers: list = []
    max_markets_to_analyze: int = Field(default=5, ge=1)


class WebSocketConfig(BaseModel):
    enabled: bool = True
    max_reconnect_delay: int = Field(default=60, ge=5)


class RiskManagerConfig(BaseModel):
    enabled: bool = True
    max_daily_loss_cents: int = Field(default=1000, ge=100)
    check_interval_seconds: int = Field(default=30, ge=10)
    telegram_alerts: bool = True


class KalshiConfig(BaseModel):
    """Top-level Kalshi configuration — validates entire kalshi: section."""

    enabled: bool = False
    mode: str = "demo"
    api_key_id: str = ""
    private_key_path: str = "keys/kalshi_private.pem"
    max_cost_per_trade_cents: int = Field(default=500, ge=1)
    max_total_exposure_cents: int = Field(default=5000, ge=100)
    max_open_positions: int = Field(default=10, ge=1)
    default_contract_count: int = Field(default=10, ge=1)
    rate_limit_per_second: float = Field(default=10.0, ge=1.0)
    rate_limit_burst: int = Field(default=15, ge=1)
    categories: list = ["economics", "crypto", "politics", "finance"]

    arbitrage: ArbitrageConfig = ArbitrageConfig()
    spread_bot: SpreadBotConfig = SpreadBotConfig()
    whale_tracker: WhaleTrackerConfig = WhaleTrackerConfig()
    technical_bot: TechnicalBotConfig = TechnicalBotConfig()
    sports_scanner: SportsScannerConfig = SportsScannerConfig()
    market_maker: MarketMakerConfig = MarketMakerConfig()
    esports_scanner: EsportsScannerConfig = EsportsScannerConfig()
    ai_agent: AIAgentConfig = AIAgentConfig()
    websocket: WebSocketConfig = WebSocketConfig()
    risk_manager: RiskManagerConfig = RiskManagerConfig()

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("demo", "live"):
            raise ValueError(f"kalshi.mode must be 'demo' or 'live', got '{v}'")
        return v


def validate_kalshi_config(raw: dict) -> KalshiConfig:
    """Parse and validate the raw kalshi config dict."""
    return KalshiConfig(**raw)
