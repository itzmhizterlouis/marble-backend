from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import UTC, date, timedelta
from statistics import median
from typing import Any

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import get_settings
from .database import get_db
from .deps import get_current_user
from .entitlements import require_capability
from .models import (
    AccountAnalyticsSnapshot,
    AnalyticsPeriodSnapshot,
    GeneratedInsight,
    Post,
    Publication,
    PublicationMetricSnapshot,
    SocialConnection,
    User,
    utcnow,
)
from .providers import ProviderError, UploadPostClient

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)
PRIMARY_METRICS = {
    "tiktok": (("views", "video_views", "play_count"), "Video views"),
    "instagram": (("reach", "views", "impressions", "plays"), "Accounts reached"),
    "youtube": (("views", "view_count"), "Views"),
    "facebook": (("reach", "views", "impressions", "post_impressions"), "People reached"),
}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


def flatten_metrics(payload: Any, prefix: str = "") -> dict[str, int | float]:
    output: dict[str, int | float] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            number = _number(value)
            if number is not None:
                output[str(key).lower()] = number
                output[path.lower()] = number
            elif isinstance(value, (dict, list)):
                output.update(flatten_metrics(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            output.update(flatten_metrics(value, f"{prefix}.{index}"))
    return output


def normalize_metrics(platform: str, payload: dict) -> tuple[dict, str | None, str | None]:
    metric_payload = payload.get("post_metrics") if isinstance(payload.get("post_metrics"), dict) else payload
    flattened = flatten_metrics(metric_payload)
    candidates, label = PRIMARY_METRICS.get(platform, (("views", "reach", "impressions"), "Exposure"))
    preferred = payload.get("primary_impressions_field")
    primary = (
        str(preferred).lower()
        if preferred and str(preferred).lower() in flattened
        else next((name for name in candidates if name in flattened), None)
    )
    metric_labels = payload.get("metric_labels") if isinstance(payload.get("metric_labels"), dict) else {}
    normalized: dict[str, int | float | None] = {
        "exposure": flattened.get(primary) if primary else None,
        "likes": next((flattened[name] for name in ("likes", "like_count") if name in flattened), None),
        "comments": next((flattened[name] for name in ("comments", "comment_count") if name in flattened), None),
        "shares": next((flattened[name] for name in ("shares", "share_count") if name in flattened), None),
        "saves": next(
            (flattened[name] for name in ("saves", "favorites", "favorite_count") if name in flattened),
            None,
        ),
    }
    return normalized, primary, str(metric_labels.get(primary) or label) if primary else None


def platform_payload(payload: dict, platform: str) -> dict:
    for container in (payload, payload.get("data"), payload.get("analytics"), payload.get("platforms")):
        if isinstance(container, dict) and isinstance(container.get(platform), dict):
            return container[platform]
    results = payload.get("results")
    if isinstance(results, list):
        result = next((item for item in results if isinstance(item, dict) and item.get("platform") == platform), None)
        if result:
            return result
    return payload


def provider_payload_error(payload: dict) -> str | None:
    error = payload.get("error") or payload.get("post_metrics_error")
    if error:
        return str(error)
    if payload.get("success") is False:
        return str(payload.get("message") or "Analytics are unavailable for this platform")
    return None


def _number_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): float(number)
        for key, raw in value.items()
        if (number := _number(raw)) is not None
    }


def _timeseries(payload: dict, *keys: str) -> dict[str, float]:
    for key in keys:
        raw = payload.get(key)
        if not isinstance(raw, list):
            continue
        output: dict[str, float] = {}
        for point in raw:
            if not isinstance(point, dict) or not point.get("date"):
                continue
            value = _number(point.get("value"))
            if value is not None:
                output[str(point["date"])] = float(value)
        if output:
            return output
    return {}


def _slice_series(series: dict[str, float], start: date, end: date) -> dict[str, float]:
    return {key: value for key, value in series.items() if start.isoformat() <= key <= end.isoformat()}


def _insight_confidence(comparable_posts: int) -> str:
    if comparable_posts >= 10:
        return "high"
    if comparable_posts >= 5:
        return "medium"
    return "early_signal"


def _insight_card(
    candidate_id: str,
    kind: str,
    tone: str,
    headline: str,
    explanation: str,
    action: str,
    confidence: str,
    metric_label: str,
    metric_value: str,
    supporting_post_ids: list[str] | None = None,
) -> dict:
    return {
        "id": candidate_id,
        "kind": kind,
        "tone": tone,
        "headline": headline,
        "explanation": explanation,
        "action": action,
        "confidence": confidence,
        "metric_label": metric_label,
        "metric_value": metric_value,
        "supporting_post_ids": supporting_post_ids or [],
    }


def _merge_ai_explanations(candidates: list[dict], generated: list[dict]) -> list[dict]:
    """Keep every fact server-owned; Gemini may only replace prose without numbers."""

    by_id = {item["id"]: item for item in candidates}
    merged: list[dict] = []
    seen: set[str] = set()
    for item in generated:
        candidate_id = str(item.get("candidate_id") or "")
        source = by_id.get(candidate_id)
        if not source or candidate_id in seen:
            continue
        explanation = str(item.get("explanation") or "").strip()
        action = str(item.get("action") or "").strip()
        # Numbers belong in the immutable metric field. If the model returns
        # any, retain Reverb's deterministic copy instead of risking drift.
        if not explanation or any(character.isdigit() for character in explanation):
            explanation = source["explanation"]
        if not action or any(character.isdigit() for character in action):
            action = source["action"]
        merged.append(
            {
                **source,
                "explanation": explanation[:320],
                "action": action[:220],
            }
        )
        seen.add(candidate_id)
    return merged or candidates[:5]


async def _safe_total_exposure(
    client: UploadPostClient,
    profile: str,
    *,
    start: date,
    end: date,
    platforms: list[str],
    metrics: list[str] | None = None,
) -> tuple[dict, str | None]:
    if not platforms:
        return {}, None
    try:
        payload = await client.total_exposure(
            profile,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            metrics=metrics,
            platforms=platforms,
            breakdown=True,
        )
        if error := provider_payload_error(payload):
            return {}, error
        return payload, None
    except ProviderError as exc:
        return {}, str(exc)


async def _sync_period_snapshots(
    db: AsyncSession,
    user: User,
    client: UploadPostClient,
    connected_platforms: list[str],
    profile_payloads: dict[str, dict],
    facebook_page_id: str | None,
) -> None:
    """Persist exact date-window responses instead of reconstructing history from lifetime counters."""

    today = utcnow().date()
    non_facebook = [platform for platform in connected_platforms if platform != "facebook"]
    facebook_history = profile_payloads.get("facebook", {})
    facebook_series_key = (
        "impressions_timeseries"
        if isinstance(facebook_history.get("impressions_timeseries"), list)
        else "reach_timeseries"
    )
    facebook_series = _timeseries(facebook_history, facebook_series_key)

    for period_days in (7, 30, 90):
        current_start = today - timedelta(days=period_days - 1)
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=period_days - 1)
        current, current_error = await _safe_total_exposure(
            client,
            user.upload_post_profile,
            start=current_start,
            end=today,
            platforms=non_facebook,
        )
        previous, previous_error = await _safe_total_exposure(
            client,
            user.upload_post_profile,
            start=previous_start,
            end=previous_end,
            platforms=non_facebook,
        )
        engagement, engagement_error = await _safe_total_exposure(
            client,
            user.upload_post_profile,
            start=current_start,
            end=today,
            # Upload-Post documents that Facebook's stored aggregate snapshots
            # use rolling windows. Fetch Facebook live below so engagement is
            # not multiplied across snapshot days.
            platforms=non_facebook,
            metrics=["likes", "comments", "shares"],
        )
        facebook_period: dict = {}
        facebook_period_error: str | None = None
        if "facebook" in connected_platforms:
            try:
                response = await client.profile_analytics(
                    user.upload_post_profile,
                    ["facebook"],
                    facebook_page_id=facebook_page_id,
                    days=period_days,
                )
                facebook_period = platform_payload(response, "facebook")
                if error := provider_payload_error(facebook_period):
                    facebook_period_error = error
                    facebook_period = {}
            except ProviderError as exc:
                facebook_period_error = str(exc)

        current_total = _number(current.get("total_impressions"))
        previous_total = _number(previous.get("total_impressions"))
        per_platform = _number_map(current.get("per_platform"))
        per_day = _number_map(current.get("per_day"))
        reporting_platforms = set(per_platform)
        warnings: list[str] = []
        errors = [
            error
            for error in (
                current_error,
                previous_error,
                engagement_error,
                facebook_period_error,
            )
            if error
        ]

        if "facebook" in connected_platforms:
            facebook_current = _timeseries(facebook_period, facebook_series_key)
            if not facebook_current:
                facebook_current = _slice_series(facebook_series, current_start, today)
            facebook_previous = _slice_series(facebook_series, previous_start, previous_end)
            if facebook_current:
                facebook_metric = "impressions" if facebook_series_key == "impressions_timeseries" else "reach"
                facebook_total = _number(facebook_period.get(facebook_metric))
                if facebook_total is None:
                    facebook_total = sum(facebook_current.values())
                current_total = float(current_total or 0) + facebook_total
                per_platform["facebook"] = facebook_total
                reporting_platforms.add("facebook")
                for day, value in facebook_current.items():
                    per_day[day] = per_day.get(day, 0) + value
            else:
                warnings.append("Facebook did not return daily reach for this period.")
            if facebook_previous:
                previous_total = float(previous_total or 0) + sum(facebook_previous.values())
            else:
                previous_total = None
                warnings.append("The previous-period Facebook comparison is unavailable.")

        change_percent = (
            (float(current_total) - float(previous_total)) / float(previous_total) * 100
            if current_total is not None and previous_total not in (None, 0)
            else None
        )
        engagement_totals = {
            metric: _number((engagement.get("metrics") or {}).get(metric))
            for metric in ("likes", "comments", "shares")
        }
        for metric in engagement_totals:
            facebook_value = _number(facebook_period.get(metric))
            if facebook_value is not None:
                engagement_totals[metric] = float(engagement_totals[metric] or 0) + float(
                    facebook_value
                )
        if errors:
            warnings.append("Some provider analytics could not be refreshed.")
        missing_platforms = sorted(set(connected_platforms) - reporting_platforms)
        if missing_platforms:
            warnings.append(f"No period exposure was reported for {', '.join(missing_platforms)}.")

        has_data = current_total is not None or bool(per_day) or any(
            value is not None for value in engagement_totals.values()
        )
        status = "unavailable" if not has_data else "partial" if warnings else "available"
        db.add(
            AnalyticsPeriodSnapshot(
                user_id=user.id,
                period_days=period_days,
                start_date=current_start.isoformat(),
                end_date=today.isoformat(),
                raw_metrics={
                    "current_exposure": current,
                    "previous_exposure": previous,
                    "engagement": engagement,
                    "facebook_profile": profile_payloads.get("facebook"),
                    "facebook_period": facebook_period,
                },
                normalized_metrics={
                    "total_exposure": current_total,
                    "previous_total_exposure": previous_total,
                    "change_percent": change_percent,
                    "per_platform": per_platform,
                    "per_day": per_day,
                    "engagement": engagement_totals,
                    "connected_platforms": sorted(set(connected_platforms)),
                    "reporting_platforms": sorted(reporting_platforms),
                    "warnings": warnings,
                },
                provider_status=status,
                error_message="; ".join(errors) or None,
            )
        )


async def sync_user_analytics(db: AsyncSession, user: User, *, force: bool = False) -> None:
    client = UploadPostClient()
    connections = list(
        await db.scalars(
            select(SocialConnection).where(
                SocialConnection.user_id == user.id,
                SocialConnection.status == "connected",
            )
        )
    )
    profile_payloads: dict[str, dict] = {}
    for connection in connections:
        try:
            profile_data = await client.profile_analytics(
                user.upload_post_profile,
                [connection.platform],
                facebook_page_id=connection.target_page_id if connection.platform == "facebook" else None,
                # Facebook is the only profile endpoint that honors this exact
                # window. Keeping 180 days lets all 7/30/90-day comparisons use
                # its real daily series instead of rolling 30-day snapshots.
                days=180 if connection.platform == "facebook" else 30,
            )
            raw = platform_payload(profile_data, connection.platform)
            if error := provider_payload_error(raw):
                raise ProviderError("analytics_unavailable", error)
            profile_payloads[connection.platform] = raw
            normalized, metric, label = normalize_metrics(connection.platform, raw)
            db.add(
                AccountAnalyticsSnapshot(
                    user_id=user.id,
                    platform=connection.platform,
                    raw_metrics=raw,
                    normalized_metrics=normalized,
                    primary_metric=metric,
                    primary_label=label,
                    provider_status="available",
                )
            )
        except ProviderError as exc:
            db.add(
                AccountAnalyticsSnapshot(
                    user_id=user.id,
                    platform=connection.platform,
                    raw_metrics={},
                    normalized_metrics={},
                    provider_status="unavailable",
                    error_message=str(exc),
                )
            )

    await _sync_period_snapshots(
        db,
        user,
        client,
        [connection.platform for connection in connections],
        profile_payloads,
        next(
            (
                connection.target_page_id
                for connection in connections
                if connection.platform == "facebook"
            ),
            None,
        ),
    )

    cutoff = utcnow() - timedelta(days=90)
    publications = list(
        await db.scalars(
            select(Publication)
            .join(Post)
            .where(
                Post.user_id == user.id,
                Publication.status == "published",
                Publication.published_at >= cutoff,
            )
        )
    )
    for publication in publications:
        age = utcnow() - publication.published_at
        latest_capture = await db.scalar(
            select(func.max(PublicationMetricSnapshot.captured_at)).where(
                PublicationMetricSnapshot.publication_id == publication.id
            )
        )
        if latest_capture and latest_capture.tzinfo is None:
            latest_capture = latest_capture.replace(tzinfo=UTC)
        interval = timedelta(hours=23) if age <= timedelta(days=30) else timedelta(days=6)
        if not force and (age < timedelta(hours=1) or (latest_capture and utcnow() - latest_capture < interval)):
            continue
        try:
            raw_response = await client.post_analytics(
                request_id=publication.provider_request_id,
                platform=publication.platform,
                platform_post_id=publication.provider_post_id,
                profile=user.upload_post_profile,
            )
            raw = platform_payload(raw_response, publication.platform)
            if error := provider_payload_error(raw):
                raise ProviderError("analytics_unavailable", error)
            normalized, metric, label = normalize_metrics(publication.platform, raw)
            db.add(
                PublicationMetricSnapshot(
                    publication_id=publication.id,
                    raw_metrics=raw,
                    normalized_metrics=normalized,
                    primary_metric=metric,
                    primary_label=label,
                    provider_status="available",
                )
            )
        except ProviderError as exc:
            db.add(
                PublicationMetricSnapshot(
                    publication_id=publication.id,
                    raw_metrics={},
                    normalized_metrics={},
                    provider_status="unavailable",
                    error_message=str(exc),
                )
            )
    await db.commit()


async def _latest_account_snapshots(db: AsyncSession, user_id: str) -> list[dict]:
    rows = list(
        await db.scalars(
            select(AccountAnalyticsSnapshot)
            .where(AccountAnalyticsSnapshot.user_id == user_id)
            .order_by(AccountAnalyticsSnapshot.platform, AccountAnalyticsSnapshot.captured_at.desc())
        )
    )
    grouped: dict[str, list[AccountAnalyticsSnapshot]] = defaultdict(list)
    for row in rows:
        grouped[row.platform].append(row)
    result = []
    for platform, snapshots in grouped.items():
        latest = snapshots[0]
        successful = next((item for item in snapshots if item.provider_status == "available"), None)
        source = successful or latest
        result.append(
            {
                "platform": platform,
                "metrics": source.normalized_metrics,
                "primary_metric": source.primary_metric,
                "primary_label": source.primary_label,
                "captured_at": source.captured_at,
                "availability": latest.provider_status,
                "stale": latest.provider_status != "available" and bool(successful),
                "error": latest.error_message,
            }
        )
    return result


async def _latest_period_snapshot(
    db: AsyncSession, user_id: str, period_days: int
) -> tuple[AnalyticsPeriodSnapshot | None, bool, str | None]:
    snapshots = list(
        await db.scalars(
            select(AnalyticsPeriodSnapshot)
            .where(
                AnalyticsPeriodSnapshot.user_id == user_id,
                AnalyticsPeriodSnapshot.period_days == period_days,
            )
            .order_by(AnalyticsPeriodSnapshot.captured_at.desc())
        )
    )
    if not snapshots:
        return None, False, None
    latest = snapshots[0]
    usable = next(
        (item for item in snapshots if item.provider_status in {"available", "partial"}),
        None,
    )
    source = usable or latest
    stale = source.id != latest.id or latest.provider_status == "unavailable"
    return source, stale, latest.error_message


async def analytics_overview(
    db: AsyncSession,
    user: User,
    period_days: int,
    *,
    generate_ai: bool = False,
) -> dict:
    now = utcnow()
    start = now - timedelta(days=period_days)
    rows = list(
        await db.execute(
            select(PublicationMetricSnapshot, Publication, Post)
            .join(Publication, Publication.id == PublicationMetricSnapshot.publication_id)
            .join(Post, Post.id == Publication.post_id)
            .where(
                Post.user_id == user.id,
                Publication.published_at >= start,
                PublicationMetricSnapshot.provider_status == "available",
            )
            .order_by(PublicationMetricSnapshot.captured_at.desc())
            .options(selectinload(Post.media))
        )
    )
    latest_by_publication: dict[str, tuple] = {}
    for snapshot, publication, post in rows:
        latest_by_publication.setdefault(publication.id, (snapshot, publication, post))
    current = list(latest_by_publication.values())

    def exposure(row: tuple) -> float | None:
        return _number((row[0].normalized_metrics or {}).get("exposure"))

    reverb_platform_totals: dict[str, float] = defaultdict(float)
    reverb_platform_known: set[str] = set()
    post_totals: dict[str, float] = defaultdict(float)
    post_known: set[str] = set()
    platform_exposures: dict[str, list[float]] = defaultdict(list)
    publication_exposures: list[tuple[str, str, float]] = []
    for row in current:
        value = exposure(row)
        if value is None:
            continue
        snapshot, publication, post = row
        reverb_platform_totals[publication.platform] += value
        reverb_platform_known.add(publication.platform)
        post_totals[post.id] += value
        post_known.add(post.id)
        platform_exposures[publication.platform].append(float(value))
        publication_exposures.append((post.id, publication.platform, float(value)))

    period_snapshot, period_stale, period_error = await _latest_period_snapshot(
        db, user.id, period_days
    )
    period_data = period_snapshot.normalized_metrics if period_snapshot else {}
    total = _number(period_data.get("total_exposure"))
    previous_total = _number(period_data.get("previous_total_exposure"))
    change = _number(period_data.get("change_percent"))
    period_platforms = _number_map(period_data.get("per_platform"))
    period_trend = _number_map(period_data.get("per_day"))
    engagement = {
        metric: _number((period_data.get("engagement") or {}).get(metric))
        for metric in ("likes", "comments", "shares")
    }
    connected_platforms = list(period_data.get("connected_platforms") or [])
    reporting_platforms = list(period_data.get("reporting_platforms") or [])
    warnings = list(period_data.get("warnings") or [])
    if period_stale:
        warnings.append("The latest successful period snapshot is being shown.")
    if period_error and period_error not in warnings:
        warnings.append(period_error)

    strongest = max(period_platforms, key=period_platforms.get) if period_platforms else None
    ranked_post_ids = sorted(post_known, key=lambda item: post_totals[item], reverse=True)
    post_rows = {row[2].id: row[2] for row in current}
    top_posts = [
        {
            "id": post_id,
            "title": post_rows[post_id].title or post_rows[post_id].caption[:80],
            "exposure": post_totals[post_id],
        }
        for post_id in ranked_post_ids[:3]
    ]
    top_post = top_posts[0] if top_posts else None
    comparable_posts = len(post_known)
    confidence = _insight_confidence(comparable_posts)

    candidates: list[dict] = []
    if change is not None:
        tone = "positive" if change > 0 else "negative" if change < 0 else "neutral"
        direction = "up" if change > 0 else "down" if change < 0 else "unchanged"
        candidates.append(
            _insight_card(
                "momentum",
                "momentum",
                tone,
                "Exposure is gaining momentum" if change > 0 else "Exposure needs attention" if change < 0 else "Exposure is holding steady",
                f"Total exposure is {direction} {abs(change):.1f}% compared with the previous {period_days}-day period.",
                "Keep the formats driving the increase in rotation." if change > 0 else "Review the strongest recent post and reuse one proven element in the next upload." if change < 0 else "Test one clear creative change in the next post so its effect is easier to measure.",
                confidence,
                "Change vs prior period",
                f"{change:+.1f}%",
            )
        )

    platform_total = sum(period_platforms.values())
    if strongest and platform_total > 0:
        contribution = period_platforms[strongest] / platform_total * 100
        candidates.append(
            _insight_card(
                "strongest_platform",
                "platform",
                "positive",
                f"{strongest.title()} is leading exposure",
                f"{strongest.title()} contributed {contribution:.1f}% of reported exposure during this period.",
                f"Keep {strongest.title()} selected for the next post, while adapting the copy for the other destinations.",
                confidence,
                "Exposure contribution",
                f"{contribution:.1f}%",
            )
        )

    if comparable_posts >= 3 and top_post:
        typical = median(post_totals.values())
        if typical > 0:
            multiple = top_post["exposure"] / typical
            candidates.append(
                _insight_card(
                    "breakout_post",
                    "winner",
                    "positive",
                    "One post is setting the pace",
                    f"“{top_post['title'][:65]}” delivered {multiple:.1f}× the median exposure of comparable Reverb posts.",
                    "Open that post and reuse one recognisable element—its topic, opening, or caption structure—in your next draft.",
                    confidence,
                    "Versus median Reverb post",
                    f"{multiple:.1f}×",
                    [top_post["id"]],
                )
            )

    known_engagement = {key: value for key, value in engagement.items() if value is not None}
    if known_engagement:
        interaction_total = sum(known_engagement.values())
        leading_interaction = max(known_engagement, key=known_engagement.get)
        engagement_actions = {
            "likes": "Reuse the clearest visual or message from the posts receiving the strongest reaction.",
            "comments": "Build the next caption around a direct question and respond while the conversation is active.",
            "shares": "Repurpose the most useful post into another platform-specific version while it is resonating.",
        }
        candidates.append(
            _insight_card(
                "engagement_signal",
                "engagement",
                "positive",
                f"{leading_interaction.title()} are the strongest interaction signal",
                f"Reverb recorded {interaction_total:,.0f} total interactions, led by {leading_interaction}.",
                engagement_actions[leading_interaction],
                confidence,
                "Recorded interactions",
                f"{interaction_total:,.0f}",
            )
        )

    # Normalize every destination against its own median before comparing
    # content characteristics. This prevents a naturally larger platform from
    # making a caption length or video duration look better by association.
    platform_medians = {
        platform: median(values)
        for platform, values in platform_exposures.items()
        if values and median(values) > 0
    }
    score_parts: dict[str, list[float]] = defaultdict(list)
    for post_id, platform, value in publication_exposures:
        if baseline := platform_medians.get(platform):
            score_parts[post_id].append(value / baseline * 100)
    performance_scores = {
        post_id: sum(values) / len(values)
        for post_id, values in score_parts.items()
        if values
    }

    pattern_options: list[tuple[float, dict]] = []

    def add_pattern(
        candidate_id: str,
        positive_label: str,
        negative_label: str,
        values: dict[str, bool | None],
        action: str,
    ) -> None:
        positive = [(post_id, performance_scores[post_id]) for post_id, flag in values.items() if flag is True and post_id in performance_scores]
        negative = [(post_id, performance_scores[post_id]) for post_id, flag in values.items() if flag is False and post_id in performance_scores]
        if len(positive) < 2 or len(negative) < 2:
            return
        positive_average = sum(value for _, value in positive) / len(positive)
        negative_average = sum(value for _, value in negative) / len(negative)
        winner, loser = (positive, negative) if positive_average >= negative_average else (negative, positive)
        winner_average = max(positive_average, negative_average)
        loser_average = min(positive_average, negative_average)
        if loser_average <= 0:
            return
        difference = (winner_average / loser_average - 1) * 100
        if difference < 15:
            return
        winner_label = positive_label if winner is positive else negative_label
        supporting_ids = [post_id for post_id, _ in sorted(winner, key=lambda item: item[1], reverse=True)[:3]]
        pattern_options.append(
            (
                difference,
                _insight_card(
                    candidate_id,
                    "content_pattern",
                    "positive",
                    f"{winner_label} are showing promise",
                    f"{winner_label} scored {difference:.0f}% higher across {len(positive) + len(negative)} comparable posts after platform normalization.",
                    action,
                    _insight_confidence(len(positive) + len(negative)),
                    "Relative performance",
                    f"+{difference:.0f}%",
                    supporting_ids,
                ),
            )
        )

    add_pattern(
        "caption_length_pattern",
        "Shorter captions",
        "Longer captions",
        {post_id: len(post_rows[post_id].caption.strip()) < 120 for post_id in post_known},
        "Use the stronger caption length as a starting point, while preserving the details each platform requires.",
    )
    add_pattern(
        "hashtag_pattern",
        "Posts with five or fewer hashtags",
        "Posts with more than five hashtags",
        {
            post_id: len(post_rows[post_id].hashtags or []) <= 5
            for post_id in post_known
            if post_rows[post_id].hashtags
        },
        "Use the stronger hashtag range on the next post, then compare again as the sample grows.",
    )
    add_pattern(
        "duration_pattern",
        "Videos up to 30 seconds",
        "Videos longer than 30 seconds",
        {
            post_id: post_rows[post_id].media.duration_seconds <= 30
            for post_id in post_known
            if post_rows[post_id].media and post_rows[post_id].media.duration_seconds is not None
        },
        "Try the stronger duration range again without changing several other creative variables at once.",
    )
    if pattern_options:
        candidates.append(max(pattern_options, key=lambda item: item[0])[1])

    candidates = candidates[:6]
    accounts = await _latest_account_snapshots(db, user.id)
    evidence = {
        "period": f"{period_days}d",
        "sample_size": comparable_posts,
        "confidence": confidence,
        "verified_metrics": {
            "total_exposure": total,
            "previous_total_exposure": previous_total,
            "change_percent": change,
            "platform_exposure": period_platforms,
            "engagement": engagement,
        },
        "candidate_insights": candidates,
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True).encode()
    ).hexdigest()
    stored = await db.scalar(select(GeneratedInsight).where(GeneratedInsight.user_id == user.id, GeneratedInsight.period_days == period_days))
    fallback_cards = candidates[:5]
    summary = {
        "text": fallback_cards[0]["explanation"] if fallback_cards else None,
        "minimum_posts": 3,
        "comparable_posts": comparable_posts,
        "confidence": confidence,
        "generated_by": "reverb",
        "cards": fallback_cards,
    }
    if stored and stored.source_fingerprint == fingerprint and stored.summary.get("cards"):
        summary = stored.summary

    settings = get_settings()
    should_generate = (
        generate_ai
        and comparable_posts >= 3
        and bool(candidates)
        and settings.ai_enabled
        and bool(settings.gemini_api_key)
        and (
            not stored
            or stored.source_fingerprint != fingerprint
            or stored.summary.get("generated_by") != "gemini"
        )
    )
    if should_generate:
        try:
            from .gemini import GeminiClient

            generated, usage = await GeminiClient().create_analytics_insights(evidence)
            cards = _merge_ai_explanations(candidates, generated)
            summary = {
                **summary,
                "text": cards[0]["explanation"] if cards else None,
                "generated_by": "gemini",
                "cards": cards,
                "usage": usage,
            }
        except Exception as exc:
            logger.info("AI analytics insight generation failed for user %s: %s", user.id, exc)

    if stored:
        if stored.source_fingerprint != fingerprint or stored.summary != summary:
            stored.source_fingerprint = fingerprint
            stored.summary = summary
    else:
        db.add(GeneratedInsight(user_id=user.id, period_days=period_days, source_fingerprint=fingerprint, summary=summary))
    await db.commit()
    return {
        "period": f"{period_days}d",
        "total_exposure": total,
        "previous_total_exposure": previous_total,
        "change_percent": change,
        "date_range": {
            "start": period_snapshot.start_date if period_snapshot else None,
            "end": period_snapshot.end_date if period_snapshot else None,
        },
        "captured_at": period_snapshot.captured_at if period_snapshot else None,
        "accounts": accounts,
        "platforms": [
            {"platform": platform, "exposure": period_platforms[platform]}
            for platform in sorted(period_platforms)
        ],
        "reverb_platforms": [
            {"platform": platform, "exposure": reverb_platform_totals[platform]}
            for platform in sorted(reverb_platform_known)
        ],
        "trend": [
            {"date": day, "exposure": period_trend[day]}
            for day in sorted(period_trend)
        ],
        "engagement": engagement,
        "top_post": top_post,
        "top_posts": top_posts,
        "insight": {
            "text": summary.get("text"),
            "minimum_posts": summary.get("minimum_posts", 3),
            "comparable_posts": summary.get("comparable_posts", comparable_posts),
        },
        "insights": summary.get("cards", []),
        "insights_generated_by": summary.get("generated_by", "reverb"),
        "insight_confidence": summary.get("confidence", confidence),
        "coverage": {
            "connected": len(connected_platforms),
            "reporting": len(reporting_platforms),
        },
        "warnings": warnings,
        "stale": period_stale,
        "partial": (
            not period_snapshot
            or period_snapshot.provider_status != "available"
            or any(item["availability"] != "available" for item in accounts)
        ),
    }


@router.get("/overview")
async def get_overview(
    period: str = Query(default="30d", pattern="^(7d|30d|90d)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_capability(db, user, "analytics")
    return await analytics_overview(db, user, int(period[:-1]))


@router.get("/posts/{post_id}")
async def get_post_analytics(post_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await require_capability(db, user, "analytics")
    post = await db.scalar(
        select(Post).where(Post.id == post_id, Post.user_id == user.id).options(selectinload(Post.publications))
    )
    if not post:
        raise HTTPException(status_code=404, detail={"code": "post_not_found", "message": "Post not found"})
    items = []
    for publication in post.publications:
        snapshots = list(
            await db.scalars(
                select(PublicationMetricSnapshot)
                .where(PublicationMetricSnapshot.publication_id == publication.id)
                .order_by(PublicationMetricSnapshot.captured_at.desc())
            )
        )
        latest = snapshots[0] if snapshots else None
        successful = next(
            (item for item in snapshots if item.provider_status == "available"),
            None,
        )
        source = successful or latest
        items.append(
            {
                "platform": publication.platform,
                "metrics": source.normalized_metrics if source else None,
                "primary_metric": source.primary_metric if source else None,
                "primary_label": source.primary_label if source else None,
                "captured_at": source.captured_at if source else None,
                "availability": latest.provider_status if latest else "pending",
                "stale": bool(latest and successful and latest.id != successful.id),
                "error": latest.error_message if latest else None,
            }
        )
    return {"post_id": post.id, "items": items}


@router.post("/refresh", status_code=202)
async def refresh_analytics(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await require_capability(db, user, "analytics")
    latest = await db.scalar(select(func.max(AccountAnalyticsSnapshot.captured_at)).where(AccountAnalyticsSnapshot.user_id == user.id))
    if latest:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=UTC)
        remaining = timedelta(minutes=15) - (utcnow() - latest)
        if remaining.total_seconds() > 0:
            raise HTTPException(status_code=429, detail={"code": "analytics_refresh_cooldown", "message": "Analytics can be refreshed once every 15 minutes", "retry_after_seconds": int(remaining.total_seconds())})
    try:
        from .tasks import sync_analytics
        sync_analytics.delay(user.id, True)
    except CeleryError as exc:
        raise HTTPException(status_code=503, detail={"code": "analytics_queue_unavailable", "message": "Analytics refresh is temporarily unavailable"}) from exc
    return {"message": "Analytics refresh started"}
