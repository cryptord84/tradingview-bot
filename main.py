"""TradingView SOL Trading Bot - Main FastAPI Application."""

import asyncio
import logging
import logging.handlers
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import load_config, get
from app.database import init_db
from app.routers import webhook, dashboard
from app.services.telegram_commands import TelegramCommandHandler
from app.services.kamino_client import KaminoClient
from app.services.ngrok_monitor import get_ngrok_monitor
from app.services.wallet_service import WalletService
from app.utils.csv_backup import run_daily_backup
from app.services.scout_service import scout_scheduler
from app.services.position_monitor import get_position_monitor
from app.services.price_feed import get_price_feed

# --- Logging Setup ---
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

log_cfg = {}
try:
    log_cfg = get("logging") or {}
except Exception:
    pass


class ColoredFormatter(logging.Formatter):
    """Adds ANSI colors to terminal log output by level."""

    RESET = "\033[0m"
    COLORS = {
        logging.DEBUG: "\033[36m",     # cyan
        logging.INFO: "",              # default (no color)
        logging.WARNING: "\033[33m",   # yellow
        logging.ERROR: "\033[31m",     # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        msg = super().format(record)
        if color:
            return f"{color}{msg}{self.RESET}"
        return msg


_log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(ColoredFormatter(_log_fmt))

_file = logging.handlers.RotatingFileHandler(
    log_cfg.get("file", "logs/bot.log"),
    maxBytes=(log_cfg.get("max_size_mb", 50)) * 1024 * 1024,
    backupCount=log_cfg.get("backup_count", 5),
)
_file.setFormatter(logging.Formatter(_log_fmt))

logging.basicConfig(
    level=getattr(logging, (log_cfg.get("level") or get("server", "log_level", "INFO")).upper(), logging.INFO),
    handlers=[_console, _file],
)
logger = logging.getLogger("bot")


_tg_handler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global _tg_handler
    logger.info("Starting TradingView SOL Trading Bot")
    load_config()
    init_db()
    run_daily_backup()

    # Start Telegram command listener
    _tg_handler = TelegramCommandHandler()
    tg_task = asyncio.create_task(_tg_handler.run())

    # Start ngrok URL monitor
    ngrok = get_ngrok_monitor()
    ngrok_task = ngrok.start()

    # Start daily scout scheduler (3:30 AM X.com scan)
    scout_task = asyncio.create_task(scout_scheduler())

    # Start real-time price feed (Binance WS + CoinGecko polling)
    price_feed = get_price_feed()
    if price_feed.enabled:
        price_feed.start()
        logger.info("Real-time price feed started")

    # Start position monitor (TP/SL auto-close)
    pos_monitor = get_position_monitor()
    if pos_monitor.enabled:
        pos_monitor_task = pos_monitor.start()

    # Start Kalshi WebSocket feed (real-time orderbook + trades)
    from app.services.kalshi_ws_feed import get_kalshi_ws_feed
    kalshi_ws = get_kalshi_ws_feed()
    if kalshi_ws.enabled:
        kalshi_ws.start()
        logger.info("Kalshi WebSocket feed started")

    # Start Kalshi arbitrage scanner
    from app.services.kalshi_arbitrage import get_arbitrage_scanner
    arb_scanner = get_arbitrage_scanner()
    if arb_scanner.enabled:
        arb_scanner.start()
        logger.info("Kalshi arbitrage scanner started")

    # Start Kalshi spread bot
    from app.services.kalshi_spread_bot import get_spread_bot
    spread_bot = get_spread_bot()
    if spread_bot.enabled:
        spread_bot.start()
        logger.info("Kalshi spread bot started")

    # Start Kalshi whale tracker
    from app.services.kalshi_whale_tracker import get_whale_tracker
    whale_tracker = get_whale_tracker()
    if whale_tracker.enabled:
        whale_tracker.start()
        logger.info("Kalshi whale tracker started")

    # Start Kalshi technical bot
    from app.services.kalshi_technical_bot import get_technical_bot
    tech_bot = get_technical_bot()
    if tech_bot.enabled:
        tech_bot.start()
        logger.info("Kalshi technical bot started")

    # Start Kalshi market maker
    from app.services.kalshi_market_maker import get_market_maker
    market_maker = get_market_maker()
    if market_maker.enabled:
        market_maker.start()
        logger.info("Kalshi market maker started")

    # Start Kalshi sports scanner
    from app.services.kalshi_sports_scanner import get_sports_scanner
    sports_scanner = get_sports_scanner()
    if sports_scanner.enabled:
        sports_scanner.start()
        logger.info("Kalshi sports scanner started")

    # Start Kalshi esports scanner
    from app.services.kalshi_esports_scanner import get_esports_scanner
    esports_scanner = get_esports_scanner()
    if esports_scanner.enabled:
        esports_scanner.start()
        logger.info("Kalshi esports scanner started")

    # Start Kalshi AI agent bot
    from app.services.kalshi_ai_agent import get_ai_agent_bot
    ai_bot = get_ai_agent_bot()
    if ai_bot.enabled:
        ai_bot.start()
        logger.info("Kalshi AI agent bot started")

    # Start Kalshi risk manager (global circuit breaker)
    from app.services.kalshi_risk_manager import get_risk_manager
    risk_manager = get_risk_manager()
    if risk_manager.enabled:
        risk_manager.start()
        logger.info(
            f"Kalshi risk manager started: max daily loss "
            f"${risk_manager.max_daily_loss_cents/100:.2f}"
        )

    # Start portfolio rebalancer (auto-rebalance mode)
    from app.services.portfolio_rebalancer import get_rebalancer
    rebalancer = get_rebalancer()
    if rebalancer.enabled and rebalancer.auto_rebalance:
        rebalancer.start()
        logger.info("Portfolio rebalancer auto-loop started")

    # Auto-deposit idle USDC into Kamino on startup
    kamino = KaminoClient()
    if kamino.enabled and kamino.auto_deposit:
        try:
            wallet = WalletService()
            usdc_balance = await wallet.get_usdc_balance()
            result = await kamino.deposit_idle(wallet.get_keypair(), usdc_balance)
            if result.get("success"):
                logger.info(f"Startup: deposited {result['amount_usdc']:.2f} USDC into Kamino")
            elif result.get("skipped"):
                logger.info(f"Startup: Kamino deposit skipped — {result.get('reason')}")
            await wallet.close()
        except Exception as e:
            logger.warning(f"Startup Kamino deposit failed: {e}")

    # Prime Kamino balance cache so first trade decision has full purchasing power
    if kamino.enabled:
        try:
            wallet = WalletService()
            pos = await kamino.get_user_position(wallet.public_key)
            logger.info(f"Startup: Kamino balance cached — ${pos.get('deposited_usdc', 0):.2f} USDC")
            await wallet.close()
        except Exception as e:
            logger.warning(f"Startup Kamino cache prime failed: {e}")
    await kamino.close()

    logger.info("Bot initialized successfully")

    yield

    logger.info("Shutting down bot")
    if price_feed.enabled:
        await price_feed.stop()
    if _tg_handler:
        await _tg_handler.stop()
    tg_task.cancel()
    await ngrok.stop()
    if pos_monitor.enabled:
        await pos_monitor.stop()
    if kalshi_ws.enabled:
        await kalshi_ws.stop()
    if arb_scanner.enabled:
        arb_scanner.stop()
    if spread_bot.enabled:
        await spread_bot.stop()
    if whale_tracker.enabled:
        whale_tracker.stop()
    if tech_bot.enabled:
        tech_bot.stop()
    if sports_scanner.enabled:
        sports_scanner.stop()
    if market_maker.enabled:
        await market_maker.stop()
    if esports_scanner.enabled:
        esports_scanner.stop()
    if ai_bot.enabled:
        ai_bot.stop()
    if risk_manager.enabled:
        await risk_manager.stop()
    if rebalancer.enabled and rebalancer.auto_rebalance:
        await rebalancer.stop()


app = FastAPI(
    title="TradingView SOL Bot",
    description="Automated SOL trading via TradingView webhooks + Claude AI",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for dashboard frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(webhook.router)
app.include_router(dashboard.router)

# Static files (dashboard)
STATIC_DIR = Path("static")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    """Serve dashboard or redirect."""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {
        "service": "TradingView SOL Bot",
        "status": "running",
        "timestamp": datetime.utcnow().isoformat(),
        "docs": "/docs",
    }


@app.get("/cheatsheet")
async def cheatsheet():
    """Serve the cheatsheet page."""
    page = STATIC_DIR / "cheatsheet.html"
    if page.exists():
        return FileResponse(str(page))
    return {"error": "cheatsheet.html not found"}


@app.get("/changelog")
async def changelog():
    """Serve the changelog page."""
    page = STATIC_DIR / "changelog.html"
    if page.exists():
        return FileResponse(str(page))
    return {"error": "changelog.html not found"}


@app.get("/health")
async def health():
    """Detailed health check with per-service status."""
    services = {}

    try:
        from app.services.price_feed import get_price_feed
        pf = get_price_feed()
        services["price_feed"] = {"running": pf._running, "connected": pf._ws_connected if hasattr(pf, '_ws_connected') else pf._running}
    except Exception:
        services["price_feed"] = {"running": False}

    try:
        from app.services.kalshi_ws_feed import get_kalshi_ws_feed
        ws = get_kalshi_ws_feed()
        services["kalshi_ws"] = {"running": ws._running, "connected": ws._connected, "subscriptions": len(ws._subscribed_tickers)}
    except Exception:
        services["kalshi_ws"] = {"running": False}

    try:
        from app.services.kalshi_risk_manager import get_risk_manager
        rm = get_risk_manager()
        services["circuit_breaker"] = {"running": rm._running, "tripped": rm._tripped}
    except Exception:
        services["circuit_breaker"] = {"running": False}

    bot_checks = [
        ("market_maker", "app.services.kalshi_market_maker", "get_market_maker"),
        ("spread_bot", "app.services.kalshi_spread_bot", "get_spread_bot"),
        ("technical_bot", "app.services.kalshi_technical_bot", "get_technical_bot"),
        ("ai_agent", "app.services.kalshi_ai_agent", "get_ai_agent_bot"),
        ("arb_scanner", "app.services.kalshi_arbitrage", "get_arbitrage_scanner"),
        ("whale_tracker", "app.services.kalshi_whale_tracker", "get_whale_tracker"),
        ("sports_scanner", "app.services.kalshi_sports_scanner", "get_sports_scanner"),
        ("esports_scanner", "app.services.kalshi_esports_scanner", "get_esports_scanner"),
    ]
    for name, module_path, getter_name in bot_checks:
        try:
            import importlib
            mod = importlib.import_module(module_path)
            bot = getattr(mod, getter_name)()
            services[name] = {"running": bot._running, "enabled": bot.enabled}
        except Exception:
            services[name] = {"running": False, "enabled": False}

    try:
        from app.services.ngrok_monitor import get_ngrok_monitor
        ng = get_ngrok_monitor()
        services["ngrok"] = {"online": ng.current_url is not None, "url": ng.current_url}
    except Exception:
        services["ngrok"] = {"online": False}

    all_ok = all(
        s.get("running", s.get("online", False))
        for name, s in services.items()
        if s.get("enabled", True)  # only check enabled services
    )

    return {
        "status": "healthy" if all_ok else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "services": services,
    }
