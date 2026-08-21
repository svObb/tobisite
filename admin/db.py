"""Подключение панели: свой движок под ролью admin_ro, отдельно от бота.

models.Session здесь не используется намеренно: у бота своя роль с правом
записи и свой пул, и панель не должна ни делить его, ни менять.
"""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def connect(db_url: str):
    """(engine, sessionmaker). Engine возвращается отдельно — его закрывают на shutdown.

    NullPool: соединения asyncpg привязаны к event loop, а у каждого теста он
    свой — общий пул отдавал бы соединение из чужого. Панель внутренняя,
    несколько запросов на страницу, экономить здесь нечего.
    """
    engine = create_async_engine(db_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
