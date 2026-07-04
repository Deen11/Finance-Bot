import os
import json
import logging
import re
import calendar
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import io
import base64
from PIL import Image
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, BotCommand
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
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY    = os.environ["GEMINI_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
SHEET_ID          = os.environ["SHEET_ID"]
MONTHLY_BUDGET    = float(os.environ.get("MONTHLY_BUDGET", "1700"))
BUDGET_ALERT_PCT  = 80  # warn when this % of budget is spent

# ── Gemini ────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.5-flash")

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


# ── Parsing helpers ───────────────────────────────────────────────────────────
def extract_json_payload(raw: str) -> Optional[str]:
    raw = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1)
    brace_match = re.search(r"\{[^{}]*\}", raw)
    if brace_match:
        return brace_match.group(0)
    return None


def simple_parse_transaction(text: str) -> Optional[dict]:
    text_lower = text.lower().strip()
    amount_match = re.search(r"\$?([0-9]+(?:\.[0-9]+)?)", text_lower)
    if not amount_match:
        return None

    amount = float(amount_match.group(1))
    trans_type = "expense"
    if re.search(r"\b(salary|income|earned|received|paycheck|ang bao|angbao)\b", text_lower):
        trans_type = "income"

    category = "Other"
    if re.search(r"\b(food|lunch|dinner|breakfast|supper|hawker|kopitiam|boba|kopi|snack|snacks|delivery|mcdonald|mcdonalds|starbucks)\b", text_lower):
        category = "Food"
    elif re.search(r"\b(bus|mrt|grab|taxi|simplygo|ez.?link|transport|uber)\b", text_lower):
        category = "Transport"
    elif re.search(r"\b(clothes|shoes|electronics|games|beauty|gadgets|gift|shopping|earphones|headphones|phone|watch|ipad|laptop)\b", text_lower):
        category = "Shopping"
    elif re.search(r"\b(mom|mum|mother|parents|family|dad|father)\b", text_lower):
        category = "Family"
    elif re.search(r"\b(savings|maribank|mari bank|transfer|deposit)\b", text_lower):
        category = "Savings"
    elif re.search(r"\b(netflix|spotify|apple music|youtube|icloud|subscription|sub|prime)\b", text_lower):
        category = "Subscriptions"
    elif trans_type == "income":
        category = "Income"

    desc = re.sub(r"\$?[0-9]+(?:\.[0-9]+)?", "", text_lower)
    desc = re.sub(r"\b(spent|paid|bought|purchased|received|got|for|on|to)\b", "", desc)
    desc = re.sub(r"[^a-z0-9 ]", "", desc).strip()
    words = [w for w in desc.split() if w and w not in {"a", "the", "to", "on", "for", "i"}]
    desc = " ".join(words[:5]) or category

    return {"amount": amount, "category": category, "description": desc.title(), "type": trans_type}


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


def normalize_transaction(result: dict) -> Optional[dict]:
    if not result:
        return None
    try:
        amount = result.get("amount")
        if isinstance(amount, str):
            amount = amount.replace("$", "").replace(",", "").strip()
        amount = float(amount)
    except (TypeError, ValueError):
        return None

    category  = str(result.get("category", "Other")).strip().title()
    description = str(result.get("description", "")).strip() or category
    trans_type  = str(result.get("type", "expense")).strip().lower()
    if trans_type not in {"expense", "income"}:
        trans_type = "expense"

    return {"amount": amount, "category": category, "description": description, "type": trans_type}


# ── Google Sheets helpers ─────────────────────────────────────────────────────
def get_spreadsheet():
    creds_data = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_data,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def get_worksheet():
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet("Transactions")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet("Transactions", rows=2000, cols=7)
        ws.append_row(["Date", "Time", "Month", "Type", "Category", "Description", "Amount"])
    return ws


def get_recurring_sheet():
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet("Recurring")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet("Recurring", rows=100, cols=5)
        ws.append_row(["Description", "Category", "Amount", "Day", "Type"])
    return ws


def get_config_sheet():
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet("Config")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet("Config", rows=50, cols=2)
        ws.append_row(["Key", "Value"])
    return ws


def save_chat_id(chat_id: int):
    ws = get_config_sheet()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if r.get("Key") == "chat_id":
            ws.update_cell(i, 2, str(chat_id))
            return
    ws.append_row(["chat_id", str(chat_id)])


def load_chat_id() -> Optional[int]:
    try:
        for r in get_config_sheet().get_all_records():
            if r.get("Key") == "chat_id":
                return int(r["Value"])
    except Exception:
        pass
    return None


# ── Transactions ──────────────────────────────────────────────────────────────
def log_transaction(amount: float, category: str, description: str, trans_type: str):
    ws = get_worksheet()
    now = datetime.now(SGT)  # Always log in Singapore Time
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
        month_str = datetime.now(SGT).strftime("%B %Y")
    ws = get_worksheet()
    records = ws.get_all_records()
    income = expenses = 0.0
    categories: dict = {}
    for r in records:
        if r.get("Month") != month_str:
            continue
        try:
            amt = float(r.get("Amount", 0))
        except (ValueError, TypeError):
            continue
        trans_type = r.get("Type", "").lower()
        cat = r.get("Category", "Other")
        if trans_type == "income":
            income += amt
        else:
            expenses += abs(amt)
            categories[cat] = categories.get(cat, 0) + abs(amt)
    return {
        "month": month_str,
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "categories": dict(sorted(categories.items(), key=lambda x: x[1], reverse=True)),
    }


def fetch_today() -> list:
    ws = get_worksheet()
    today_str = date.today().strftime("%Y-%m-%d")
    return [r for r in ws.get_all_records() if r.get("Date") == today_str]


def fetch_history(n: int = 10) -> list:
    """Returns last n transactions with their actual sheet row numbers."""
    ws = get_worksheet()
    all_records = ws.get_all_records()
    # row number in sheet = index + 2 (1-indexed + header row)
    indexed = [(i + 2, r) for i, r in enumerate(all_records)]
    return indexed[-n:]


def delete_row_by_sheet_index(row_num: int):
    ws = get_worksheet()
    ws.delete_rows(row_num)


def undo_last_transaction() -> Optional[dict]:
    ws = get_worksheet()
    records = ws.get_all_records()
    if not records:
        return None
    last = records[-1]
    ws.delete_rows(len(records) + 1)  # +1 for header
    return last


# ── Budget alert ──────────────────────────────────────────────────────────────
async def maybe_send_budget_alert(update: Update, expenses: float):
    pct = (expenses / MONTHLY_BUDGET * 100) if MONTHLY_BUDGET > 0 else 0
    if pct >= 100:
        await update.message.reply_text(
            f"🚨 *Over Budget!*\nYou've exceeded your budget by `${expenses - MONTHLY_BUDGET:.2f}`!",
            parse_mode="Markdown",
        )
    elif pct >= BUDGET_ALERT_PCT:
        await update.message.reply_text(
            f"⚠️ *Budget Alert!* You've used *{pct:.0f}%* of your monthly budget.\n"
            f"Only `${MONTHLY_BUDGET - expenses:.2f}` left this month.",
            parse_mode="Markdown",
        )


# ── Scheduled jobs ────────────────────────────────────────────────────────────
async def weekly_summary_job(context):
    """Sends weekly summary every Sunday 9am SGT (1am UTC)."""
    chat_id = load_chat_id()
    if not chat_id:
        logger.warning("weekly_summary_job: no chat_id saved yet")
        return
    try:
        data = fetch_monthly_summary()
        cat_lines = ""
        for cat, amt in data["categories"].items():
            filled = min(int(amt / 80), 10)
            bar = "█" * filled + "░" * (10 - filled)
            cat_lines += f"  {bar} {cat}: *${amt:.2f}*\n"
        net_emoji = "✅" if data["net"] >= 0 else "⚠️"
        text = (
            f"📊 *Weekly Update — {data['month']}*\n\n"
            f"💰 Income:   `${data['income']:.2f}`\n"
            f"💸 Expenses: `${data['expenses']:.2f}`\n"
            f"{net_emoji} Net:      `${data['net']:.2f}`\n\n"
            f"*Breakdown:*\n{cat_lines}"
        )
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"weekly_summary_job error: {e}")


async def daily_recurring_job(context):
    """Auto-logs recurring transactions on their scheduled day (midnight SGT = 4pm UTC)."""
    chat_id = load_chat_id()
    today_day = date.today().day
    try:
        recurring = get_recurring_sheet().get_all_records()
    except Exception as e:
        logger.error(f"daily_recurring_job: could not fetch recurring sheet: {e}")
        return

    logged = []
    for r in recurring:
        try:
            if int(r.get("Day", -1)) != today_day:
                continue
            amount = float(r.get("Amount", 0))
            category = str(r.get("Category", "Other"))
            description = str(r.get("Description", ""))
            trans_type = str(r.get("Type", "expense")).lower()
            log_transaction(amount, category, description, trans_type)
            logged.append(f"• {description} `${amount:.2f}` _{category}_")
        except Exception as e:
            logger.error(f"daily_recurring_job: row error: {e}")

    if logged and chat_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔄 *Auto-logged recurring transactions:*\n\n" + "\n".join(logged),
            parse_mode="Markdown",
        )


# ── Telegram command handlers ─────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Save chat_id so scheduled jobs know where to send messages
    try:
        save_chat_id(update.effective_chat.id)
    except Exception as e:
        logger.error(f"save_chat_id error: {e}")

    await update.message.reply_text(
        "👋 *Finance Tracker*\n\n"
        "Just text me what you spent:\n"
        "• `spent $12 on chicken rice`\n"
        "• `paid mom $800`\n"
        "• `salary $3000 received`\n"
        "• `Apple Music $5.98`\n\n"
        "*Commands:*\n"
        "/summary — This month's full breakdown\n"
        "/today — What you've logged today\n"
        "/budget — How much you have left\n"
        "/history — Last 10 transactions\n"
        "/undo — Delete the last entry\n"
        "/delete `<number>` — Delete entry from /history\n"
        "/addrecurring `<desc> <amount> <day>` — Add recurring\n"
        "/listrecurring — View recurring transactions\n"
        "/removerecurring `<number>` — Remove recurring\n"
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


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching...")
    try:
        indexed = fetch_history(10)
        if not indexed:
            await msg.edit_text("📋 No transactions yet!")
            return
        lines = ""
        for display_num, (_, r) in enumerate(indexed, start=1):
            amt = abs(float(r.get("Amount", 0)))
            t = r.get("Type", "").lower()
            emoji = "💸" if t == "expense" else "💰"
            lines += f"`{display_num}.` {emoji} `${amt:.2f}` {r.get('Category')} — {r.get('Description')} _{r.get('Date', '')}_\n"
        await msg.edit_text(
            f"📋 *Last {len(indexed)} Transactions*\n\n{lines}\n"
            f"Use /delete `<number>` to remove an entry.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"History error: {e}")
        await msg.edit_text("❌ Error fetching history.")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Undoing...")
    try:
        deleted = undo_last_transaction()
        if not deleted:
            await msg.edit_text("❌ Nothing to undo.")
            return
        amt = abs(float(deleted.get("Amount", 0)))
        await msg.edit_text(
            f"🗑️ *Deleted last entry:*\n"
            f"{deleted.get('Description')} · `${amt:.2f}` _{deleted.get('Category')}_",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Undo error: {e}")
        await msg.edit_text("❌ Error undoing. Try again.")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /delete <number from /history>"""
    msg = await update.message.reply_text("⏳ Deleting...")
    try:
        if not context.args:
            await msg.edit_text("Usage: /delete `<number>` — get the number from /history", parse_mode="Markdown")
            return
        n = int(context.args[0])
        indexed = fetch_history(10)
        if n < 1 or n > len(indexed):
            await msg.edit_text(f"❌ Invalid number. Pick between 1 and {len(indexed)}.")
            return
        row_num, record = indexed[n - 1]
        delete_row_by_sheet_index(row_num)
        amt = abs(float(record.get("Amount", 0)))
        await msg.edit_text(
            f"🗑️ *Deleted:*\n"
            f"{record.get('Description')} · `${amt:.2f}` _{record.get('Category')}_",
            parse_mode="Markdown",
        )
    except (ValueError, IndexError):
        await msg.edit_text("❌ Invalid number. Use /history first to see the list.")
    except Exception as e:
        logger.error(f"Delete error: {e}")
        await msg.edit_text("❌ Error deleting. Try again.")


async def cmd_add_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /addrecurring mom 800 12  (description, amount, day of month)"""
    msg = await update.message.reply_text("⏳ Adding...")
    try:
        if not context.args or len(context.args) < 3:
            await msg.edit_text(
                "Usage: /addrecurring `<description> <amount> <day>`\n"
                "Example: `/addrecurring mom 800 12`\n"
                "Example: `/addrecurring Spotify 6.48 1`",
                parse_mode="Markdown",
            )
            return

        # Last arg is day, second-to-last is amount, rest is description
        day = int(context.args[-1])
        amount = float(context.args[-2])
        description = " ".join(context.args[:-2]).title()

        if not 1 <= day <= 31:
            await msg.edit_text("❌ Day must be between 1 and 31.")
            return

        # Guess category from description
        desc_lower = description.lower()
        category = "Other"
        if re.search(r"\b(mom|mum|mother|dad|father|family|parents)\b", desc_lower):
            category = "Family"
        elif re.search(r"\b(spotify|netflix|apple|icloud|youtube|subscription)\b", desc_lower):
            category = "Subscriptions"
        elif re.search(r"\b(savings|maribank)\b", desc_lower):
            category = "Savings"
        elif re.search(r"\b(grab|bus|mrt|taxi|transport)\b", desc_lower):
            category = "Transport"

        ws = get_recurring_sheet()
        ws.append_row([description, category, amount, day, "expense"])

        await msg.edit_text(
            f"✅ *Recurring added!*\n"
            f"{description} · `${amount:.2f}` on the *{day}{'st' if day==1 else 'nd' if day==2 else 'rd' if day==3 else 'th'}* of each month\n"
            f"_{category}_",
            parse_mode="Markdown",
        )
    except (ValueError, IndexError):
        await msg.edit_text(
            "❌ Wrong format. Try: `/addrecurring mom 800 12`",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"AddRecurring error: {e}")
        await msg.edit_text("❌ Error adding. Try again.")


async def cmd_list_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching...")
    try:
        records = get_recurring_sheet().get_all_records()
        if not records:
            await msg.edit_text("📋 No recurring transactions set up yet.\nUse /addrecurring to add one.")
            return
        lines = ""
        for i, r in enumerate(records, start=1):
            day = r.get("Day", "?")
            suffix = "st" if day == 1 else "nd" if day == 2 else "rd" if day == 3 else "th"
            lines += f"`{i}.` {r.get('Description')} · `${float(r.get('Amount', 0)):.2f}` on *{day}{suffix}* _{r.get('Category')}_\n"
        await msg.edit_text(
            f"🔄 *Recurring Transactions*\n\n{lines}\n"
            f"Use /removerecurring `<number>` to delete one.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"ListRecurring error: {e}")
        await msg.edit_text("❌ Error fetching recurring list.")


async def cmd_remove_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Removing...")
    try:
        if not context.args:
            await msg.edit_text("Usage: /removerecurring `<number>` — get number from /listrecurring", parse_mode="Markdown")
            return
        n = int(context.args[0])
        ws = get_recurring_sheet()
        records = ws.get_all_records()
        if n < 1 or n > len(records):
            await msg.edit_text(f"❌ Invalid number. Pick between 1 and {len(records)}.")
            return
        deleted = records[n - 1]
        ws.delete_rows(n + 1)  # +1 for header row
        await msg.edit_text(
            f"🗑️ *Removed recurring:*\n{deleted.get('Description')} · `${float(deleted.get('Amount', 0)):.2f}`",
            parse_mode="Markdown",
        )
    except (ValueError, IndexError):
        await msg.edit_text("❌ Invalid number. Use /listrecurring first.")
    except Exception as e:
        logger.error(f"RemoveRecurring error: {e}")
        await msg.edit_text("❌ Error removing. Try again.")


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
        # Check budget alert after every expense
        if normalized["type"] == "expense":
            summary = fetch_monthly_summary()
            await maybe_send_budget_alert(update, summary["expenses"])
    except Exception as e:
        logger.error(f"Log error: {e}")
        await msg.edit_text("❌ Error saving. Try again.")



# ── OCR / Image handling ──────────────────────────────────────────────────────
IMAGE_PROMPT = """You are a personal finance parser for a Singaporean user.
Analyse this image carefully. It may be ANY of these formats:
- Banking app transaction detail screen (DBS, POSB, OCBC, UOB, PayLah, PayNow)
- Credit/debit card transaction record
- Hawker or restaurant receipt
- Supermarket or retail receipt
- Grab, Gojek or taxi receipt
- Food delivery receipt (FoodPanda, GrabFood)
- Any payment confirmation screen

IMPORTANT: Look for any dollar amount (SGD, $, or just a number with decimals).
In banking screenshots, the merchant name is often in the Description field.
Card transactions often show merchant names like "UMC-S BurgerKing", "NTUC", "FAIRPRICE" etc.

Extract the transaction and return ONLY valid JSON — no markdown, no backticks:
{"amount": <positive number>, "category": "<category>", "description": "<3-5 word description>", "type": "<expense or income>"}

Categories:
- Food        → restaurants, fast food (McDonald's, Burger King, KFC), cafes, hawker, food delivery, supermarkets with food
- Transport   → Grab, Gojek, taxi, EZ-Link, SimplyGo, bus, MRT, petrol
- Shopping    → retail, clothes, electronics, NTUC, FairPrice, Guardian, Watsons
- Family      → transfers to family members
- Savings     → transfers to savings account, MariBank
- Subscriptions → Netflix, Spotify, Apple, iCloud, digital subscriptions
- Income      → salary credited, PayNow received, bank credits
- Other       → anything else

Always try your best to extract an amount. Only return the error JSON if there is truly no financial information at all.
{"error": "no transaction found"}"""


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Reading image...")
    try:
        # Get highest resolution photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()

        # Send to Gemini Vision
        img = Image.open(io.BytesIO(bytes(photo_bytes)))
        response = gemini.generate_content([img, IMAGE_PROMPT])
        raw = response.text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        payload = extract_json_payload(raw) or raw
        result = json.loads(payload.strip())
        normalized = normalize_transaction(result)

        if not normalized or "error" in result:
            await msg.edit_text(
                "❓ Couldn't find a transaction in that image.\n"
                "Try a clearer photo of the receipt total, or type it manually.",
                parse_mode="Markdown",
            )
            return

        log_transaction(
            normalized["amount"],
            normalized["category"],
            normalized["description"],
            normalized["type"],
        )

        emoji = "💰" if normalized["type"] == "income" else "💸"
        await msg.edit_text(
            f"{emoji} *Logged from image!*\n"
            f"{normalized['description'].title()} · `${normalized['amount']:.2f}`\n"
            f"_{normalized['category']}_",
            parse_mode="Markdown",
        )

        # Budget alert
        if normalized["type"] == "expense":
            summary = fetch_monthly_summary()
            await maybe_send_budget_alert(update, summary["expenses"])

    except Exception as e:
        logger.error(f"Photo handler error: {e}")
        await msg.edit_text("❌ Error reading image. Try a clearer photo or type the amount manually.")


# ── Entry point ───────────────────────────────────────────────────────────────

async def post_init(application):
    """Register bot commands so / shows the preview in Telegram."""
    await application.bot.set_my_commands([
        BotCommand("summary",         "📊 This month's full breakdown"),
        BotCommand("today",           "📅 What you've logged today"),
        BotCommand("budget",          "💳 How much you have left this month"),
        BotCommand("history",         "📋 Last 10 transactions"),
        BotCommand("undo",            "↩️ Delete the last entry"),
        BotCommand("delete",          "🗑 Delete entry by number from /history"),
        BotCommand("addrecurring",    "🔄 Add a recurring transaction"),
        BotCommand("listrecurring",   "📋 View all recurring transactions"),
        BotCommand("removerecurring", "🗑 Remove a recurring transaction"),
        BotCommand("help",            "👋 Show all commands"),
    ])
    logger.info("Bot commands registered with Telegram")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start",            cmd_start))
    app.add_handler(CommandHandler("help",             cmd_start))
    app.add_handler(CommandHandler("summary",          cmd_summary))
    app.add_handler(CommandHandler("today",            cmd_today))
    app.add_handler(CommandHandler("budget",           cmd_budget))
    app.add_handler(CommandHandler("history",          cmd_history))
    app.add_handler(CommandHandler("undo",             cmd_undo))
    app.add_handler(CommandHandler("delete",           cmd_delete))
    app.add_handler(CommandHandler("addrecurring",     cmd_add_recurring))
    app.add_handler(CommandHandler("listrecurring",    cmd_list_recurring))
    app.add_handler(CommandHandler("removerecurring",  cmd_remove_recurring))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Scheduled jobs
    jq = app.job_queue
    if jq:
        # Weekly summary: Sunday 9am SGT = Sunday 1am UTC
        jq.run_daily(weekly_summary_job, time=dtime(1, 0, 0), days=(6,))
        # Daily recurring check: midnight SGT = 4pm UTC
        jq.run_daily(daily_recurring_job, time=dtime(16, 0, 0))
        logger.info("Scheduled jobs registered (weekly summary + daily recurring)")
    else:
        logger.warning("JobQueue not available — install python-telegram-bot[job-queue]")

    logger.info("Bot running — polling for updates")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
