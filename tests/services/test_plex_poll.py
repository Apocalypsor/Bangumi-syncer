"""Plex 主动拉取：分页、手动已看、持久化去重与重试。"""

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import respx

from app.core.database.connection import DatabaseConnection
from app.core.database.plex_poll import PlexPollRepository
from app.models.sync import SyncResponse
from app.services.plex_poll import sync_service as poll_module
from app.services.plex_poll.reader import (
    PlexReader,
    PlexReadError,
    PlexScanTimeout,
    PlexWatchRecord,
)
from app.services.plex_poll.scheduler import PlexPollScheduler
from app.services.plex_poll.sync_service import PlexPollBusyError, PlexPollSyncService


def episode(key="1", **overrides):
    return {
        "ratingKey": key,
        "type": "episode",
        "grandparentTitle": "测试番剧",
        "title": "单集标题",
        "originalTitle": "Episode Title",
        "parentIndex": 1,
        "index": int(key),
        "viewCount": 1,
        **overrides,
    }


def response(container):
    return httpx.Response(200, json={"MediaContainer": container})


@pytest.mark.asyncio
async def test_reader_paginates_short_pages_and_filters_watched():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/identity":
            return response({"machineIdentifier": "server-one"})
        if request.url.path == "/library/sections":
            return response(
                {
                    "Directory": [
                        {"key": "1", "type": "show"},
                        {"key": "2", "type": "artist"},
                        {"key": "3", "type": "movie"},
                    ]
                }
            )
        offset = int(request.url.params["X-Plex-Container-Start"])
        if request.url.path == "/library/sections/1/all":
            assert request.url.params["episode.viewCount>>"] == "0"
            assert request.url.params["type"] == "4"
            return response(
                {
                    "offset": offset,
                    "totalSize": 3,
                    "Metadata": (
                        [episode("1"), episode("2", viewCount=0)]
                        if offset == 0
                        else [episode("3")]
                    ),
                }
            )
        assert request.url.path == "/library/sections/3/all"
        assert request.url.params["unwatched"] == "0"
        assert request.url.params["type"] == "1"
        return response(
            {
                "totalSize": 1,
                "Metadata": [
                    {"ratingKey": "4", "type": "movie", "title": "电影", "viewCount": 2}
                ],
            }
        )

    with respx.mock(base_url="http://plex:32400") as mock:
        mock.route().mock(side_effect=handle)
        async with PlexReader("http://plex:32400", "private-token") as reader:
            assert await reader.server_id() == "server-one"
            records = [rec async for rec in reader.watched()]
    assert [r.rating_key for r in records] == ["1", "3", "4"]
    assert all(r.headers["X-Plex-Token"] == "private-token" for r in requests)
    assert all("private-token" not in str(r.url) for r in requests)
    item = records[0].to_custom_item("alice")
    assert item.title == "测试番剧"
    assert item.ori_title is None
    assert item.source == "plex_poll"
    assert item.user_name == "alice"
    assert item.season == item.episode == 1
    assert "lastViewedAt" not in records[0].metadata  # 手动标记可无时间戳
    assert records[-1].to_custom_item("alice").media_type == "movie"


@pytest.mark.asyncio
async def test_library_selection_and_unknown_library():
    with respx.mock(base_url="http://plex") as mock:
        mock.get("/library/sections").mock(
            return_value=response(
                {
                    "Directory": [
                        {"key": "1", "type": "show"},
                        {"key": "2", "type": "movie"},
                    ]
                }
            )
        )
        selected = mock.get("/library/sections/2/all").mock(
            return_value=response({"totalSize": 0})
        )
        async with PlexReader("http://plex", "token") as reader:
            assert [r async for r in reader.watched("2")] == []
            with pytest.raises(PlexReadError, match="library_ids"):
                [r async for r in reader.watched("9")]
        assert selected.call_count == 1


@pytest.mark.asyncio
async def test_xml_identity_library_and_episode_responses():
    with respx.mock(base_url="http://plex") as mock:
        mock.get("/identity").mock(
            return_value=httpx.Response(
                200, text='<MediaContainer machineIdentifier="xml-server"/>'
            )
        )
        mock.get("/library/sections").mock(
            return_value=httpx.Response(
                200,
                text='<MediaContainer size="1"><Directory key="1" type="show"/></MediaContainer>',
            )
        )
        mock.get("/library/sections/1/all").mock(
            return_value=httpx.Response(
                200,
                text='<MediaContainer totalSize="1" offset="0"><Video ratingKey="8" type="episode" viewCount="1" grandparentTitle="XML 番剧" parentIndex="0" index="2"/></MediaContainer>',
            )
        )
        async with PlexReader("http://plex", "token") as reader:
            assert await reader.server_id() == "xml-server"
            records = [r async for r in reader.watched()]
    assert len(records) == 1
    assert records[0].to_custom_item("alice").season == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filtered_response",
    [response({"size": 0}), httpx.Response(400), httpx.Response(422)],
)
async def test_unsupported_episode_filter_falls_back_once(filtered_response):
    def handle(request):
        if "episode.viewCount>>" in request.url.params:
            return filtered_response
        return response(
            {
                "totalSize": 3,
                "Metadata": [
                    episode("1"),
                    episode("2", viewCount=0, lastViewedAt=100),
                    episode("3", viewCount=0, viewOffset=1000),
                ],
            }
        )

    with respx.mock(base_url="http://plex") as mock:
        mock.get("/library/sections").mock(
            return_value=response({"Directory": [{"key": "1", "type": "show"}]})
        )
        pages = mock.get("/library/sections/1/all").mock(side_effect=handle)
        async with PlexReader("http://plex", "token") as reader:
            assert [r.rating_key async for r in reader.watched()] == ["1"]
        assert pages.call_count == 2


@pytest.mark.asyncio
async def test_auth_failure_never_falls_back_to_full_library():
    with respx.mock(base_url="http://plex") as mock:
        mock.get("/library/sections").mock(
            return_value=response({"Directory": [{"key": "1", "type": "show"}]})
        )
        pages = mock.get("/library/sections/1/all").mock(
            return_value=httpx.Response(401)
        )
        async with PlexReader("http://plex", "token") as reader:
            with pytest.raises(PlexReadError, match="401"):
                [r async for r in reader.watched()]
        assert pages.call_count == 1


@pytest.mark.asyncio
async def test_scan_budget_also_bounds_requests_without_watched_results():
    import time

    async def stalled_response(request):
        await asyncio.Event().wait()

    with respx.mock(base_url="http://plex", assert_all_called=False) as mock:
        mock.get("/identity").mock(side_effect=stalled_response)
        async with PlexReader(
            "http://plex", "token", deadline=time.monotonic() + 0.05
        ) as reader:
            with pytest.raises(PlexScanTimeout):
                await reader.server_id()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_page",
    [
        {"Metadata": [episode()], "totalSize": 2},
        {"Metadata": [], "totalSize": 2},
        {"Metadata": [episode("2")], "totalSize": 2, "offset": 0},
    ],
)
async def test_broken_pagination_is_not_silently_successful(second_page):
    with respx.mock(base_url="http://plex") as mock:
        mock.get("/library/sections").mock(
            return_value=response({"Directory": [{"key": "1", "type": "show"}]})
        )
        mock.get("/library/sections/1/all").mock(
            side_effect=[
                response({"Metadata": [episode()], "totalSize": 2}),
                response(second_page),
            ]
        )
        async with PlexReader("http://plex", "token") as reader:
            with pytest.raises(PlexReadError):
                [r async for r in reader.watched()]


@pytest.mark.asyncio
async def test_missing_total_reads_until_empty():
    with respx.mock(base_url="http://plex") as mock:
        mock.get("/library/sections").mock(
            return_value=response({"Directory": [{"key": "1", "type": "show"}]})
        )
        pages = mock.get("/library/sections/1/all").mock(
            side_effect=[
                response({"Metadata": [episode()]}),
                response({"Metadata": [episode("2")]}),
                response({"size": 0}),
            ]
        )
        async with PlexReader("http://plex", "token") as reader:
            assert len([r async for r in reader.watched()]) == 2
        assert pages.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_response",
    [httpx.Response(401, text="secret"), httpx.Response(200, text="secret")],
)
async def test_reader_errors_do_not_expose_token_or_body(bad_response):
    with respx.mock(base_url="http://plex") as mock:
        mock.get("/identity").mock(return_value=bad_response)
        async with PlexReader("http://plex", "secret") as reader:
            with pytest.raises(PlexReadError) as exc:
                await reader.server_id()
            assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/plex",
        "http://user:secret@plex",
        "http://plex?token=secret",
        "https://",
    ],
)
def test_reject_credential_urls(url):
    with pytest.raises(PlexReadError):
        PlexReader(url, "token")


@pytest.fixture
def sync_env(monkeypatch, tmp_path):
    cfg = {
        "enabled": True,
        "url": "http://plex",
        "token": "secret",
        "user_name": "alice",
        "library_ids": "",
        "sync_interval": "*/15 * * * *",
    }
    conn = DatabaseConnection(str(tmp_path / "history.db"))
    repository = PlexPollRepository(conn)
    records = [PlexWatchRecord("1", episode()), PlexWatchRecord("2", episode("2"))]

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def server_id(self):
            return "server-one"

        async def watched(self, library_ids):
            for record in records:
                yield record

    sync = Mock(return_value=SyncResponse(status="success", message="ok"))
    monkeypatch.setattr(poll_module, "PlexReader", Reader)
    monkeypatch.setattr(
        poll_module.config_manager, "get_plex_poll_config", lambda: dict(cfg)
    )
    monkeypatch.setattr(
        poll_module.config_manager, "get_scheduler_config", lambda: {"job_timeout": 300}
    )
    monkeypatch.setattr(poll_module.database_manager, "plex_poll", repository)
    monkeypatch.setattr(poll_module, "notify_batch_sync_summary", Mock())
    monkeypatch.setattr(poll_module, "notify_source_event", Mock())
    # 拉取驱动只依赖统一入口；避免初始化完整匹配器时下载 bangumi-data。
    monkeypatch.setitem(
        sys.modules,
        "app.services.sync_service",
        SimpleNamespace(sync_service=SimpleNamespace(sync_custom_item=sync)),
    )
    yield cfg, records, sync, conn
    conn.close()


@pytest.mark.asyncio
async def test_persistent_dedup_full_sync_and_user_isolation(sync_env):
    cfg, records, sync, conn = sync_env
    assert (await PlexPollSyncService().run_sync()).synced_count == 2
    conn.close()  # 重新连接与新服务实例仍去重
    assert (await PlexPollSyncService().run_sync()).skipped_count == 2
    records.append(PlexWatchRecord("3", episode("3")))
    assert (await PlexPollSyncService().run_sync()).synced_count == 1
    assert (await PlexPollSyncService().run_sync(full=True)).synced_count == 3
    cfg["token"] = "other-user-token"
    assert (await PlexPollSyncService().run_sync()).synced_count == 3
    cfg["user_name"] = "bob"
    assert (await PlexPollSyncService().run_sync()).synced_count == 3
    assert sync.call_args.args[0].user_name == "bob"
    rows = (
        conn._get_connection().execute("SELECT scope FROM plex_poll_history").fetchall()
    )
    assert all("secret" not in row[0] and len(row[0]) == 64 for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,data",
    [
        ("error", None),
        ("ignored", None),
        ("queued", None),
        ("success", {"account_results": [{"status": "success"}, {"status": "failed"}]}),
        ("success", {"account_results": [{"status": "success", "mark_status": -1}]}),
    ],
)
async def test_failure_ignored_and_partial_success_remain_retryable(
    sync_env, status, data
):
    _, _, sync, _ = sync_env
    sync.side_effect = [
        SyncResponse(status=status, message="retry", data=data),
        SyncResponse(status="success", message="ok"),
    ]
    first = await PlexPollSyncService().run_sync()
    assert first.synced_count == 1
    sync.side_effect = None
    second = await PlexPollSyncService().run_sync()
    assert second.synced_count == second.skipped_count == 1


@pytest.mark.asyncio
async def test_malformed_item_does_not_block_later_items(sync_env):
    _, records, sync, _ = sync_env
    del records[0].metadata["parentIndex"]
    result = await PlexPollSyncService().run_sync()
    assert result.error_count == result.synced_count == 1
    assert sync.call_count == 1


@pytest.mark.asyncio
async def test_time_budget_retains_progress_for_next_round(sync_env, monkeypatch):
    monkeypatch.setattr(
        poll_module.config_manager, "get_scheduler_config", lambda: {"job_timeout": 1}
    )
    clock = Mock(side_effect=[0, 0, 2])
    monkeypatch.setattr(poll_module, "time", SimpleNamespace(monotonic=clock))
    result = await PlexPollSyncService().run_sync()
    assert result.synced_count == 1
    assert "时间上限" in result.message
    clock.side_effect = None
    clock.return_value = 0
    result = await PlexPollSyncService().run_sync()
    assert result.synced_count == result.skipped_count == 1


@pytest.mark.asyncio
async def test_history_write_failure_remains_retryable(sync_env, monkeypatch):
    original_save = poll_module.database_manager.plex_poll.save
    with monkeypatch.context() as m:
        m.setattr(
            poll_module.database_manager.plex_poll,
            "save",
            Mock(side_effect=OSError("disk full")),
        )
        result = await PlexPollSyncService().run_sync()
        assert not result.success
        assert result.error_count == 2
    assert poll_module.database_manager.plex_poll.save == original_save
    assert (await PlexPollSyncService().run_sync()).synced_count == 2


def test_config_normalization_and_token_is_sensitive(monkeypatch):
    from app.core.config_schema import is_sensitive_field, scheduler_id_for_section

    monkeypatch.setattr(poll_module.config_manager, "get_section", lambda *args: {})
    config = poll_module.config_manager.get_plex_poll_config()
    assert config["enabled"] is False
    assert config["sync_interval"] == PlexPollScheduler.DEFAULT_CRON
    monkeypatch.setattr(
        poll_module.config_manager,
        "get_section",
        lambda *args: {"enabled": "false", "sync_interval": "  "},
    )
    assert poll_module.config_manager.get_plex_poll_config()["enabled"] is False
    assert (
        poll_module.config_manager.get_plex_poll_config()["sync_interval"]
        == PlexPollScheduler.DEFAULT_CRON
    )
    assert is_sensitive_field("plex-poll", "token")
    assert scheduler_id_for_section("plex-poll") == "plex_poll"


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_overlap_background_write(sync_env):
    _, _, sync, _ = sync_env
    started, release = threading.Event(), threading.Event()

    def slow_sync(*args):
        started.set()
        release.wait(timeout=5)
        return SyncResponse(status="success", message="ok")

    sync.side_effect = slow_sync
    service = PlexPollSyncService()
    caller = asyncio.create_task(service.run_sync())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        with pytest.raises(PlexPollBusyError):
            service.start_sync()
        assert service.running
    finally:
        release.set()
        await service._task
    assert not service.running
    assert (await service.run_sync()).skipped_count == 2


@pytest.mark.asyncio
async def test_scheduler_enable_disable_and_config_reload(sync_env):
    cfg, _, _, _ = sync_env
    scheduler = PlexPollScheduler()
    try:
        assert await scheduler.start()
        assert scheduler.scheduler.get_job(scheduler.JOB_ID)
        cfg["sync_interval"] = "0 3 * * *"
        await scheduler.apply_config_after_save()
        assert "hour='3'" in str(scheduler.scheduler.get_job(scheduler.JOB_ID).trigger)
        cfg["enabled"] = False
        await scheduler.apply_config_after_save()
        assert scheduler.scheduler is None
        cfg["enabled"] = True
        cfg["token"] = ""
        assert not scheduler._is_enabled()
    finally:
        await scheduler.stop()
