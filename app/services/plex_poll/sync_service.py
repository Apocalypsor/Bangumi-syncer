"""Plex 已看状态 → CustomItem → 共享同步流程。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict

from ...core.background_tasks import register_background_task
from ...core.config import config_manager
from ...core.database import database_manager
from ...core.logging import logger
from ..base.models import BaseSyncResult
from ..base.notifier_helpers import notify_batch_sync_summary, notify_source_event
from .reader import PLEX_POLL_SOURCE, PlexReader, PlexReadError, PlexScanTimeout


class PlexPollBusyError(Exception):
    """已有一轮扫描在运行。"""


class PlexPollSyncService:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._last_result: BaseSyncResult | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "running": self.running,
            "last_result": asdict(self._last_result) if self._last_result else None,
        }

    def start_sync(self, *, full: bool = False) -> asyncio.Task:
        """手动与定时入口共用一个任务，HTTP 断开也不会重复提交。"""
        if self.running:
            raise PlexPollBusyError("Plex 同步正在运行，请等待本轮完成")
        cfg = config_manager.get_plex_poll_config()
        if not cfg["enabled"]:
            raise ValueError("Plex 主动同步未启用，请先在配置页启用并保存")
        if not all(cfg[key] for key in ("url", "token", "user_name")):
            raise ValueError("请先配置 Plex 服务器地址、Token 和媒体服务器用户名")
        self._last_result = None
        self._task = register_background_task(self._run_sync(cfg, full=full))
        return self._task

    async def run_sync(self, *, full: bool = False) -> BaseSyncResult:
        # 不能取消正在后台线程执行的 Bangumi 写入；保留任务直到写入和历史落盘完成。
        return await asyncio.shield(self.start_sync(full=full))

    async def _run_sync(self, cfg: dict, *, full: bool) -> BaseSyncResult:
        from ..sync_service import sync_service

        synced = skipped = errors = 0
        message = ""
        deadline = time.monotonic() + max(
            1, int(config_manager.get_scheduler_config().get("job_timeout", 300))
        )
        try:
            async with PlexReader(
                cfg["url"], cfg["token"], deadline=deadline
            ) as reader:
                server_id = await reader.server_id()
                # 同一服务器的不同 Plex Token/路由用户名不可共用去重记录。
                # Token 只参与摘要，绝不把凭据写入历史、日志或请求 URL。
                scope = hashlib.sha256(
                    json.dumps([server_id, cfg["token"], cfg["user_name"]]).encode()
                ).hexdigest()
                known = (
                    set()
                    if full
                    else await asyncio.to_thread(
                        database_manager.plex_poll.synced_keys, scope
                    )
                )
                async for record in reader.watched(cfg["library_ids"]):
                    if time.monotonic() >= deadline:
                        message = (
                            "本轮达到时间上限，已保存成功进度；下轮继续处理剩余条目"
                        )
                        break
                    if record.rating_key in known:
                        skipped += 1
                        continue
                    try:
                        item = record.to_custom_item(cfg["user_name"])
                        result = await asyncio.to_thread(
                            sync_service.sync_custom_item, item, PLEX_POLL_SOURCE
                        )
                        account_results = (result.data or {}).get(
                            "account_results"
                        ) or []
                        if result.status == "success" and all(
                            outcome.get("status") == "success"
                            and outcome.get("mark_status", 0) in (0, 1, 2)
                            for outcome in account_results
                        ):
                            await asyncio.to_thread(
                                database_manager.plex_poll.save,
                                scope,
                                record.rating_key,
                            )
                            known.add(record.rating_key)
                            synced += 1
                        elif result.status == "ignored":
                            # 权限/屏蔽规则等可能改变，下次仍允许重新尝试。
                            skipped += 1
                        else:
                            # 包含 queued / 部分账号失败，不提前确认成功。
                            errors += 1
                    except Exception as exc:
                        logger.warning(
                            "Plex 条目 %s 同步失败 (%s)，下轮重试",
                            record.rating_key,
                            type(exc).__name__,
                        )
                        errors += 1
                    await asyncio.sleep(0.05)
        except PlexScanTimeout:
            message = "本轮达到时间上限，已保存成功进度；下轮继续处理剩余条目"
        except Exception as exc:
            errors += 1
            message = (
                str(exc)
                if isinstance(exc, PlexReadError)
                else "Plex 扫描失败，请检查配置及服务器响应"
            )
            logger.error("%s (%s)", message, type(exc).__name__)
            notify_source_event(PLEX_POLL_SOURCE, "failed", error_message=message)

        summary = f"Plex 同步: 成功 {synced}, 跳过 {skipped}, 失败 {errors}"
        self._last_result = BaseSyncResult(
            errors == 0,
            f"{summary}；{message}" if message else summary,
            synced,
            skipped,
            errors,
        )
        notify_batch_sync_summary(
            PLEX_POLL_SOURCE, synced + skipped + errors, synced, errors, skipped=skipped
        )
        return self._last_result


plex_poll_sync_service = PlexPollSyncService()
