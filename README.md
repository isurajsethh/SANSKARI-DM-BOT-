# Sanskari DM Bot (Render Ready)

## Files Included
- `bot.py` — Complete Telegram DM Relay Bot + Flask health server (`/` & `/health`) + SQLite storage
- `requirements.txt` — Python dependencies
- `Procfile` — Render start command
- `render.yaml` — Render blueprint config

## Render Deployment Steps
1. Extract this ZIP and upload all files to a GitHub repository.
2. On Render, create a **New Web Service** connected to that GitHub repository.
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. Add Environment Variables:
   - `BOT_TOKEN` = Your `@BotFather` token
   - `OWNER_ID` = `8283878643`
6. Deploy and add your Render URL to UptimeRobot / BetterStack (`https://your-app.onrender.com/health`) for 24x7 uptime.
