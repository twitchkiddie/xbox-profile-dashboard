"""
Tests for xbox_api.py using pytest-httpx to fake HTTP responses.

Strategy:
- Reset the module-level _client before each test so init_client() creates a
  fresh AsyncClient that pytest-httpx can intercept.
- Patch can_make_requests() → True and sync_rate_limit_from_headers() → no-op
  so tests never touch the real database.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

import xbox_api
from xbox_api import RateLimitExceeded

BASE = "https://xbl.io/api/v2"
XUID = xbox_api.XUID

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def fresh_client(httpx_mock):
    """Reset the global client before each test and patch DB helpers."""
    xbox_api._client = None
    xbox_api.init_client()
    with (
        patch("xbox_api.can_make_requests", return_value=True),
        patch("xbox_api.sync_rate_limit_from_headers", new_callable=AsyncMock),
    ):
        yield
    await xbox_api.close_client()


# ---------------------------------------------------------------------------
# _get — envelope unwrapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unwraps_content_envelope(httpx_mock):
    """API responses wrapped in {"content": {...}} are unwrapped transparently."""
    httpx_mock.add_response(
        url=f"{BASE}/account",
        json={"content": {"gamertag": "TestGamertag"}, "code": 200},
    )
    result = await xbox_api._get("/account")
    assert result == {"gamertag": "TestGamertag"}


@pytest.mark.asyncio
async def test_get_returns_raw_when_no_envelope(httpx_mock):
    """Responses without a content envelope are returned as-is."""
    httpx_mock.add_response(
        url=f"{BASE}/friends",
        json={"people": [{"gamertag": "FriendA"}]},
    )
    result = await xbox_api._get("/friends")
    assert result == {"people": [{"gamertag": "FriendA"}]}


@pytest.mark.asyncio
async def test_get_raises_on_http_error(httpx_mock):
    """Non-200 responses raise httpx.HTTPStatusError."""
    httpx_mock.add_response(url=f"{BASE}/account", status_code=429)
    with pytest.raises(httpx.HTTPStatusError):
        await xbox_api._get("/account")


@pytest.mark.asyncio
async def test_get_raises_rate_limit_exceeded_when_budget_exhausted(httpx_mock):
    """When can_make_requests() is False no HTTP call is made and RateLimitExceeded is raised."""
    with patch("xbox_api.can_make_requests", return_value=False):
        with pytest.raises(RateLimitExceeded):
            await xbox_api._get("/account")
    # No HTTP request should have been made
    assert httpx_mock.get_requests() == []


# ---------------------------------------------------------------------------
# get_all_games — field mapping and Win32 filter
# ---------------------------------------------------------------------------


def _make_title(title_id, name, devices, ach=None, gp=False):
    return {
        "titleId": title_id,
        "name": name,
        "displayImage": f"http://example.com/{title_id}.png",
        "devices": devices,
        "xboxLiveTier": "Full",
        "pfn": None,
        "achievement": ach
        or {
            "currentGamerscore": 100,
            "totalGamerscore": 1000,
            "progressPercentage": 10,
            "currentAchievements": 5,
            "totalAchievements": 50,
        },
        "titleHistory": {"lastTimePlayed": "2024-06-01T12:00:00Z"},
        "gamePass": {"isGamePass": gp},
    }


def test_is_pc_only_devices():
    """PC-only tokens are dropped; Xbox hybrids and unknown lists are kept."""
    assert xbox_api.is_pc_only_devices(["Win32"]) is True
    assert xbox_api.is_pc_only_devices(["PC"]) is True
    assert xbox_api.is_pc_only_devices(["windows"]) is True
    assert xbox_api.is_pc_only_devices(["Win32", "PC"]) is True
    assert xbox_api.is_pc_only_devices(["XboxSeries", "Win32"]) is False
    assert xbox_api.is_pc_only_devices(["XboxOne", "PC"]) is False
    assert xbox_api.is_pc_only_devices(["Xbox360"]) is False
    assert xbox_api.is_pc_only_devices([]) is False
    assert xbox_api.is_pc_only_devices(None) is False
    assert xbox_api.is_pc_only_devices(["Mobile"]) is False


@pytest.mark.asyncio
async def test_get_all_games_filters_win32_only(httpx_mock):
    """PC-only titles are excluded; Xbox and Play Anywhere titles are kept."""
    httpx_mock.add_response(
        url=f"{BASE}/player/titleHistory/{XUID}?maxItems=10000",
        json={
            "titles": [
                _make_title("111", "Xbox Game", ["XboxSeries", "Win32"]),
                _make_title("222", "PC Only", ["Win32"]),
                _make_title("333", "Modern PC", ["PC"]),
                _make_title("444", "Play Anywhere", ["XboxOne", "PC"]),
            ]
        },
    )
    games = await xbox_api.get_all_games()
    assert [g["title_id"] for g in games] == ["111", "444"]
    assert xbox_api.get_all_games.last_api_calls == 1


@pytest.mark.asyncio
async def test_get_all_games_follows_continuation_token(httpx_mock):
    """Pages past the historical 100-title cap via continuationToken."""
    httpx_mock.add_response(
        url=f"{BASE}/player/titleHistory/{XUID}?maxItems=10000",
        json={
            "titles": [_make_title("111", "Recent", ["XboxSeries"])],
            "pagingInfo": {"continuationToken": "page-2"},
        },
    )
    httpx_mock.add_response(
        url=f"{BASE}/player/titleHistory/{XUID}?maxItems=10000&continuationToken=page-2",
        json={
            "content": {
                "titles": [_make_title("222", "Destiny", ["XboxOne"])],
                "pagingInfo": {"continuationToken": None},
            },
            "code": 200,
        },
    )
    games = await xbox_api.get_all_games()
    assert [g["title_id"] for g in games] == ["111", "222"]
    assert xbox_api.get_all_games.last_api_calls == 2


@pytest.mark.asyncio
async def test_get_all_games_falls_back_to_titles_route(httpx_mock):
    """A 404 on titleHistory retries the legacy /titles route with the same paging."""
    httpx_mock.add_response(url=f"{BASE}/player/titleHistory/{XUID}?maxItems=10000", status_code=404)
    httpx_mock.add_response(
        url=f"{BASE}/titles/{XUID}?maxItems=10000",
        json={"titles": [_make_title("999", "Halo", ["XboxSeries"], gp=True)]},
    )
    games = await xbox_api.get_all_games()
    assert games[0]["title_id"] == "999"
    assert xbox_api.get_all_games.last_api_calls == 2


@pytest.mark.asyncio
async def test_get_all_games_maps_fields(httpx_mock):
    """All expected fields are present and correctly mapped."""
    httpx_mock.add_response(
        url=f"{BASE}/player/titleHistory/{XUID}?maxItems=10000",
        json={"titles": [_make_title("999", "Halo", ["XboxSeries"], gp=True)]},
    )
    games = await xbox_api.get_all_games()
    g = games[0]
    assert g["title_id"] == "999"
    assert g["name"] == "Halo"
    assert g["is_gamepass"] is True
    assert g["current_gamerscore"] == 100
    assert g["total_gamerscore"] == 1000
    assert g["last_played"] == "2024-06-01T12:00:00Z"


# ---------------------------------------------------------------------------
# get_game_stats — MinutesPlayed extraction
# ---------------------------------------------------------------------------


def test_parse_minutes_played_reads_grouped_and_top_level_shapes():
    """MinutesPlayed is not always a top-level statlistscollection of strings."""
    grouped = {
        "groups": [
            {
                "name": "Hero",
                "statListsCollection": [{"stats": [{"name": "MinutesPlayed", "titleId": "10", "value": 90}]}],
            }
        ]
    }
    top_level = {
        "content": {
            "statlistscollection": [
                {"stats": [{"name": "MinutesPlayed", "titleid": "11", "value": "15"}]},
            ]
        },
        "code": 200,
    }
    assert xbox_api.parse_minutes_played(grouped) == {"10": 90}
    assert xbox_api.parse_minutes_played(top_level) == {"11": 15}
    assert xbox_api.parse_minutes_played({"statlistscollection": []}) == {}


@pytest.mark.asyncio
async def test_get_game_stats_extracts_minutes_played(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/player/stats",
        json={
            "groups": [
                {
                    "name": "Hero",
                    "titleid": "123",
                    "statlistscollection": [
                        {
                            "stats": [
                                {"name": "MinutesPlayed", "titleid": "123", "value": "450"},
                                {"name": "SomethingElse", "value": "99"},
                            ]
                        }
                    ],
                }
            ]
        },
    )
    result = await xbox_api.get_game_stats("123")
    assert result == {"minutes_played": 450}


@pytest.mark.asyncio
async def test_get_game_stats_returns_none_when_missing(httpx_mock):
    """Returns None for minutes_played when the stat is absent."""
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/player/stats",
        json={"statlistscollection": []},
    )
    result = await xbox_api.get_game_stats("123")
    assert result == {"minutes_played": None}


@pytest.mark.asyncio
async def test_get_minutes_played_batches(httpx_mock, monkeypatch):
    """One POST covers STATS_BATCH_SIZE titles; the remainder is a second call."""
    monkeypatch.setattr(xbox_api, "STATS_BATCH_SIZE", 2)
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/player/stats",
        json={"statlistscollection": [{"stats": [{"name": "MinutesPlayed", "titleid": "1", "value": "5"}]}]},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/player/stats",
        json={
            "stats": [{"name": "MinutesPlayed", "titleId": "3", "value": 0}],
        },
    )
    found, calls = await xbox_api.get_minutes_played(["1", "2", "1", "3"])
    assert calls == 2
    assert found["1"] == 5
    assert found["3"] == 0
    assert "2" not in found


# ---------------------------------------------------------------------------
# get_title_achievements — pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_title_achievements_paginates(httpx_mock):
    """Follows continuationToken across pages and returns all achievements."""
    httpx_mock.add_response(
        url=f"{BASE}/achievements/title/ABC",
        json={"achievements": [{"id": "1"}], "pagingInfo": {"continuationToken": "tok1"}},
    )
    httpx_mock.add_response(
        url=f"{BASE}/achievements/title/ABC?continuationToken=tok1",
        json={"achievements": [{"id": "2"}], "pagingInfo": {"continuationToken": None}},
    )
    achs, api_calls = await xbox_api.get_title_achievements("ABC")
    assert len(achs) == 2
    assert api_calls == 2


@pytest.mark.asyncio
async def test_get_title_achievements_single_page(httpx_mock):
    """Stops after one page when there is no continuationToken."""
    httpx_mock.add_response(
        url=f"{BASE}/achievements/title/ABC",
        json={"achievements": [{"id": "1"}, {"id": "2"}], "pagingInfo": {}},
    )
    achs, api_calls = await xbox_api.get_title_achievements("ABC")
    assert len(achs) == 2
    assert api_calls == 1


# ---------------------------------------------------------------------------
# get_screenshots — continuation token and response shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_screenshots_returns_items_and_next_token(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/dvr/screenshots",
        json={"screenshots": [{"contentId": "s1"}, {"contentId": "s2"}], "continuationToken": "next123"},
    )
    items, next_token = await xbox_api.get_screenshots()
    assert len(items) == 2
    assert next_token == "next123"


@pytest.mark.asyncio
async def test_get_screenshots_passes_continuation_token_in_url(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/dvr/screenshots?continuationToken=tok99",
        json={"screenshots": [], "continuationToken": None},
    )
    items, next_token = await xbox_api.get_screenshots("tok99")
    assert items == []
    assert next_token is None
