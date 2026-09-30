"""Every test runs against a database of its own, created for the run and
dropped after it, so nothing a test writes can reach the one the backend uses."""

import asyncio
import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

os.environ.setdefault("OPUS_PLUGIN_ROOT", "/nonexistent")

from opus.config import settings

_served = make_url(settings.database_url)
_TEST_DB = f"{_served.database}_test"
settings.database_url = _served.set(database=_TEST_DB).render_as_string(hide_password=False)
_maintenance = _served.set(drivername="postgresql", database="postgres").render_as_string(
    hide_password=False)


def _admin(sql: str) -> None:
    with psycopg.connect(_maintenance, autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture(scope="session", autouse=True)
def database():
    _admin(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
    _admin(f'CREATE DATABASE "{_TEST_DB}"')
    command.upgrade(Config("alembic.ini"), "head")
    yield
    _admin(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')


@pytest.fixture(scope="session", autouse=True)
async def pool(database):
    from opus import db
    yield
    await db.engine.dispose()


@pytest.fixture(autouse=True)
async def clean(pool, monkeypatch, tmp_path):
    from opus import db, provision, runner, settings_store
    from opus.api.routers import engines

    monkeypatch.setattr(settings, "landing_root", str(tmp_path / "landing"))
    monkeypatch.setattr(settings, "engines_dir", str(tmp_path / "engines"))
    monkeypatch.setattr(settings, "engines_url", "http://engines.test:8099")
    yield
    supervisors = [live.supervisor for live in runner._live.values() if live.supervisor]
    if supervisors:
        await asyncio.wait(supervisors, timeout=10)
    runner._live.clear()
    runner._slots.clear()
    provision.failures.clear()
    provision._locks.clear()
    engines._answers.clear()
    async with db.SessionLocal() as session:
        await session.execute(text("TRUNCATE job_events, jobs, runs, settings"))
        await session.commit()
    settings_store.forget_runtime()


@pytest.fixture
async def token():
    from opus.settings_store import store_credentials
    value = "test-token"
    await store_credentials({"access_library_token": value})
    return value


@pytest.fixture
async def api(token, roster):
    import httpx
    import opus_auth
    from opus.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://opus",
                                 cookies={opus_auth.SESSION_COOKIE: roster("filip")}) as client:
        yield client


@pytest.fixture
def roster(monkeypatch):
    """Library's roster as this module sees it: an admin and a member. Hands back
    the session cookie either of them would carry."""
    import opus_auth
    from opus_auth import ADMIN, USER
    from opus_auth.authority import Person

    from opus import auth

    class Roster:
        people = {"filip": Person("filip", 1, ADMIN, "Filip"), "ana": Person("ana", 1, USER, "Ana")}

        async def current(self, name, version):
            found = self.people.get(name)
            return found if found and found.version == version else None

    monkeypatch.setattr(auth, "authority", Roster())
    return lambda name: opus_auth.issue(settings.session_key, name, 1)
