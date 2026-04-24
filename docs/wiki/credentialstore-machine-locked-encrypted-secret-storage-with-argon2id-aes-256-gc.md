---
{
  "title": "CredentialStore: Machine-Locked Encrypted Secret Storage with Argon2id + AES-256-GCM",
  "summary": "`CredentialStore` encrypts API keys and OAuth tokens to `~/.pocketpaw/secrets.enc` using a key derived from machine identity via Argon2id, ensuring the encrypted file is only usable on the same machine and user account. Version 2 upgraded from Fernet + PBKDF2 to AES-256-GCM + Argon2id with automatic migration and AEAD authentication.",
  "concepts": [
    "CredentialStore",
    "Argon2id",
    "AES-256-GCM",
    "AEAD",
    "PBKDF2",
    "Fernet",
    "machine identity",
    "encrypted secrets",
    "v1 to v2 migration",
    "SECRET_FIELDS",
    "file permissions"
  ],
  "categories": [
    "security",
    "credentials",
    "encryption"
  ],
  "source_docs": [
    "8bbe74aa07559ff1"
  ],
  "backlinks": null,
  "word_count": 496,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`credentials.py` provides encrypted at-rest storage for PocketPaw's API keys, bot tokens, and OAuth secrets. Instead of writing plaintext values to `config.json`, the `CredentialStore` derives a machine-specific encryption key and stores secrets in `~/.pocketpaw/secrets.enc`. The design goal is that even if the encrypted file is copied to another machine, it cannot be decrypted.

## Machine Identity Key Derivation

The encryption key is derived from a combination of machine-specific identifiers:

1. **macOS**: `IOPlatformUUID` from `ioreg` — a hardware UUID stable across reboots but unique per machine
2. **Linux**: `/etc/machine-id` or `/var/lib/dbus/machine-id` — systemd/dbus stable identifiers
3. **Fallback**: `platform.node()` (hostname)

`uuid.getnode()` is explicitly avoided because it returns a random MAC on systems without a discoverable NIC, producing a different value on every process start — which would make the derived key non-deterministic.

CI environments are short-circuited with a fixed `"CI_ENVIRONMENT_ID"` constant to prevent ioreg calls in GitHub Actions.

## v1 → v2 Migration

The store supports two encryption formats:

| Version | KDF | Cipher | File marker |
|---|---|---|---|
| v1 | PBKDF2-HMAC-SHA256 | Fernet (AES-128-CBC + HMAC-SHA256) | no header |
| v2 | Argon2id | AES-256-GCM (AEAD) | `PAW` magic bytes |

On first read, the store checks for the `PAW` header. If absent, it treats the file as v1, decrypts with Fernet, re-encrypts with AES-256-GCM, and writes the v2 file atomically. A backup of the v1 file is preserved alongside the new file.

AES-256-GCM provides **Authenticated Encryption with Associated Data (AEAD)** — the ciphertext is authenticated, so any tampering with the file is detected on decryption rather than producing corrupted data silently.

## Argon2id Parameters

Argon2id is a memory-hard password hashing function that resists GPU and ASIC brute-force attacks. It replaced PBKDF2 (v1) because PBKDF2-SHA256 can be accelerated on modern GPUs to billions of iterations per second; Argon2id's memory hardness makes such attacks impractical.

## File Permissions

```python
def _ensure_permissions(path: Path, mode: int = 0o600) -> None:
    path.chmod(mode)

def _ensure_dir_permissions(path: Path) -> None:
    _ensure_permissions(path, mode=0o700)
```

Both the secrets file (`0o600`, owner read/write) and its parent directory (`0o700`, owner rwx) are chmod'd after every write. This prevents other users on a shared system from reading the encrypted file even if they have access to the home directory.

## `SECRET_FIELDS` Allowlist

```python
SECRET_FIELDS: frozenset[str] = frozenset({
    "telegram_bot_token", "openai_api_key", "anthropic_api_key", ...
})
```

Only fields in `SECRET_FIELDS` are routed to the encrypted store. Non-secret config values (e.g., model names, timeouts) stay in plaintext `config.json`. This separation prevents accidental encryption of non-sensitive data while ensuring all known secret field names are explicitly enumerated.

## Known Gaps

- **Machine-identity binding has limits**: If a user restores from a Time Machine backup to a different Mac, the hardware UUID changes and credentials become unrecoverable — there is no key escrow or recovery mechanism.
- **Salt stored alongside ciphertext**: The `.salt` file is in the same directory as `secrets.enc`. An attacker with filesystem access has both and can attempt offline Argon2id attacks (though the memory-hardness substantially raises the cost).
