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
.venv/bin/celery -A app.tasks.celery_app worker --loglevel=INFO -Q celery
.venv/bin/celery -A app.tasks.celery_app worker --loglevel=INFO -Q subtitles --concurrency=1 --prefetch-multiplier=1 -n subtitles@%h
.venv/bin/celery -A app.tasks.celery_app beat --loglevel=INFO
```

SQLite is the zero-setup development default. Use PostgreSQL in staging and production.

## External service setup

- Upload-Post: set `UPLOAD_POST_API_KEY` and `UPLOAD_POST_WEBHOOK_SECRET`; configure the webhook URL as `https://<railway-host>/v1/webhooks/upload-post`.
- Brevo: set `BREVO_API_KEY`, a verified `BREVO_SENDER_EMAIL`, and optionally `BREVO_SENDER_NAME`. Terminal publishing failures are delivered from a deduplicated background outbox; set `PUBLISH_FAILURE_EMAILS_ENABLED=false` only if you need to pause those alerts. An hourly task emails creators about 24 hours before their 7-day Pro trial ends (once per trial); `TRIAL_REMINDER_EMAILS_ENABLED=false` pauses it.
- Google: create a web OAuth client and add `https://<railway-host>/v1/auth/google/callback` as an authorized redirect URI.
- Frontend: set `FRONTEND_URL` to the exact Vercel origin and `API_PUBLIC_URL` to the Railway origin.
- R2: set `STORAGE_BACKEND=r2`, `R2_ENDPOINT`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_REGION=auto`. The R2 token should be limited to Object Read & Write on this bucket only. Configure the private `reverb` bucket CORS for the exact `FRONTEND_URL`, `PUT`, the `Content-Type` request header, and the `ETag` exposed response header so browsers can complete signed parts. `R2_PRESIGN_TTL_SECONDS` controls signed-part expiry and `R2_MULTIPART_PART_SIZE_BYTES` must be at least 5 MiB.
- Paystack: create monthly NGN plans for Basic (₦10,000 / `1000000` kobo) and Pro (₦20,000 / `2000000` kobo), then set their plan codes, the secret key, and `PAYSTACK_CALLBACK_URL=https://reverb.com.ng/billing/callback`. Configure Paystack's webhook URL as `https://<railway-host>/v1/webhooks/paystack`; the backend verifies `x-paystack-signature` before storing each event for asynchronous processing.
- Gemini: enable billing for the Google AI project, create a server-side API key, and set `GEMINI_API_KEY`. `GEMINI_MODEL`, per-user fair-use limits, global daily ceilings, and the emergency `AI_ENABLED` switch are configurable. Videos are fetched from private R2 only inside the worker, deleted from Gemini immediately after processing, and never included in application logs.
- Admin: set `ADMIN_EMAILS` to a comma-separated list of verified Reverb user emails. Only those users can open `/admin/access` or grant/reset access.
- Referrals: deploy migration `0014_affiliate_referrals` with `AFFILIATE_ENABLED=false`. This generates codes for existing creators without back-crediting old payments. In Paystack test mode, enable Transfers, confirm the NGN balance can fund payouts, and test recipient verification, owner approval, OTP if enabled, transfer webhooks and failure reconciliation. Then set `AFFILIATE_ENABLED=true` on Railway. No new frontend secret or environment variable is needed. The backend owns the NGN transfer fee schedule in `app/affiliate.py`; update it when Paystack changes its prices before approving new payouts.
- Referral discount: migration `0016_referral_discount` adds a durable first-payment checkout. A verified creator referred at signup pays ₦8,000/₦16,000 for the first month only. Paystack receives a card-only transaction **without** a plan (otherwise it overrides the discounted amount). The billing worker then creates the existing full-price plan subscription with its first debit one calendar month after payment. The referrer still receives ₦2,000/₦4,000; commissions record both the actual payment and original-price basis. Duplicate checkout requests reuse the same reference and hosted URL; an unfinished discounted checkout is bound to the initially selected plan. Uncertain subscription creation is reconciled by provider subscription identity/webhooks, never blindly repeated. `/billing` surfaces renewal setup failures without asking for another payment. No additional plan codes or environment variables are required. Test this flow with test keys before deployment; do not replace production keys for testing.

R2 parts upload directly from the browser with bounded concurrency. Completing an upload returns `202` with media status `processing`; FFprobe, checksum generation, and thumbnail creation run in Celery, then emit a `media.updated` realtime event when the asset becomes `ready` or `failed`.

## Railway

Create one Railway project containing this service, PostgreSQL, and Redis. Keep `STORAGE_ROOT=/data` for temporary FFmpeg/provider materialization and legacy local media. With `STORAGE_BACKEND=r2`, new source videos and thumbnails are stored in the private R2 bucket and the volume is only a processing/compatibility fallback. The container supervises FastAPI, the publishing Celery worker, a separate single-concurrency subtitles worker, and Celery beat. Railway should provide `DATABASE_URL` and `REDIS_URL`; convert the database URL to the `postgresql+asyncpg://` scheme if needed.

Run `alembic upgrade head` as part of startup (already configured in `supervisord.conf`). Keep Upload-Post, Brevo, Google, JWT, and webhook secrets only in Railway variables.

## Paid-feature rollout

Deploy this migration and backend with `BILLING_ENFORCEMENT_ENABLED=false`. This preserves existing access while the migration grants every existing verified creator one complimentary Basic month and records prior publishers as having consumed the one-post preview.

Before enabling enforcement:

1. Configure both Paystack plan codes, callback URL and webhook in test mode.
2. Set `GEMINI_API_KEY`, `ADMIN_EMAILS`, and suitable global AI daily ceilings.
3. Verify one Basic checkout, one Pro checkout, a signed webhook, cancellation, an admin grant, analytics refresh and one AI generation.
4. Deploy the frontend and run an analytics backfill by activating Pro or invoking the analytics refresh worker.
5. Set `BILLING_ENFORCEMENT_ENABLED=true` and redeploy the backend.

Paystack, Gemini, Groq and Upload-Post secrets are backend-only. The frontend requires no new billing or AI secret variables. Upload-Post Professional currently limits this release to 25 creator profiles, so monitor provider capacity during the beta.

## Verification

```bash
.venv/bin/ruff check app tests
.venv/bin/pytest -q
```

API errors consistently use `{ code, message, field_errors?, request_id }`. Swagger documentation is available at `/docs` outside any proxy path.

## Auto subtitles

Deploy migration `0018_auto_subtitles`. Set the backend-only `GROQ_API_KEY` (Groq Whisper Large V3 Turbo). `SUBTITLES_ENABLED=false` pauses new subtitle jobs; `SUBTITLES_DAILY_LIMIT` defaults to 20 render attempts per creator per rolling day, with at most two active jobs per creator. No frontend key is needed.

The Docker image supplies FFmpeg/libass and Noto fonts. Local FFmpeg must include the `ass` filter. The dedicated `subtitles` queue keeps rendering out of the publishing worker; FFmpeg uses two encoding threads. For increased volume, move this queue to a separate Railway worker using the same database, Redis, R2 and Groq settings (disable the bundled subtitles supervisor program then).

Jobs save word timing before rendering. Corrections create immutable video versions, and drafts explicitly select a version. Publishing waits for readiness; immediate posts, schedules and targeted retries resolve the same selection. A failed render can reuse its transcript. Preview URLs are signed; derived videos expire with their source media, while active schedules and subtitle jobs protect retention. Queue reconciliation recovers missed deliveries and exposes interrupted jobs for retry after 40 minutes. No customer audio, subtitle text or provider error bodies are logged.

### Analytics refreshes

Analytics runs on the dedicated `analytics` Celery queue (one worker process, prefetch 1); publishing stays on `celery`. All processes sharing an Upload-Post key use Redis request pacing (one analytics call per four seconds) and a shared cooldown from `Retry-After` / `X-RateLimit-Reset`. Requests pause before exhausting the reported general allowance. A 429 defers the job rather than recording failed metrics for every remaining post.

Refreshes coalesce per creator, including delayed publication triggers, and completed refreshes have a 15-minute cooldown. Completed snapshots are committed and skipped on retries. Queue reservations survive worker restarts; an expiring execution lease prevents overlapping work during rolling deploys. Redis outages defer analytics instead of issuing unpaced requests.

Home and Insights retain previously reported actual views during temporary refresh failures, with the original capture timestamp. Hidden/deleted posts, access failures, and posts without actual views remain excluded. No reach/impression values are substituted. Runtime coordination tests use a disposable local `redis-server` and never production Redis.
