"""Generate or import an EVM private key, encrypt it for config.yaml storage.

Two modes:
  1. Generate fresh keypair (recommended for a clean dedicated bot wallet)
  2. Import existing private key (if you already have an EVM wallet you want to use)
"""

import getpass
import sys

from eth_account import Account

from app.services.evm_wallet_service import encrypt_evm_private_key


def main():
    print("=== EVM Wallet Key Generator / Encryptor ===")
    print("This tool prepares an EVM (Ethereum/Arbitrum/Base/etc.) keypair for")
    print("encrypted storage in config.yaml under the `evm_wallet` section.\n")
    print("WARNING: Use a dedicated trading wallet with limited funds.")
    print("WARNING: Never share your private key or the encryption password.\n")

    mode = input("Choose: [g]enerate new keypair, or [i]mport existing key? [g/i]: ").strip().lower()

    if mode == "g":
        # Generate fresh — eth_account uses os.urandom internally.
        Account.enable_unaudited_hdwallet_features()
        acct, mnemonic = Account.create_with_mnemonic()
        print(f"\n✓ Generated new EVM keypair.")
        print(f"  Address:  {acct.address}")
        print(f"  Mnemonic: {mnemonic}")
        print(f"  Private key: {acct.key.hex()}")
        print()
        print("⚠ SAVE THE MNEMONIC SECURELY (paper, password manager, hardware key).")
        print("⚠ The mnemonic is the ONLY way to recover this key if you lose it.")
        print("⚠ The bot only needs the encrypted private key (below) — discard the")
        print("  plaintext key and mnemonic from your screen/clipboard once stored safely.\n")
        confirm = input("Type 'I HAVE SAVED THE MNEMONIC' to continue with encryption: ").strip()
        if confirm != "I HAVE SAVED THE MNEMONIC":
            print("Aborted.")
            sys.exit(1)
        private_key = acct.key.hex()
    elif mode == "i":
        private_key = getpass.getpass("Enter existing EVM private key (hex, with or without 0x): ")
        if not private_key:
            print("Error: no private key provided.")
            sys.exit(1)
        # Verify it parses
        try:
            acct = Account.from_key(private_key)
            print(f"✓ Imported address: {acct.address}")
        except Exception as e:
            print(f"Error: invalid private key — {e}")
            sys.exit(1)
    else:
        print("Aborted — choose 'g' or 'i'.")
        sys.exit(1)

    print()
    password = getpass.getpass("Enter encryption password (use the same one as your Solana wallet for ops simplicity, or a new one): ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: passwords do not match.")
        sys.exit(1)

    encrypted = encrypt_evm_private_key(private_key, password)

    print()
    print("=" * 70)
    print("ADD THIS TO config.yaml:")
    print("=" * 70)
    print(f"""
evm_wallet:
  encrypted_private_key: "{encrypted}"
  encryption_password_env: WALLET_ENCRYPTION_PASSWORD
  rpc_url: https://arb1.arbitrum.io/rpc
  chain_id: 42161  # Arbitrum One
  address: "{acct.address}"  # informational; address is derived from key on load
""")
    print("=" * 70)
    print(f"Wallet address: {acct.address}")
    print()
    print("Next: bridge $50-100 USDC to this address on Arbitrum One via")
    print("Wormhole, native bridge, or any centralized exchange withdrawal.")
    print("Verify balance with:")
    print("  venv/bin/python -c \"import asyncio; from app.services.evm_wallet_service import EVMWalletService; asyncio.run(EVMWalletService().get_usdc_balance())\"")


if __name__ == "__main__":
    main()
