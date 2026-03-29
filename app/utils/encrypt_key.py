"""Utility to encrypt a Phantom wallet private key for secure storage."""

import getpass
import sys

from app.services.wallet_service import encrypt_private_key


def main():
    print("=== Wallet Private Key Encryption Tool ===")
    print("This tool encrypts your Solana private key for safe storage in config.yaml.\n")
    print("WARNING: Never share your private key or the encryption password.")
    print("Use a dedicated trading wallet with limited funds.\n")

    private_key = getpass.getpass("Enter your base58 private key: ")
    if not private_key or len(private_key) < 32:
        print("Error: Invalid private key.")
        sys.exit(1)

    password = getpass.getpass("Enter encryption password: ")
    confirm = getpass.getpass("Confirm password: ")

    if password != confirm:
        print("Error: Passwords do not match.")
        sys.exit(1)

    encrypted = encrypt_private_key(private_key, password)

    print("\n=== Encrypted Key (copy to config.yaml -> wallet.encrypted_private_key) ===")
    print(encrypted)
    print("\n=== Add to .env ===")
    print(f"WALLET_ENCRYPTION_PASSWORD={password}")
    print("\nDone. Keep your .env file secure and never commit it to git.")


if __name__ == "__main__":
    main()
