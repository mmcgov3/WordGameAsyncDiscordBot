"""
Replay runner: recompute daily winners for a past range of NY days.

Use this when a parsing bug means the daily job scored a stretch of days wrong.
It rewinds the totals files to a known-good git revision and re-plays each day
in order using the *current* parsing code in main.py.

It never posts to Discord and never commits — it only reads channel history and
rewrites the totals files on disk, plus a markdown report of what changed.

Config (env vars):
    DISCORD_BOT_TOKEN   same token as production
    CHANNELS            same spec as production:
                        "chan_id:totals_private.json,chan_id2:totals_public.json"
    BASELINE_REF        git revision holding the totals to rewind to. Must be the
                        state *before* START_DATE was processed.
    START_DATE          first NY day to replay (YYYY-MM-DD)
    END_DATE            last NY day to replay, inclusive (YYYY-MM-DD)
    REPORT_PATH         markdown report output (default: backfill_report.md)
    DRY_RUN             "1" to compute and report without writing totals files
    FORCE               "1" to skip the baseline sanity check

Example (the Aug 2026 "with no hints" fix):
    BASELINE_REF=6fac58d START_DATE=2026-08-01 END_DATE=2026-08-07 python backfill.py
"""
import json
import os
import subprocess
import sys
from datetime import date, timedelta

import discord

import main

# The phrase the buggy code required. Kept only so the report can point at the
# messages the fix actually changed; scoring always uses main.NO_HINTS_PHRASE.
OLD_NO_HINTS_PHRASE = "with no hints"


# =========================
# Config
# =========================
def read_config():
    baseline_ref = os.environ.get("BASELINE_REF", "").strip()
    start_s = os.environ.get("START_DATE", "").strip()
    end_s = os.environ.get("END_DATE", "").strip()

    missing = [
        name
        for name, val in (("BASELINE_REF", baseline_ref), ("START_DATE", start_s), ("END_DATE", end_s))
        if not val
    ]
    if missing:
        sys.exit(f"Missing required env var(s): {', '.join(missing)}")

    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)
    if end < start:
        sys.exit(f"END_DATE ({end}) is before START_DATE ({start}).")

    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)

    return baseline_ref, days


# =========================
# Git helpers
# =========================
def git_show(ref: str, path: str) -> str:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"git show {ref}:{path} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def load_baseline(ref: str, path: str, first_day: date) -> dict:
    data = json.loads(git_show(ref, path))
    data.setdefault("meta", {}).setdefault("last_processed_day_ny", None)
    for key in ("lifetime", "monthly", "yearly"):
        data.setdefault(key, {})

    expected = (first_day - timedelta(days=1)).isoformat()
    actual = data["meta"]["last_processed_day_ny"]
    if actual != expected and os.environ.get("FORCE", "") != "1":
        sys.exit(
            f"Baseline {ref}:{path} has last_processed_day_ny={actual!r}, "
            f"expected {expected!r} (the day before START_DATE).\n"
            f"Pick a different BASELINE_REF, or set FORCE=1 if you know better."
        )
    return data


def totals_commits_by_day(path: str) -> dict[str, tuple[str, str]]:
    """
    Maps a processed NY day -> (commit that recorded it, preceding commit).

    Lets the report show who *was* credited for each day before the replay.
    """
    proc = subprocess.run(
        ["git", "log", "--format=%H", "--", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {}

    commits = [c for c in proc.stdout.split() if c]  # newest first
    by_day: dict[str, tuple[str, str]] = {}
    for i, commit in enumerate(commits):
        if i + 1 >= len(commits):
            break
        try:
            data = json.loads(git_show(commit, path))
        except SystemExit:
            continue
        day = data.get("meta", {}).get("last_processed_day_ny")
        if day and day not in by_day:
            by_day[day] = (commit, commits[i + 1])
    return by_day


def old_winners_for_day(path: str, day: date, day_map: dict[str, tuple[str, str]]):
    """Reconstructs a day's recorded wins from the lifetime deltas in git."""
    entry = day_map.get(day.isoformat())
    if not entry:
        return None

    commit, parent = entry
    after = json.loads(git_show(commit, path)).get("lifetime", {})
    before = json.loads(git_show(parent, path)).get("lifetime", {})

    winners: dict[str, list[str]] = {g: [] for g in main.GAMES}
    for user, stats in after.items():
        prev = before.get(user, {})
        for game in main.GAMES:
            if int(stats.get(game, 0)) - int(prev.get(game, 0)) > 0:
                winners[game].append(user)
    return winners


# =========================
# Scanning
# =========================
async def scan_day(channel, day_ny: date):
    """Mirrors main.process_channel's scan, plus hint-phrase diagnostics."""
    start_utc, end_utc = main.ny_day_window_utc(day_ny)

    day_stats = main.DayStats()
    scanned = 0
    recognized = 0
    rescued = []   # messages the fix changed: "no hints" but not "with no hints"
    suspicious = []  # still penalized despite mentioning hints somehow

    async for msg in channel.history(limit=None, after=start_utc, before=end_utc, oldest_first=False):
        scanned += 1
        if msg.author.bot:
            continue

        parsed = main.parse_game_metric(msg.content)
        if not parsed:
            continue

        recognized += 1
        game, metric, pretty = parsed
        user = msg.author.display_name
        day_stats.note(user, game, metric, pretty)

        lower = msg.content.lower()
        if game != "wordle":
            if main.NO_HINTS_PHRASE in lower and OLD_NO_HINTS_PHRASE not in lower:
                rescued.append((user, game, pretty))
            elif "hint" in lower and main.NO_HINTS_PHRASE not in lower:
                suspicious.append((user, game, msg.content.splitlines()[0][:80]))

    return day_stats, scanned, recognized, rescued, suspicious


# =========================
# Diffing
# =========================
def bucket_delta(before: dict, after: dict) -> list[str]:
    lines = []
    for user in sorted(set(before) | set(after), key=str.lower):
        b = before.get(user, {})
        a = after.get(user, {})
        changes = []
        for game in main.GAMES + ["total"]:
            bv, av = int(b.get(game, 0)), int(a.get(game, 0))
            if bv != av:
                changes.append(f"{game} {bv}→{av} ({av - bv:+d})")
        if changes:
            lines.append(f"  - **{user}**: " + ", ".join(changes))
    return lines


def diff_report(path: str, before: dict, after: dict, days: list[date]) -> list[str]:
    lines = [f"### `{path}`"]

    scopes = [("lifetime", before.get("lifetime", {}), after.get("lifetime", {}))]
    for mk in sorted({main.month_key(d) for d in days}):
        scopes.append(
            (f"monthly {mk}", before.get("monthly", {}).get(mk, {}), after.get("monthly", {}).get(mk, {}))
        )
    for yk in sorted({main.year_key(d) for d in days}):
        scopes.append(
            (f"yearly {yk}", before.get("yearly", {}).get(yk, {}), after.get("yearly", {}).get(yk, {}))
        )

    any_change = False
    for label, b, a in scopes:
        rows = bucket_delta(b, a)
        if rows:
            any_change = True
            lines.append(f"- {label}:")
            lines.extend(rows)

    if not any_change:
        lines.append("- _no net change_")
    return lines


# =========================
# Replay
# =========================
async def replay_channel(client, channel_id: int, totals_path: str, days: list[date],
                         baseline_ref: str, dry_run: bool, report: list[str]):
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    current = main.load_totals(totals_path)
    totals = load_baseline(baseline_ref, totals_path, days[0])
    day_map = totals_commits_by_day(totals_path)

    report.append(f"## {totals_path} (channel {channel_id})")
    report.append(f"Rewound to `{baseline_ref}`, replaying {days[0]} → {days[-1]}.")
    report.append("")

    for day in days:
        day_stats, scanned, recognized, rescued, suspicious = await scan_day(channel, day)
        winners, best = main.compute_winners(day_stats)

        if not main.apply_daily_wins(totals, day, winners):
            report.append(f"### {day} — SKIPPED (already recorded as last processed day)")
            report.append("")
            continue

        old = old_winners_for_day(totals_path, day, day_map)

        report.append(f"### {day}")
        report.append(f"Scanned {scanned} messages, recognized {recognized} game posts.")
        for game in main.GAMES:
            new_w = winners.get(game, [])
            if not new_w and not (old or {}).get(game):
                continue
            pretty = ""
            if game in best:
                val = best[game]
                pretty = (("X/6" if val == 100 else f"{val}/6") if game == "wordle"
                          else main.fmt_seconds(val))
                pretty = f" (best {pretty})"
            new_s = ", ".join(new_w) or "—"
            if old is None:
                report.append(f"- {game}: {new_s}{pretty}")
                continue
            old_s = ", ".join(old.get(game, [])) or "—"
            marker = "  ← CHANGED" if sorted(old.get(game, [])) != sorted(new_w) else ""
            report.append(f"- {game}: {old_s} → {new_s}{pretty}{marker}")

        if rescued:
            report.append(f"- _hint-fix applied to {len(rescued)} post(s):_ " +
                          ", ".join(f"{u}/{g} {p}" for u, g, p in rescued))
        if suspicious:
            report.append("- _⚠ still penalized but mentions hints — check the wording:_")
            for user, game, snippet in suspicious:
                report.append(f"    - {user} / {game}: `{snippet}`")
        report.append("")

    report.append("**Net change vs. the totals currently on disk:**")
    report.extend(diff_report(totals_path, current, totals, days))
    report.append("")

    if dry_run:
        report.append(f"_DRY_RUN: {totals_path} not written._")
    else:
        main.save_totals(totals_path, totals)
        report.append(f"_Wrote {totals_path}._")
    report.append("")


class BackfillClient(discord.Client):
    def __init__(self, channels, days, baseline_ref, dry_run, **kwargs):
        super().__init__(**kwargs)
        self.channels_spec = channels
        self.days = days
        self.baseline_ref = baseline_ref
        self.dry_run = dry_run
        self.report: list[str] = []
        self.failed = False

    async def on_ready(self):
        try:
            for channel_id, totals_path in self.channels_spec:
                await replay_channel(
                    self, channel_id, totals_path, self.days,
                    self.baseline_ref, self.dry_run, self.report,
                )
        except Exception as exc:  # keep the report; surface the failure at exit
            self.failed = True
            self.report.append(f"\n**FAILED:** `{type(exc).__name__}: {exc}`")
            raise
        finally:
            await self.close()


def run():
    baseline_ref, days = read_config()
    channels = main.parse_channels_spec(main.CHANNELS_SPEC)
    dry_run = os.environ.get("DRY_RUN", "") == "1"
    report_path = os.environ.get("REPORT_PATH", "backfill_report.md")

    header = [
        f"# Backfill replay — {days[0]} → {days[-1]}",
        "",
        f"Baseline: `{baseline_ref}` · Channels: {len(channels)} · "
        f"{'DRY RUN (no files written)' if dry_run else 'writing totals files'}",
        "",
    ]
    print("\n".join(header))

    intents = discord.Intents.default()
    intents.message_content = True
    client = BackfillClient(channels, days, baseline_ref, dry_run, intents=intents)

    try:
        client.run(main.TOKEN)
    finally:
        body = "\n".join(header + client.report)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(body)
        print("\n".join(client.report))
        print(f"\nReport written to {report_path}")

    if client.failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
