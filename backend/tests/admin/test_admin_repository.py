# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for Admin Repository."""


from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.repository.admin_repository import AdminRepository

# The suite has no Postgres, and the real media_items table cannot be created
# on SQLite because of its ARRAY and JSONB columns. The reaper's statement only
# touches the columns below, so a stand-in table of the same name lets the test
# assert which rows the reaper actually changes rather than how its SQL reads.
metadata = MetaData()
media_items_table = Table(
    "media_items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("status", String),
    Column("error_message", String),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
)

NOW = datetime.now(timezone.utc)
LONG_AGO = NOW - timedelta(hours=3)
RECENTLY = NOW - timedelta(minutes=5)


@asynccontextmanager
async def sqlite_session(rows: list[dict]):
    """Yields a session over a SQLite database seeded with the given rows."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await session.execute(media_items_table.insert(), rows)
            await session.commit()
            yield session
    finally:
        await engine.dispose()


async def get_rows(session) -> dict[int, tuple[str, str | None]]:
    result = await session.execute(
        select(
            media_items_table.c.id,
            media_items_table.c.status,
            media_items_table.c.error_message,
        ),
    )
    return {row.id: (row.status, row.error_message) for row in result.all()}


@pytest.mark.asyncio
async def test_cleanup_stuck_jobs_spares_a_job_its_worker_is_still_writing_to():
    rows = [
        {
            "id": 1,
            "status": "processing",
            "created_at": LONG_AGO,
            "updated_at": RECENTLY,
            "deleted_at": None,
        },
        {
            "id": 2,
            "status": "processing",
            "created_at": LONG_AGO,
            "updated_at": LONG_AGO,
            "deleted_at": None,
        },
    ]

    async with sqlite_session(rows) as session:
        count = await AdminRepository(db=session).cleanup_stuck_jobs()
        statuses = await get_rows(session)

    assert count == 1
    assert statuses[1][0] == "processing"
    assert statuses[2][0] == "stopped"


@pytest.mark.asyncio
async def test_cleanup_stuck_jobs_records_why_the_job_was_stopped():
    rows = [
        {
            "id": 1,
            "status": "processing",
            "created_at": LONG_AGO,
            "updated_at": LONG_AGO,
            "deleted_at": None,
        },
    ]

    async with sqlite_session(rows) as session:
        await AdminRepository(db=session).cleanup_stuck_jobs()
        statuses = await get_rows(session)

    assert statuses[1][1]


@pytest.mark.asyncio
async def test_cleanup_stuck_jobs_leaves_soft_deleted_rows_alone():
    rows = [
        {
            "id": 1,
            "status": "processing",
            "created_at": LONG_AGO,
            "updated_at": LONG_AGO,
            "deleted_at": NOW,
        },
    ]

    async with sqlite_session(rows) as session:
        count = await AdminRepository(db=session).cleanup_stuck_jobs()
        statuses = await get_rows(session)

    assert count == 0
    assert statuses[1][0] == "processing"


@pytest.mark.asyncio
async def test_cleanup_stuck_jobs_ignores_jobs_that_already_finished():
    rows = [
        {
            "id": 1,
            "status": "completed",
            "created_at": LONG_AGO,
            "updated_at": LONG_AGO,
            "deleted_at": None,
        },
    ]

    async with sqlite_session(rows) as session:
        count = await AdminRepository(db=session).cleanup_stuck_jobs()
        statuses = await get_rows(session)

    assert count == 0
    assert statuses[1][0] == "completed"
