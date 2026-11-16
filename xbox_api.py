import logging
import os
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

from config import TITLE_HISTORY_MAX_ITEMS, TITLE_HISTORY_MAX_PAGES
from database import can_make_requests, sync_rate_limit_from_headers

load_dotenv()

log = logging.getLogger("xbox.api")

API_KEY = os.getenv("OPENXBL_API_KEY")
XUID = os.getenv("XBOX_XUID")
GAMERTAG = os.getenv("XBOX_GAMERTAG")
BASE_URL = "https://xbl.io/api/v2"
HEADERS: dict[str, str] = {"Accept": "application/json"}
if API_KEY:
    HEADERS["X-Authorization"] = API_KEY

_client: httpx.AsyncClient | None = None


class RateLimitExceeded(Exception):
    pass


def init_client():
    global _client
    if _client is not None:
        log.warning("HTTP client already initialized — skipping")
        return
    _client = httpx.AsyncClient(headers=HEADERS, timeout=30)


def get_client() -> httpx.AsyncClient | None:
    """Return the shared HTTP client for reuse (e.g., CDN proxy downloads)."""
    return _client


async def close_client():
    global _client
    if _client:
        await _client.aclose()
        _client = None


async def _request(method: str, endpoint: str, json_body: dict | None = None) -> dict:
    """Perform one budgeted OpenXBL call and return the raw JSON object.

    The v2 envelope ``{"content": ..., "code": ...}`` is left intact so callers
    that need a sibling ``pagingInfo`` can see it. ``_get`` unwraps ``content``.
    """
    if _client is None:
        raise RuntimeError("HTTP client not initialized — call init_client() first")
    if not can_make_requests():
        log.warning("Rate limit budget exhausted — blocking request to %s", endpoint)
        raise RateLimitExceeded("Rate limit budget exhausted (145/150). Try again later.")
    url = f"{BASE_URL}{endpoint}"
    log.debug("%s %s", method, url)
    if method == "POST":
        resp = await _client.post(url, json=json_body or {})
    else:
        resp = await _client.get(url)
    # Update rate limit from API headers (authoritative source)
    await sync_rate_limit_from_headers(resp.headers, endpoint, resp.status_code)
    if resp.status_code != 200:
        log.error("API %s returned %d: %s", endpoint, resp.status_code, resp.text[:200])
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _unwrap_content(data: dict) -> dict:
    # API wraps all responses in {"content": {...}, "code": ...} envelope
    if isinstance(data, dict) and "content" in data and isinstance(data["content"], dict):
        return data["content"]
    return data if isinstance(data, dict) else {}


async def _get(endpoint: str) -> dict:
    return _unwrap_content(await _request("GET", endpoint))


# Titlehub device tokens. Older payloads say "Win32"; the current Xbox app says "PC".
# Play Anywhere titles list an Xbox console alongside the PC token and must be kept.
_XBOX_CONSOLE_DEVICES = frozenset({"xbox360", "xboxone", "xboxseries", "xboxseriesx", "xboxseriess", "scarlett"})
_PC_ONLY_DEVICES = frozenset({"win32", "pc", "windows"})


def is_pc_only_devices(devices) -> bool:
    """True when the title is PC-only and has no Xbox console device.

    An empty or missing device list is kept: we don't drop a title we can't
    classify. Hybrids such as ``["XboxSeries", "Win32"]`` or ``["XboxOne", "PC"]``
    stay, because those are Play Anywhere / Xbox titles with a PC build.
    """
    if isinstance(devices, str):
        devices = [devices]
    if not isinstance(devices, list) or not devices:
        return False
    names = {str(d).strip().lower() for d in devices if str(d).strip()}
    if not names or names & _XBOX_CONSOLE_DEVICES:
        return False
    return names <= _PC_ONLY_DEVICES


def _continuation_token(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    paging = payload.get("pagingInfo") or payload.get("paging_info") or {}
    token = None
    if isinstance(paging, dict):
        token = paging.get("continuationToken") or paging.get("continuation_token")
    if not token:
        token = payload.get("continuationToken") or payload.get("continuation_token")
    if token is None:
        return None
    text = str(token).strip()
    if not text or text.lower() == "null":
        return None
    return text


def _titles_from_payload(payload: dict) -> list:
    if not isinstance(payload, dict):
        return []
    titles = payload.get("titles")
    if isinstance(titles, list):
        return titles
    content = payload.get("content")
    if isinstance(content, dict):
        inner = content.get("titles")
        if isinstance(inner, list):
            return inner
    return []


def _map_title(title: dict) -> dict | None:
    title_id = title.get("titleId")
    name = title.get("name")
    if title_id is None or not name:
        return None
    devices = title.get("devices") or []
    if is_pc_only_devices(devices):
        return None
    ach = title.get("achievement") or {}
    hist = title.get("titleHistory") or {}
    gp = title.get("gamePass") or {}
    return {
        "title_id": str(title_id),
        "name": name,
        "display_image": title.get("displayImage", ""),
        "devices": devices if isinstance(devices, list) else [],
        "current_gamerscore": ach.get("currentGamerscore", 0),
        "total_gamerscore": ach.get("totalGamerscore", 0),
        "progress_percentage": ach.get("progressPercentage", 0),
        "current_achievements": ach.get("currentAchievements", 0),
        "total_achievements": ach.get("totalAchievements", 0),
        "last_played": hist.get("lastTimePlayed"),
        "xbox_live_tier": title.get("xboxLiveTier"),
        "pfn": title.get("pfn"),
        "is_gamepass": gp.get("isGamePass", False),
    }


async def _page_titles(path: str) -> tuple[list[dict], int]:
    """Follow maxItems + continuationToken until the history is exhausted."""
    titles: list[dict] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    continuation: str | None = None
    api_calls = 0

    for page_index in range(TITLE_HISTORY_MAX_PAGES):
        if page_index and not can_make_requests():
            log.warning(
                "Title history paging stopped — budget exhausted after %d titles (%d calls)",
                len(titles),
                api_calls,
            )
            break
        params: list[tuple[str, str]] = [("maxItems", str(TITLE_HISTORY_MAX_ITEMS))]
        if continuation:
            params.append(("continuationToken", continuation))
        raw = await _request("GET", f"{path}?{urlencode(params)}")
        api_calls += 1
        page = _titles_from_payload(raw)
        content = raw.get("content") if isinstance(raw.get("content"), dict) else None
        token = _continuation_token(content) or _continuation_token(raw)
        added = 0
        for title in page:
            if not isinstance(title, dict):
                continue
            tid = str(title.get("titleId") or "")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            titles.append(title)
            added += 1
        log.info(
            "Title history %s page %d: %d new titles (%d total)",
            path,
            page_index + 1,
            added,
            len(titles),
        )
        if not token or token in seen_tokens or added == 0:
            break
        seen_tokens.add(token)
        continuation = token

    return titles, api_calls


async def get_profile() -> dict:
    return await _get("/account")


async def resolve_identity() -> tuple[str, str]:
    """Fetch XUID and gamertag from /account and populate module globals.

    Called at startup when XBOX_XUID or XBOX_GAMERTAG are not set in .env.
    Returns (xuid, gamertag).
    """
    global XUID, GAMERTAG
    data = await get_profile()
    users = data.get("profileUsers", [])
    if not users:
        raise ValueError("No profileUsers in /account response")
    user = users[0]
    xuid = str(user.get("id", ""))
    gamertag = ""
    for s in user.get("settings", []):
        if s.get("id") == "Gamertag":
            gamertag = s.get("value", "")
            break
    if not xuid:
        raise ValueError("Could not extract XUID from /account response")
    XUID = xuid
    GAMERTAG = gamertag or GAMERTAG
    log.info("Identity resolved: gamertag=%s xuid=%s", GAMERTAG, XUID)
    return XUID, GAMERTAG  # type: ignore[return-value]


async def get_all_games() -> list[dict]:
    """Fetch the full title history, paging past titlehub's default 100.

    ``/player/titleHistory/{xuid}`` is the documented OpenXBL route. ``maxItems``
    is what the Xbox app sends (10000); ``pagingInfo.continuationToken`` covers
    services that still page. ``/titles/{xuid}`` is the fallback — it is the
    route that returned exactly 100 when called with no query string.

    ``last_api_calls`` records how many requests this invocation spent so sync
    logs can count multi-page fetches. Verified against OpenXBL's published
    schema and the titlehub contract, not a live key.
    """
    raw_titles: list[dict] = []
    api_calls = 0
    primary = f"/player/titleHistory/{XUID}"
    try:
        raw_titles, api_calls = await _page_titles(primary)
    except httpx.HTTPStatusError as exc:
        # The failed call was already logged against the rate budget inside _request.
        if exc.response.status_code not in (404, 405):
            get_all_games.last_api_calls = 1
            raise
        log.warning("Title history %s returned %s — falling back to /titles", primary, exc.response.status_code)
        raw_titles, extra = await _page_titles(f"/titles/{XUID}")
        api_calls = extra + 1

    games = []
    for title in raw_titles:
        mapped = _map_title(title)
        if mapped is not None:
            games.append(mapped)
    get_all_games.last_api_calls = api_calls
    log.info(
        "Returning %d games from %d titles (%d API calls, PC-only filtered)",
        len(games),
        len(raw_titles),
        api_calls,
    )
    return games


get_all_games.last_api_calls = 1


def library_api_calls(fetcher=None) -> int:
    """How many title-history requests the last get_all_games spent.

    Mocks that replace get_all_games don't set an int, so callers fall back to 1.
    """
    raw = getattr(fetcher if fetcher is not None else get_all_games, "last_api_calls", None)
    if isinstance(raw, int) and raw > 0:
        return raw
    return 1


async def get_game_stats(title_id: str) -> dict:
    data = await _get(f"/achievements/stats/{title_id}")
    minutes = None
    for stat_list in data.get("statlistscollection", []):
        for stat in stat_list.get("stats", []):
            if stat.get("name") == "MinutesPlayed":
                minutes = int(stat.get("value", 0))
    return {"minutes_played": minutes}


async def get_title_achievements(title_id: str) -> tuple[list[dict], int]:
    """Fetch all title achievements, paginating if >150. Returns (achievements, api_calls)."""
    all_achs = []
    api_calls = 0
    continuation = None

    while True:
        url = f"/achievements/title/{title_id}"
        if continuation:
            url += "?" + urlencode({"continuationToken": continuation})
        data = await _get(url)
        api_calls += 1
        page_achs = data.get("achievements", [])
        all_achs.extend(page_achs)

        paging = data.get("pagingInfo", {})
        continuation = paging.get("continuationToken")
        if not continuation:
            break

        log.debug(
            "Title %s: paginating (%d so far, continuation=%s)",
            title_id,
            len(all_achs),
            continuation[:20] if continuation else "",
        )
        if not can_make_requests():
            log.warning(
                "Title %s: stopping pagination — budget exhausted (%d achievements so far)", title_id, len(all_achs)
            )
            break

    log.info("Title achievements for %s: %d achievements in %d API call(s)", title_id, len(all_achs), api_calls)
    return all_achs, api_calls


async def get_player_achievements(title_id: str) -> list[dict]:
    """Fetch player's achievement progress for a title, paginating if needed."""
    all_achs = []
    continuation = None

    while True:
        url = f"/achievements/player/{XUID}/{title_id}"
        if continuation:
            url += "?" + urlencode({"continuationToken": continuation})
        data = await _get(url)
        all_achs.extend(data.get("achievements", []))

        paging = data.get("pagingInfo", {})
        continuation = paging.get("continuationToken")
        if not continuation:
            break

        log.debug("Player achs %s: paginating (%d so far)", title_id, len(all_achs))
        if not can_make_requests():
            log.warning("Player achs %s: stopping pagination — budget exhausted (%d so far)", title_id, len(all_achs))
            break

    return all_achs


async def get_x360_achievements(title_id: str) -> list[dict]:
    """Fetch Xbox 360 achievements (uses legacy endpoint). Returns only unlocked achievements."""
    data = await _get(f"/achievements/x360/{XUID}/title/{title_id}")
    achs = data.get("achievements", [])
    log.info("X360 achievements for %s: %d returned (unlocked only)", title_id, len(achs))
    return achs


async def get_friends() -> list[dict]:
    data = await _get("/friends")
    return data.get("people", [])


async def get_screenshots(continuation_token: str | None = None) -> tuple[list[dict], str | None]:
    """Fetch one page of DVR screenshots (100/page). Returns (screenshots, next_token)."""
    endpoint = "/dvr/screenshots"
    if continuation_token:
        endpoint += "?" + urlencode({"continuationToken": continuation_token})
    data = await _get(endpoint)
    values = data.get("screenshots", data.get("values", []))
    next_token = data.get("continuationToken") or data.get("pagingInfo", {}).get("continuationToken")
    log.info("Screenshots page: %d items, has_next=%s", len(values), bool(next_token))
    return values, next_token
