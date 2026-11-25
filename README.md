# Word Game Async Discord Bot

A Discord bot that scores daily puzzle results and keeps a running leaderboard. People paste their share text from Wordle and LinkedIn's daily games into a channel. Once a day the bot reads the previous day's posts, works out who won each game, posts a report, and saves the updated totals back to this repo.

It runs as a scheduled GitHub Actions job, so nothing needs to be hosted. Each run connects to Discord, does its work, and exits.

## Supported games

Wordle is scored by guesses. A 2/6 beats a 4/6, and a failed puzzle (X/6) ranks below every solve.

The LinkedIn games are scored by time: Zip, Tango, Queens, Crossclimb, Patches and Wend. If the share text doesn't say the puzzle was solved with no hints, the bot adds 60 seconds to the time. LinkedIn puts that line in the share text on its own, so players don't need to type anything.

A post counts if its first line starts with the game name, which is how the native share text is laid out. For example:

```
Wordle 1,618 3/6
```

```
Zip #262 | 0:37
With no hints
```

The bot takes the first `M:SS` value in the post as the time. Posts from other bots and messages that don't match a game are skipped.

## How a day is scored

Days follow the America/New_York calendar. For each channel, the bot:

1. Reads every message posted between midnight and midnight New York time on the previous day.
2. Gives one win per game to whoever had the best result. Everyone tied for first gets a win.
3. Adds those wins to lifetime, monthly and yearly totals in that channel's JSON file.

The report posted to the channel shows that day's winner and best result for each game, how many games each person won that day, the current month's standings, how many completed months each person has won, and the all-time leaderboard. On the last day of a month the bot also posts a message congratulating the month's winner, or co-winners if there's a tie. On January 1 the report adds a recap of the previous year.

Each totals file records the last day it processed, so a re-run for the same day won't count wins twice.

## Configuration

The bot reads two environment variables, both set as GitHub Actions secrets:

`DISCORD_BOT_TOKEN` is the bot's token from the Discord Developer Portal. The bot needs the Message Content intent turned on, plus permission to read message history and send messages in each channel it scores.

`CHANNELS` maps Discord channel IDs to totals files, separated by commas. You can list any number of channels, and each one keeps its own separate leaderboard:

```
CHANNELS="123456789012345678:totals_a.json,987654321098765432:totals_b.json"
```

`PROCESS_DATE` is optional and takes a date in `YYYY-MM-DD` format. Set it to score a specific New York day instead of yesterday.

## Files

`main.py` is the bot: parsing, scoring, totals, and report formatting.

`totals_*.json` are the saved leaderboards, one per channel. The daily workflow commits them after each run, so the git history doubles as a day-by-day record of the standings.

`backfill.py` re-scores a past range of days. It exists for the case where a parsing bug scored some days wrong. It rewinds the totals to an earlier commit, replays each day through the current `main.py` against the real channel history, and writes a markdown report of what changed. For each day the report shows the old winners and the new ones, and it flags posts that mention hints but still got the penalty. It never posts to Discord and never commits anything.

`test_bot.py` runs the production code against a private test channel and a scratch totals file. It reads its settings from a gitignored `.env.test` file (copy `.env.test.example` to start) and won't run if pointed at a real totals file.

## Workflows

`daily.yml` runs every day at 05:05 UTC, which is just after midnight in New York. It scores yesterday for each channel in `CHANNELS`, then commits any changed totals files. You can also start it by hand from the Actions tab.

`backfill.yml` is manual only. You give it a baseline git ref, a start date, an end date, and choose whether it's a dry run. It runs `backfill.py` and uploads the corrected totals and the report as a downloadable artifact. Nothing is committed, so you look over the results before copying the corrected files in.

## Running locally

Requires Python 3.10 or newer and `discord.py`.

```
pip install discord.py
cp .env.test.example .env.test   # add your token and test channel ID
python test_bot.py
```

Post a few results in your test channel first. By default the test runner scores today, not yesterday, so those posts get picked up.

## Known limitations

Players are identified by Discord display name, not user ID. Changing your nickname starts a new row on the leaderboard, and two people with the same display name get counted as one.

Discord messages are capped in length, so the daily report is cut off at 1,900 characters. On a busy channel the leaderboards near the bottom can get truncated.
