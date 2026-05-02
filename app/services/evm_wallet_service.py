"""EVM wallet management — secure key loading + balance queries on Arbitrum.

Mirrors `wallet_service.py` (Solana) for the EVM execution lane. Phase 1 of
the 1inch integration: read-only setup. Transaction signing/sending is added
in Phase 2.
"""

import base64
import logging
import time
from typing import Optional

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from eth_account import Account
from eth_account.signers.local import LocalAccount

from app.config import get, get_env

logger = logging.getLogger("bot.evm_wallet")

# Default Arbitrum One config — overridable via config.yaml evm_wallet section.
DEFAULT_RPC_URL = "https://arb1.arbitrum.io/rpc"
DEFAULT_CHAIN_ID = 42161

# Common ERC20 token contracts on Arbitrum One (canonical addresses).
# Used for balance queries; trading routes resolved separately via 1inch.
ARBITRUM_TOKENS = {
    "USDC":  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # native USDC (Circle)
    "USDT":  "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    "WETH":  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # wrapped ETH
    "ARB":   "0x912CE59144191C1204E64559FE8253a0e49E6548",
    "LINK":  "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
    "UNI":   "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0",
    "AAVE":  "0xba5DdD1f9d7F570dc94a51479a000E3BCE967196",
    "LDO":   "0x13Ad51ed4F1B7e9Dc168d8a00cB3f4dDD85EfA60",
    "COMP":  "0x354A6dA3fcde098F8389cad84b0182725c6C91dE",
    "MKR":   "0x2e9a6Df78E42a30712c10a9Dc4b1C8656f8F2879",
    "INJ":   "0x97ad75064b20fb2B2447feD4fa953bF7F007a706",
}

# ERC20 balanceOf(address) selector — first 4 bytes of keccak256("balanceOf(address)")
ERC20_BALANCE_OF_SELECTOR = "0x70a08231"


def _derive_key(password: str) -> bytes:
    """Derive encryption key from password using PBKDF2.

    Uses the same salt + iteration count as wallet_service._derive_key so a single
    encryption password unlocks both Solana and EVM keys (operational simplicity).
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"tradingview-bot-salt-v1",
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def decrypt_evm_private_key() -> LocalAccount:
    """Decrypt the EVM private key from config.yaml and return an eth_account LocalAccount."""
    cfg = get("evm_wallet") or {}
    encrypted = cfg.get("encrypted_private_key", "")
    if not encrypted:
        raise ValueError("No encrypted_private_key in config.yaml under evm_wallet")

    password_env = cfg.get("encryption_password_env", "WALLET_ENCRYPTION_PASSWORD")
    password = get_env(password_env)
    if not password:
        raise ValueError(f"Env var {password_env} not set. Cannot decrypt EVM wallet key.")

    key = _derive_key(password)
    f = Fernet(key)
    decrypted = f.decrypt(encrypted.encode()).decode()
    # Stored as 0x-prefixed hex; eth_account accepts both with and without prefix.
    return Account.from_key(decrypted)


def encrypt_evm_private_key(private_key_hex: str, password: str) -> str:
    """Encrypt a hex private key (with or without 0x prefix) for storage in config.yaml."""
    pk = private_key_hex if private_key_hex.startswith("0x") else "0x" + private_key_hex
    if len(pk) != 66:
        raise ValueError(f"Invalid private key length: expected 66 hex chars (0x + 64), got {len(pk)}")
    key = _derive_key(password)
    f = Fernet(key)
    return f.encrypt(pk.encode()).decode()


class EVMWalletService:
    """Query EVM wallet balances on Arbitrum (or other EVM chain). Read-only in Phase 1.

    Uses raw JSON-RPC over httpx to keep dependencies tight (no full web3.py
    Web3 instance for simple eth_getBalance / eth_call). Phase 2 will add
    web3.Web3 for transaction signing + nonce handling.
    """

    _CACHE_TTL = 15  # seconds — match Solana wallet TTL

    def __init__(self):
        cfg = get("evm_wallet") or {}
        self.rpc_url = cfg.get("rpc_url", DEFAULT_RPC_URL)
        self.chain_id = int(cfg.get("chain_id", DEFAULT_CHAIN_ID))
        self._client = httpx.AsyncClient(timeout=15)
        self._account: Optional[LocalAccount] = None
        self._cache: dict[str, tuple[float, any]] = {}

    def get_account(self) -> LocalAccount:
        if self._account is None:
            self._account = decrypt_evm_private_key()
        return self._account

    @property
    def address(self) -> str:
        return self.get_account().address

    async def _rpc(self, method: str, params: list) -> dict:
        resp = await self._client.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        return resp.json()

    def _get_cached(self, key: str) -> Optional[float]:
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < self._CACHE_TTL:
                return val
        return None

    def _set_cached(self, key: str, val: float) -> None:
        self._cache[key] = (time.time(), val)

    def invalidate_cache(self) -> None:
        self._cache.clear()

    async def get_eth_balance_wei(self) -> int:
        """Native ETH balance (gas token on Arbitrum) in wei."""
        data = await self._rpc("eth_getBalance", [self.address, "latest"])
        return int(data["result"], 16)

    async def get_eth_balance(self) -> float:
        """Native ETH balance in ETH (cached for 15s)."""
        cached = self._get_cached("eth")
        if cached is not None:
            return cached
        wei = await self.get_eth_balance_wei()
        eth = wei / 1e18
        self._set_cached("eth", eth)
        return eth

    async def get_erc20_balance(self, contract_addr: str, decimals: int = 18) -> float:
        """Read ERC20 balanceOf(address) for the wallet via eth_call."""
        # Build calldata: balanceOf selector + 32-byte zero-padded address
        addr_clean = self.address.lower().removeprefix("0x").rjust(64, "0")
        calldata = ERC20_BALANCE_OF_SELECTOR + addr_clean
        data = await self._rpc("eth_call", [
            {"to": contract_addr, "data": calldata}, "latest",
        ])
        result_hex = data.get("result", "0x0")
        if not result_hex or result_hex == "0x":
            return 0.0
        raw = int(result_hex, 16)
        return raw / (10 ** decimals)

    async def get_usdc_balance(self) -> float:
        """USDC balance on Arbitrum (6 decimals)."""
        cached = self._get_cached("usdc")
        if cached is not None:
            return cached
        bal = await self.get_erc20_balance(ARBITRUM_TOKENS["USDC"], decimals=6)
        self._set_cached("usdc", bal)
        return bal

    async def get_chain_id(self) -> int:
        """Verify RPC is on the expected chain."""
        data = await self._rpc("eth_chainId", [])
        return int(data["result"], 16)

    async def get_block_number(self) -> int:
        """Sanity check: current block number on the chain."""
        data = await self._rpc("eth_blockNumber", [])
        return int(data["result"], 16)

    async def close(self) -> None:
        await self._client.aclose()
