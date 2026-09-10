# Reverb backend

Production API for Reverb's authentication, social connections, resumable video uploads, immediate publishing, shared-time scheduling, per-platform status, and retries.

## Local development

Requirements: Python 3.12+, PostgreSQL, Redis, FFmpeg and FFprobe.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Run the worker and scheduler separately during local development:

```bash
.venv/bin/celery -A app.tasks.celery_app worker --loglevel=INFO
.venv/bin/celery -A app.tasks.celery_app beat --loglevel=INFO
```

SQLite is the zero-setup development default. Use PostgreSQL in staging and production.

## External service setup

- Upload-Post: set `UPLOAD_POST_API_KEY` and `UPLOAD_POST_WEBHOOK_SECRET`; configure the webhook URL as `https://<railway-host>/v1/webhooks/upload-post`.
- Brevo: set `BREVO_API_KEY`, a verified `BREVO_SENDER_EMAIL`, and optionally `BREVO_SENDER_NAME`. Terminal publishing failures are delivered from a deduplicated background outbox; set `PUBLISH_FAILURE_EMAILS_ENABLED=false` only if you need to pause those alerts.
- Google: create a web OAuth client and add `https://<railway-host>/v1/auth/google/callback` as an authorized redirect URI.
- Frontend: set `FRONTEND_URL` to the exact Vercel origin and `API_PUBLIC_URL` to the Railway origin.

## Railway

Create one Railway project containing this service, PostgreSQL, and Redis. Attach a persistent volume at `/data`, set `STORAGE_ROOT=/data`, and keep this application service at one replica because the media volume is not replicated. The container supervises FastAPI, one Celery worker, and Celery beat. Railway should provide `DATABASE_URL` and `REDIS_URL`; convert the database URL to the `postgresql+asyncpg://` scheme if needed.

Run `alembic upgrade head` as part of startup (already configured in `supervisord.conf`). Keep Upload-Post, Brevo, Google, JWT, and webhook secrets only in Railway variables.

## Verification

```bash
.venv/bin/ruff check app tests
.venv/bin/pytest -q
```

API errors consistently use `{ code, message, field_errors?, request_id }`. Swagger documentation is available at `/docs` outside any proxy path.
