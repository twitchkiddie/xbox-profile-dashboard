from fastapi import APIRouter
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel

import database as db
from config import CacheKey
from database.cache import _cache_invalidate

router = APIRouter()


class AuroraUpdate(BaseModel):
    enabled: bool


@router.post("/api/settings/aurora")
async def update_aurora(payload: AuroraUpdate):
    """Persist the aurora + glass preference. The next full load omits both."""
    await db.set_aurora_enabled(payload.enabled)
    _cache_invalidate(CacheKey.PAGE_CONTEXT)
    return ORJSONResponse({"enabled": payload.enabled})
