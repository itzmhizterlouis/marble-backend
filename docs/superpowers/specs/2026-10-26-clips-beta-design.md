# Reverb Clips private beta

Approved flow: a separate Clips section, available only to selected verified users, accepts a long video and generates reviewable Vizard clips. A clip opens a publishing panel in the workspace (full-screen on mobile), with connected destinations, captions, required titles and now/shared scheduling. Publishing uses Reverb's existing backend and content validators; each clip becomes one ordinary post. No batch publishing in this release.

## Approach

Use Vizard's clipping API behind a small replaceable adapter. Reuse R2 multipart uploads, Post records, Upload-Post delivery and SSE. An embedded composer keeps the creator with their generated clips; routing directly into Prepare was considered but would interrupt review of multiple clips. A custom clipping engine was considered but requires substantially more editing and framing work.

## Builder defaults

- File uploads only; MP4/MOV, 2 GiB and 60 minutes for clipping sources. Existing publishing limits stay 500 MiB/10 minutes.
- One provider project per source upload; repeated creation returns that project. One active job per creator.
- Preferred clip lengths 30–60 or 60–90 seconds; maximum five results (fewer or none are valid).
- Start with the standard v1 model. Model and template IDs are server configuration; v2 reserves 1.25x credits. Optional topic instructions, subtitles and silence removal. B-roll remains disabled until an owner approves its quality and costs.
- Default user allowance: 60 source minutes per UTC month. Global default budget: 600 V1-equivalent credits per UTC month. Reserve before submission, retain reservations for uncertain/accepted requests, release only before external submission or on a definitive rejection. Deleted/failed projects never reset consumed usage.
- New jobs require environment enablement, an owner-controlled global flag and an individual grant. Every route checks ownership; all mutations check the grant. Turning the flag off pauses new provider submissions, while accepted work can reconcile and existing published posts remain normal posts.
- Clipping sources expire seven days after completion; output clips after 30 days unless referenced by a draft, schedule or publication. Project/usage records persist. Vizard processing is disclosed before submission and on the public privacy/service-provider pages.

## Reliability and boundaries

Persist submitting state before the external POST and the provider project ID immediately after acceptance. Never automatically repeat a submission whose acceptance is uncertain. Owners can reconcile such jobs using a matching Vizard project ID/name. Use authenticated status queries as the source of truth; a callback only wakes reconciliation. Poll in a dedicated worker with backoff and bounded timeouts. Persist all state before SSE; reload/reconnect refetches authoritative state.

Copy outputs into private R2 storage with bounded downloads, host validation, actual ffprobe validation and thumbnails. Temporary provider links are never permanent playback URLs. Source links are signed immediately before submission with a six-hour lifetime. No API key, signed source URL, provider error body or unrelated creator data appears in public responses/logs.

Reopening Post clip returns the same draft; opening an already submitted clip goes to its existing post. Existing backend entitlement, caption, title, scheduling and per-platform retry rules remain authoritative. No automatic publication, automatic AI copy generation or duplicate subtitle rendering.

## Acceptance

Disabled/ungranted users cannot access jobs or long uploads. Concurrent creation cannot exceed allowances or submit twice. Accepted/uncertain jobs survive worker restarts without resubmission. Provider errors and zero results are explicit. Invalid/oversized outputs cannot publish. A clip creates one draft, publishes/schedules through existing endpoints, appears in Content and reflects per-destination status/retry. Navigation, desktop panel, mobile composer and all controls are accessible and responsive. No new local dependencies or local Python execution under SentinelOne; backend verification runs in approved CI.
