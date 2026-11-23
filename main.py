import glob, os, subprocess, sys, threading, time
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Forex bot is running", 200


def find_bot_file():
    env = os.getenv("BOT_FILE")
    if env and os.path.exists(env):
        return env
    for path in sorted(glob.glob("*.py")):
        if path == "main.py":
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                if "telegram" in f.read().lower():
                    return path
        except OSError:
            pass
    return None


def run_bot_forever():
    bot_file = find_bot_file()
    if not bot_file:
        print("ERROR: bot file not found. Set BOT_FILE env var.", flush=True)
        return
    while True:
        print(f"Starting bot: {bot_file}", flush=True)
        code = subprocess.call([sys.executable, "-u", bot_file])
        print(f"Bot exited with code {code}, restarting in 10s", flush=True)
        time.sleep(10)


threading.Thread(target=run_bot_forever, daemon=True).start()
