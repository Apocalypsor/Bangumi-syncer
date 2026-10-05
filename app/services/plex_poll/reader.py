"""只读 Plex 媒体库，分页获取当前账号的已看剧集与电影。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import httpx

from ...models.sync import CustomItem

PLEX_POLL_SOURCE = "plex_poll"
PAGE_SIZE = 200


class PlexReadError(Exception):
    """可直接展示给用户的错误，不包含 Token 或原始响应。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PlexScanTimeout(Exception):
    """整轮扫描的时间预算耗尽，已同步进度仍然有效。"""


@dataclass
class PlexWatchRecord:
    rating_key: str
    metadata: dict[str, Any]

    def to_custom_item(self, user_name: str) -> CustomItem:
        md = self.metadata
        movie = md.get("type") == "movie"
        title = md.get("title") if movie else md.get("grandparentTitle")
        if not title or not str(title).strip():
            raise ValueError("Plex 条目缺少电影或剧集标题")
        # episode.originalTitle 可能是单集名称，不能用于匹配整部番剧。
        return CustomItem(
            media_type="movie" if movie else "episode",
            title=title,
            ori_title=md.get("originalTitle") if movie else None,
            season=1 if movie else int(md["parentIndex"]),
            episode=1 if movie else int(md["index"]),
            release_date=md.get("originallyAvailableAt") or "",
            user_name=user_name,
            source=PLEX_POLL_SOURCE,
            raw_payload={
                "source": PLEX_POLL_SOURCE,
                "metadata": {
                    key: md.get(key)
                    for key in (
                        "ratingKey",
                        "type",
                        "title",
                        "grandparentTitle",
                        "originalTitle",
                        "parentIndex",
                        "index",
                        "viewCount",
                        "lastViewedAt",
                        "librarySectionID",
                        "originallyAvailableAt",
                    )
                },
            },
        )


class PlexReader:
    def __init__(self, url: str, token: str, *, deadline: float | None = None):
        self.deadline = deadline
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PlexReadError(
                "Plex 地址须为 http(s) 服务器地址，不能包含账号、密码或查询参数"
            )
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            headers={"Accept": "application/json", "X-Plex-Token": token},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> PlexReader:
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        await self.client.__aexit__(*args)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            if self.deadline is None:
                response = await self.client.get(path, params=params)
            else:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise PlexScanTimeout
                try:
                    response = await asyncio.wait_for(
                        self.client.get(path, params=params), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    raise PlexScanTimeout from None
            response.raise_for_status()
            if response.content.lstrip().startswith(b"<"):
                root = ElementTree.fromstring(response.content)
                if root.tag != "MediaContainer":
                    raise ValueError
                container = dict(root.attrib)
                # 只需要库与视频的一级属性；不解析或保存无关的媒体文件路径。
                for tag, key in (("Directory", "Directory"), ("Video", "Metadata")):
                    rows = [dict(child.attrib) for child in root.findall(tag)]
                    if rows:
                        container[key] = rows
            else:
                container = response.json()["MediaContainer"]
            if not isinstance(container, dict):
                raise ValueError
            return container
        except httpx.HTTPStatusError as exc:
            raise PlexReadError(
                f"Plex 返回 HTTP {exc.response.status_code}，请检查地址、Token 和媒体库权限",
                status_code=exc.response.status_code,
            ) from None
        except httpx.RequestError:
            raise PlexReadError("无法连接 Plex，请检查服务器地址、网络及证书") from None
        except (ValueError, KeyError, TypeError, ElementTree.ParseError):
            raise PlexReadError("Plex 返回了无法识别的数据，请检查服务器地址") from None

    async def server_id(self) -> str:
        identity = await self._get("identity")
        server_id = identity.get("machineIdentifier")
        if not server_id:
            raise PlexReadError("Plex 未返回服务器标识，无法安全区分同步记录")
        return str(server_id)

    async def watched(self, library_ids: str = "") -> AsyncIterator[PlexWatchRecord]:
        selected = {part.strip() for part in library_ids.split(",") if part.strip()}
        sections = (await self._get("library/sections")).get("Directory", [])
        available = {
            str(section["key"]): section
            for section in sections
            if section.get("type") in ("show", "movie")
        }
        if selected - available.keys():
            raise PlexReadError(
                "所选媒体库不存在、无权访问或不是剧集/电影库，请检查 library_ids"
            )
        for section_id, section in available.items():
            if selected and section_id not in selected:
                continue
            # 新版 Plex 支持按单集播放次数筛选；旧版拒绝或返回空时，
            # 只退回一次完整剧集扫描，并始终在本地核验 viewCount。
            for filtered in (True, False) if section["type"] == "show" else (True,):
                found = False
                try:
                    async for record in self._watched_section(
                        section_id, section["type"], filtered
                    ):
                        found = True
                        yield record
                except PlexReadError as exc:
                    if not (
                        filtered
                        and section["type"] == "show"
                        and not found
                        and exc.status_code in (400, 422)
                    ):
                        raise
                if found:
                    break

    async def _watched_section(
        self, section_id: str, section_type: str, filtered: bool
    ) -> AsyncIterator[PlexWatchRecord]:
        offset = 0
        seen: set[str] = set()
        while True:
            params = {
                "type": 4 if section_type == "show" else 1,
                "X-Plex-Container-Start": offset,
                "X-Plex-Container-Size": PAGE_SIZE,
            }
            if filtered:
                params[
                    "episode.viewCount>>" if section_type == "show" else "unwatched"
                ] = 0
            page = await self._get(
                f"library/sections/{quote(section_id, safe='')}/all", params
            )
            entries = page.get("Metadata")
            total = page.get("totalSize")
            if entries is None and (str(page.get("size")) == "0" or str(total) == "0"):
                entries = []
            if not isinstance(entries, list) or any(
                not isinstance(md, dict) or not md.get("ratingKey") for md in entries
            ):
                raise PlexReadError("Plex 返回无效的媒体列表，已停止本轮扫描")
            if not entries:
                if total is not None and offset < int(total):
                    raise PlexReadError("Plex 分页提前结束，请稍后重试")
                break
            if "offset" in page and int(page["offset"]) != offset:
                raise PlexReadError("Plex 返回了错误的分页位置，请稍后重试")
            keys = {str(md["ratingKey"]) for md in entries}
            if keys.issubset(seen):
                raise PlexReadError("Plex 返回重复分页，已停止本轮扫描")
            for md in entries:
                key = str(md["ratingKey"])
                if key in seen:
                    continue
                seen.add(key)
                if (
                    md.get("type") in ("episode", "movie")
                    and int(md.get("viewCount") or 0) > 0
                ):
                    yield PlexWatchRecord(key, md)
            offset += len(entries)
            if total is not None and offset >= int(total):
                break
            # Plex 可能返回比请求值小的页；无 totalSize 时继续读到空页。
