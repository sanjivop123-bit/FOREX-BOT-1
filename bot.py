import asyncio
import logging
import os

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import signal_hub_ptb as signal_hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("forex-bot")

TOKEN = os.getenv("BOT_TOKEN")
API = "https://api.frankfurter.app"
MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]

HELP = (
    "Forex Bot commands:\n"
    "/price EURUSD - live rate of a pair\n"
    "/majors - all major pairs\n"
    "/convert 100 USD INR - currency converter\n"
    "/help - this message"
)


def get_rate(base: str, quote: str) -> float:
    r = requests.get(f"{API}/latest", params={"from": base, "to": quote}, timeout=15)
    r.raise_for_status()
    return r.json()["rates"][quote]


def parse_pair(text: str):
    t = text.upper().replace("/", "").replace("-", "").strip()
    if len(t) != 6 or not t.isalpha():
        return None
    return t[:3], t[3:]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to Forex Bot!\n\n" + HELP)
    # start forex signals for this user
    await signal_hub.on_start(context.bot, update.effective_chat.id)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price EURUSD")
        return
    pair = parse_pair(context.args[0])
    if not pair:
        await update.message.reply_text("Invalid pair. Example: /price EURUSD")
        return
    try:
        rate = await asyncio.to_thread(get_rate, *pair)
        await update.message.reply_text(f"{pair[0]}/{pair[1]} = {rate:.5f}")
    except Exception as e:
        log.warning("price error: %s", e)
        await update.message.reply_text("Could not get that pair right now.")


async def majors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    for p in MAJORS:
        b, q = p[:3], p[3:]
        try:
            rate = await asyncio.to_thread(get_rate, b, q)
            lines.append(f"{b}/{q}: {rate:.5f}")
        except Exception:
            lines.append(f"{b}/{q}: unavailable")
    await update.message.reply_text("\n".join(lines))


async def convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(context.args[0])
        base, quote = context.args[1].upper(), context.args[2].upper()
        rate = await asyncio.to_thread(get_rate, base, quote)
        await update.message.reply_text(f"{amount:g} {base} = {amount * rate:,.4f} {quote}")
    except Exception:
        await update.message.reply_text("Usage: /convert 100 USD INR")


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("majors", majors))
    app.add_handler(CommandHandler("convert", convert))
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
