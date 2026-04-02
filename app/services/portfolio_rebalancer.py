"""Portfolio Rebalancer — auto-rebalance across multiple Solana tokens via Jupiter."""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

from app.config import get
from app.database import log_wallet_tx
from app.services.jupiter_client import JupiterClient
from app.services.telegram_service import TelegramService
from app.services.wallet_service import WalletService

logger = logging.getLogger("bot.rebalancer")

# Token decimals for on-chain amounts
TOKEN_DECIMALS = {
    "SOL": 9,
    "USDC": 6,
    "JTO": 9,
    "WIF": 6,
    "BONK": 5,
    "PYTH": 6,
    "RAY": 6,
}


class PortfolioRebalancer:
    """Rebalances wallet across configured Solana tokens via Jupiter swaps."""

    def __init__(self):
        cfg = get("rebalancer") or {}
        self.enabled = cfg.get("enabled", False)
        self.auto_rebalance = cfg.get("auto_rebalance", False)
        self.check_interval = cfg.get("check_interval_seconds", 3600)
        self.drift_threshold = cfg.get("drift_threshold_percent", 5.0)
        self.min_trade_usd = cfg.get("min_trade_usd", 5.0)
        self.slippage_bps = cfg.get("slippage_bps", 150)
        self.targets = cfg.get("targets", {})
        self.token_mints = cfg.get("token_mints", {})

        # Well-known mints (SOL and USDC come from jupiter config)
        jup_cfg = get("jupiter") or {}
        self.sol_mint = jup_cfg.get("sol_mint", "So11111111111111111111111111111111111111112")
        self.usdc_mint = jup_cfg.get("usdc_mint", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

        self._last_rebalance: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def _get_mint(self, symbol: str) -> str:
        """Resolve token symbol to its on-chain mint address."""
        symbol = symbol.upper()
        if symbol == "SOL":
            return self.sol_mint
        if symbol == "USDC":
            return self.usdc_mint
        mint = self.token_mints.get(symbol)
        if not mint:
            raise ValueError(f"No mint address configured for {symbol}")
        return mint

    async def get_current_allocations(self) -> dict:
        """Fetch all token balances and compute current % allocations.

        Returns dict with keys: balances, prices, values_usd, allocations, total_usd.
        """
        wallet = WalletService()
        jupiter = JupiterClient()
        try:
            balances: dict[str, float] = {}
            prices: dict[str, float] = {}

            # SOL balance
            balances["SOL"] = await wallet.get_balance_sol()

            # SPL token balances (stagger to avoid RPC 429s)
            for symbol in self.targets:
                if symbol == "SOL":
                    continue
                try:
                    mint = self._get_mint(symbol)
                    balance = await self._get_spl_balance(wallet, mint, symbol)
                    balances[symbol] = balance
                except Exception as e:
                    logger.warning(f"Could not fetch {symbol} balance: {e}")
                    balances[symbol] = 0.0
                await asyncio.sleep(0.3)

            # Fetch prices — use real-time feed first, fall back to Jupiter
            from app.services.price_feed import get_price_feed
            feed = get_price_feed()
            for symbol in self.targets:
                try:
                    if feed.is_running:
                        pd = feed.get_price(symbol)
                        if pd and pd.price > 0:
                            prices[symbol] = pd.price
                            continue
                    prices[symbol] = await jupiter.get_token_price(symbol)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(f"Could not fetch {symbol} price: {e}")
                    prices[symbol] = 0.0

            # Compute USD values and allocations
            values_usd: dict[str, float] = {}
            total_usd = 0.0
            for symbol in self.targets:
                val = balances.get(symbol, 0.0) * prices.get(symbol, 0.0)
                values_usd[symbol] = val
                total_usd += val

            allocations: dict[str, float] = {}
            for symbol in self.targets:
                allocations[symbol] = (values_usd[symbol] / total_usd * 100) if total_usd > 0 else 0.0

            return {
                "balances": balances,
                "prices": prices,
                "values_usd": values_usd,
                "allocations": allocations,
                "total_usd": total_usd,
            }
        finally:
            await jupiter.close()
            await wallet.close()

    async def _get_spl_balance(self, wallet: WalletService, mint: str, symbol: str) -> float:
        """Query SPL token balance for a given mint address."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    wallet.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            wallet.public_key,
                            {"mint": mint},
                            {"encoding": "jsonParsed"},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    logger.warning(f"RPC error fetching {symbol} balance: {data['error']}")
                    return 0.0
                accounts = data.get("result", {}).get("value", [])
                total = 0.0
                for account in accounts:
                    parsed = account.get("account", {}).get("data", {}).get("parsed", {})
                    amount = parsed.get("info", {}).get("tokenAmount", {}).get("uiAmount", 0.0)
                    total += amount or 0.0
                return total
        except Exception as e:
            logger.warning(f"Could not fetch {symbol} SPL balance: {e}")
            return 0.0

    def calculate_rebalance(self, current: dict) -> list[dict]:
        """Compare current vs target allocations and return list of trades needed.

        Each trade dict has: symbol, action (sell/buy), amount_usd, amount_tokens,
        from_mint, to_mint, drift_pct.

        Strategy: sell overweight tokens to USDC first, then buy underweight tokens with USDC.
        """
        allocations = current["allocations"]
        total_usd = current["total_usd"]
        prices = current["prices"]
        trades = []

        if total_usd <= 0:
            return trades

        for symbol, target_pct in self.targets.items():
            current_pct = allocations.get(symbol, 0.0)
            drift = current_pct - target_pct
            abs_drift = abs(drift)

            if abs_drift < self.drift_threshold:
                continue

            trade_usd = abs(drift / 100) * total_usd

            if trade_usd < self.min_trade_usd:
                continue

            price = prices.get(symbol, 0)
            if price <= 0:
                continue

            amount_tokens = trade_usd / price
            mint = self._get_mint(symbol)

            if drift > 0:
                # Overweight: sell TOKEN -> USDC
                trades.append({
                    "symbol": symbol,
                    "action": "sell",
                    "amount_usd": round(trade_usd, 2),
                    "amount_tokens": amount_tokens,
                    "drift_pct": round(drift, 2),
                    "input_mint": mint,
                    "output_mint": self.usdc_mint,
                })
            else:
                # Underweight: buy USDC -> TOKEN
                trades.append({
                    "symbol": symbol,
                    "action": "buy",
                    "amount_usd": round(trade_usd, 2),
                    "amount_tokens": amount_tokens,
                    "drift_pct": round(drift, 2),
                    "input_mint": self.usdc_mint,
                    "output_mint": mint,
                })

        # Sort: sells first (to accumulate USDC), then buys
        trades.sort(key=lambda t: (0 if t["action"] == "sell" else 1, -t["amount_usd"]))
        return trades

    async def execute_rebalance(self) -> dict:
        """Execute a full rebalance: fetch allocations, compute trades, execute via Jupiter."""
        logger.info("Starting portfolio rebalance")
        telegram = TelegramService()
        try:
            current = await self.get_current_allocations()
            trades = self.calculate_rebalance(current)

            if not trades:
                logger.info("Portfolio is within drift threshold, no rebalance needed")
                return {
                    "status": "balanced",
                    "message": "Portfolio is within drift threshold",
                    "current": current["allocations"],
                    "targets": self.targets,
                    "trades_executed": 0,
                }

            jupiter = JupiterClient()
            wallet = WalletService()
            executed = []
            errors = []

            try:
                keypair = wallet.get_keypair()

                for trade in trades:
                    try:
                        symbol = trade["symbol"]
                        action = trade["action"]
                        amount_usd = trade["amount_usd"]

                        # Determine amount in input token lamports
                        if action == "sell":
                            # Selling TOKEN for USDC
                            decimals = TOKEN_DECIMALS.get(symbol, 6)
                            amount_lamports = int(trade["amount_tokens"] * (10 ** decimals))
                        else:
                            # Buying TOKEN with USDC (input is USDC, 6 decimals)
                            amount_lamports = int(amount_usd * 1_000_000)

                        logger.info(
                            f"Rebalance {action.upper()} {symbol}: "
                            f"${amount_usd:.2f} ({trade['amount_tokens']:.6f} tokens)"
                        )

                        result = await jupiter.execute_swap(
                            keypair=keypair,
                            input_mint=trade["input_mint"],
                            output_mint=trade["output_mint"],
                            amount_lamports=amount_lamports,
                            slippage_bps=self.slippage_bps,
                        )

                        log_wallet_tx(
                            tx_type="rebalance",
                            direction="out" if action == "sell" else "in",
                            amount=amount_usd,
                            token=symbol,
                            fee_sol=0.000005,
                            tx_signature=result.get("tx_signature", ""),
                            notes=f"Rebalance {action} {symbol} ${amount_usd:.2f}",
                        )

                        executed.append({
                            "symbol": symbol,
                            "action": action,
                            "amount_usd": amount_usd,
                            "tx_signature": result.get("tx_signature", ""),
                        })

                        # Brief pause between swaps to let transactions settle
                        await asyncio.sleep(2)

                    except Exception as e:
                        logger.error(f"Rebalance trade failed for {trade['symbol']}: {e}")
                        errors.append({"symbol": trade["symbol"], "error": str(e)})

                # Invalidate wallet cache after rebalance
                wallet.invalidate_cache()

            finally:
                await jupiter.close()
                await wallet.close()

            self._last_rebalance = datetime.utcnow().isoformat()

            # Telegram notification
            summary_lines = [f"Portfolio Rebalance Complete"]
            for ex in executed:
                summary_lines.append(
                    f"  {ex['action'].upper()} {ex['symbol']}: ${ex['amount_usd']:.2f}"
                )
            if errors:
                summary_lines.append(f"  Errors: {len(errors)}")
            await telegram.send_message("\n".join(summary_lines))

            return {
                "status": "executed",
                "trades_executed": len(executed),
                "executed": executed,
                "errors": errors,
                "timestamp": self._last_rebalance,
            }

        except Exception as e:
            logger.exception(f"Rebalance failed: {e}")
            await telegram.send_message(f"Rebalance FAILED: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            await telegram.close()

    async def get_status(self) -> dict:
        """Return current allocations, targets, drift per token, and last rebalance time."""
        try:
            current = await self.get_current_allocations()
        except Exception as e:
            logger.error(f"Failed to get current allocations: {e}")
            return {
                "enabled": self.enabled,
                "auto_rebalance": self.auto_rebalance,
                "error": str(e),
            }

        drift: dict[str, float] = {}
        for symbol in self.targets:
            drift[symbol] = round(
                current["allocations"].get(symbol, 0.0) - self.targets.get(symbol, 0.0), 2
            )

        return {
            "enabled": self.enabled,
            "auto_rebalance": self.auto_rebalance,
            "drift_threshold": self.drift_threshold,
            "min_trade_usd": self.min_trade_usd,
            "targets": self.targets,
            "current_allocations": {k: round(v, 2) for k, v in current["allocations"].items()},
            "values_usd": {k: round(v, 2) for k, v in current["values_usd"].items()},
            "balances": current["balances"],
            "prices": {k: round(v, 4) for k, v in current["prices"].items()},
            "total_usd": round(current["total_usd"], 2),
            "drift": drift,
            "last_rebalance": self._last_rebalance,
        }

    def toggle_auto(self) -> dict:
        """Toggle auto-rebalance on/off."""
        self.auto_rebalance = not self.auto_rebalance
        logger.info(f"Auto-rebalance toggled to: {self.auto_rebalance}")
        if self.auto_rebalance and not self._running:
            self.start()
        elif not self.auto_rebalance and self._running:
            self.stop()
        return {"auto_rebalance": self.auto_rebalance}

    # ── Background auto-rebalance loop ──

    def start(self) -> Optional[asyncio.Task]:
        """Start the background auto-rebalance loop."""
        if self._running:
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Rebalancer auto-loop started (interval={self.check_interval}s)")
        return self._task

    async def stop(self):
        """Stop the background auto-rebalance loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Rebalancer auto-loop stopped")

    async def _loop(self):
        """Background loop: check allocations and rebalance if needed."""
        while self._running and self.auto_rebalance:
            try:
                logger.info("Auto-rebalance check running")
                current = await self.get_current_allocations()
                trades = self.calculate_rebalance(current)
                if trades:
                    logger.info(f"Drift detected, executing {len(trades)} rebalance trades")
                    await self.execute_rebalance()
                else:
                    logger.info("Auto-rebalance check: portfolio within threshold")
            except Exception as e:
                logger.error(f"Auto-rebalance loop error: {e}")
            await asyncio.sleep(self.check_interval)


# ── Singleton ──

_rebalancer: Optional[PortfolioRebalancer] = None


def get_rebalancer() -> PortfolioRebalancer:
    global _rebalancer
    if _rebalancer is None:
        _rebalancer = PortfolioRebalancer()
    return _rebalancer
