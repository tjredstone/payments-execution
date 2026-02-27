# Supabase Postgres Setup

This project uses Supabase Postgres as the only database backend.

## 1. Install dependencies

```bash
pip install -r advisory/requirements.txt
```

## 2. Configure environment

Use Render/Supabase environment secrets (or local `.env` for testing):

```bash
DB_BACKEND=postgres
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<db>?sslmode=require
```

Notes:
- `sslmode=require` is mandatory for secure transport.
- Prefer Supabase pooled connection string for app workloads.

## 3. Keep existing app secrets

You still need:
- `TOKENS_ENCRYPTION_KEY`
- `APP_SESSION_SECRET`
- TrueLayer secrets (`TRUELAYER_CLIENT_ID`, `TRUELAYER_CLIENT_SECRET`, `TRUELAYER_REDIRECT_URI`)

## 4. Initialize schema

Schema is created automatically by app startup (`init_db()`), `run_daily.py`, and related scripts.

For least-privilege operation:
- Temporarily allow schema create for bootstrap.
- Run one startup/smoke test to create tables.
- Revoke schema create.
- Set `DB_SKIP_SCHEMA_INIT=1` for normal runtime.

## 5. Security recommendations

- Use a dedicated least-privilege DB role for the app (not owner/superuser).
- Restrict database access to your app environment only.
- Rotate DB credentials on a schedule.
- Keep row-level tenant scoping in application logic (already implemented via session user IDs).
- Use a separate migration/bootstrap role if possible.

## 6. Local fallback

No SQLite fallback is used in this deployment. Use a separate Supabase project for local/test environments if needed.
