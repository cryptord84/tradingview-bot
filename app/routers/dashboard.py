"""Dashboard API endpoints."""

import logging
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get, reload_config
from app.database import get_trades, get_stats, export_csv, get_today_trades
from app.models import DashboardStats, SettingsUpdate
from app.services.jupiter_client import JupiterClient
from app.services.wallet_service import WalletService
from app import state
from app.services.ngrok_monitor import get_ngrok_monitor

logger = logging.getLogger("bot.dashboard")

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/stats")
async def get_dashboard_stats():
    """Get live dashboard statistics."""
    try:
        jupiter = JupiterClient()
        wallet = WalletService()

        sol_price = await jupiter.get_sol_price()
        balances = await wallet.get_total_usd_balance(sol_price)

        db_stats = get_stats()
        total = db_stats["total_trades"]
        wins = db_stats["winning_trades"]
        total_usd = balances["total_usd"]

        await jupiter.close()
        await wallet.close()

        return DashboardStats(
            wallet_balance_sol=balances["sol"],
            wallet_balance_usdc=balances["usdc"],
            wallet_balance_usd=total_usd,
            sol_price_usd=sol_price,
            total_trades=total,
            winning_trades=wins,
            losing_trades=db_stats["losing_trades"],
            win_rate=(wins / total * 100) if total > 0 else 0,
            total_pnl_usd=db_stats["total_pnl_usd"],
            total_pnl_percent=(db_stats["total_pnl_usd"] / total_usd * 100) if total_usd > 0 else 0,
            today_pnl_usd=db_stats["today_pnl_usd"],
            avg_trade_size_sol=db_stats["avg_trade_size_sol"],
            last_signal_time=db_stats["last_trade_time"],
            bot_status="running",
        )
    except Exception as e:
        logger.error(f"Stats error: {e}")
        # Return defaults on error (e.g., no wallet configured yet)
        db_stats = get_stats()
        return DashboardStats(
            total_trades=db_stats["total_trades"],
            total_pnl_usd=db_stats["total_pnl_usd"],
            today_pnl_usd=db_stats["today_pnl_usd"],
            bot_status="error",
        )


@router.get("/trades")
async def get_trade_history(limit: int = 100, offset: int = 0):
    """Get trade history."""
    trades = get_trades(limit=min(limit, 500), offset=offset)
    return {"trades": trades, "total": len(trades)}


@router.get("/trades/today")
async def get_todays_trades():
    """Get today's trades."""
    return {"trades": get_today_trades()}


@router.get("/price")
async def get_sol_price():
    """Get current SOL price."""
    jupiter = JupiterClient()
    try:
        price = await jupiter.get_sol_price()
        market = await jupiter.get_market_data()
        return {"price": price, "market_data": market}
    finally:
        await jupiter.close()


@router.get("/settings")
async def get_settings():
    """Get current risk/trading settings."""
    risk = get("risk")
    geo = get("geo_risk")
    jupiter = get("jupiter")
    return {
        "risk": risk,
        "geo_risk": geo,
        "jupiter": {
            "slippage_bps": jupiter.get("slippage_bps", 100),
            "priority_fee_lamports": jupiter.get("priority_fee_lamports", 50000),
        },
    }


@router.post("/settings")
async def update_settings(updates: SettingsUpdate):
    """Update risk/trading settings (runtime only, does not persist to YAML)."""
    # For production, you'd write back to config.yaml
    # This updates the in-memory config
    import yaml

    config_path = Path("config.yaml")
    if not config_path.exists():
        raise HTTPException(status_code=500, detail="config.yaml not found")

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    update_dict = updates.model_dump(exclude_none=True)
    for key, val in update_dict.items():
        if key == "geo_risk_weight":
            cfg.setdefault("geo_risk", {})["weight"] = val
        elif key in ("slippage_bps", "priority_fee_lamports"):
            cfg.setdefault("jupiter", {})[key] = val
        elif key in cfg.get("risk", {}):
            cfg["risk"][key] = val

    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)

    reload_config()
    return {"status": "ok", "updated": update_dict}


@router.get("/export/csv")
async def export_trades_csv():
    """Export all trades as CSV for tax compliance."""
    path = export_csv()
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail="No trades to export")
    return FileResponse(path, media_type="text/csv", filename=Path(path).name)


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "tradingview-bot"}


@router.get("/bot/status")
async def bot_status():
    """Bot running state + uptime."""
    return {
        "active": state.is_active(),
        "uptime": state.get_uptime(),
    }


@router.post("/bot/start")
async def bot_start():
    return state.start_bot()


@router.post("/bot/stop")
async def bot_stop():
    return state.stop_bot()


@router.get("/system/status")
async def system_status():
    """Check all component connections and return status map."""
    results = {}

    def err(e): return str(e)[:60]

    # 1. Database
    try:
        from app.database import get_db
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        results["database"] = {"ok": True, "label": "SQLite"}
    except Exception as e:
        results["database"] = {"ok": False, "label": "SQLite", "error": err(e)}

    # 2. Wallet / RPC
    try:
        wallet = WalletService()
        bal = await wallet.get_balance_sol()
        await wallet.close()
        results["wallet"] = {"ok": True, "label": "Solana RPC", "detail": f"{bal:.4f} SOL"}
    except Exception as e:
        results["wallet"] = {"ok": False, "label": "Solana RPC", "error": err(e)}

    # 3. Price feed — Binance public API (no key, no rate limit)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.binance.us/api/v3/ticker/price",
                params={"symbol": "SOLUSDT"},
            )
            resp.raise_for_status()
            price = float(resp.json()["price"])
            results["price_feed"] = {"ok": True, "label": "Binance US", "detail": f"SOL ${price:.2f}"}
    except Exception as e:
        results["price_feed"] = {"ok": False, "label": "Binance Feed", "error": str(e)[:60]}

    # 4. Jupiter Swap API (quote endpoint ping)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                    "https://lite-api.jup.ag/swap/v1/quote",
                    params={
                        "inputMint": "So11111111111111111111111111111111111111112",
                        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        "amount": "1000000",
                        "slippageBps": "50",
                    },
                )
            results["jupiter"] = {"ok": resp.status_code < 500, "label": "Jupiter DEX", "detail": f"HTTP {resp.status_code}"}
    except Exception as e:
        results["jupiter"] = {"ok": False, "label": "Jupiter DEX", "error": err(e)}

    # 5. Claude CLI
    cli_path = get("claude", "cli_path", "claude")
    cli_mode = get("claude", "mode", "cli")
    if cli_mode == "cli":
        found = shutil.which(cli_path)
        results["claude"] = {
            "ok": found is not None,
            "label": "Claude CLI",
            "detail": found or "not found in PATH",
        }
    else:
        api_key = get("claude", "api_key", "")
        results["claude"] = {
            "ok": bool(api_key),
            "label": "Claude API",
            "detail": "key configured" if api_key else "no api_key set",
        }

    # 6. Telegram
    tg_cfg = get("telegram")
    tg_token = tg_cfg.get("bot_token", "")
    tg_enabled = tg_cfg.get("enabled", False)
    if tg_enabled and tg_token:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(f"https://api.telegram.org/bot{tg_token}/getMe")
                data = resp.json()
                ok = data.get("ok", False)
                name = data.get("result", {}).get("username", "?") if ok else "auth failed"
                results["telegram"] = {"ok": ok, "label": "Telegram Bot", "detail": f"@{name}"}
        except Exception as e:
            results["telegram"] = {"ok": False, "label": "Telegram Bot", "error": err(e)}
    else:
        results["telegram"] = {"ok": False, "label": "Telegram Bot", "detail": "disabled or no token"}

    # 7. News providers (with fallback)
    news_cfg = get("news")
    newsapi_key = news_cfg.get("newsapi_key", "")
    tavily_key = news_cfg.get("tavily_api_key", "")
    preferred = news_cfg.get("provider", "newsapi")
    news_ok = False

    async with httpx.AsyncClient(timeout=8) as client:
        # Try preferred provider first
        providers = []
        if preferred == "newsapi":
            if newsapi_key:
                providers.append(("NewsAPI", "https://newsapi.org/v2/top-headlines",
                                  {"country": "us", "pageSize": 1, "apiKey": newsapi_key}))
            if tavily_key:
                providers.append(("Tavily", "https://api.tavily.com/search", None))
        else:
            if tavily_key:
                providers.append(("Tavily", "https://api.tavily.com/search", None))
            if newsapi_key:
                providers.append(("NewsAPI", "https://newsapi.org/v2/top-headlines",
                                  {"country": "us", "pageSize": 1, "apiKey": newsapi_key}))

        for name, url, params in providers:
            try:
                if name == "Tavily":
                    resp = await client.post(url, json={
                        "api_key": tavily_key, "query": "crypto", "max_results": 1, "search_depth": "basic"
                    })
                else:
                    resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    results["news"] = {"ok": True, "label": name, "detail": "active"}
                    news_ok = True
                    break
                else:
                    results["news"] = {"ok": False, "label": name, "detail": f"HTTP {resp.status_code}"}
            except Exception as e:
                results["news"] = {"ok": False, "label": name, "error": err(e)}

        if not providers:
            results["news"] = {"ok": False, "label": "News", "detail": "no keys configured"}

    # 8. ngrok tunnel
    ngrok = get_ngrok_monitor()
    ngrok_status = ngrok.get_status()
    results["ngrok"] = {
        "ok": ngrok_status["online"],
        "label": "ngrok Tunnel",
        "detail": ngrok_status["current_url"] or "not detected",
    }

    return {
        "components": results,
        "bot_active": state.is_active(),
        "uptime": state.get_uptime(),
        "ngrok": ngrok_status,
    }


@router.get("/ngrok")
async def get_ngrok_status():
    """Get current ngrok tunnel status with URL history."""
    ngrok = get_ngrok_monitor()
    status = await ngrok.check_once()
    return status
