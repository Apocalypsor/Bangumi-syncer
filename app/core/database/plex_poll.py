"""Plex 主动拉取的持久化成功记录（独立于可清理的展示日志）。"""

from .base_repository import BaseRepository


class PlexPollRepository(BaseRepository):
    def synced_keys(self, scope: str) -> set[str]:
        return self._run_read(
            lambda conn: {
                row[0]
                for row in conn.execute(
                    "SELECT rating_key FROM plex_poll_history WHERE scope = ?", (scope,)
                )
            },
            error_msg="读取 Plex 同步历史失败",
            reraise=True,
        )

    def save(self, scope: str, rating_key: str) -> None:
        self._run_write(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO plex_poll_history (scope, rating_key) VALUES (?, ?)",
                (scope, rating_key),
            ),
            error_msg="保存 Plex 同步历史失败",
            reraise=True,
        )
