"""Plex 主动同步：已登录用户可启动扫描并查询结果。"""

from fastapi import APIRouter, Depends, HTTPException, Query

from ..services.plex_poll.sync_service import PlexPollBusyError, plex_poll_sync_service
from .deps import get_current_user_flexible

router = APIRouter(prefix="/api/plex-poll", tags=["plex-poll"])


@router.get("/status")
async def plex_poll_status(
    current_user: dict = Depends(get_current_user_flexible),
) -> dict:
    return {"status": "success", "data": plex_poll_sync_service.status()}


@router.post("/sync/manual", status_code=202)
async def plex_poll_manual_sync(
    full: bool = Query(False, description="重新核对全部已看项目，包括本地已同步项目"),
    current_user: dict = Depends(get_current_user_flexible),
) -> dict:
    try:
        plex_poll_sync_service.start_sync(full=full)
    except PlexPollBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "status": "accepted",
        "message": "Plex 同步已开始，可在此查看结果或到同步记录查看详情",
    }
