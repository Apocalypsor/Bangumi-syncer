"""Plex 主动同步调度器，与配置页保存联动。"""

from ...core.config import config_manager
from ...core.logging import logger
from ..base.scheduler import BaseScheduler
from .sync_service import PlexPollBusyError, plex_poll_sync_service


class PlexPollScheduler(BaseScheduler):
    JOB_ID = "plex_poll_sync"
    DEFAULT_CRON = "*/15 * * * *"
    DRIVER_NAME = "Plex 主动同步"

    def _is_enabled(self) -> bool:
        cfg = self._get_driver_config()
        return bool(
            cfg["enabled"] and all(cfg[key] for key in ("url", "token", "user_name"))
        )

    def _get_driver_config(self) -> dict:
        return config_manager.get_plex_poll_config()

    async def _run_sync_job(self) -> None:
        if not self._is_enabled():
            return
        try:
            await plex_poll_sync_service.run_sync()
        except PlexPollBusyError:
            logger.debug("Plex 已有同步任务，本次定时触发跳过")


plex_poll_scheduler = PlexPollScheduler()
