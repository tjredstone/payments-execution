# Advisory Data Retention Policy (Local)

This project keeps data only as long as needed for advisory computation and validation.

## Default Retention Matrix

| Data set | Table | Default retention | Control env var |
|---|---|---:|---|
| OAuth state | `oauth_states` | Immediate after consume/expiry | automatic (`purge_expired_oauth_states`) |
| Transactions | `transactions` | 180 days | `TX_RETENTION_DAYS` |
| Balance snapshots | `balance_snapshots` | 180 days | `BALANCE_RETENTION_DAYS` |
| Direct debits | `direct_debits` | 180 days | `DIRECT_DEBIT_RETENTION_DAYS` |
| Standing orders | `standing_orders` | 180 days | `STANDING_ORDER_RETENTION_DAYS` |
| Advisory history | `advisory_log` | 365 days | `ADVISORY_RETENTION_DAYS` |
| Raw payload JSON (`raw_json` / `payload_json`) | multiple tables | 30 days | `RAW_PAYLOAD_RETENTION_DAYS` |

## Scrubbing Behavior

Before deletion, old raw provider payloads are scrubbed to `{}` once they exceed `RAW_PAYLOAD_RETENTION_DAYS`.
This preserves core normalized fields while minimizing stored sensitive detail.

## Where Enforced

`advisory/run_daily.py` calls `purge_old_data(...)` on each run, so retention enforcement is automatic.
