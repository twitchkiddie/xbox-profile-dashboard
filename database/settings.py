import os

from .connection import get_connection, get_read_connection

# Persisted preference. "1" keeps today's animated aurora + glass. "0" turns both off.
AURORA_SETTING_KEY = "aurora_glass"


async def get_setting(key: str) -> str | None:
    conn = await get_read_connection()
    cursor = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


def _env_aurora_enabled() -> bool | None:
    raw = os.getenv("XBOX_AURORA")
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


async def aurora_enabled() -> bool:
    """Whether to render the aurora canvas and start the glass scripts.

    A saved setting wins. Otherwise ``XBOX_AURORA`` (0/1) applies. With neither
    set, the animated background stays on — that is the behavior today.
    """
    stored = await get_setting(AURORA_SETTING_KEY)
    if stored is not None:
        return stored == "1"
    from_env = _env_aurora_enabled()
    if from_env is not None:
        return from_env
    return True


async def set_aurora_enabled(enabled: bool) -> None:
    await set_setting(AURORA_SETTING_KEY, "1" if enabled else "0")


async def set_setting(key: str, value: str):
    conn = await get_connection()
    await conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()
