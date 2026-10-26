# Reverb Clips Beta Implementation Plan

> **For agentic workers:** Execute inline with superpowers:executing-plans. Track tasks below.

**Goal:** Add a user-gated Vizard clipping workspace with direct Reverb publishing.

**Architecture:** A dedicated clipping worker submits durable projects and imports outputs to R2. Authenticated API routes own access, quotas and draft deduplication. The frontend uses the existing upload helper, validation, SSE and publishing endpoints.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, HTTPX, Celery/Redis, R2, React/TypeScript and React Query. No new dependencies.

**Spec:** ../specs/2026-10-26-clips-beta-design.md

## Global constraints

- Preserve unrelated account-connection changes in both working trees.
- No local Python, installs, production deploy or production provider charges.
- Global feature disabled by default; selected-user grants required.
- Sources: 2 GiB/3600 seconds; ordinary posts: existing 500 MiB/600 seconds.
- Real data only; all publication and caption safeguards remain enforced.

## Review focus

- A timeout after provider acceptance must not trigger another paid submission.
- Revoked or foreign users must not mutate/access another creator's assets.
- Concurrent quota reservations and duplicate draft actions must remain atomic.
- Partial imports, expired links and worker restarts must preserve completed work.
- SSE loss, drawer reopen and reload must restore durable state without republishing.

## Tasks

- [ ] 1. Add failing backend route/adapter/worker tests in `tests/test_clipping.py`; run them in approved isolated CI.
- [ ] 2. Add `ClippingAccess`, `ClippingProject`, `GeneratedClip`, source-purpose and baked-subtitle metadata in `app/models.py`, with migration `0023_clipping_beta.py` and configuration defaults.
- [ ] 3. Implement Vizard contracts in `app/vizard.py`, permission/quota/project/draft APIs in `app/clipping.py`, and durable worker/reconciliation in `app/clipping_worker.py`. Extend R2 signing, media purpose validation, source retention and routing; register worker/beat/router. Tests cover access, budgets, duplicate requests/drafts, uncertain submission, authenticated retrieval and bounded imports.
- [ ] 4. Add failing UI tests, then `src/clipping.tsx` and `src/clipping.test.tsx`: upload and restored projects, quiet clip gallery, embedded composer, validation and status/retry links. Extend API/types, gated navigation, private SEO route and SSE invalidation. Add owner grant/global controls via a separate admin component.
- [ ] 5. Update privacy/subprocessors, environment examples and operational notes. Run frontend typecheck, suite/build/Sites checks and complete backend CI plus migration checks. Review feature guards, media retention and retries before final reporting.

Native execution is authorized by the user's request to implement the agreed flow. No further design approval is required for the bounded defaults above.
