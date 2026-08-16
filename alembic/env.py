import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

import config
from models import Base

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(
        url=config.DATABASE_URL, target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    engine = create_async_engine(
        config.DATABASE_URL, connect_args={"statement_cache_size": 0}
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
