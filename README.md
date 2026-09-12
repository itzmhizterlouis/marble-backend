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
- R2: set `STORAGE_BACKEND=r2`, `R2_ENDPOINT`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_REGION=auto`. The R2 token should be limited to Object Read & Write on this bucket only. Configure the private `reverb` bucket CORS for the exact `FRONTEND_URL`, `PUT`, the `Content-Type` request header, and the `ETag` exposed response header so browsers can complete signed parts. `R2_PRESIGN_TTL_SECONDS` controls signed-part expiry and `R2_MULTIPART_PART_SIZE_BYTES` must be at least 5 MiB.
- Paystack: create monthly NGN plans for Basic (₦10,000 / `1000000` kobo) and Pro (₦20,000 / `2000000` kobo), then set their plan codes, the secret key, and `PAYSTACK_CALLBACK_URL=https://reverb.com.ng/billing/callback`. Configure Paystack's webhook URL as `https://<railway-host>/v1/webhooks/paystack`; the backend verifies `x-paystack-signature` before storing each event for asynchronous processing.
- Gemini: enable billing for the Google AI project, create a server-side API key, and set `GEMINI_API_KEY`. `GEMINI_MODEL`, per-user fair-use limits, global daily ceilings, and the emergency `AI_ENABLED` switch are configurable. Videos are fetched from private R2 only inside the worker, deleted from Gemini immediately after processing, and never included in application logs.
- Admin: set `ADMIN_EMAILS` to a comma-separated list of verified Reverb user emails. Only those users can open `/admin/access` or grant/reset access.

R2 parts upload directly from the browser with bounded concurrency. Completing an upload returns `202` with media status `processing`; FFprobe, checksum generation, and thumbnail creation run in Celery, then emit a `media.updated` realtime event when the asset becomes `ready` or `failed`.

## Railway

Create one Railway project containing this service, PostgreSQL, and Redis. Keep `STORAGE_ROOT=/data` for temporary FFmpeg/provider materialization and legacy local media. With `STORAGE_BACKEND=r2`, new source videos and thumbnails are stored in the private R2 bucket and the volume is only a processing/compatibility fallback. The container supervises FastAPI, one Celery worker, and Celery beat. Railway should provide `DATABASE_URL` and `REDIS_URL`; convert the database URL to the `postgresql+asyncpg://` scheme if needed.

Run `alembic upgrade head` as part of startup (already configured in `supervisord.conf`). Keep Upload-Post, Brevo, Google, JWT, and webhook secrets only in Railway variables.

## Paid-feature rollout

Deploy this migration and backend with `BILLING_ENFORCEMENT_ENABLED=false`. This preserves existing access while the migration grants every existing verified creator one complimentary Basic month and records prior publishers as having consumed the one-post preview.

Before enabling enforcement:

1. Configure both Paystack plan codes, callback URL and webhook in test mode.
2. Set `GEMINI_API_KEY`, `ADMIN_EMAILS`, and suitable global AI daily ceilings.
3. Verify one Basic checkout, one Pro checkout, a signed webhook, cancellation, an admin grant, analytics refresh and one AI generation.
4. Deploy the frontend and run an analytics backfill by activating Pro or invoking the analytics refresh worker.
5. Set `BILLING_ENFORCEMENT_ENABLED=true` and redeploy the backend.

Paystack, Gemini and Upload-Post secrets are backend-only. The frontend requires no new billing or AI secret variables. Upload-Post Professional currently limits this release to 25 creator profiles, so monitor provider capacity during the beta.

## Verification

```bash
.venv/bin/ruff check app tests
.venv/bin/pytest -q
```

API errors consistently use `{ code, message, field_errors?, request_id }`. Swagger documentation is available at `/docs` outside any proxy path.
