"""Alembic environment. Migrations run as the database OWNER role (MA_MIGRATION_DATABASE_URL);
the application runs as a separate non-owner role so row-level security applies to it."""
from alembic import context
from sqlalchemy import create_engine

from db.models import Base
from settings import get_settings

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = context.config.attributes.get("url") or get_settings().migration_database_url
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=get_settings().migration_database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
