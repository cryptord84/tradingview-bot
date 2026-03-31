"""Dashboard API endpoints."""

import logging
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get, reload_config
from app.database import get_trades, get_stats, export_csv, get_today_trades, get_wallet_transactions, get_kamino_net_deposited, get_open_positions, get_all_positions, get_position_analytics, get_backtests, insert_backtest, delete_backtest
from app.models import DashboardStats, SettingsUpdate
from app.services.jupiter_client import JupiterClient
from app.services.wallet_service import WalletService
from app import state
from app.services.kamino_client import KaminoClient
from app.services.ngrok_monitor import get_ngrok_monitor
from app.services.scout_service import get_latest_report, get_all_reports, ScoutService

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

    # 8. Kamino Lend
    kamino_cfg = get("kamino")
    if kamino_cfg.get("enabled"):
        try:
            kamino = KaminoClient()
            metrics = await kamino.get_reserve_metrics()
            await kamino.close()
            if metrics.get("available"):
                results["kamino"] = {
                    "ok": True,
                    "label": "Kamino Lend",
                    "detail": f"USDC {metrics['supply_apy']:.2f}% APY",
                }
            else:
                results["kamino"] = {"ok": False, "label": "Kamino Lend", "detail": metrics.get("error", "unavailable")}
        except Exception as e:
            results["kamino"] = {"ok": False, "label": "Kamino Lend", "error": err(e)}
    else:
        results["kamino"] = {"ok": False, "label": "Kamino Lend", "detail": "disabled"}

    # 9. ngrok tunnel
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


@router.get("/kamino")
async def get_kamino_status():
    """Get Kamino Lend yield status and position info."""
    kamino = KaminoClient()
    wallet = WalletService()

    try:
        metrics = await kamino.get_reserve_metrics()
        position = await kamino.get_user_position(wallet.public_key)
        usdc_balance = await wallet.get_usdc_balance()

        deposited = position.get("deposited_usdc", 0)
        total_usdc = deposited + usdc_balance
        net_deposited = get_kamino_net_deposited()
        earnings = deposited - net_deposited if deposited > 0 else 0.0

        return {
            "enabled": kamino.enabled,
            "auto_deposit": kamino.auto_deposit,
            "auto_withdraw": kamino.auto_withdraw,
            "wallet_address": wallet.public_key,
            "deposited_usdc": deposited,
            "wallet_usdc": usdc_balance,
            "total_usdc": total_usdc,
            "yield_pct_deposited": (deposited / total_usdc * 100) if total_usdc > 0 else 0,
            "supply_apy": metrics.get("supply_apy", 0),
            "borrow_apy": metrics.get("borrow_apy", 0),
            "utilization": metrics.get("utilization", 0),
            "daily_yield_est": deposited * metrics.get("supply_apy", 0) / 100 / 365,
            "monthly_yield_est": deposited * metrics.get("supply_apy", 0) / 100 / 12,
            "earnings_usdc": earnings,
        }
    finally:
        await kamino.close()
        await wallet.close()


@router.post("/kamino/deposit")
async def kamino_deposit(body: dict):
    """Manually deposit USDC into Kamino. Body: {"amount": 50.0} or {"percent": 80}"""
    kamino = KaminoClient()
    wallet = WalletService()

    try:
        usdc_balance = await wallet.get_usdc_balance()
        amount = body.get("amount")
        percent = body.get("percent")

        if percent is not None:
            amount = usdc_balance * float(percent) / 100

        if amount is None or amount <= 0:
            raise HTTPException(400, "Provide 'amount' (USD) or 'percent' (of wallet USDC)")

        amount = min(amount, usdc_balance - kamino.reserve_usdc)
        if amount < kamino.min_deposit:
            raise HTTPException(400, f"Amount ${amount:.2f} below minimum ${kamino.min_deposit}")

        result = await kamino.deposit(wallet.get_keypair(), amount)
        return result
    finally:
        await kamino.close()
        await wallet.close()


@router.get("/wallet/transactions")
async def get_wallet_tx_log(limit: int = 50):
    """Get wallet transaction log."""
    txs = get_wallet_transactions(limit=min(limit, 200))
    return {"transactions": txs, "total": len(txs)}


@router.post("/kamino/withdraw")
async def kamino_withdraw(body: dict):
    """Manually withdraw USDC from Kamino. Body: {"amount": 50.0} or {"percent": 100} or {"all": true}"""
    kamino = KaminoClient()
    wallet = WalletService()

    try:
        if body.get("all"):
            result = await kamino.withdraw_all(wallet.get_keypair())
            return result

        position = await kamino.get_user_position(wallet.public_key)
        deposited = position.get("deposited_usdc", 0)

        if deposited <= 0:
            raise HTTPException(400, "No USDC deposited in Kamino")

        amount = body.get("amount")
        percent = body.get("percent")

        if percent is not None:
            amount = deposited * float(percent) / 100

        if amount is None or amount <= 0:
            raise HTTPException(400, "Provide 'amount' (USD), 'percent' (of deposited), or 'all': true")

        amount = min(amount, deposited)
        result = await kamino.withdraw(wallet.get_keypair(), amount)
        return result
    finally:
        await kamino.close()
        await wallet.close()


@router.get("/scout")
async def get_scout_report():
    """Get latest scout report and recent history."""
    latest = get_latest_report()
    history = get_all_reports(limit=7)
    return {
        "latest": latest,
        "history_dates": [r.get("date") for r in history],
        "total_reports": len(history),
        "sources": latest.get("sources_searched", []) if latest else [],
    }


@router.post("/scout/run")
async def run_scout_now():
    """Manually trigger a scout scan."""
    scout = ScoutService()
    try:
        report = await scout.generate_report()
        return {"status": "ok", "result_count": report.get("result_count", 0), "report": report}
    finally:
        await scout.close()


@router.get("/positions")
async def get_positions_api(status: str = "all", limit: int = 50):
    """Get positions, optionally filtered by status."""
    if status == "open":
        positions = get_open_positions()
    else:
        positions = get_all_positions(limit=min(limit, 200))

    # Add live P&L for open positions
    has_open = any(p["status"] == "open" for p in positions)
    if has_open:
        jupiter = JupiterClient()
        try:
            current_price = await jupiter.get_sol_price()
            for p in positions:
                if p["status"] == "open":
                    p["current_price"] = current_price
                    p["unrealized_pnl_usdc"] = (current_price - p["entry_price"]) * p["amount_sol"]
                    p["unrealized_pnl_percent"] = ((current_price - p["entry_price"]) / p["entry_price"]) * 100
                    p["distance_to_tp_percent"] = ((p["tp_price"] - current_price) / current_price) * 100
                    p["distance_to_sl_percent"] = ((current_price - p["sl_price"]) / current_price) * 100
                    # Effective SL considers trailing stop
                    trail_sl = p.get("trail_sl_price") or 0
                    p["effective_sl"] = max(p["sl_price"], trail_sl)
                    p["trail_active"] = trail_sl > p["sl_price"]
        finally:
            await jupiter.close()

    return {"positions": positions, "total": len(positions)}


@router.post("/positions/{position_id}/close")
async def manual_close_position(position_id: int):
    """Manually close a position at market price."""
    from app.services.position_monitor import get_position_monitor

    positions = get_open_positions()
    pos = next((p for p in positions if p["id"] == position_id), None)
    if not pos:
        raise HTTPException(status_code=404, detail=f"Open position #{position_id} not found")

    monitor = get_position_monitor()
    jupiter = JupiterClient()
    try:
        current_price = await jupiter.get_sol_price()
        await monitor._close_position(pos, current_price, "manual", jupiter)
    finally:
        await jupiter.close()

    return {"status": "ok", "position_id": position_id}


@router.get("/positions/analytics")
async def position_analytics():
    """Get aggregated position analytics: win rate by strategy, equity curve, monthly P&L."""
    return get_position_analytics()


# ── Backtest Tracker ──────────────────────────────────────────────

@router.get("/backtests")
async def list_backtests(strategy: str = None, limit: int = 50):
    """Get backtest results, optionally filtered by strategy."""
    results = get_backtests(strategy=strategy, limit=limit)
    return {"backtests": results, "total": len(results)}


@router.post("/backtests")
async def add_backtest(data: dict):
    """Add a backtest result manually."""
    required = ["strategy_name", "version", "timeframe", "symbol"]
    for field in required:
        if field not in data:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
    bt_id = insert_backtest(data)
    return {"id": bt_id, "status": "ok"}


@router.post("/backtests/import")
async def import_backtests_from_reports():
    """Scan Exports/ directory and import all .report.txt files."""
    import re
    from pathlib import Path

    exports_dir = Path("Exports")
    if not exports_dir.exists():
        return {"imported": 0, "error": "Exports/ directory not found"}

    existing = {bt.get("source_file") for bt in get_backtests(limit=500)}
    imported = 0
    errors = []

    for report_file in sorted(exports_dir.glob("*.report.txt")):
        fname = report_file.name
        if fname in existing:
            continue

        try:
            text = report_file.read_text()
            bt = _parse_report_txt(text, fname)
            if bt:
                insert_backtest(bt)
                imported += 1
        except Exception as e:
            errors.append(f"{fname}: {e}")

    return {"imported": imported, "errors": errors, "total_in_db": len(get_backtests(limit=500))}


@router.delete("/backtests/{backtest_id}")
async def remove_backtest(backtest_id: int):
    """Delete a backtest record."""
    delete_backtest(backtest_id)
    return {"status": "ok"}


def _parse_report_txt(text: str, filename: str) -> dict:
    """Parse a .report.txt file into a backtest dict."""
    import re

    def extract(pattern, txt, group=1, as_float=True):
        m = re.search(pattern, txt, re.MULTILINE)
        if not m:
            return None
        val = m.group(group).strip().replace(",", "").replace("$", "").replace("+", "").replace("%", "")
        if as_float:
            try:
                return float(val)
            except ValueError:
                return None
        return val

    # Parse strategy name and version from filename
    # e.g. SOL_Confluence_Pro_v3.5_COINBASE_SOLUSD_2026-03-29.report.txt
    # or SOL_Mean_Reversion_v1.3_—_4H_Backtest_BINANCE_SOLUSDT_2026-03-30.report.txt
    name_match = re.match(r"(.+?)_(v[\d.]+)", filename)
    if name_match:
        strategy_name = name_match.group(1).replace("_", " ")
        version = name_match.group(2)
    else:
        strategy_name = filename.split("_")[0]
        version = "unknown"

    symbol = extract(r"Symbol\s*:\s*(\S+)", text, as_float=False) or ""
    tf_raw = extract(r"Timeframe:\s*(.+?)$", text, as_float=False) or ""
    tf_raw = tf_raw.strip()
    # Normalize: "4 hours" → "4H", "1 hour" → "1H", "D" → "D"
    tf_map = {"4 hours": "4H", "1 hour": "1H", "2 hours": "2H", "1 day": "D", "1 week": "W",
              "240": "4H", "60": "1H", "120": "2H", "d": "D", "w": "W"}
    timeframe = tf_map.get(tf_raw.lower(), tf_raw)
    period_line = re.search(r"Period\s*:\s*(.+?)$", text, re.MULTILINE)
    period_start, period_end = None, None
    if period_line:
        parts = period_line.group(1).split("—")
        if len(parts) == 2:
            period_start = parts[0].strip()
            period_end = parts[1].strip()

    capital = extract(r"Capital\s*:\s*\$([\d,.]+)", text)

    # Determine status from filename/strategy
    status = "tested"

    return {
        "strategy_name": strategy_name,
        "version": version,
        "timeframe": timeframe,
        "symbol": symbol,
        "period_start": period_start,
        "period_end": period_end,
        "initial_capital": capital,
        "net_profit_usd": extract(r"Net Profit\s*:\s*\$([+\-\d,.]+)", text),
        "net_profit_pct": extract(r"Net Profit\s*:.*?\(([+\-\d,.]+)%\)", text),
        "gross_profit": extract(r"Gross Profit\s*:\s*\$([\d,.]+)", text),
        "gross_loss": extract(r"Gross Loss\s*:\s*-?\$?([\d,.]+)", text),
        "profit_factor": extract(r"Profit Factor\s*:\s*([\d.]+)", text),
        "total_trades": int(extract(r"Total Trades\s*:\s*(\d+)", text) or 0),
        "winning_trades": int(extract(r"Winners.*?:\s*(\d+)", text) or 0),
        "losing_trades": int(extract(r"Losers.*?:\s*(\d+)", text) or extract(r"Winners\s*/\s*Losers\s*:\s*\d+\s*/\s*(\d+)", text) or 0),
        "win_rate": extract(r"Win Rate\s*:\s*([\d.]+)", text),
        "avg_win": extract(r"Avg Win\s*:\s*\$([+\-\d,.]+)", text),
        "avg_loss": extract(r"Avg Loss\s*:\s*-?\$?([\d,.]+)", text),
        "win_loss_ratio": extract(r"Win/Loss Ratio\s*:\s*([\d.]+)", text),
        "largest_win": extract(r"Largest Win\s*:\s*\$([+\-\d,.]+)", text),
        "largest_loss": extract(r"Largest Loss\s*:\s*-?\$?([\d,.]+)", text),
        "max_drawdown": extract(r"Max Drawdown \(EOB\)\s*:\s*-?\$?([\d,.]+)", text),
        "sharpe_ratio": extract(r"Sharpe Ratio\s*:\s*([+\-\d.]+)", text),
        "sortino_ratio": extract(r"Sortino Ratio\s*:\s*([+\-\d.]+)", text),
        "long_trades": int(extract(r"Long\s*:\s*(\d+)\s*trades", text) or 0),
        "long_win_rate": extract(r"Long\s*:.*?\(([\d.]+)%\)", text),
        "long_pnl": extract(r"Long\s*:.*?P&L:\s*\$([+\-\d,.]+)", text),
        "short_trades": int(extract(r"Short\s*:\s*(\d+)\s*trades", text) or 0),
        "short_win_rate": extract(r"Short\s*:.*?\(([\d.]+)%\)", text),
        "short_pnl": extract(r"Short\s*:.*?P&L:\s*\$([+\-\d,.]+)", text),
        "source_file": filename,
        "notes": "",
        "status": status,
    }
