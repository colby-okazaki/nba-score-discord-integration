import time
import requests
from datetime import datetime, date, timezone
import os
from dotenv import load_dotenv

load_dotenv()
WEBHOOK = os.getenv("DISCORD_WEBHOOK")


ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# --- CONFIG ---
#PING_USER_ID = "428747562686611457"
POLL_SECONDS = 60
# -------------

alerted_games = set()
last_day = date.today()
posted_schedule_for_day = False  # only post schedule once per day


def send_discord(msg: str) -> None:
    payload = {
        "content": msg,
        #"allowed_mentions": {"users": [PING_USER_ID]},
    }
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()


def fetch_scoreboard() -> dict:
    today = datetime.now().strftime("%Y%m%d")
    r = requests.get(f"{ESPN_URL}?dates={today}", timeout=10)
    r.raise_for_status()
    return r.json()


def get_home_away(event: dict):
    comp = event["competitions"][0]
    competitors = comp["competitors"]
    home = next(t for t in competitors if t["homeAway"] == "home")
    away = next(t for t in competitors if t["homeAway"] == "away")
    return away, home


def parse_event_datetime_local(event: dict):
    iso = event.get("date")
    if not iso:
        return None
    iso = iso.replace("Z", "+00:00")
    dt_utc = datetime.fromisoformat(iso)  # aware UTC
    return dt_utc.astimezone()  # convert to local timezone


def format_time_until(start_local: datetime) -> str:
    now = datetime.now().astimezone()
    delta = start_local - now

    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "started"

    minutes = (seconds + 59) // 60  # round up
    hours = minutes // 60
    mins = minutes % 60

    if hours == 0:
        return f"in {mins}m"
    if mins == 0:
        return f"in {hours}h"
    return f"in {hours}h {mins}m"


def format_start_time(start_local: datetime) -> str:
    # mac/linux supports %-I; if that ever errors for you, swap to "%I:%M %p"
    return start_local.strftime("%-I:%M %p")


def is_halftime(event: dict) -> bool:
    st = event["status"]["type"]
    return (st.get("name") == "STATUS_HALFTIME") or (st.get("detail") == "Halftime")


def format_live_line(event: dict) -> str:
    away, home = get_home_away(event)
    status = event["status"]["type"].get("detail", "Unknown")
    away_score = away.get("score", "0")
    home_score = home.get("score", "0")
    return f"{away['team']['displayName']} {away_score} – {home['team']['displayName']} {home_score} ({status})"


def post_schedule_once(events: list) -> None:
    """
    Post all games once per day, including start time + time-until.
    """
    global posted_schedule_for_day
    if posted_schedule_for_day:
        return

    if not events:
        send_discord("🗓️ **Today's NBA schedule**\nNo NBA games found for today.")
        posted_schedule_for_day = True
        return

    # Sort by start time
    def sort_key(e):
        dt = parse_event_datetime_local(e)
        return dt.timestamp() if dt else float("inf")

    events_sorted = sorted(events, key=sort_key)

    lines = []
    for e in events_sorted:
        start_local = parse_event_datetime_local(e)
        away, home = get_home_away(e)

        if start_local:
            t_str = format_start_time(start_local)
            until = format_time_until(start_local)
            lines.append(f"{t_str} ({until}) — {away['team']['displayName']} @ {home['team']['displayName']}")
        else:
            lines.append(f"TBD — {away['team']['displayName']} @ {home['team']['displayName']}")

    msg = "🗓️ **Today's NBA schedule**\n" + "\n".join(lines)
    send_discord(msg)
    posted_schedule_for_day = True


def check_games() -> None:
    data = fetch_scoreboard()
    events = data.get("events", [])

    # Post schedule only once (on first run per day)
    post_schedule_once(events)

    # Halftime alerts
    for event in events:
        game_id = event.get("id")
        if not game_id:
            continue

        if is_halftime(event) and game_id not in alerted_games:
            send_discord(f"<@{PING_USER_ID}>\n🏀 **HALFTIME**\n{format_live_line(event)}")
            alerted_games.add(game_id)


def main() -> None:
    global last_day, posted_schedule_for_day

    if not DISCORD_WEBHOOK.startswith("https://discord.com/api/webhooks/"):
        raise ValueError("DISCORD_WEBHOOK is not set to a valid Discord webhook URL.")

    send_discord(f"✅ Halftime bot started. <@{PING_USER_ID}>")

    # First run: post schedule once + start monitoring
    check_games()

    while True:
        try:
            # New day reset
            if date.today() != last_day:
                alerted_games.clear()
                last_day = date.today()
                posted_schedule_for_day = False
                send_discord("🔄 New day — reset alerted games.")
                check_games()

            check_games()
        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
