1) Put signal_hub.py next to your main bot file.

2) requirements.txt -> add:
   requests
   pandas

3) In your existing bot file add 1 import + 1 line (nothing else changes):

   import signal_hub

   @bot.message_handler(commands=['start'])
   def start(message):
       ... your existing welcome code (unchanged) ...
       signal_hub.on_start(bot, message.chat.id)     # <-- add this line

4) Render > Environment:
   TWELVE_DATA_API_KEY = your key
   (BOT_TOKEN etc. stay as they are)

5) Redeploy, then send /start to the bot.

Notes
- Free Twelve Data plan = 8 calls/min, 800/day. 6 pairs every 15 min = ~576/day (OK).
  More pairs or faster scans need a paid plan.
- Render free disk resets on redeploy, so subscribers.json is lost;
  users just press /start again (or move subscribers to PostgreSQL).
- Free Render web services sleep when idle; the scanner stops while asleep.
  Use a paid instance or a Background Worker for 24/7 signals.
- Only ONE instance of the bot may poll Telegram at a time.
