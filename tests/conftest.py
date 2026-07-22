import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
import os

# Use test database from env or default
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://talentscope:talentscope@localhost:5432/talentscope_test"
)


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(TEST_DB_URL)
    # pgvector extension must exist before create_all() tries to build Vector columns
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    from app.database import Base
    from app import models  # noqa
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(test_engine):
    Session = sessionmaker(bind=test_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def client(test_engine, monkeypatch):
    from app.database import get_db
    from app.main import app
    Session = sessionmaker(bind=test_engine)

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
