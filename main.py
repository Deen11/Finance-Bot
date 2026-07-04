import os
import json
import logging
import re
import calendar
from datetime import datetime, date
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # loads .env file automatically

import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]   # full JSON string of service account
SHEET_ID         = os.environ["SHEET_ID"]
MONTHLY_BUDGET   = float(os.environ.get("MONTHLY_BUDGET", "1700"))

# ── Gemini ────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.0-flash-exp")

PARSE_PROMPT = """You are a personal finance parser for a Singaporean user. Parse the message into JSON.

Categories (pick the best match):
- Food        → meals, lunch, dinner, supper, breakfast, hawker, kopitiam, boba, kopi, drinks, snacks, food delivery
- Transport   → bus, MRT, Grab, taxi, SimplyGo, EZ-Link top-up
- Shopping    → clothes, shoes, electronics, games, personal care, gadgets, random purchases
- Family      → anything sent to mom / mum / mother / parents
- Savings     → transfers to savings account, MariBank deposits
- Subscriptions → Netflix, Spotify, Apple Music, iCloud, YouTube Premium, any digital sub
- Income      → salary, allowance, received money, earned money, ang bao
- Other       → anything that doesn't fit above

Example messages:
- spent $12 on chicken rice
- paid mom $800
- bought earphones $35
- salary $3000 received
- Spotify $6.48
- spent $5.98 on Apple Music

Message: "{message}"

Return ONLY valid JSON — no markdown, no backticks, no explanation:
{{"amount": <positive number>, "category": "<category>", "description": "<3-5 word description>", "type": "<expense or income>"}}

If the message is not a financial transaction, return:
{{"error": "not a transaction"}}"""


def extract_json_payload(raw: str) -> Optional[str]:
    raw = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1)

    brace_match = re.search(r"(\{(?:[^{}]|(?1))*\})", raw)
    if brace_match:
        return brace_match.group(1)

    return None


def simple_parse_transaction(text: str) -> Optional[dict]:
    text = text.lower().strip()
    amount_match = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", text)
    if not amount_match:
        return None

    amount = float(amount_match.group(1))
    trans_type = "expense"
    if re.search(r"\b(salary|income|earned|received|paycheck|ang bao|angbao)\b", text):
        trans_type = "income"

    category = "Other"
    if re.search(r"\b(food|lunch|dinner|breakfast|hawker|kopitiam|boba|kopi|snack|snacks|delivery)\b", text):
        category = "Food"
    elif re.search(r"\b(bus|mrt|grab|taxi|simplygo|ez-link|ezlink|transport|uber)\b", text):
        category = "Transport"
    elif re.search(r"\b(clothes|shoes|electronics|games|beauty|personal care|gadgets|gift|shopping|earphones|headphones|phone|watch)\b", text):
        category = "Shopping"
    elif re.search(r"\b(mom|mum|mother|parents|family|dad|father)\b", text):
        category = "Family"
    elif re.search(r"\b(savings|mari bank|maribank|transfer|deposit)\b", text):
        category = "Savings"
    elif re.search(r"\b(netflix|spotify|apple music|youtube premium|icloud|subscription|sub)\b", text):
        category = "Subscriptions"
    elif trans_type == "income":
        category = "Income"

    description = re.sub(r"\$?[0-9]+(?:\.[0-9]+)?", "", text)
    description = re.sub(r"\b(spent|paid|bought|purchased|received|got|for|on|to)\b", "", description)
    description = re.sub(r"[^a-z0-9 ]", "", description).strip()
    description_words = [w for w in description.split() if w and w not in {"a", "the", "to", "on", "for"}]
    description = " ".join(description_words[:5]) or category

    return {
        "amount": amount,
        "category": category,
        "description": description.title(),
        "type": trans_type,
    }


async def parse_with_gemini(text: str) -> Optional[dict]:
    try:
        response = gemini.generate_content(PARSE_PROMPT.format(message=text))
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        payload = extract_json_payload(raw) or raw
        return json.loads(payload.strip())
    except Exception as e:
        logger.error(f"Gemini parse error: {e}")
        return simple_parse_transaction(text)


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_worksheet():
    creds_data = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_data,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)

    # Create sheet with headers if it doesn't exist
    try:
        ws = spreadsheet.worksheet("Transactions")
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet("Transactions", rows=2000, cols=7)
        ws.append_row(["Date", "Time", "Month", "Type", "Category", "Description", "Amount"])

    return ws


def normalize_transaction(result: dict) -> Optional[dict]:
    if not result:
        return None

    # Clean up values that Gemini may return with dollar signs or whitespace.
    try:
        amount = result.get("amount")
        if isinstance(amount, str):
            amount = amount.replace("$", "").replace(",", "").strip()
        amount = float(amount)
    except (TypeError, ValueError):
        return None

    category = str(result.get("category", "Other")).strip().title()
    description = str(result.get("description", "")).strip()
    trans_type = str(result.get("type", "expense")).strip().lower()

    if trans_type not in {"expense", "income"}:
        trans_type = "expense"

    if not description:
        description = category

    return {
        "amount": amount,
        "category": category,
        "description": description,
        "type": trans_type,
    }


def log_transaction(amount: float, category: str, description: str, trans_type: str):
    ws = get_worksheet()
    now = datetime.now()
    signed = amount if trans_type == "income" else -amount
    ws.append_row([
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        now.strftime("%B %Y"),
        trans_type.capitalize(),
        category,
        description,
        signed,
    ])


def fetch_monthly_summary(month_str: Optional[str] = None) -> dict:
    if not month_str:
        month_str = datetime.now().strftime("%B %Y")

    ws = get_worksheet()
    records = ws.get_all_records()

    income = 0.0
    expenses = 0.0
    categories: dict[str, float] = {}

    for r in records:
        if r.get("Month") != month_str:
            continue
        try:
            amt = float(r.get("Amount", 0))
        except (ValueError, TypeError):
            continue

        trans_type = r.get("Type", "").lower()
        category = r.get("Category", "Other")

        if trans_type == "income":
            income += amt
        else:
            expenses += abs(amt)
            categories[category] = categories.get(category, 0) + abs(amt)

    return {
        "month": month_str,
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "categories": dict(sorted(categories.items(), key=lambda x: x[1], reverse=True)),
    }


def fetch_today() -> list:
    ws = get_worksheet()
    records = ws.get_all_records()
    today_str = date.today().strftime("%Y-%m-%d")
    return [r for r in records if r.get("Date") == today_str]


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Finance Tracker*\n\n"
        "Just text me what you spent:\n"
        "• `spent $12 on chicken rice`\n"
        "• `paid mom $800`\n"
        "• `bought earphones $35`\n"
        "• `salary $3000 received`\n"
        "• `Spotify $6.48`\n\n"
        "*Commands:*\n"
        "/summary — This month's full breakdown\n"
        "/today — What you've logged today\n"
        "/budget — How much you have left\n"
        "/help — Show this again",
        parse_mode="Markdown",
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching...")
    try:
        data = fetch_monthly_summary()

        cat_lines = ""
        for cat, amt in data["categories"].items():
            filled = min(int(amt / 80), 10)
            bar = "█" * filled + "░" * (10 - filled)
            cat_lines += f"  {bar} {cat}: *${amt:.2f}*\n"

        net_emoji = "✅" if data["net"] >= 0 else "⚠️"

        text = (
            f"📊 *{data['month']}*\n\n"
            f"💰 Income:   `${data['income']:.2f}`\n"
            f"💸 Expenses: `${data['expenses']:.2f}`\n"
            f"{net_emoji} Net:      `${data['net']:.2f}`\n\n"
            f"*Breakdown:*\n{cat_lines}"
        )
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Summary error: {e}")
        await msg.edit_text("❌ Couldn't fetch summary. Try again.")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching...")
    try:
        records = fetch_today()

        if not records:
            await msg.edit_text("📅 Nothing logged today yet!")
            return

        lines = ""
        total = 0.0
        for r in records:
            amt = abs(float(r.get("Amount", 0)))
            t = r.get("Type", "").lower()
            emoji = "💸" if t == "expense" else "💰"
            lines += f"{emoji} `${amt:.2f}` {r.get('Category')} — {r.get('Description')} _{r.get('Time', '')}_\n"
            if t == "expense":
                total += amt

        await msg.edit_text(
            f"📅 *Today*\n\n{lines}\n*Total spent: ${total:.2f}*",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Today error: {e}")
        await msg.edit_text("❌ Error fetching today's transactions.")


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Checking...")
    try:
        data = fetch_monthly_summary()
        remaining = MONTHLY_BUDGET - data["expenses"]

        today_date = date.today()
        last_day = calendar.monthrange(today_date.year, today_date.month)[1]
        days_left = last_day - today_date.day + 1

        pct = min((data["expenses"] / MONTHLY_BUDGET * 100) if MONTHLY_BUDGET > 0 else 0, 100)
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)

        status = "✅" if remaining > 0 else "🚨 OVER BUDGET"
        daily_left = remaining / days_left if days_left > 0 else 0

        text = (
            f"💳 *Budget — {data['month']}*\n\n"
            f"`{bar}` {pct:.0f}% used\n\n"
            f"Budget:    `${MONTHLY_BUDGET:.2f}`\n"
            f"Spent:     `${data['expenses']:.2f}`\n"
            f"Remaining: `${remaining:.2f}` {status}\n\n"
            f"📆 {days_left} days left in month\n"
            f"💡 Daily allowance: `${daily_left:.2f}/day`"
        )
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Budget error: {e}")
        await msg.edit_text("❌ Error checking budget.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    msg = await update.message.reply_text("⏳")

    result = await parse_with_gemini(text)
    normalized = normalize_transaction(result)

    if not normalized or "error" in (result or {}):
        await msg.edit_text(
            "❓ Didn't catch that as a transaction.\nTry: *spent $12 on lunch*",
            parse_mode="Markdown",
        )
        return

    try:
        log_transaction(
            normalized["amount"],
            normalized["category"],
            normalized["description"],
            normalized["type"],
        )
        emoji = "💰" if normalized["type"] == "income" else "💸"
        await msg.edit_text(
            f"{emoji} *Logged!*\n"
            f"{normalized['description'].title()} · `${normalized['amount']:.2f}`\n"
            f"_{normalized['category']}_",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Log error: {e}")
        await msg.edit_text("❌ Error saving. Try again.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_start))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("budget",  cmd_budget))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot running — polling for updates")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
