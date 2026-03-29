"""TradingView SOL Trading Bot - Main FastAPI Application."""

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
from app.utils.csv_backup import run_daily_backup

# --- Logging Setup ---
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

log_cfg = {}
try:
    log_cfg = get("logging") or {}
except Exception:
    pass

logging.basicConfig(
    level=getattr(logging, (log_cfg.get("level") or get("server", "log_level", "INFO")).upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_cfg.get("file", "logs/bot.log"),
            maxBytes=(log_cfg.get("max_size_mb", 50)) * 1024 * 1024,
            backupCount=log_cfg.get("backup_count", 5),
        ),
    ],
)
logger = logging.getLogger("bot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting TradingView SOL Trading Bot")
    load_config()
    init_db()
    run_daily_backup()
    logger.info("Bot initialized successfully")
    yield
    logger.info("Shutting down bot")


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


@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
