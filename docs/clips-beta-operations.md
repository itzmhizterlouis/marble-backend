# Clips beta operations

Clipping is disabled by default and does not change paid-plan entitlements. No new frontend environment variables are required.

1. Apply migration `0023_clipping_beta` and deploy both repositories with `CLIPPING_ENABLED=false`.
2. Set `STORAGE_BACKEND=r2`, existing R2 credentials and `VIZARD_API_KEY` in backend secrets. Use a paid Vizard workspace with sufficient minutes and permission to process your customers' videos. Confirm commercial usage/retention terms with Vizard before inviting customers.
3. Keep `VIZARD_CLIP_MODEL=clip_v1` initially. `clip_v2` reserves 1.25x credits. `CLIPPING_MONTHLY_CREDITS` is the shared UTC-month ceiling; default 600. This application reservation is deliberately conservative and may differ from provider rounding/refunds. It is not an invoice balance or a live Vizard quota reading.
4. Configure optional `VIZARD_CLEAN_TEMPLATE_ID` and `VIZARD_BOLD_TEMPLATE_ID` from the Vizard editor. Use 9:16 templates. Without a clean ID the standard provider template is used; Bold is only offered when its template ID exists. API B-roll remains off.
5. Set `CLIPPING_ENABLED=true`, enable the runtime Clips beta switch on `/admin/access`, then grant individual verified creators a source-minute allowance. Revoke a grant to remove access. Pause the runtime switch to stop new submissions; already accepted projects continue reconciling.
6. Verify the `clipping` worker and periodic reconciler are running. They use a dedicated queue and one worker slot, leaving publishing workers separate. Server polling is the fallback; the browser uses SSE and reload/window-focus recovery.
7. Optionally configure Vizard's completion webhook to `https://api.reverb.com.ng/v1/webhooks/vizard?token=<VIZARD_WEBHOOK_TOKEN>` using a newly generated secret. Treat this URL as a secret and redact its query in proxy/access logs. This callback cannot write results: it only schedules a fresh authenticated provider query. No undocumented Vizard HMAC scheme is assumed.
8. Run acceptance with an owned video: generate clips, reload during processing, preview a saved R2 output, create a draft twice (same post ID), publish one destination, schedule another clip, and verify both appear in Content. Confirm subtitle choices do not add a second burned-in subtitle layer.

## Submission recovery

The provider's submission API has no verified idempotency guarantee. Reverb stores a submitting state before sending and never automatically re-sends an uncertain POST. A timeout or interrupted response becomes `uncertain`. In Vizard find the project named `Reverb:<Reverb project UUID>`. On `/admin/access`, enter its numeric Vizard project ID to reconcile. The server verifies the name before adopting the ID. Never submit the source a second time merely because the first response was lost.

Partial output saves can be retried from Clips. This re-queries the existing Vizard project, retrieves fresh output URLs and skips clips already saved. It does not submit the long video again. Provider output URLs are temporary (documented seven-day lifetime); permanent playback is from Reverb's private R2 objects via short-lived signed URLs.

## Limits and retention

Sources are MP4/MOV, at most 2 GiB/60 minutes by default. Normal publishing limits remain 500 MiB/10 minutes; imports are additionally limited to three minutes per generated clip. Up to five clips are returned; zero matching clips is a valid completed outcome. User limits count source time; the shared provider budget includes the model multiplier. One active job per creator and shared submission pacing (3/minute, 20/hour) bound usage. Usage/project records remain after file deletion so removing media cannot reset allowances.

Unused sources expire after seven days. Unused outputs expire after 30 days; outputs referenced by posts remain available. The normal post deletion path schedules unused media cleanup. Uncertain projects retain their source pending owner review. Provider-side copies follow Vizard's own retention/deletion terms.

Live acceptance and a real Vizard quality/cost comparison are required before enabling customer grants. Automated CI uses HTTP contract fixtures and does not submit paid jobs.
