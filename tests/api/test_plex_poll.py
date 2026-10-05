"""Plex 主动同步 API 的鉴权、启动和状态查询。"""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import plex_poll
from app.api.deps import get_current_user_flexible
from app.services.plex_poll.sync_service import PlexPollBusyError


@pytest.mark.asyncio
async def test_manual_sync_and_safe_status(monkeypatch):
    app = FastAPI()
    app.include_router(plex_poll.router)
    app.dependency_overrides[get_current_user_flexible] = lambda: {"username": "admin"}
    service = Mock()
    service.status.return_value = {"running": True, "last_result": None}
    monkeypatch.setattr(plex_poll, "plex_poll_sync_service", service)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post("/api/plex-poll/sync/manual?full=true")
        assert result.status_code == 202
        service.start_sync.assert_called_once_with(full=True)
        assert (await client.get("/api/plex-poll/status")).json()[
            "data"
        ] == service.status.return_value
        service.start_sync.side_effect = PlexPollBusyError("busy")
        assert (await client.post("/api/plex-poll/sync/manual")).status_code == 409
        service.start_sync.side_effect = ValueError("disabled")
        assert (await client.post("/api/plex-poll/sync/manual")).status_code == 400


@pytest.mark.asyncio
async def test_endpoints_require_auth(monkeypatch):
    app = FastAPI()
    app.include_router(plex_poll.router)
    service = Mock()
    monkeypatch.setattr(plex_poll, "plex_poll_sync_service", service)

    def reject():
        raise HTTPException(status_code=401)

    app.dependency_overrides[get_current_user_flexible] = reject
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/plex-poll/status")).status_code == 401
        assert (await client.post("/api/plex-poll/sync/manual")).status_code == 401
    service.start_sync.assert_not_called()
