from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, timedelta
from typing import Any

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import get_db
from .deps import get_current_user
from .entitlements import require_capability
from .models import (
    AccountAnalyticsSnapshot,
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
    flattened = flatten_metrics(payload)
    candidates, label = PRIMARY_METRICS.get(platform, (("views", "reach", "impressions"), "Exposure"))
    primary = next((name for name in candidates if name in flattened), None)
    normalized: dict[str, int | float | None] = {
        "exposure": flattened.get(primary) if primary else None,
        "likes": next((flattened[name] for name in ("likes", "like_count") if name in flattened), None),
        "comments": next((flattened[name] for name in ("comments", "comment_count") if name in flattened), None),
        "shares": next((flattened[name] for name in ("shares", "share_count") if name in flattened), None),
    }
    return normalized, primary, label if primary else None


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
    for connection in connections:
        try:
            profile_data = await client.profile_analytics(
                user.upload_post_profile,
                [connection.platform],
                facebook_page_id=connection.target_page_id if connection.platform == "facebook" else None,
            )
            raw = platform_payload(profile_data, connection.platform)
            if error := provider_payload_error(raw):
                raise ProviderError("analytics_unavailable", error)
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


async def analytics_overview(db: AsyncSession, user: User, period_days: int) -> dict:
    now = utcnow()
    start = now - timedelta(days=period_days)
    prior_start = start - timedelta(days=period_days)
    rows = list(
        await db.execute(
            select(PublicationMetricSnapshot, Publication, Post)
            .join(Publication, Publication.id == PublicationMetricSnapshot.publication_id)
            .join(Post, Post.id == Publication.post_id)
            .where(Post.user_id == user.id, PublicationMetricSnapshot.captured_at >= prior_start)
            .order_by(PublicationMetricSnapshot.captured_at.desc())
        )
    )
    latest_by_publication: dict[str, tuple] = {}
    for snapshot, publication, post in rows:
        latest_by_publication.setdefault(publication.id, (snapshot, publication, post))
    current = [row for row in latest_by_publication.values() if row[1].published_at and row[1].published_at >= start]
    previous = [row for row in latest_by_publication.values() if row[1].published_at and prior_start <= row[1].published_at < start]

    def exposure(row: tuple) -> float | None:
        return _number((row[0].normalized_metrics or {}).get("exposure"))

    current_values = [value for row in current if (value := exposure(row)) is not None]
    previous_values = [value for row in previous if (value := exposure(row)) is not None]
    total = sum(current_values) if current_values else None
    previous_total = sum(previous_values) if previous_values else None
    change = ((total - previous_total) / previous_total * 100) if total is not None and previous_total else None
    platform_totals: dict[str, float] = defaultdict(float)
    platform_known: set[str] = set()
    post_totals: dict[str, float] = defaultdict(float)
    post_known: set[str] = set()
    trend: dict[str, float] = defaultdict(float)
    for row in current:
        value = exposure(row)
        if value is None:
            continue
        snapshot, publication, post = row
        platform_totals[publication.platform] += value
        platform_known.add(publication.platform)
        post_totals[post.id] += value
        post_known.add(post.id)
        if publication.published_at:
            trend[publication.published_at.date().isoformat()] += value
    strongest = max(platform_known, key=lambda item: platform_totals[item]) if platform_known else None
    top_id = max(post_known, key=lambda item: post_totals[item]) if post_known else None
    top_row = next((row for row in current if row[2].id == top_id), None)
    comparable_posts = len(post_known)
    insight = None
    if comparable_posts >= 3 and strongest and top_row:
        insight = f"{strongest.title()} is driving the most exposure in this period. Your strongest Reverb post is “{top_row[2].title or top_row[2].caption[:45]}”."
    accounts = await _latest_account_snapshots(db, user.id)
    fingerprint = hashlib.sha256(json.dumps({"total": total, "platforms": platform_totals, "top": top_id}, sort_keys=True).encode()).hexdigest()
    stored = await db.scalar(select(GeneratedInsight).where(GeneratedInsight.user_id == user.id, GeneratedInsight.period_days == period_days))
    summary = {"text": insight, "minimum_posts": 3, "comparable_posts": comparable_posts}
    if stored:
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
        "captured_at": max((row[0].captured_at for row in current), default=None),
        "accounts": accounts,
        "platforms": [{"platform": platform, "exposure": platform_totals[platform]} for platform in sorted(platform_known)],
        "trend": [{"date": date, "exposure": trend[date]} for date in sorted(trend)],
        "top_post": ({"id": top_row[2].id, "title": top_row[2].title or top_row[2].caption[:80], "exposure": post_totals[top_id]} if top_row and top_id else None),
        "insight": summary,
        "partial": any(item["availability"] != "available" for item in accounts),
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
        latest = await db.scalar(
            select(PublicationMetricSnapshot)
            .where(PublicationMetricSnapshot.publication_id == publication.id, PublicationMetricSnapshot.provider_status == "available")
            .order_by(PublicationMetricSnapshot.captured_at.desc())
        )
        items.append({"platform": publication.platform, "metrics": latest.normalized_metrics if latest else None, "primary_metric": latest.primary_metric if latest else None, "primary_label": latest.primary_label if latest else None, "captured_at": latest.captured_at if latest else None})
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
