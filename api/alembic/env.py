"""Alembic environment configuration."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, engine_from_config, pool
from sqlalchemy.engine import make_url

from runtime_secrets import runtime_value

# Import all models so Alembic can detect schema changes via autogenerate.
from models import Base  # noqa: F401

# ---------------------------------------------------------------------------
# Alembic Config object
# ---------------------------------------------------------------------------
config = context.config

# Programmatic verification targets take precedence over the process default.
# Keep URLs out of ConfigParser so percent-encoded credentials are not treated
# as interpolation syntax.
database_url = config.attributes.get("database_url")
if database_url is None:
    database_url = runtime_value("DATABASE_URL")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection required)."""
    url = database_url or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    if database_url:
        parsed = make_url(database_url)
        query = dict(parsed.query)
        query.setdefault("sslmode", os.environ.get("DB_SSLMODE", "prefer"))
        parsed = parsed.update_query_dict(query)
        connect_args = {
            "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
            "options": f"-c statement_timeout={int(os.environ.get('DB_STATEMENT_TIMEOUT_MS', '30000'))}",
        }
        sslrootcert = os.environ.get("DB_SSLROOTCERT")
        if sslrootcert:
            connect_args["sslrootcert"] = sslrootcert
        connectable = create_engine(parsed, poolclass=pool.NullPool, connect_args=connect_args)
    else:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
