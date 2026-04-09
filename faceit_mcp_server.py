"""FACEIT CS2 MCP Server — standalone single-file edition.

Exposes live CS2 FACEIT data as MCP tools for Claude Desktop / Claude Code.

Quick start
-----------
1. Install deps:
       pip install mcp aiohttp aiosqlite python-dotenv

2. Create a .env file (or pass env vars directly):
       FACEIT_API_KEY=your_key_here

3. Add to Claude Desktop config (claude_desktop_config.json):
       {
         "mcpServers": {
           "faceit-cs2": {
             "command": "python",
             "args": ["/full/path/to/faceit_mcp_server.py"],
             "env": { "FACEIT_API_KEY": "your_key_here" }
           }
         }
       }

   Or add via Claude Code CLI:
       claude mcp add faceit-cs2 -- python /full/path/to/faceit_mcp_server.py

Available tools
---------------
  get_player_stats    — ELO, level, region, lifetime K/D, HS%, win rate, streaks
  get_match_history   — last N matches with map, W/L, K/D, kills, HS%, K/R
  compare_players     — side-by-side stats for 2–6 FACEIT nicknames
  get_leaderboard     — registered users ranked by live ELO (requires DB with users)
  get_elo_trend       — stored ELO snapshots for a registered user (requires DB with history)

Environment variables
---------------------
  FACEIT_API_KEY   (required) FACEIT Data API v4 key
  DB_PATH          (optional) SQLite path — defaults to ~/.faceit-mcp/data.db
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parent / ".env")

FACEIT_API_KEY: str = (os.getenv("FACEIT_API_KEY") or "").strip()
DB_PATH: str = (os.getenv("DB_PATH") or "").strip() or str(Path.home() / ".faceit-mcp" / "data.db")

FACEIT_BASE_URL = "https://open.faceit.com/data/v4"
GAME_ID = "cs2"

HTTP_TIMEOUT_SEC = 15
FACEIT_RETRY_ATTEMPTS = 2
FACEIT_RETRY_BASE_DELAY = 1.5
FACEIT_RETRY_MAX_DELAY = 10.0
FACEIT_CIRCUIT_THRESHOLD = max(0, int(os.getenv("FACEIT_CIRCUIT_FAILURE_THRESHOLD", "4")))
FACEIT_CIRCUIT_OPEN_SEC = float(os.getenv("FACEIT_CIRCUIT_OPEN_SEC", "60"))

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC)
_LEADERBOARD_SEM = asyncio.Semaphore(8)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL Cache
# ---------------------------------------------------------------------------

class TTLCache:
    """LRU cache with per-entry TTL expiry."""

    def __init__(self, maxsize: int = 1000) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str, ttl: float) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.monotonic() - ts > ttl:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return val

    def set(self, key: str, value: Any) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (time.monotonic(), value)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)


# ---------------------------------------------------------------------------
# FACEIT API client
# ---------------------------------------------------------------------------

_TTL_PLAYER = 60.0
_TTL_NICKNAME = 120.0
_TTL_LIFETIME = 120.0
_TTL_MATCH_STATS = 60.0


class FaceitAPIError(Exception):
    pass


class FaceitNotFoundError(FaceitAPIError):
    pass


class FaceitUnavailableError(FaceitAPIError):
    pass


class FaceitCircuitOpenError(FaceitUnavailableError):
    pass


class FaceitRateLimitError(FaceitAPIError):
    pass


class FaceitAPI:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        cache: TTLCache | None = None,
    ) -> None:
        self._session = session
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._cache = cache
        self._circuit_open_until: float = 0.0
        self._circuit_fail_streak: int = 0

    async def _do_request(self, method: str, url: str, **kwargs: Any) -> Any:
        async with self._session.request(
            method, url, headers=self._headers, timeout=_HTTP_TIMEOUT, **kwargs
        ) as resp:
            if resp.status == 404:
                raise FaceitNotFoundError("Not found")
            if resp.status == 429:
                raise FaceitRateLimitError("Rate limited")
            if resp.status >= 500:
                raise FaceitUnavailableError(f"Server error {resp.status}")
            if resp.status >= 400:
                text = await resp.text()
                logger.warning("FACEIT %s %s", resp.status, text[:200])
                raise FaceitAPIError(f"API error {resp.status}")
            return await resp.json()

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{FACEIT_BASE_URL}{path}"
        last_exc: Exception = FaceitAPIError("unknown")

        if FACEIT_CIRCUIT_THRESHOLD > 0 and time.monotonic() < self._circuit_open_until:
            raise FaceitCircuitOpenError("FACEIT circuit open — cooling down.")

        for attempt in range(FACEIT_RETRY_ATTEMPTS):
            try:
                result = await self._do_request(method, url, **kwargs)
                self._circuit_fail_streak = 0
                return result
            except (FaceitRateLimitError, FaceitUnavailableError) as exc:
                last_exc = exc
                if attempt < FACEIT_RETRY_ATTEMPTS - 1:
                    delay = min(FACEIT_RETRY_MAX_DELAY, FACEIT_RETRY_BASE_DELAY * (2 ** attempt))
                    await asyncio.sleep(delay)
            except aiohttp.ServerTimeoutError:
                last_exc = FaceitUnavailableError(f"Timed out after {HTTP_TIMEOUT_SEC}s")
                if attempt < FACEIT_RETRY_ATTEMPTS - 1:
                    delay = min(FACEIT_RETRY_MAX_DELAY, FACEIT_RETRY_BASE_DELAY * (2 ** attempt))
                    await asyncio.sleep(delay)
            except aiohttp.ClientError as exc:
                raise FaceitUnavailableError(str(exc)) from exc
            except (FaceitNotFoundError, FaceitAPIError):
                raise

        if FACEIT_CIRCUIT_THRESHOLD > 0 and isinstance(
            last_exc, (FaceitRateLimitError, FaceitUnavailableError)
        ):
            self._circuit_fail_streak += 1
            if self._circuit_fail_streak >= FACEIT_CIRCUIT_THRESHOLD:
                self._circuit_open_until = time.monotonic() + FACEIT_CIRCUIT_OPEN_SEC
                self._circuit_fail_streak = 0
                logger.warning("FACEIT circuit open for %.0fs", FACEIT_CIRCUIT_OPEN_SEC)

        raise last_exc

    async def _cached_get(self, key: str, ttl: float, path: str, **kwargs: Any) -> Any:
        if self._cache is not None:
            hit = self._cache.get(key, ttl)
            if hit is not None:
                return hit
        result = await self._request_json("GET", path, **kwargs)
        if self._cache is not None:
            self._cache.set(key, result)
        return result

    async def get_player_by_nickname(self, nickname: str) -> dict[str, Any]:
        return await self._cached_get(
            f"nick:{nickname.lower()}", _TTL_NICKNAME,
            "/players", params={"nickname": nickname, "game": GAME_ID},
        )

    async def get_player_by_id(self, player_id: str) -> dict[str, Any]:
        return await self._cached_get(f"player:{player_id}", _TTL_PLAYER, f"/players/{player_id}")

    async def get_player_stats_lifetime(self, player_id: str) -> dict[str, Any]:
        return await self._cached_get(
            f"lifetime:{player_id}", _TTL_LIFETIME, f"/players/{player_id}/stats/{GAME_ID}"
        )

    async def get_player_match_stats(
        self, player_id: str, limit: int = 10, offset: int = 0
    ) -> dict[str, Any]:
        return await self._cached_get(
            f"match_stats:{player_id}:{limit}:{offset}", _TTL_MATCH_STATS,
            f"/players/{player_id}/games/{GAME_ID}/stats",
            params={"limit": limit, "offset": offset},
        )


# ---------------------------------------------------------------------------
# Stat parsers
# ---------------------------------------------------------------------------

def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(val: float | None) -> int | str:
    return int(val) if val is not None else "N/A"


def _fmt(v: float | None, fmt: str, suffix: str = "", fallback: str = "N/A") -> str:
    if v is None:
        return fallback
    try:
        return format(float(v), fmt) + suffix
    except (TypeError, ValueError):
        return fallback


def _first_present(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _infer_win(result: Any) -> bool | None:
    if result is None:
        return None
    if isinstance(result, bool):
        return result
    s = str(result).strip().lower()
    if s in ("1", "win", "won", "true", "w"):
        return True
    if s in ("0", "loss", "lose", "false", "l"):
        return False
    return None


def _pick(lifetime: dict[str, Any], *keys: str) -> Any:
    """Exact key lookup, then case-insensitive fallback."""
    for k in keys:
        if k in lifetime:
            return lifetime[k]
    lower = {str(k).strip().lower(): v for k, v in lifetime.items()}
    for k in keys:
        if (kl := k.strip().lower()) in lower:
            return lower[kl]
    return None


def _pick_substring(lifetime: dict[str, Any], needle: str) -> Any:
    n = needle.lower()
    for k, v in sorted(lifetime.items(), key=lambda kv: str(kv[0])):
        if v is not None and v != "" and n in str(k).lower():
            return v
    return None


def _pick_rounds(lifetime: dict[str, Any]) -> Any:
    scored: list[tuple[int, Any, str]] = []
    for k, v in lifetime.items():
        if v is None or v == "":
            continue
        kl = str(k).lower()
        if "round" not in kl or "win" in kl or ("per" in kl and "round" in kl):
            continue
        score = (2 if "total" in kl else 0) + (2 if "played" in kl or kl.strip() == "rounds" else 0) + (1 if "rounds" in kl else 0)
        scored.append((score, v, kl))
    return scored and sorted(scored, key=lambda t: (-t[0], t[2]))[0][1]


def _segment_sort_key(segment: Any) -> tuple[str, str]:
    if not isinstance(segment, dict):
        return ("", "")
    name = str(segment.get("label") or segment.get("name") or segment.get("mode") or segment.get("type") or "")
    sid = str(segment.get("segment_id") or segment.get("id") or "")
    return (name, sid)


def extract_cs2_game(player: dict[str, Any]) -> dict[str, Any] | None:
    games = player.get("games") or {}
    return games.get(GAME_ID) or games.get("cs2")


def lifetime_map_from_stats_response(st: dict[str, Any] | None) -> dict[str, Any]:
    """Merge lifetime + segment stats into one flat label→value dict."""
    if not isinstance(st, dict):
        return {}

    merged: dict[str, Any] = {}

    def merge_missing(src: dict[str, Any]) -> None:
        for k, v in src.items():
            if v is None or v == "":
                continue
            ks = str(k).strip()
            if merged.get(ks) in (None, ""):
                merged[ks] = v

    life = st.get("lifetime")
    if isinstance(life, dict):
        merge_missing(life)

    for seg in sorted(st.get("segments") or [], key=_segment_sort_key):
        if not isinstance(seg, dict):
            continue
        raw = seg.get("stats")
        if isinstance(raw, dict):
            merge_missing(raw)
        elif isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    continue
                label = row.get("label") or row.get("name") or row.get("key")
                val = row.get("value") if row.get("value") is not None else row.get("count")
                if label is not None and val is not None:
                    merge_missing({str(label): val})

    return merged


def _infer_wl(p: dict[str, Any]) -> None:
    """Fill missing wins/losses from matches + win rate, or from each other."""
    mf = _to_float(p.get("matches"))
    if mf is None or mf <= 0:
        return
    wn, ls = p.get("wins"), p.get("losses")
    wr = p.get("win_rate_pct")

    if wn is None and ls is None and wr is not None:
        try:
            mi = int(round(mf))
            w = max(0, min(int(round(mf * float(wr) / 100.0)), mi))
            p["wins"], p["losses"] = float(w), float(mi - w)
        except (TypeError, ValueError):
            pass
    elif ls is None and wn is not None:
        try:
            p["losses"] = max(0.0, mf - float(wn))
        except (TypeError, ValueError):
            pass
    elif wn is None and ls is not None:
        try:
            p["wins"] = max(0.0, mf - float(ls))
        except (TypeError, ValueError):
            pass


def _enrich_lifetime_stats(p: dict[str, Any]) -> None:
    """Fill derived fields (wins/losses, averages, K/R) where possible."""
    mf = _to_float(p.get("matches"))

    _infer_wl(p)

    if mf and mf > 0:
        if p.get("kills") is None and p.get("avg_kills") is not None:
            p["kills"] = float(p["avg_kills"]) * mf
        if p.get("deaths") is None and p.get("avg_deaths") is not None:
            p["deaths"] = float(p["avg_deaths"]) * mf

    if p.get("kills") is None and p.get("kd") and p.get("deaths"):
        try:
            if (df := float(p["deaths"])) > 0:
                p["kills"] = float(p["kd"]) * df
        except (TypeError, ValueError):
            pass

    if p.get("deaths") is None and p.get("kd") and p.get("kills"):
        try:
            if (kdf := float(p["kd"])) > 0:
                p["deaths"] = float(p["kills"]) / kdf
        except (TypeError, ValueError):
            pass

    if mf and mf > 0:
        if p.get("avg_kills") is None and p.get("kills") is not None:
            p["avg_kills"] = float(p["kills"]) / mf
        if p.get("avg_deaths") is None and p.get("deaths") is not None:
            p["avg_deaths"] = float(p["deaths"]) / mf

    if p.get("kr") is None and p.get("kills") is not None and p.get("rounds"):
        try:
            if (rf := float(p["rounds"])) > 0:
                p["kr"] = float(p["kills"]) / rf
        except (TypeError, ValueError, ZeroDivisionError):
            pass


def parse_lifetime_stats(lifetime: dict[str, Any]) -> dict[str, Any]:
    mvps = _pick(lifetime, "MVPs", "MVP", "Total MVPs", "Total MVP", "MVP Stars", "Most Valuable Player")
    if mvps is None:
        mvps = _pick_substring(lifetime, "mvp")

    kr = _pick(lifetime, "Average K/R Ratio", "K/R Ratio", "Average KR", "Average K/R", "KPR", "K/R", "Average Kills per Round", "Kills per Round", "Kills Per Round")
    if kr is None:
        for needle in ("kills per round", "average k/r", "average kr", "k/r ratio", "kpr"):
            if (kr := _pick_substring(lifetime, needle)) is not None:
                break

    rounds = _pick(lifetime, "Rounds", "Total Rounds", "Rounds Played", "Total Rounds Played", "Rounds played")
    if rounds is None:
        rounds = _pick_rounds(lifetime)

    result = {
        "matches":            _to_float(_pick(lifetime, "Matches", "Total Matches", "Number of Matches", "Games")),
        "win_rate_pct":       _to_float(_pick(lifetime, "Win Rate %", "Win Rate", "Win Rate % ")),
        "kd":                 _to_float(_pick(lifetime, "Average K/D Ratio", "Average K/D", "K/D Ratio", "Average KDR", "KDR")),
        "hs_pct":             _to_float(_pick(lifetime, "Average Headshots %", "Headshots %", "Average Headshots")),
        "longest_win_streak": _to_float(_pick(lifetime, "Longest Win Streak", "Longest Win Streak ", "Best Win Streak")),
        "wins":               _to_float(_pick(lifetime, "Wins", "Total Wins", "Games Won", "Match Wins", "Game Wins", "Games Win")),
        "losses":             _to_float(_pick(lifetime, "Losses", "Total Losses", "Games Lost", "Match Losses", "Game Losses", "Games Loss")),
        "kills":              _to_float(_pick(lifetime, "Kills", "Total Kills", "Total kills", "Kill Count")),
        "deaths":             _to_float(_pick(lifetime, "Deaths", "Total Deaths", "Total deaths")),
        "assists":            _to_float(_pick(lifetime, "Assists", "Total Assists", "Total assists")),
        "rounds":             _to_float(rounds),
        "mvps":               _to_float(mvps),
        "avg_kills":          _to_float(_pick(lifetime, "Average Kills", "Avg Kills", "Kills / Match", "Kills per Match", "Average Kills per Match")),
        "avg_deaths":         _to_float(_pick(lifetime, "Average Deaths", "Avg Deaths", "Deaths / Match", "Deaths per Match", "Average Deaths per Match")),
        "kr":                 _to_float(kr),
    }
    _enrich_lifetime_stats(result)
    return result


def parse_match_stats_row(stats: dict[str, Any]) -> dict[str, Any]:
    kills = _first_present(stats, "Kills", "Total Kills")
    deaths = _first_present(stats, "Deaths", "Total Deaths")
    kd = _first_present(stats, "K/D Ratio", "KDR", "Average K/D Ratio")
    if kd is None and kills is not None and deaths:
        try:
            kd = float(kills) / float(deaths) if float(deaths) else None
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    map_name = _first_present(stats, "Map", "Map Name")
    return {
        "match_id": str(v) if (v := _first_present(stats, "Match Id", "Match ID", "MatchId", "match_id")) else None,
        "won":        _infer_win(_first_present(stats, "Result", "Game Result")),
        "map":        str(map_name) if map_name else "—",
        "kills":      _to_float(kills),
        "deaths":     _to_float(deaths),
        "kd":         _to_float(kd),
        "hs_pct":     _to_float(_first_present(stats, "Average Headshots %", "Headshots %", "Average Headshots")),
        "mvps":       _to_float(_first_present(stats, "MVPs", "MVP", "Total MVPs", "Total MVP", "MVP Stars")),
        "kr":         _to_float(_first_present(stats, "Average K/R Ratio", "K/R Ratio", "Average K/R", "K/R", "Average Kills per Round", "Kills per Round", "Kills Per Round")),
        "rounds":     _to_float(_first_present(stats, "Rounds", "Rounds Played", "Total Rounds", "Total Rounds Played")),
        "finished_at": _first_present(stats, "Match Finished At", "Finished At"),
    }


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id      INTEGER PRIMARY KEY,
    faceit_nickname  TEXT    NOT NULL,
    faceit_player_id TEXT    NOT NULL,
    registered_at    TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS elo_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    elo          INTEGER NOT NULL,
    level        INTEGER NOT NULL,
    recorded_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_elo_snapshots_tid ON elo_snapshots(telegram_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_users_player_id   ON users(faceit_player_id);
"""


async def init_db(db_path: str = DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                await db.execute(stmt)
        await db.commit()


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="faceit-cs2",
    instructions=(
        "Provides live CS2 FACEIT data: player stats, match history, player comparisons, "
        "leaderboard, and ELO trend for bot-registered users. "
        "Always call get_player_stats before answering questions about a specific player."
    ),
)

_faceit: FaceitAPI | None = None


def _api() -> FaceitAPI:
    assert _faceit is not None, "Server not initialised"
    return _faceit


async def _load_player(nickname: str) -> dict[str, Any]:
    pl = await _api().get_player_by_nickname(nickname.strip())
    pid = pl.get("player_id")
    if not pid:
        raise FaceitAPIError("No player_id in response")
    p, st = await asyncio.gather(
        _api().get_player_by_id(pid),
        _api().get_player_stats_lifetime(pid),
    )
    g = extract_cs2_game(p) or {}
    parsed = parse_lifetime_stats(lifetime_map_from_stats_response(st if isinstance(st, dict) else None))
    return {
        "player_id": pid,
        "nickname":  p.get("nickname") or nickname,
        "elo":       int(g.get("faceit_elo") or 0),
        "level":     int(g.get("skill_level") or 0),
        "region":    str(g.get("region") or "—"),
        "country":   (p.get("country") or "").upper() or "—",
        "faceit_url": str(p.get("faceit_url") or ""),
        **{k: parsed[k] for k in ("kd", "hs_pct", "win_rate_pct", "matches", "wins", "losses", "longest_win_streak", "avg_kills", "kr", "mvps")},
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_player_stats(nickname: str) -> str:
    """Return ELO, skill level, region, and lifetime CS2 stats for a FACEIT player.

    Args:
        nickname: FACEIT nickname (case-insensitive).
    """
    try:
        b = await _load_player(nickname)
    except FaceitNotFoundError:
        return json.dumps({"error": f"Player '{nickname}' not found on FACEIT."})
    except FaceitAPIError as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps({
        "nickname":   b["nickname"],
        "elo":        b["elo"],
        "level":      b["level"],
        "region":     b["region"],
        "country":    b["country"],
        "faceit_url": b["faceit_url"],
        "stats": {
            "kd":               _fmt(b["kd"], ".2f"),
            "hs_pct":           _fmt(b["hs_pct"], ".1f", "%"),
            "win_rate_pct":     _fmt(b["win_rate_pct"], ".1f", "%"),
            "matches":          _to_int(b["matches"]),
            "wins":             _to_int(b["wins"]),
            "losses":           _to_int(b["losses"]),
            "longest_win_streak": _to_int(b["longest_win_streak"]),
            "avg_kills_per_match": _fmt(b["avg_kills"], ".2f"),
            "kr":               _fmt(b["kr"], ".2f"),
            "mvps":             _to_int(b["mvps"]),
        },
    }, indent=2)


@mcp.tool()
async def get_match_history(nickname: str, limit: int = 10) -> str:
    """Return the most recent CS2 matches for a FACEIT player.

    Args:
        nickname: FACEIT nickname.
        limit: Number of matches to return (1–20, default 10).
    """
    limit = max(1, min(20, limit))
    try:
        pl = await _api().get_player_by_nickname(nickname.strip())
        pid = pl.get("player_id")
        if not pid:
            raise FaceitAPIError("No player_id in response")
        raw = await _api().get_player_match_stats(pid, limit=limit)
    except FaceitNotFoundError:
        return json.dumps({"error": f"Player '{nickname}' not found on FACEIT."})
    except FaceitAPIError as exc:
        return json.dumps({"error": str(exc)})

    matches = []
    for it in (raw or {}).get("items") or []:
        if not isinstance(it, dict) or not isinstance(it.get("stats"), dict):
            continue
        row = parse_match_stats_row(it["stats"])
        matches.append({
            "match_id":   row["match_id"],
            "map":        row["map"],
            "result":     "Win" if row["won"] is True else ("Loss" if row["won"] is False else "Unknown"),
            "kd":         _fmt(row["kd"], ".2f"),
            "kills":      _to_int(row["kills"]),
            "deaths":     _to_int(row["deaths"]),
            "hs_pct":     _fmt(row["hs_pct"], ".0f", "%"),
            "mvps":       _to_int(row["mvps"]),
            "kr":         _fmt(row["kr"], ".2f"),
            "finished_at": row["finished_at"],
        })

    return json.dumps({"nickname": nickname, "matches_returned": len(matches), "matches": matches}, indent=2)


@mcp.tool()
async def compare_players(nicknames: list[str]) -> str:
    """Compare CS2 FACEIT stats for 2–6 players side by side.

    Args:
        nicknames: List of 2–6 FACEIT nicknames.
    """
    if not 2 <= len(nicknames) <= 6:
        return json.dumps({"error": "Provide 2–6 nicknames."})

    results = await asyncio.gather(*[_load_player(n) for n in nicknames], return_exceptions=True)

    players, errors = [], []
    for nick, res in zip(nicknames, results):
        if isinstance(res, FaceitNotFoundError):
            errors.append(f"{nick}: not found")
        elif isinstance(res, Exception):
            errors.append(f"{nick}: {res}")
        else:
            players.append(res)

    if len(players) < 2:
        return json.dumps({"error": "Could not load enough players.", "details": errors})

    return json.dumps({
        "players": [
            {
                "nickname":     b["nickname"],
                "elo":          b["elo"],
                "level":        b["level"],
                "kd":           _fmt(b["kd"], ".2f"),
                "hs_pct":       _fmt(b["hs_pct"], ".1f", "%"),
                "win_rate_pct": _fmt(b["win_rate_pct"], ".1f", "%"),
                "matches":      _to_int(b["matches"]),
                "avg_kills":    _fmt(b["avg_kills"], ".2f"),
                "kr":           _fmt(b["kr"], ".2f"),
            }
            for b in players
        ],
        "skipped": errors or None,
    }, indent=2)


@mcp.tool()
async def get_leaderboard() -> str:
    """Return all registered users ranked by their current live FACEIT CS2 ELO.

    Note: requires users registered in the DB (via DB_PATH).
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT telegram_id, faceit_nickname, faceit_player_id FROM users ORDER BY faceit_nickname COLLATE NOCASE"
            ) as cur:
                users = [dict(r) for r in await cur.fetchall()]
    except Exception as exc:
        return json.dumps({"error": f"DB error: {exc}"})

    if not users:
        return json.dumps({"leaderboard": [], "note": "No registered users."})

    async def _fetch(u: dict) -> dict[str, Any]:
        async with _LEADERBOARD_SEM:
            try:
                p = await _api().get_player_by_id(u["faceit_player_id"])
            except FaceitAPIError:
                return {"nickname": u["faceit_nickname"], "elo": 0, "level": 0}
        g = extract_cs2_game(p) or {}
        return {
            "nickname": str(p.get("nickname") or u["faceit_nickname"]),
            "elo":      int(g.get("faceit_elo") or 0),
            "level":    int(g.get("skill_level") or 0),
        }

    rows = await asyncio.gather(*[_fetch(u) for u in users], return_exceptions=True)
    valid = sorted([r for r in rows if isinstance(r, dict)], key=lambda r: -r["elo"])

    return json.dumps({"registered_users": len(users), "leaderboard": valid}, indent=2)


@mcp.tool()
async def get_elo_trend(nickname: str) -> str:
    """Return stored ELO snapshots for a registered FACEIT player.

    Snapshots are recorded by a companion bot whenever the player checks their stats.

    Args:
        nickname: FACEIT nickname (must be registered in the DB).
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT telegram_id FROM users WHERE faceit_nickname = ? COLLATE NOCASE LIMIT 1",
                (nickname.strip(),),
            ) as cur:
                row = await cur.fetchone()

            if not row:
                return json.dumps({
                    "error": f"'{nickname}' is not registered. "
                             "Only users who linked their FACEIT account have stored ELO history."
                })

            async with db.execute(
                "SELECT elo, level, recorded_at FROM elo_snapshots "
                "WHERE telegram_id = ? ORDER BY recorded_at ASC",
                (row["telegram_id"],),
            ) as cur:
                snaps = [dict(r) for r in await cur.fetchall()]
    except Exception as exc:
        return json.dumps({"error": f"DB error: {exc}"})

    if not snaps:
        return json.dumps({"nickname": nickname, "snapshots": [], "note": "No ELO history yet."})

    elos = [s["elo"] for s in snaps]
    return json.dumps({
        "nickname":         nickname,
        "snapshots_count":  len(snaps),
        "elo_min":          min(elos),
        "elo_max":          max(elos),
        "elo_latest":       elos[-1],
        "elo_change_total": elos[-1] - elos[0],
        "snapshots":        snaps,
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _amain() -> None:
    global _faceit
    if not FACEIT_API_KEY:
        raise SystemExit(
            "FACEIT_API_KEY is not set. "
            "Add it to a .env file next to this script or pass it as an environment variable."
        )
    await init_db()
    async with aiohttp.ClientSession() as session:
        _faceit = FaceitAPI(session, FACEIT_API_KEY, cache=TTLCache(maxsize=500))
        await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_amain())
