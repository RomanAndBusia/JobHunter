"""Bootstrap current metadata only for an empty database; migrate existing data."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db.session import Base, engine
from app import models  # noqa: F401


def migrate():
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'alembic'))
    tables = set(inspect(engine).get_table_names()) - {'alembic_version'}
    if not tables:
        Base.metadata.create_all(engine)
        command.stamp(config, 'head')
    else:
        command.upgrade(config, 'head')


if __name__ == '__main__':
    migrate()
