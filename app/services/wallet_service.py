"""Wallet management - secure key loading and balance queries."""

import base64
import logging
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

    def __init__(self):
        self.rpc_url = get("wallet", "rpc_url", "https://api.mainnet-beta.solana.com")
        self._client = httpx.AsyncClient(timeout=15)
        self._keypair: Optional[Keypair] = None

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

    async def get_balance_sol(self) -> float:
        """Get wallet balance in SOL."""
        lamports = await self.get_balance_lamports()
        return lamports / 1_000_000_000

    async def get_usdc_balance(self) -> float:
        """Get USDC SPL token balance (6 decimals)."""
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
            return total
        except Exception as e:
            logger.warning(f"Could not fetch USDC balance: {e}")
            return 0.0

    async def get_total_usd_balance(self, sol_price: float) -> dict:
        """Get total portfolio value: SOL + USDC combined."""
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
        return {
            "sol": sol_balance,
            "usdc": usdc_balance,
            "sol_usd_value": sol_usd,
            "total_usd": sol_usd + usdc_balance,
        }

    async def close(self):
        await self._client.aclose()
