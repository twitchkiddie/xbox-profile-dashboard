"""Central configuration — all policy constants live here.

APP_VERSION follows semver. Bump minor for behavior changes and patch for fixes.
Import from this module instead of hardcoding values throughout the codebase.
To tune sync behaviour, rate limits, or cache lifetimes, change only this file.
"""

APP_VERSION = "1.5.0"

# ── Rate Limiting ──────────────────────────────────────────────────────────────
RATE_LIMIT_MAX = 150  # OpenXBL hard cap per hour
RATE_LIMIT_BUDGET = 145  # Conservative budget (5-call safety buffer)

# ── Sync Policy ────────────────────────────────────────────────────────────────
UNIFIED_SYNC_CONCURRENCY = 5  # Parallel game syncs during user-triggered sync
SCHEDULED_SYNC_CONCURRENCY = 3  # Parallel game syncs during background scheduled sync
UNIFIED_GAME_BUDGET_PCT = 0.85  # Fraction of remaining budget for game details phase
# Spend most of the hourly budget on detail backfill. The 5-call safety buffer
# stays in RATE_LIMIT_BUDGET (limit - 5); this cap never crosses it.
SCHEDULED_GAME_BUDGET_PCT = 0.90  # Fraction of remaining budget for scheduled detail sync
SCHEDULED_GAME_BUDGET_CAP = 130  # Hard cap on API calls per scheduled detail sync run
MIN_SYNC_BUDGET = 5  # Minimum remaining calls required to start any sync

# ── Image proxy warming ────────────────────────────────────────────────────────
# Proxy widths pre-encoded into the /img disk cache at sync time so first page
# views never pay the upstream-fetch + encode cost. Mirrors the template
# `thumb()` sizes for game art (48/160/200/240/480 → size * 2).
IMG_WARM_WIDTHS = (96, 320, 400, 480, 960)

# ── Pagination ─────────────────────────────────────────────────────────────────
LIBRARY_PAGE_SIZE = 50
ACHIEVEMENTS_PAGE_SIZE = 60
CAPTURES_PAGE_SIZE = 50
TIMELINE_PAGE_SIZE = 50
LIBRARY_EXPORT_LIMIT = 10_000

# Titlehub returns 100 titles when maxItems is omitted. The Xbox app asks for
# 10000 in one shot; if the service still pages, follow continuationToken.
TITLE_HISTORY_MAX_ITEMS = 10_000
TITLE_HISTORY_MAX_PAGES = 40

# POST /player/stats accepts many MinutesPlayed rows per call. 100 matches the
# batch size other OpenXBL clients use without tripping payload limits.
STATS_BATCH_SIZE = 100


class CacheKey:
    """String constants for in-memory cache keys.

    Use these instead of bare string literals so typos are caught at import
    time and all invalidation sites stay in sync automatically.
    """

    DASHBOARD_STATS = "dashboard_stats"
    ACHIEVEMENT_STATS = "achievement_stats"
    PAGE_CONTEXT = "page_context"
    HEATMAP_ROLLING = "heatmap_rolling"
    HEATMAP_YEAR_RANGE = "heatmap_year_range"
    FRIENDS = "friends"
    GAMES_WITH_ACHIEVEMENTS = "games_with_achievements"
    TIMELINE_STATS = "timeline_stats"

    # Prefixes covering the dynamically-keyed entries below (heatmap_year/activity),
    # for use with _cache_invalidate_prefix. HEATMAP_PREFIX also matches
    # HEATMAP_ROLLING and HEATMAP_YEAR_RANGE by construction; TIMELINE_PREFIX
    # matches TIMELINE_STATS and the per-page-size timeline event entries.
    HEATMAP_PREFIX = "heatmap_"
    ACTIVITY_PREFIX = "activity_"
    TIMELINE_PREFIX = "timeline_"

    @staticmethod
    def timeline_events(per_page: int) -> str:
        return f"timeline_events_p1_{per_page}"

    @staticmethod
    def heatmap_year(year: int) -> str:
        return f"heatmap_{year}"

    @staticmethod
    def activity(year: int, month: int) -> str:
        return f"activity_{year}_{month}"
