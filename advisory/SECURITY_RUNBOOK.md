# Security Runbook (Local Pilot)

## 1. Pre-run security check

Run:

```bash
python advisory/security_check.py
```

Address any `failed` checks before connecting real accounts.

## 2. Encryption key rotation

1. Generate a new Fernet key.
2. Set:
   - `OLD_TOKENS_ENCRYPTION_KEY=<current key>`
   - `TOKENS_ENCRYPTION_KEY=<new key>`
3. Run:

```bash
python advisory/rotate_encryption_key.py
```

By default, this creates a timestamped DB backup before rotation.

## 3. Key rotation cadence

- Local pilot minimum: rotate every 90 days.
- Immediately rotate if any secret exposure is suspected.

## 4. Post-rotation verification

Run:

```bash
python advisory/run_daily.py
python advisory/bank_normalise.py --summary-only
python advisory/engine.py
```

Ensure these complete without decryption/token errors.
