import os
import json
import discord
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, time, date
from zoneinfo import ZoneInfo

# =========================
# Config (env vars)
# =========================
TOKEN = os.environ["DISCORD_BOT_TOKEN"]

# Best-version multi-channel format:
# CHANNELS="123456789012345678:totals_private.json,987654321098765432:totals_public.json"
CHANNELS_SPEC = os.environ["CHANNELS"]

NY_TZ = ZoneInfo("America/New_York")
GAMES = ["wordle", "zip", "tango", "queens", "crossclimb"]

# =========================
# Time window helpers
# =========================
def ny_day_window_utc(day_ny: date):
    start_ny = datetime.combine(day_ny, time(0, 0, 0), tzinfo=NY_TZ)
    end_ny = start_ny + timedelta(days=1)
    return start_ny.astimezone(timezone.utc), end_ny.astimezone(timezone.utc)

def today_ny_date() -> date:
    return datetime.now(NY_TZ).date()

def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"

def prev_month_key(d: date) -> str:
    if d.month == 1:
        return f"{d.year - 1:04d}-12"
    return f"{d.year:04d}-{d.month - 1:02d}"

def year_key(d: date) -> str:
    return f"{d.year:04d}"

def prev_year_key(d: date) -> str:
    return f"{d.year - 1:04d}"

# =========================
# Channel spec parsing
# =========================
def parse_channels_spec(spec: str) -> list[tuple[int, str]]:
    """
    Parses CHANNELS env var:
      "channel_id:totals.json,channel_id2:totals2.json"
    Returns list[(channel_id, totals_path)]
    """
    items: list[tuple[int, str]] = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            raise ValueError(f"Bad CHANNELS entry '{raw}'. Expected 'channel_id:totals_file.json'")
        ch_s, path = raw.split(":", 1)
        ch_s = ch_s.strip()
        path = path.strip()
        if not ch_s.isdigit():
            raise ValueError(f"Bad channel id '{ch_s}' in CHANNELS. Must be numeric.")
        if not path:
            raise ValueError(f"Bad totals path in CHANNELS entry '{raw}'.")
        items.append((int(ch_s), path))

    if not items:
        raise ValueError("CHANNELS is empty after parsing.")
    return items

# =========================
# LinkedIn time parsing (colon-based)
# =========================
def extract_time_seconds(message: str) -> int | None:
    """
    Finds the first token containing ':' and parses it as M:SS.
    Assumes exactly one ':' and total < 1 hour.
    """
    for token in message.split():
        if ":" not in token:
            continue

        cleaned = token.strip(".,!?:;()[]{}<>")

        if ":" not in cleaned:
            continue

        left, right = cleaned.split(":", 1)

        if not (left.isdigit() and right.isdigit()):
            continue

        minutes = int(left)
        seconds = int(right)

        if seconds >= 60:
            continue

        return minutes * 60 + seconds

    return None

def fmt_seconds(sec: int) -> str:
    m = sec // 60
    s = sec % 60
    return f"{m}:{s:02d}"

# =========================
# Parsing (NO regex)
# =========================
def parse_wordle_score(first_line: str) -> int | None:
    parts = first_line.strip().split()
    if len(parts) < 3 or parts[0].lower() != "wordle":
        return None
    frac = parts[-1]
    if "/6" not in frac:
        return None
    left = frac.split("/")[0].upper()
    if left == "X":
        return 100
    try:
        v = int(left)
        return v if 1 <= v <= 6 else 100
    except ValueError:
        return None

def parse_linkedin_game(message_content: str, game_name: str):
    """
    Returns (game_name, seconds, pretty_time) or None
    """
    if not message_content:
        return None

    lines = [ln.strip() for ln in message_content.splitlines() if ln.strip()]
    if not lines:
        return None

    if not lines[0].lower().startswith(game_name.lower()):
        return None

    seconds = extract_time_seconds(message_content)
    if seconds is None:
        return None

    return game_name, seconds, fmt_seconds(seconds)

def parse_game_metric(message_content: str):
    """
    Returns: (game_key, metric_value, pretty_metric)
    metric_value: lower is better.
    """
    if not message_content:
        return None

    first_line = message_content.strip().splitlines()[0].strip()
    if not first_line:
        return None

    if first_line.lower().startswith("wordle"):
        score = parse_wordle_score(first_line)
        if score is None:
            return None
        pretty = f"{score}/6" if score != 100 else "X/6"
        return ("wordle", score, pretty)

    for game in ["zip", "tango", "queens", "crossclimb"]:
        parsed = parse_linkedin_game(message_content, game)
        if parsed:
            return parsed

    return None

# =========================
# Daily bests + winners
# =========================
@dataclass
class DayStats:
    best_metric: dict[str, dict[str, int]] = field(default_factory=dict)
    best_pretty: dict[str, dict[str, str]] = field(default_factory=dict)

    def note(self, user: str, game: str, metric: int, pretty: str):
        if user not in self.best_metric:
            self.best_metric[user] = {}
            self.best_pretty[user] = {}
        prev = self.best_metric[user].get(game)
        if prev is None or metric < prev:
            self.best_metric[user][game] = metric
            self.best_pretty[user][game] = pretty

def compute_winners(day_stats: DayStats):
    winners_by_game = {g: [] for g in GAMES}
    best_value_by_game: dict[str, int] = {}

    for game in GAMES:
        best = None
        for user, gmap in day_stats.best_metric.items():
            if game not in gmap:
                continue
            val = gmap[game]
            if best is None or val < best:
                best = val

        if best is None:
            continue

        best_value_by_game[game] = best

        for user, gmap in day_stats.best_metric.items():
            if gmap.get(game) == best:
                winners_by_game[game].append(user)

    return winners_by_game, best_value_by_game

# =========================
# Totals persistence
# =========================
def ensure_user_bucket(bucket: dict, user: str):
    if user not in bucket:
        bucket[user] = {g: 0 for g in GAMES}
        bucket[user]["total"] = 0

def load_totals(path: str):
    if not os.path.exists(path):
        return {"meta": {"last_processed_day_ny": None}, "lifetime": {}, "monthly": {}, "yearly": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Backward-compatible defaults
    data.setdefault("meta", {}).setdefault("last_processed_day_ny", None)
    data.setdefault("lifetime", {})
    data.setdefault("monthly", {})
    data.setdefault("yearly", {})
    return data

def save_totals(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)

def apply_daily_wins(totals: dict, day_ny: date, winners_by_game: dict[str, list[str]]):
    day_key = day_ny.isoformat()
    last = totals.get("meta", {}).get("last_processed_day_ny")

    if last == day_key:
        return False

    totals.setdefault("meta", {})["last_processed_day_ny"] = day_key
    totals.setdefault("lifetime", {})
    totals.setdefault("monthly", {})
    totals.setdefault("yearly", {})

    mk = month_key(day_ny)
    yk = year_key(day_ny)

    totals["monthly"].setdefault(mk, {})
    totals["yearly"].setdefault(yk, {})

    for game, winners in winners_by_game.items():
        for user in winners:
            ensure_user_bucket(totals["lifetime"], user)
            ensure_user_bucket(totals["monthly"][mk], user)
            ensure_user_bucket(totals["yearly"][yk], user)

            totals["lifetime"][user][game] += 1
            totals["lifetime"][user]["total"] += 1

            totals["monthly"][mk][user][game] += 1
            totals["monthly"][mk][user]["total"] += 1

            totals["yearly"][yk][user][game] += 1
            totals["yearly"][yk][user]["total"] += 1

    return True

# =========================
# Report building
# =========================
def format_leaderboard(bucket: dict, title: str, max_rows: int = 10) -> list[str]:
    if not bucket:
        return [f"{title}\n- _no data_"]

    rows = []
    for user, stats in bucket.items():
        total = int(stats.get("total", 0))
        rows.append((total, user, stats))
    rows.sort(reverse=True, key=lambda x: x[0])

    lines = [title]
    for total, user, stats in rows[:max_rows]:
        lines.append(
            f"- **{user}** — {total} "
            f"(Wdl {stats.get('wordle',0)}, Zip {stats.get('zip',0)}, "
            f"Tng {stats.get('tango',0)}, Qns {stats.get('queens',0)}, Xcl {stats.get('crossclimb',0)})"
        )
    return lines

def build_report(day_ny: date, scanned: int, recognized: int,
                 winners_by_game: dict[str, list[str]],
                 best_value_by_game: dict[str, int],
                 totals: dict,
                 header_suffix: str | None = None):
    lines = []
    suffix = f" — {header_suffix}" if header_suffix else ""
    lines.append(f"🗓️ **Daily game winners — {day_ny} (America/New_York){suffix}**")
    lines.append(f"Scanned **{scanned}** messages; recognized **{recognized}** game posts.")
    lines.append("")

    for game in GAMES:
        winners = winners_by_game.get(game, [])
        if not winners:
            lines.append(f"**{game.title()}**: _no posts_")
            continue

        best_val = best_value_by_game[game]
        best_pretty = (f"{best_val}/6" if (game == "wordle" and best_val != 100) else "X/6") if game == "wordle" else fmt_seconds(best_val)
        winner_str = ", ".join(f"**{w}**" for w in winners)
        lines.append(f"**{game.title()}**: {winner_str} (best: {best_pretty})")

    day_totals = {}
    for game, winners in winners_by_game.items():
        for w in winners:
            day_totals[w] = day_totals.get(w, 0) + 1

    if day_totals:
        lines.append("")
        lines.append("**Wins today:**")
        for user, t in sorted(day_totals.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- **{user}**: {t}")

    # Monthly leaderboard every day (current month)
    mk = month_key(day_ny)
    month_bucket = totals.get("monthly", {}).get(mk, {})
    lines.append("")
    lines.extend(format_leaderboard(month_bucket, f"📅 **Monthly wins — {mk}**", max_rows=10))

    # All-time leaderboard
    lines.append("")
    lifetime = totals.get("lifetime", {})
    lines.extend(format_leaderboard(lifetime, "🏆 **All-time wins**", max_rows=10))

    # Yearly recap only on Jan 1 (previous year)
    if day_ny.month == 1 and day_ny.day == 1:
        pyk = prev_year_key(day_ny)
        year_bucket = totals.get("yearly", {}).get(pyk, {})
        lines.append("")
        lines.extend(format_leaderboard(year_bucket, f"🎉 **Yearly wins — {pyk}**", max_rows=10))

    # Monthly recap only on day 1 (previous month)
    # if day_ny.day == 1:
    #     pmk = prev_month_key(day_ny)
    #     month_bucket = totals.get("monthly", {}).get(pmk, {})
    #     lines.append("")
    #     lines.extend(format_leaderboard(month_bucket, f"📅 **Monthly wins — {pmk}**", max_rows=10))

    msg = "\n".join(lines)
    return msg if len(msg) <= 1900 else msg[:1900] + "\n…(truncated)"

# =========================
# Per-channel processing
# =========================
async def process_channel(
    client: discord.Client,
    channel_id: int,
    totals_path: str,
    day_ny: date,
):
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    start_utc, end_utc = ny_day_window_utc(day_ny)

    day_stats = DayStats()
    scanned = 0
    recognized = 0

    async for msg in channel.history(limit=None, after=start_utc, before=end_utc, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue

        parsed = parse_game_metric(msg.content)
        if not parsed:
            continue

        recognized += 1
        game, metric, pretty = parsed
        user = msg.author.display_name
        day_stats.note(user, game, metric, pretty)

    winners_by_game, best_value_by_game = compute_winners(day_stats)

    totals = load_totals(totals_path)
    updated = apply_daily_wins(totals, day_ny, winners_by_game)
    if updated:
        save_totals(totals_path, totals)

    # optional header suffix to help you distinguish reports
    suffix = os.path.basename(totals_path)
    report = build_report(day_ny, scanned, recognized, winners_by_game, best_value_by_game, totals, header_suffix=suffix)
    await channel.send(report)

# =========================
# Discord batch client
# =========================
class BatchClient(discord.Client):
    async def on_ready(self):
        try:
            channels = parse_channels_spec(CHANNELS_SPEC)

            # Process yesterday's results (NY calendar day)
            day_ny = today_ny_date() - timedelta(days=1)

            for channel_id, totals_path in channels:
                await process_channel(self, channel_id, totals_path, day_ny)

        finally:
            await self.close()

def main():
    intents = discord.Intents.default()
    intents.message_content = True
    client = BatchClient(intents=intents)
    client.run(TOKEN)

if __name__ == "__main__":
    main()
