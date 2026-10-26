import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Alembic Config object — gives access to alembic.ini values
config = context.config

# Wire Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import Base and all models so Alembic can detect schema changes
from app.core.config import settings  # noqa: E402
from app.db.session import (  # noqa: E402
    Base,
    _build_ssl_arg,
    _normalize_database_url,
)

# Models must be imported for autogenerate to pick them up.
from app.db import models as _models  # noqa: F401

target_metadata = Base.metadata

# Override the URL from the environment (ignores the blank value in alembic.ini).
# Strip PlanetScale / Aiven-style `?ssl-mode=…` query params and re-emit the
# cleaned URL plus the corresponding connect_args. SQLAlchemy's aiomysql
# dialect forwards query-string keys as kwargs to `aiomysql.connect()`,
# which doesn't accept `ssl-mode` — so we must drop it here.
_clean_url, _connect_args = _normalize_database_url(settings.DATABASE_URL)
# ConfigParser treats percent signs as interpolation markers. Preserve URL-
# encoded credentials (for example %40 for @) when storing the connection URL.
config.set_main_option("sqlalchemy.url", _clean_url.replace("%", "%%"))
# Keep driver options as Python objects under connect_args. Serializing SSL as
# sqlalchemy.ssl passes an invalid engine argument and loses the SSLContext.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection required)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine."""
    section = config.get_section(config.config_ini_section, {})
    # Pass connect_args directly to the engine factory so
    # aiomysql's connect() gets them as **kwargs. We rebuild the SSL
    # arg via `_build_ssl_arg` so we never pass a dict to the asyncio
    # SSL transport (which breaks with `'dict' has no attribute 'wrap_bio'`).
    if _connect_args:
        ssl_arg = _connect_args.get("ssl")
        if isinstance(ssl_arg, dict):
            rebuilt = _build_ssl_arg(ssl_arg)
            if rebuilt is False:
                _connect_args.pop("ssl", None)
            else:
                _connect_args["ssl"] = rebuilt
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_connect_args,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
