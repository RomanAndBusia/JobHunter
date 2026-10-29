import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

os.environ["JOBHUNTER_BROWSER_AUTOSTART"] = "false"
os.environ["JOBHUNTER_DATABASE_URL"] = "sqlite:///:memory:"

from app.db.session import Base  # noqa: E402
from app import models  # noqa: E402,F401


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
