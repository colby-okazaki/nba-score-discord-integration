# nba_score_bot.py
import argparse
import asyncio
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import requests
import discord
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

if not TOKEN:
    raise ValueError("Missing TOKEN in .env")
if not CHANNEL_ID:
    raise ValueError("Missing CHANNEL_ID in .env")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event="
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
# The day's schedule is posted at this hour, Pacific time.
SCHEDULE_HOUR_PT = 9
EXPIRED_CLOCKS = {"0.0", "0:00", "00:00", "0.00"}
STATE_PATH = Path(__file__).resolve().parent / "bot_state.json"
LOCK_KEYS = ("1", "2", "3", "4", "final")
# Only final scores are posted. Each game is first checked about one average
# NBA game length after tip-off, then every few minutes until it goes final.
GAME_LENGTH = timedelta(hours=2, minutes=15)
RECHECK_EVERY = timedelta(minutes=5)
# Never post a final for a game that tipped off longer ago than this.
FINAL_WINDOW = timedelta(hours=12)
# Each GitHub run: how long it stays alive, how far ahead it will wait for
# the next check, and how often it refreshes ESPN while waiting.
WATCH_MAX = timedelta(hours=5, minutes=30)
HANDOFF_WINDOW = timedelta(hours=6)
WATCH_REFRESH = timedelta(minutes=30)
REDISPATCH_FLAG = Path(__file__).resolve().parent / ".redispatch"
CLOSED_STATUS_NAMES = {
    "STATUS_FINAL",
    "STATUS_POSTPONED",
    "STATUS_CANCELED",
    "STATUS_CANCELLED",
    "STATUS_FORFEIT",
}
AWARDS = (
    ("pts", "🔥 Most Points"),
    ("reb", "🧹 Most Rebounds"),
    ("ast", "🎯 Most Assists"),
    ("blk", "🚫 Most Blocks"),
    ("stl", "🥷 Most Steals"),
)

intents = discord.Intents.default()
bot = discord.Client(intents=intents)


@dataclass
class GameSnapshot:
    period: int
    state: str
    is_final: bool
    is_break: bool
    is_halftime: bool
    status_name: str = ""


@dataclass
class GameRecord:
    event_id: str
    slate_date: str
    posted: Set[str] = field(default_factory=set)
    snapshot: Optional[GameSnapshot] = None


@dataclass
class DateFlags:
    schedule_posted: bool = False
    summary_posted: bool = False
    backfilled: bool = False


games: Dict[str, GameRecord] = {}
dates: Dict[str, DateFlags] = {}


def date_flags(yyyymmdd: str) -> DateFlags:
    flags = dates.get(yyyymmdd)
    if flags is None:
        flags = DateFlags()
        dates[yyyymmdd] = flags
    return flags


def game_record(event_id: str, slate_date: str) -> GameRecord:
    rec = games.get(event_id)
    if rec is None:
        rec = GameRecord(event_id=event_id, slate_date=slate_date)
        games[event_id] = rec
    elif slate_date and rec.slate_date != slate_date:
        rec.slate_date = slate_date
    return rec


def slate_label(yyyymmdd: str) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d")
    return d.strftime("%A, %B ") + str(d.day)


def watch_dates() -> List[str]:
    now = datetime.now(ET)
    return [
        (now - timedelta(days=1)).strftime("%Y%m%d"),
        now.strftime("%Y%m%d"),
        (now + timedelta(days=1)).strftime("%Y%m%d"),
    ]


def load_state() -> None:
    games.clear()
    dates.clear()
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception as e:
        print("State load error:", e, flush=True)
        return

    if "games" in data:
        _load_v2(data)
    else:
        _load_v1(data)


def _snapshot_from_dict(raw: Optional[dict]) -> Optional[GameSnapshot]:
    if not raw:
        return None
    return GameSnapshot(
        period=int(raw.get("period") or 0),
        state=str(raw.get("state") or ""),
        is_final=bool(raw.get("is_final")),
        is_break=bool(raw.get("is_break")),
        is_halftime=bool(raw.get("is_halftime")),
        status_name=str(raw.get("status_name") or ""),
    )


def _load_v2(data: dict) -> None:
    for event_id, raw in (data.get("games") or {}).items():
        rec = GameRecord(
            event_id=str(event_id),
            slate_date=str(raw.get("slate_date") or ""),
            posted=set(str(k) for k in (raw.get("posted") or [])),
            snapshot=_snapshot_from_dict(raw.get("snapshot")),
        )
        games[rec.event_id] = rec
    for yyyymmdd, raw in (data.get("dates") or {}).items():
        dates[str(yyyymmdd)] = DateFlags(
            schedule_posted=bool(raw.get("schedule_posted")),
            summary_posted=bool(raw.get("summary_posted")),
            backfilled=bool(raw.get("backfilled")),
        )


def _load_v1(data: dict) -> None:
    posted_by_game: Dict[str, Set[str]] = {}
    for event_id, alert_key in data.get("posted") or []:
        posted_by_game.setdefault(str(event_id), set()).add(str(alert_key))

    last_seen = data.get("last_seen") or {}
    for event_id, snap in last_seen.items():
        eid = str(event_id)
        rec = GameRecord(
            event_id=eid,
            slate_date="",
            posted=posted_by_game.get(eid, set()),
            snapshot=_snapshot_from_dict(snap),
        )
        games[eid] = rec

    for event_id, keys in posted_by_game.items():
        rec = game_record(event_id, "")
        rec.posted.update(keys)

    for yyyymmdd in data.get("backfilled_dates") or []:
        date_flags(str(yyyymmdd)).backfilled = True
    for yyyymmdd in data.get("schedule_posted") or []:
        date_flags(str(yyyymmdd)).schedule_posted = True
    for yyyymmdd in data.get("summary_posted") or []:
        date_flags(str(yyyymmdd)).summary_posted = True


def save_state() -> None:
    payload = {
        "games": {
            eid: {
                "slate_date": rec.slate_date,
                "posted": sorted(rec.posted),
                "snapshot": asdict(rec.snapshot) if rec.snapshot else None,
            }
            for eid, rec in games.items()
        },
        "dates": {
            day: {
                "schedule_posted": flags.schedule_posted,
                "summary_posted": flags.summary_posted,
                "backfilled": flags.backfilled,
            }
            for day, flags in dates.items()
        },
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2))


def lock_game(event_id: str) -> None:
    rec = games.get(event_id)
    if rec is None:
        rec = game_record(event_id, "")
    rec.posted.update(LOCK_KEYS)


def fetch_scoreboard_for_date(yyyymmdd: str) -> dict:
    r = requests.get(f"{ESPN_SCOREBOARD}?dates={yyyymmdd}", timeout=10)
    r.raise_for_status()
    return r.json()


def events_for_date(yyyymmdd: str) -> List[dict]:
    try:
        data = fetch_scoreboard_for_date(yyyymmdd)
    except Exception as e:
        print(f"Scoreboard fetch error ({yyyymmdd}):", e, flush=True)
        return []
    events = []
    for event in data.get("events", []):
        if event.get("id") is not None:
            events.append(event)
    return events


def fetch_summary(event_id: str) -> dict:
    r = requests.get(f"{ESPN_SUMMARY}{event_id}", timeout=10)
    r.raise_for_status()
    return r.json()


def snapshot_event(event: dict) -> GameSnapshot:
    status = event.get("status") or {}
    st = status.get("type") or {}
    name = (st.get("name") or "").upper()
    detail = (st.get("detail") or "").lower()
    description = (st.get("description") or "").lower()
    state = (st.get("state") or "").lower()
    period = int(status.get("period") or 0)
    clock = str(status.get("displayClock") or "").strip()

    is_final = (
        name == "STATUS_FINAL"
        or state == "post"
        or bool(st.get("completed"))
        or "final" in detail
    )
    is_halftime = (
        name == "STATUS_HALFTIME"
        or "halftime" in detail
        or "halftime" in description
    )
    is_end_period = name == "STATUS_END_PERIOD" or "end of" in detail or "end of" in description
    starting_period = "start of" in detail or "start of" in description
    clock_expired = (
        clock in EXPIRED_CLOCKS
        and state == "in"
        and period >= 1
        and not is_final
        and not starting_period
    )
    is_break = (not is_final) and (is_halftime or is_end_period or clock_expired)
    return GameSnapshot(
        period=period,
        state=state,
        is_final=is_final,
        is_break=is_break,
        is_halftime=is_halftime,
        status_name=name,
    )


def event_is_closed(event: dict) -> bool:
    snap = snapshot_event(event)
    return snap.is_final or snap.status_name in CLOSED_STATUS_NAMES


def tip_time(event: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(event.get("date")).replace("Z", "+00:00"))
    except Exception:
        return None


def schedule_post_time(yyyymmdd: str) -> datetime:
    day = datetime.strptime(yyyymmdd, "%Y%m%d")
    return day.replace(hour=SCHEDULE_HOUR_PT, tzinfo=PT)


def next_check_time(slates: Dict[str, List[dict]]) -> Optional[datetime]:
    """When the next schedule post or final-score check is due."""
    now = datetime.now(ET)
    best: Optional[datetime] = None
    today_pt = now.astimezone(PT).strftime("%Y%m%d")
    for yyyymmdd, events in slates.items():
        if events and yyyymmdd >= today_pt and not date_flags(yyyymmdd).schedule_posted:
            due = max(schedule_post_time(yyyymmdd), now)
            if best is None or due < best:
                best = due
    for events in slates.values():
        for event in events:
            if event_is_closed(event):
                continue
            tip = tip_time(event)
            if tip is None:
                continue
            due = tip + GAME_LENGTH
            if due <= now:
                due = now + RECHECK_EVERY
            if best is None or due < best:
                best = due
    return best


def get_home_away_from_event(event: dict):
    competitors = event["competitions"][0]["competitors"]
    home = next(t for t in competitors if t["homeAway"] == "home")
    away = next(t for t in competitors if t["homeAway"] == "away")
    return away, home


def format_score_line_from_event(event: dict) -> str:
    away, home = get_home_away_from_event(event)
    away_team = away["team"]["displayName"]
    home_team = home["team"]["displayName"]
    away_score = away.get("score", "0")
    home_score = home.get("score", "0")
    return f"{away_team} {away_score} – {home_team} {home_score}"


def get_score_line_from_summary(summary: dict) -> str:
    header = summary.get("header", {})
    comps = header.get("competitions", [])
    if not comps:
        return "Final score unavailable"
    comp = comps[0]
    competitors = comp.get("competitors", [])

    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")

    home_team = home["team"]["displayName"]
    away_team = away["team"]["displayName"]
    home_score = home.get("score", "0")
    away_score = away.get("score", "0")

    return f"{away_team} {away_score} – {home_team} {home_score}"


def get_team_info_from_summary(summary: dict):
    header = summary.get("header", {})
    comps = header.get("competitions", [])
    if not comps:
        return None

    comp = comps[0]
    competitors = comp.get("competitors", [])

    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None

    def logo(team_obj: dict) -> Optional[str]:
        logos = team_obj.get("team", {}).get("logos", [])
        if logos and isinstance(logos, list):
            return logos[0].get("href")
        return None

    away_name = away["team"]["displayName"]
    home_name = home["team"]["displayName"]
    away_score = int(away.get("score", 0))
    home_score = int(home.get("score", 0))
    away_logo = logo(away)
    home_logo = logo(home)

    return away_name, away_score, away_logo, home_name, home_score, home_logo


def winner_logo_from_summary(summary: dict) -> Optional[str]:
    info = get_team_info_from_summary(summary)
    if not info:
        return None

    away_name, away_score, away_logo, home_name, home_score, home_logo = info

    if away_score > home_score:
        return away_logo or home_logo
    if home_score > away_score:
        return home_logo or away_logo
    return home_logo or away_logo


def _stat_map(stats: List[dict]) -> Dict[str, str]:
    mapped = {}
    for item in stats:
        value = item.get("displayValue")
        if value is None:
            continue
        for key in (item.get("abbreviation"), item.get("label"), item.get("name")):
            if key:
                mapped[str(key)] = str(value)
    return mapped


def format_team_box(summary: dict) -> str:
    info = get_team_info_from_summary(summary)
    teams = (summary.get("boxscore") or {}).get("teams") or []
    if not info or len(teams) < 2:
        return "Team stats unavailable"

    away_name, away_score, _, home_name, home_score, _ = info
    by_name = {t.get("team", {}).get("displayName"): t for t in teams}

    def line(team_name: str, score: int) -> str:
        block = by_name.get(team_name) or {}
        stats = _stat_map(block.get("statistics") or [])
        fg = stats.get("fieldGoalsMade-fieldGoalsAttempted") or stats.get("FG") or "—"
        fg_pct = stats.get("FG%") or stats.get("fieldGoalPct") or "—"
        three = stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted") or stats.get("3PT") or "—"
        three_pct = stats.get("3P%") or stats.get("threePointFieldGoalPct") or "—"
        ft = stats.get("freeThrowsMade-freeThrowsAttempted") or stats.get("FT") or "—"
        ft_pct = stats.get("FT%") or stats.get("freeThrowPct") or "—"
        reb = stats.get("REB") or stats.get("totalRebounds") or "—"
        ast = stats.get("AST") or stats.get("assists") or "—"
        to = stats.get("TO") or stats.get("turnovers") or "—"
        stl = stats.get("STL") or stats.get("steals") or "—"
        blk = stats.get("BLK") or stats.get("blocks") or "—"
        return (
            f"**{team_name} {score}**\n"
            f"FG {fg} ({fg_pct}%) · 3PT {three} ({three_pct}%) · FT {ft} ({ft_pct}%)\n"
            f"REB {reb} · AST {ast} · TO {to} · STL {stl} · BLK {blk}"
        )

    return f"{line(away_name, away_score)}\n\n{line(home_name, home_score)}"


def _stat_int(stats: List[str], idx: Optional[int]) -> int:
    if idx is None or idx >= len(stats):
        return 0
    raw = str(stats[idx]).split("-")[0]
    try:
        return int(raw)
    except Exception:
        return 0


def player_rows_from_summary(summary: dict) -> List[dict]:
    rows = []
    for team_block in (summary.get("boxscore") or {}).get("players") or []:
        team = team_block.get("team") or {}
        team_abbr = team.get("abbreviation") or team.get("displayName") or "?"
        for group in team_block.get("statistics") or []:
            labels = [str(lab).upper() for lab in (group.get("labels") or [])]
            idx = {lab: i for i, lab in enumerate(labels)}
            for athlete_row in group.get("athletes") or []:
                ath = athlete_row.get("athlete") or {}
                stats = athlete_row.get("stats") or []
                name = ath.get("displayName") or "Unknown"
                rows.append(
                    {
                        "id": str(ath.get("id") or name),
                        "name": name,
                        "team": team_abbr,
                        "pts": _stat_int(stats, idx.get("PTS")),
                        "reb": _stat_int(stats, idx.get("REB")),
                        "ast": _stat_int(stats, idx.get("AST")),
                        "stl": _stat_int(stats, idx.get("STL")),
                        "blk": _stat_int(stats, idx.get("BLK")),
                    }
                )
    return rows


def top_stat_for_team(team_block: dict, stat: str) -> str:
    team_name = team_block["team"]["displayName"]
    best_name = "Unavailable"
    best_val = -1

    for group in team_block.get("statistics", []):
        labels = group.get("labels") or []
        athletes = group.get("athletes") or []
        try:
            idx = [str(lab).upper() for lab in labels].index(stat.upper())
        except ValueError:
            continue

        for athlete in athletes:
            stats = athlete.get("stats") or []
            val = _stat_int(stats, idx)
            if val > best_val:
                best_val = val
                best_name = athlete.get("athlete", {}).get("displayName", "Unknown")

    if best_val < 0:
        return f"{team_name}: (unavailable)"
    return f"{team_name}: {best_name} ({best_val} {stat})"


def get_boxscore_leaders(summary: dict) -> Dict[str, Tuple[str, str]]:
    info = get_team_info_from_summary(summary)
    empty = ("Away: (unavailable)", "Home: (unavailable)")
    if not info:
        return {"PTS": empty, "REB": empty, "AST": empty}

    away_name, _, _, home_name, _, _ = info
    players_blocks = (summary.get("boxscore") or {}).get("players") or []
    result = {}
    for stat in ("PTS", "REB", "AST"):
        by_team = {}
        for team_block in players_blocks:
            by_team[team_block["team"]["displayName"]] = top_stat_for_team(team_block, stat)
        result[stat] = (
            by_team.get(away_name, f"{away_name}: (unavailable)"),
            by_team.get(home_name, f"{home_name}: (unavailable)"),
        )
    return result


def get_leading_scorers(event_id: str, summary: Optional[dict] = None) -> Tuple[str, str]:
    try:
        if summary is None:
            summary = fetch_summary(event_id)
        return get_boxscore_leaders(summary)["PTS"]
    except Exception as e:
        print("Leading scorers error:", e, flush=True)
        return "Away: (unavailable)", "Home: (unavailable)"


def award_line(rows: List[dict], key: str, title: str) -> str:
    if not rows:
        return f"{title} — n/a"
    best = max(int(row[key]) for row in rows)
    if best <= 0:
        return f"{title} — n/a"
    winners = [row for row in rows if int(row[key]) == best]
    names = ", ".join(f"{w['name']} ({w['team']})" for w in winners)
    return f"{title} — {names} ({best})"


def embed_nightly_summary(yyyymmdd: str, events: List[dict], rows: List[dict]) -> discord.Embed:
    score_lines = []
    for event in events:
        snap = snapshot_event(event)
        if snap.status_name in CLOSED_STATUS_NAMES and not snap.is_final:
            score_lines.append(f"{event.get('shortName') or event.get('name')} — postponed")
            continue
        score_lines.append(format_score_line_from_event(event))

    awards = "\n".join(award_line(rows, key, title) for key, title in AWARDS)
    e = discord.Embed(
        title=f"🌙 NBA Nightly Recap — {slate_label(yyyymmdd)}",
        description="\n".join(score_lines)[:4096] or "No final scores.",
        color=0xF4C430,
    )
    e.add_field(name="🏆 Nightly awards", value=awards[:1024], inline=False)
    return e


QUARTER_TITLES = {
    "1": "🏀 End of 1st Quarter",
    "2": "🏀 HALFTIME",
    "3": "🏀 End of 3rd Quarter",
    "4": "🏀 End of 4th Quarter",
    "final": "🏁 FINAL",
}


def title_for_alert(alert_key: str) -> str:
    if alert_key in QUARTER_TITLES:
        return QUARTER_TITLES[alert_key]
    if alert_key.startswith("ot"):
        return "🏀 End of OT"
    return f"End of {alert_key}"


def embed_quarter_end(
    alert_key: str,
    score_line: str,
    away_leader: str,
    home_leader: str,
    thumbnail_url: Optional[str] = None,
    summary: Optional[dict] = None,
) -> discord.Embed:
    e = discord.Embed(title=title_for_alert(alert_key), description=score_line)
    if thumbnail_url:
        e.set_thumbnail(url=thumbnail_url)
    if alert_key == "final" and summary:
        e.add_field(name="📊 Team stats", value=format_team_box(summary)[:1024], inline=False)
        leaders = get_boxscore_leaders(summary)
        e.add_field(
            name="🔥 Points",
            value=f"{leaders['PTS'][0]}\n{leaders['PTS'][1]}",
            inline=False,
        )
        e.add_field(
            name="🧹 Rebounds",
            value=f"{leaders['REB'][0]}\n{leaders['REB'][1]}",
            inline=True,
        )
        e.add_field(
            name="🎯 Assists",
            value=f"{leaders['AST'][0]}\n{leaders['AST'][1]}",
            inline=True,
        )
    else:
        e.add_field(name="🔥 Leading scorers", value=f"{away_leader}\n{home_leader}", inline=False)
    return e


def thumbnail_for_alert(summary: Optional[dict], alert_key: str) -> Optional[str]:
    if alert_key == "final" and summary:
        return winner_logo_from_summary(summary)
    return None


def schedule_time(event: dict) -> str:
    st = ((event.get("status") or {}).get("type") or {})
    return st.get("shortDetail") or st.get("detail") or "TBD"


def embed_schedule(date_label: str, events: List[dict]) -> discord.Embed:
    lines = []
    for event in events:
        try:
            away, home = get_home_away_from_event(event)
            matchup = f"{away['team']['displayName']} @ {home['team']['displayName']}"
        except Exception:
            matchup = event.get("name") or event.get("shortName") or "NBA game"
        lines.append(f"• {matchup} — {schedule_time(event)}")
    description = "\n".join(lines) if lines else "No games listed."
    return discord.Embed(
        title=f"📅 NBA schedule — {date_label}",
        description=description[:4096],
    )


FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # GitHub Actions (Ubuntu)
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
    "C:/Windows/Fonts/arialbd.ttf",
)
_logo_cache: Dict[str, Optional[Image.Image]] = {}


def _font(size: int) -> ImageFont.ImageFont:
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int) -> ImageFont.ImageFont:
    font = _font(size)
    while size > 20 and draw.textlength(text, font=font) > max_width:
        size -= 2
        font = _font(size)
    return font


def _team_logo(url: Optional[str], size: int) -> Optional[Image.Image]:
    if not url:
        return None
    if url not in _logo_cache:
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            _logo_cache[url] = Image.open(io.BytesIO(r.content)).convert("RGBA")
        except Exception as e:
            print("Logo fetch error:", e, flush=True)
            _logo_cache[url] = None
    logo = _logo_cache[url]
    if logo is None:
        return None
    logo = logo.copy()
    logo.thumbnail((size, size))
    return logo


def _tip_label(event: dict) -> str:
    tip = tip_time(event)
    if tip is None:
        return "TBD"
    return tip.astimezone(ET).strftime("%I:%M %p").lstrip("0")


def _broadcast_label(event: dict) -> str:
    names: List[str] = []
    for b in (event.get("competitions") or [{}])[0].get("broadcasts") or []:
        for name in b.get("names") or []:
            if name not in names:
                names.append(name)
    return " · ".join(names[:2])


def render_schedule_image(events: List[dict]) -> io.BytesIO:
    """A schedule card with team logos and large text, sized for Discord."""
    width, margin, row_h, gap, top = 1200, 32, 150, 16, 90
    height = top + len(events) * (row_h + gap) + margin - gap
    img = Image.new("RGB", (width, height), (17, 24, 39))
    draw = ImageDraw.Draw(img)

    count = f"{len(events)} GAME{'S' if len(events) != 1 else ''}  ·  ALL TIMES ET"
    draw.text((margin + 8, top // 2), count, font=_font(34), fill=(209, 213, 219), anchor="lm")

    logo_size = 96
    for i, event in enumerate(sorted(events, key=lambda e: str(e.get("date")))):
        y = top + i * (row_h + gap)
        mid = y + row_h // 2
        draw.rounded_rectangle((margin, y, width - margin, y + row_h), radius=24, fill=(255, 255, 255))
        away, home = get_home_away_from_event(event)

        for team, x in ((away, margin + 28), (home, 560)):
            info = team.get("team") or {}
            logo = _team_logo(info.get("logo"), logo_size)
            if logo:
                img.paste(logo, (x + (logo_size - logo.width) // 2, mid - logo.height // 2), logo)
            name = info.get("shortDisplayName") or info.get("displayName") or "?"
            draw.text((x + logo_size + 18, mid - 8), name, font=_fit_font(draw, name, 46, 225),
                      fill=(17, 24, 39), anchor="ls")
            record = ((team.get("records") or [{}])[0]).get("summary") or ""
            if record:
                draw.text((x + logo_size + 18, mid + 12), record, font=_font(28),
                          fill=(107, 114, 128), anchor="lt")

        neutral = bool((event.get("competitions") or [{}])[0].get("neutralSite"))
        draw.text((520, mid), "vs" if neutral else "@", font=_font(40), fill=(156, 163, 175), anchor="mm")

        right = width - margin - 28
        draw.text((right, mid - 6), _tip_label(event), font=_font(46), fill=(17, 24, 39), anchor="rs")
        tv = _broadcast_label(event)
        if tv:
            draw.text((right, mid + 14), tv, font=_fit_font(draw, tv, 26, 260),
                      fill=(107, 114, 128), anchor="rt")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def send_alert(channel, event: dict, event_id: str, alert_key: str, summary: Optional[dict] = None) -> None:
    if summary is None:
        summary = fetch_summary(event_id)
    away_leader, home_leader = get_leading_scorers(event_id, summary)
    score_line = (
        get_score_line_from_summary(summary)
        if alert_key == "final"
        else format_score_line_from_event(event)
    )
    thumb = thumbnail_for_alert(summary, alert_key)
    await channel.send(
        embed=embed_quarter_end(
            alert_key, score_line, away_leader, home_leader, thumb, summary=summary
        )
    )


async def maybe_post_schedule(channel, slates: Dict[str, List[dict]]) -> None:
    """Post today's schedule once it is SCHEDULE_HOUR_PT in Pacific time."""
    now_pt = datetime.now(PT)
    yyyymmdd = now_pt.strftime("%Y%m%d")
    events = slates.get(yyyymmdd) or []
    flags = date_flags(yyyymmdd)
    if not events or flags.schedule_posted or now_pt < schedule_post_time(yyyymmdd):
        return
    heading = f"# 📅 NBA Schedule — {slate_label(yyyymmdd)}"
    try:
        image = render_schedule_image(events)
        await channel.send(heading, file=discord.File(image, filename="nba-schedule.png"))
    except Exception as e:
        print("Schedule image error, sending text instead:", e, flush=True)
        await channel.send(embed=embed_schedule(slate_label(yyyymmdd), events))
    flags.schedule_posted = True
    print(f"Posted schedule for {yyyymmdd} ({len(events)} games)", flush=True)


async def maybe_send_nightly_summary(channel, yyyymmdd: str, events: List[dict]) -> None:
    flags = date_flags(yyyymmdd)
    if flags.summary_posted or not events:
        return
    if not all(event_is_closed(event) for event in events):
        return

    finals = [event for event in events if snapshot_event(event).is_final]
    if not finals:
        return

    rows: List[dict] = []
    for event in finals:
        try:
            summary = fetch_summary(str(event["id"]))
        except Exception as e:
            print(f"Summary fetch error ({event.get('id')}):", e, flush=True)
            return
        rows.extend(player_rows_from_summary(summary))

    try:
        await channel.send(embed=embed_nightly_summary(yyyymmdd, events, rows))
    except Exception as e:
        print(f"Nightly summary send error ({yyyymmdd}):", e, flush=True)
        return
    flags.summary_posted = True
    print(f"Posted nightly recap for {yyyymmdd}", flush=True)


async def process_slate(channel, yyyymmdd: str, events: List[dict], post_finals: bool) -> None:
    now = datetime.now(ET)
    for event in events:
        event_id = str(event["id"])
        rec = game_record(event_id, yyyymmdd)
        curr = snapshot_event(event)
        rec.snapshot = curr

        if not post_finals or not curr.is_final or "final" in rec.posted:
            continue
        tip = tip_time(event)
        if tip is None or now - tip > FINAL_WINDOW:
            # Too old to be news; mark it so it is never posted late.
            lock_game(event_id)
            continue
        try:
            await send_alert(channel, event, event_id, "final")
        except Exception as e:
            print(f"Final send error ({event_id}):", e, flush=True)
            continue
        lock_game(event_id)
        print(f"Posted final: {event.get('shortName')}", flush=True)


async def run_poll(channel) -> Dict[str, List[dict]]:
    today = datetime.now(ET).strftime("%Y%m%d")
    slates = {day: events_for_date(day) for day in watch_dates()}
    for yyyymmdd, events in slates.items():
        post_finals = yyyymmdd <= today
        await process_slate(channel, yyyymmdd, events, post_finals=post_finals)
        if post_finals:
            await maybe_send_nightly_summary(channel, yyyymmdd, events)
    await maybe_post_schedule(channel, slates)
    save_state()
    return slates


async def run_watch() -> None:
    """
    Stay alive and check each game about GAME_LENGTH after tip-off, until
    WATCH_MAX runs out. If a check is still coming up, leave REDISPATCH_FLAG
    so the workflow can start a fresh run to take over.
    """
    load_state()
    REDISPATCH_FLAG.unlink(missing_ok=True)
    deadline = datetime.now(ET) + WATCH_MAX
    await bot.login(TOKEN)
    try:
        channel = await bot.fetch_channel(CHANNEL_ID)
        print(f"Watching as {bot.user} until {deadline:%H:%M} ET", flush=True)
        while True:
            slates = await run_poll(channel)
            due = next_check_time(slates)
            now = datetime.now(ET)
            if due is None or due > deadline + HANDOFF_WINDOW:
                print("No games to check soon; exiting", flush=True)
                return
            wake = min(due, now + WATCH_REFRESH)
            if wake >= deadline:
                REDISPATCH_FLAG.touch()
                print(f"Next check at {due:%H:%M} ET; handing off to a new run", flush=True)
                return
            print(f"Next check at {due:%H:%M} ET", flush=True)
            await asyncio.sleep(max(0.0, (wake - now).total_seconds()))
    finally:
        await bot.close()


def main() -> None:
    # Runs on GitHub Actions (.github/workflows/nba-poll.yml).
    argparse.ArgumentParser(description="Post NBA final scores and schedules to Discord").parse_args()
    asyncio.run(run_watch())


if __name__ == "__main__":
    main()
