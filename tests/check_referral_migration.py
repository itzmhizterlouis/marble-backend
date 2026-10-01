"""CI-only PostgreSQL migration and actual row-lock checks. Never production."""

import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.billing import activate_transaction
from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import User
from app.paystack import PaystackClient, PaystackError
from app.pricing import referral_offer
from app.referral_billing import initialize_referral_checkout, lock_billing_user


async def seed():
    async with engine.begin() as connection:
        for suffix in ("referrer", "historic", "fresh"):
            await connection.execute(text("""
                INSERT INTO users (id, email, name, email_verified, upload_post_profile, created_at, updated_at)
                VALUES (:id, :email, 'CI creator', true, :profile, NOW(), NOW())
            """), {"id": f"migration-{suffix}", "email": f"migration-{suffix}@example.com", "profile": f"migration-{suffix}"})
        await connection.execute(text("UPDATE users SET referred_by_user_id = 'migration-referrer' WHERE id = 'migration-fresh'"))
        await connection.execute(text("""
            INSERT INTO affiliate_commissions (id, referrer_user_id, referred_user_id,
                payment_reference, payment_amount_kobo, amount_kobo, available_at,
                requires_review, created_at, updated_at)
            VALUES ('migration-commission', 'migration-referrer', 'migration-historic',
                'historic-payment', 1000000, 200000, NOW(), false, NOW(), NOW())
        """))


async def concurrent_checkout_and_payment():
    initializations = []

    async def verify(self, reference):
        raise PaystackError("Transaction reference not found.", 400)

    async def initialize(self, **kwargs):
        initializations.append(kwargs)
        await asyncio.sleep(0.05)
        return {"reference": kwargs["reference"], "authorization_url": "https://checkout.paystack.com/ci-only"}

    PaystackClient.verify_transaction = verify
    PaystackClient.initialize_checkout = initialize

    async def checkout():
        async with SessionLocal() as db:
            user = await lock_billing_user(db, "migration-fresh")
            assert await referral_offer(db, user)
            return await initialize_referral_checkout(db, user, "basic", "PLN_ci_basic")

    first, second = await asyncio.gather(checkout(), checkout())
    assert first == second and len(initializations) == 1
    paid = {
        "reference": first["reference"], "status": "success", "amount": 800_000,
        "currency": "NGN", "paid_at": datetime.now(UTC).isoformat(),
        "customer": {"email": "migration-fresh@example.com"},
        "metadata": {"reverb_user_id": "migration-fresh", "reverb_plan": "basic"},
    }

    async def activate():
        async with SessionLocal() as db:
            # Load the user before taking the lock to cover stale identity maps.
            user = await db.scalar(select(User).where(User.id == "migration-fresh"))
            return (await activate_transaction(db, user, paid)).id

    first_id, second_id = await asyncio.gather(activate(), activate())
    assert first_id == second_id
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT COUNT(*) FROM subscriptions WHERE user_id = 'migration-fresh'")) == 1
        row = (await connection.execute(text("SELECT payment_amount_kobo, commission_base_kobo, amount_kobo FROM affiliate_commissions WHERE referred_user_id = 'migration-fresh'"))).one()
        assert tuple(row) == (800_000, 1_000_000, 200_000)


async def check(concurrency=False):
    async with engine.connect() as connection:
        row = (await connection.execute(text("SELECT payment_amount_kobo, commission_base_kobo, amount_kobo FROM affiliate_commissions WHERE id = 'migration-commission'"))).one()
        assert tuple(row) == (1_000_000, 1_000_000, 200_000)
        assert await connection.scalar(text("SELECT COUNT(*) FROM referral_checkouts")) == 0
    if concurrency:
        await concurrent_checkout_and_payment()


async def main():
    settings = get_settings()
    if settings.environment != "test" or make_url(settings.database_url).database != "reverb_migration_test":
        raise RuntimeError("This migration check is restricted to the disposable CI database")
    if sys.argv[1] == "seed":
        await seed()
    else:
        await check(concurrency=sys.argv[1] == "check-concurrency")
    await engine.dispose()
    print("Referral PostgreSQL check passed")


if __name__ == "__main__":
    asyncio.run(main())
