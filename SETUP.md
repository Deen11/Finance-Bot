# Finance Tracker Bot — Setup Guide

A Telegram bot that logs your expenses via natural language, powered by Gemini Flash (free) and Google Sheets.

---

## What you need
- Telegram account
- Google account
- Python 3.11+

---

## Step 1 — Create your Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Give it a name (e.g. `Deen Finance`) and a username (e.g. `deenfinance_bot`)
4. BotFather gives you a **token** — looks like `7123456789:AAF...`
5. Save this as `TELEGRAM_TOKEN`

---

## Step 2 — Get Gemini API key (free)

1. Go to **https://aistudio.google.com**
2. Sign in with your Google account
3. Click **"Get API key"** → **"Create API key"**
4. Copy the key
5. Save this as `GEMINI_API_KEY`

Free tier: 1,500 requests/day — more than enough for personal use.

---

## Step 3 — Set up Google Sheets

### 3a. Create the spreadsheet
1. Go to **https://sheets.new** to create a new Google Sheet
2. Name it `Finance Tracker` (or anything you like)
3. Copy the Sheet ID from the URL:
   - URL looks like: `https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit`
   - The long string between `/d/` and `/edit` is your `SHEET_ID`

### 3b. Create a Service Account (so the bot can write to the sheet)
1. Go to **https://console.cloud.google.com**
2. Create a new project (or use an existing one)
3. Enable APIs:
   - Search "Google Sheets API" → Enable
   - Search "Google Drive API" → Enable
4. Go to **IAM & Admin** → **Service Accounts**
5. Click **Create Service Account**
   - Name: `finance-bot` (anything)
   - Click through to finish
6. Click on your new service account → **Keys** tab
7. **Add Key** → **Create new key** → **JSON**
8. A JSON file downloads — open it and copy the entire contents

### 3c. Share your sheet with the service account
1. In your Google Sheet, click **Share**
2. Paste the `client_email` from your JSON file (looks like `finance-bot@your-project.iam.gserviceaccount.com`)
3. Give it **Editor** access
4. Uncheck "Notify people" → Share

### 3d. Format the JSON for the .env
The JSON needs to be on a single line. In terminal:
```bash
cat your-downloaded-file.json | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)))"
```
Copy the output — this is your `GOOGLE_CREDS_JSON`.

---

## Step 4 — Install and run locally

```bash
# Clone / navigate to the project folder
cd finance-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env and fill in your values

# Load env and run
export $(cat .env | xargs)
python main.py
```

You should see: `Bot running — polling for updates`

Open Telegram, find your bot, send `/start` — done.

---

## Step 5 — Deploy to Railway (so it runs 24/7)

Railway gives you $5 free credit/month — enough to run this bot continuously.

1. Go to **https://railway.app** and sign up with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
   - Push your code to a GitHub repo first (without `.env` — never commit secrets)
3. Add environment variables in Railway dashboard:
   - Go to your service → **Variables** tab
   - Add each variable from your `.env` file one by one
4. Railway auto-detects Python and runs `main.py`
5. Done — bot runs 24/7

---

## How to use the bot

Just text it naturally:

| You say | What happens |
|---|---|
| `spent $12 on chicken rice` | Logs $12 Food expense |
| `paid mom $800` | Logs $800 Family expense |
| `bought earphones $35` | Logs $35 Shopping expense |
| `took bus $1.50` | Logs $1.50 Transport expense |
| `Spotify student $6.48` | Logs $6.48 Subscriptions expense |
| `salary $3000` | Logs $3000 income |
| `/summary` | Shows full month breakdown |
| `/today` | Shows today's transactions |
| `/budget` | Shows remaining budget |

---

## Viewing your data

Your Google Sheet auto-populates with every transaction. You can:
- View it on your phone via the Google Sheets app
- Add your own charts/pivot tables
- Share read-only access with yourself on desktop

The sheet has columns: **Date, Time, Month, Type, Category, Description, Amount**

---

## Changing your monthly budget

Edit `MONTHLY_BUDGET` in your `.env` or Railway environment variables.
Default is set to `1700` (your estimated monthly spend).

---

## Troubleshooting

**Bot not responding**
- Check that `TELEGRAM_TOKEN` is correct
- Make sure the bot is running (`python main.py`)

**"Error saving"**
- Check `SHEET_ID` is correct (just the ID, not the full URL)
- Make sure you shared the sheet with the service account email

**Gemini not parsing correctly**
- The bot will reply "Didn't catch that as a transaction"
- Rephrase slightly, e.g. `spent $X on Y` always works
