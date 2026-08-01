"""
Local test runner for the daily word-game bot.

Reads config from a gitignored `.env.test` file, points the bot at your
private "testing" channel + a throwaway totals file, and runs the exact same
code path as production (main.main()). The report is posted back into the
testing channel and totals are written to the test file only.

Usage:
    cp .env.test.example .env.test   # then fill it in
    pip install discord.py
    python test_bot.py

Safety: refuses to run against the real totals files.
"""
import os
import sys

ENV_FILE = ".env.test"
PROTECTED_TOTALS = {"totals_private.json", "totals_public.json"}


def load_env_file(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(
            f"Missing {path}. Create it with:\n"
            f"  cp .env.test.example {path}\n"
            f"then fill in your token and test channel ID."
        )
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def main():
    cfg = load_env_file(ENV_FILE)

    token = cfg.get("DISCORD_BOT_TOKEN", "")
    channel_id = cfg.get("TEST_CHANNEL_ID", "")
    totals_file = cfg.get("TEST_TOTALS_FILE", "totals_test.json") or "totals_test.json"
    process_date = cfg.get("PROCESS_DATE", "")

    if not token or token == "your-bot-token-here":
        sys.exit(f"Set DISCORD_BOT_TOKEN in {ENV_FILE}.")
    if not channel_id.isdigit():
        sys.exit(f"Set a numeric TEST_CHANNEL_ID in {ENV_FILE} (got '{channel_id}').")
    if totals_file in PROTECTED_TOTALS:
        sys.exit(
            f"Refusing to run: TEST_TOTALS_FILE is '{totals_file}', a real totals file. "
            f"Use something like totals_test.json."
        )

    # Configure the env the bot reads. Must be set before importing main,
    # which reads DISCORD_BOT_TOKEN and CHANNELS at import time.
    os.environ["DISCORD_BOT_TOKEN"] = token
    os.environ["CHANNELS"] = f"{channel_id}:{totals_file}"

    import main  # noqa: E402  (import after env is configured)

    # Default to "today" in New York when no explicit date is given, since
    # test messages you just posted land in today's window.
    if not process_date:
        process_date = main.today_ny_date().isoformat()
    os.environ["PROCESS_DATE"] = process_date

    print(f"Testing channel : {channel_id}")
    print(f"Totals file     : {totals_file}")
    print(f"Processing NY day: {process_date}")
    print("Running... (report will be posted into the testing channel)\n")

    main.main()


if __name__ == "__main__":
    main()
