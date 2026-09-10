from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    hide_parameters=settings.is_production,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Celery tasks are synchronous entry points that each run their async work in a
# fresh event loop. Async driver connections cannot be safely reused across
# those loops, so task sessions deliberately do not pool connections.
task_engine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
    hide_parameters=settings.is_production,
)
TaskSessionLocal = async_sessionmaker(task_engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
