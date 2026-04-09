"""Wallet management - secure key loading and balance queries."""

import base64
import logging
import time
from typing import Optional

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from solders.keypair import Keypair

from app.config import get, get_env

logger = logging.getLogger("bot.wallet")


def _derive_key(password: str) -> bytes:
    """Derive encryption key from password using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"tradingview-bot-salt-v1",  # Static salt; password provides entropy
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def decrypt_private_key() -> Keypair:
    """Decrypt and load wallet keypair."""
    cfg = get("wallet")
    encrypted = cfg.get("encrypted_private_key", "")
    if not encrypted:
        raise ValueError("No encrypted_private_key in config.yaml")

    password_env = cfg.get("encryption_password_env", "WALLET_ENCRYPTION_PASSWORD")
    password = get_env(password_env)
    if not password:
        raise ValueError(f"Env var {password_env} not set. Cannot decrypt wallet key.")

    key = _derive_key(password)
    f = Fernet(key)
    decrypted = f.decrypt(encrypted.encode())
    secret_bytes = base64.b58decode(decrypted.decode()) if len(decrypted) < 88 else decrypted

    # Support both base58 and raw bytes
    try:
        return Keypair.from_base58_string(decrypted.decode())
    except Exception:
        return Keypair.from_bytes(secret_bytes)


def encrypt_private_key(private_key_b58: str, password: str) -> str:
    """Encrypt a base58 private key. Used during setup."""
    key = _derive_key(password)
    f = Fernet(key)
    return f.encrypt(private_key_b58.encode()).decode()


class WalletService:
    """Query wallet balance and manage SOL transactions."""

    # Cache TTL in seconds — avoids hammering the free Solana RPC
    _CACHE_TTL = 15

    def __init__(self):
        self.rpc_url = get("wallet", "rpc_url", "https://api.mainnet-beta.solana.com")
        self._client = httpx.AsyncClient(timeout=15)
        self._keypair: Optional[Keypair] = None
        self._cache: dict[str, tuple[float, any]] = {}  # key -> (timestamp, value)

    def get_keypair(self) -> Keypair:
        if self._keypair is None:
            self._keypair = decrypt_private_key()
        return self._keypair

    @property
    def public_key(self) -> str:
        return str(self.get_keypair().pubkey())

    async def get_balance_lamports(self) -> int:
        """Get wallet balance in lamports."""
        resp = await self._client.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [self.public_key],
            },
        )
        resp.raise_for_status()
        return resp.json()["result"]["value"]

    def _get_cached(self, key: str) -> Optional[float]:
        """Return cached value if still fresh, else None."""
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < self._CACHE_TTL:
                return val
        return None

    def _set_cached(self, key: str, val: float):
        self._cache[key] = (time.time(), val)

    def invalidate_cache(self):
        """Clear cache — call after trades to get fresh balances."""
        self._cache.clear()

    async def get_balance_sol(self) -> float:
        """Get wallet balance in SOL (cached for 15s)."""
        cached = self._get_cached("sol")
        if cached is not None:
            return cached
        lamports = await self.get_balance_lamports()
        sol = lamports / 1_000_000_000
        self._set_cached("sol", sol)
        return sol

    async def get_usdc_balance(self) -> float:
        """Get USDC SPL token balance (cached for 15s)."""
        cached = self._get_cached("usdc")
        if cached is not None:
            return cached
        try:
            usdc_mint = get("jupiter", "usdc_mint", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
            resp = await self._client.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.public_key,
                        {"mint": usdc_mint},
                        {"encoding": "jsonParsed"},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # Log RPC errors without crashing
            if "error" in data:
                logger.warning(f"RPC error fetching USDC balance: {data['error']}")
                return 0.0
            accounts = data.get("result", {}).get("value", [])
            total = 0.0
            for account in accounts:
                parsed = account.get("account", {}).get("data", {}).get("parsed", {})
                amount = parsed.get("info", {}).get("tokenAmount", {}).get("uiAmount", 0.0)
                total += amount or 0.0
            self._set_cached("usdc", total)
            return total
        except Exception as e:
            logger.warning(f"Could not fetch USDC balance: {e}")
            return 0.0

    # SPL token mints we track (excluding SOL native and USDC which have their own methods)
    TRACKED_TOKEN_MINTS = {
        "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
        "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
        "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
        "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        "ETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
        "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    }

    async def get_spl_token_balances(self) -> dict[str, float]:
        """Get balances for all tracked SPL tokens. Returns {symbol: amount}."""
        cached = self._get_cached("spl_tokens")
        if cached is not None:
            return cached  # type: ignore

        # Fetch ALL token accounts owned by this wallet in one RPC call
        try:
            resp = await self._client.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.public_key,
                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                        {"encoding": "jsonParsed"},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.warning(f"RPC error fetching token accounts: {data['error']}")
                return {}

            # Build reverse lookup: mint -> symbol
            mint_to_symbol = {v: k for k, v in self.TRACKED_TOKEN_MINTS.items()}

            accounts = data.get("result", {}).get("value", [])
            balances: dict[str, float] = {}
            for account in accounts:
                parsed = account.get("account", {}).get("data", {}).get("parsed", {})
                info = parsed.get("info", {})
                mint = info.get("mint", "")
                symbol = mint_to_symbol.get(mint)
                if symbol:
                    amount = info.get("tokenAmount", {}).get("uiAmount", 0.0)
                    if amount and amount > 0:
                        balances[symbol] = amount

            self._set_cached("spl_tokens", balances)  # type: ignore
            return balances
        except Exception as e:
            logger.warning(f"Could not fetch SPL token balances: {e}")
            return {}

    async def get_total_usd_balance(self, sol_price: float, token_prices: Optional[dict] = None) -> dict:
        """Get total portfolio value: SOL + USDC + all SPL tokens.

        Args:
            sol_price: Current SOL price in USD.
            token_prices: Optional dict of {symbol: {"price": float}} for SPL tokens.
                          If not provided, SPL tokens are excluded from total.
        """
        sol_balance, usdc_balance = 0.0, 0.0
        try:
            sol_balance = await self.get_balance_sol()
        except Exception as e:
            logger.warning(f"Could not fetch SOL balance: {e}")
        try:
            usdc_balance = await self.get_usdc_balance()
        except Exception as e:
            logger.warning(f"Could not fetch USDC balance: {e}")

        sol_usd = sol_balance * sol_price

        # Calculate SPL token holdings value
        token_holdings = {}
        tokens_usd = 0.0
        try:
            spl_balances = await self.get_spl_token_balances()
            for symbol, amount in spl_balances.items():
                price = 0.0
                if token_prices and symbol in token_prices:
                    price = token_prices[symbol].get("price", 0.0)
                usd_value = amount * price
                tokens_usd += usd_value
                token_holdings[symbol] = {
                    "amount": amount,
                    "price": price,
                    "usd_value": usd_value,
                }
        except Exception as e:
            logger.warning(f"Could not value SPL tokens: {e}")

        return {
            "sol": sol_balance,
            "usdc": usdc_balance,
            "sol_usd_value": sol_usd,
            "token_holdings": token_holdings,
            "tokens_usd": tokens_usd,
            "total_usd": sol_usd + usdc_balance + tokens_usd,
        }

    async def close(self):
        await self._client.aclose()
