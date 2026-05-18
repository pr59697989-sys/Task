"""
╔══════════════════════════════════════════════════════════════╗
║          NEXAUTH — Futuristic Telegram TOTP Bot              ║
║  No commands. Pure inline buttons. Animated. Session-aware.  ║
╚══════════════════════════════════════════════════════════════╝

Flow:
  User pastes a QR-URI  (otpauth://totp/...)  OR  a raw base32 secret
  Bot reads it → stores encrypted → returns live TOTP codes on demand.
  Auto-session-out after SESSION_TIMEOUT_SECONDS of inactivity.
"""

import os
import io
import re
import json
import time
import base64
import hashlib
import logging
import asyncio
from datetime import datetime, timezone
from collections import defaultdict
from functools import wraps
from typing import Optional

import pyotp
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest

# ──────────────────────────────────────────────
# Config & Logging
# ──────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("NexAuth")

BOT_TOKEN               = os.environ["BOT_TOKEN"]
MONGO_URI               = os.environ["MONGO_URI"]
ENCRYPTION_KEY          = os.environ["ENCRYPTION_KEY"]
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT", "120"))   # 2 min default
RATE_WINDOW             = 60
RATE_MAX                = 25

# ConversationHandler states
AWAIT_SECRET = 1
AWAIT_RESTORE_DATA = 2
AWAIT_DELETE_CONFIRM = 3

# ──────────────────────────────────────────────
# Crypto — AES-256-GCM
# ──────────────────────────────────────────────
def _derive_key(raw: str) -> bytes:
    try:
        k = bytes.fromhex(raw)
        if len(k) == 32:
            return k
    except ValueError:
        pass
    try:
        k = base64.b64decode(raw)
        if len(k) == 32:
            return k
    except Exception:
        pass
    return hashlib.sha256(raw.encode()).digest()

_KEY = _derive_key(ENCRYPTION_KEY)

def encrypt(plaintext: str) -> str:
    aesgcm = AESGCM(_KEY)
    nonce  = os.urandom(12)
    ct     = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()

def decrypt(token: str) -> str:
    raw   = base64.urlsafe_b64decode(token.encode())
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_KEY).decrypt(nonce, ct, None).decode()

# ──────────────────────────────────────────────
# MongoDB
# ──────────────────────────────────────────────
_mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=6_000)
_db    = _mongo["nexauth"]
users_col    = _db["users"]
accounts_col = _db["otp_accounts"]
sessions_col = _db["sessions"]

users_col.create_index("user_id", unique=True)
accounts_col.create_index([("user_id", ASCENDING), ("service_name", ASCENDING)], unique=True)
sessions_col.create_index("user_id", unique=True)
sessions_col.create_index("last_active", expireAfterSeconds=SESSION_TIMEOUT_SECONDS + 30)
log.info("✅ MongoDB ready.")

# ──────────────────────────────────────────────
# Session management
# ──────────────────────────────────────────────
def session_touch(uid: int) -> None:
    sessions_col.update_one(
        {"user_id": uid},
        {"$set": {"last_active": datetime.now(timezone.utc)}},
        upsert=True,
    )

def session_active(uid: int) -> bool:
    doc = sessions_col.find_one({"user_id": uid})
    if not doc:
        return False
    elapsed = (datetime.now(timezone.utc) - doc["last_active"]).total_seconds()
    return elapsed < SESSION_TIMEOUT_SECONDS

def session_end(uid: int) -> None:
    sessions_col.delete_one({"user_id": uid})

# ──────────────────────────────────────────────
# Rate limiter
# ──────────────────────────────────────────────
_rate: dict[int, list[float]] = defaultdict(list)

def rate_ok(uid: int) -> bool:
    now  = time.monotonic()
    hits = [t for t in _rate[uid] if now - t < RATE_WINDOW]
    if len(hits) >= RATE_MAX:
        return False
    hits.append(now)
    _rate[uid] = hits
    return True

# ──────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────
SERVICE_RE = re.compile(r"^[A-Za-z0-9._\- ]{1,64}$")
TOTP_URI_RE = re.compile(r"otpauth://totp/([^?]+)\?.*secret=([A-Z2-7]+)", re.IGNORECASE)

def upsert_user(uid: int, username: Optional[str], first_name: str) -> None:
    users_col.update_one(
        {"user_id": uid},
        {"$setOnInsert": {
            "user_id":    uid,
            "username":   username,
            "first_name": first_name,
            "joined_at":  datetime.now(timezone.utc),
        }},
        upsert=True,
    )

def get_account(uid: int, service: str):
    return accounts_col.find_one({"user_id": uid, "service_name": service})

def list_accounts(uid: int) -> list[dict]:
    return list(accounts_col.find(
        {"user_id": uid},
        {"service_name": 1, "issuer": 1, "created_at": 1, "_id": 0},
    ))

def delete_account(uid: int, service: str) -> bool:
    return accounts_col.delete_one({"user_id": uid, "service_name": service}).deleted_count > 0

def parse_secret_input(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (service_name, secret, issuer) from:
      - otpauth://totp/... URI
      - Raw base32 secret
    """
    text = text.strip()
    m = TOTP_URI_RE.search(text)
    if m:
        raw_label = m.group(1)
        secret    = m.group(2).upper().replace(" ", "")
        # label may be  "Issuer:account"  or just "account"
        if ":" in raw_label:
            issuer, account = raw_label.split(":", 1)
        else:
            issuer, account = raw_label, raw_label
        service = account.strip()[:64] or issuer.strip()[:64]
        return service, secret, issuer.strip()

    # Raw base32
    candidate = text.upper().replace(" ", "").replace("-", "")
    if re.fullmatch(r"[A-Z2-7]{16,64}=*", candidate):
        return None, candidate, None   # service unknown — will ask

    return None, None, None

# ──────────────────────────────────────────────
# UI helpers — Inline keyboards & messages
# ──────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕  Add Account",  callback_data="add"),
            InlineKeyboardButton("🔑  Get OTP",      callback_data="otp_list"),
        ],
        [
            InlineKeyboardButton("📋  My Accounts",  callback_data="list"),
            InlineKeyboardButton("🗑  Delete",        callback_data="delete_list"),
        ],
        [
            InlineKeyboardButton("💾  Backup",        callback_data="backup"),
            InlineKeyboardButton("📥  Restore",       callback_data="restore"),
        ],
        [
            InlineKeyboardButton("🔒  Lock Session",  callback_data="lock"),
        ],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️  Back", callback_data="home"),
    ]])

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌  Cancel", callback_data="home"),
    ]])

def kb_otp_refresh(service: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄  Refresh Code", callback_data=f"otp_get:{service}")],
        [InlineKeyboardButton("⬅️  Back",          callback_data="otp_list")],
    ])

def kb_service_list(accounts: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🔐  {a['service_name']}", callback_data=f"{prefix}:{a['service_name']}")]
        for a in accounts
    ]
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def kb_confirm_delete(service: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅  Yes, Delete", callback_data=f"delete_confirm:{service}"),
            InlineKeyboardButton("❌  Cancel",       callback_data="home"),
        ]
    ])

# ──────────────────────────────────────────────
# Loading animation helper
# ──────────────────────────────────────────────
LOADING_FRAMES = ["⬛⬜⬜⬜", "⬜⬛⬜⬜", "⬜⬜⬛⬜", "⬜⬜⬜⬛", "⬜⬜⬛⬜", "⬜⬛⬜⬜"]

async def animate_then(
    msg,
    label: str,
    action_coro,
    frames=LOADING_FRAMES,
    fps: float = 0.18,
):
    """Edit a message through animation frames, then execute action_coro(msg)."""
    for frame in frames:
        try:
            await msg.edit_text(f"`{frame}` {label}…", parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            pass
        await asyncio.sleep(fps)
    return await action_coro(msg)

# ──────────────────────────────────────────────
# Session guard decorator
# ──────────────────────────────────────────────
def require_session(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = (update.effective_user or update.callback_query.from_user).id
        if not rate_ok(uid):
            txt = "⚠️ Slow down! Rate limit reached."
            if update.callback_query:
                await update.callback_query.answer(txt, show_alert=True)
            else:
                await update.message.reply_text(txt)
            return
        if not session_active(uid):
            await _prompt_unlock(update, ctx)
            return
        session_touch(uid)
        return await func(update, ctx)
    return wrapper

async def _prompt_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔐 *Session Expired*\n\n"
        "Your session timed out after 2 minutes of inactivity.\n"
        "Press the button below to unlock."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔓  Unlock", callback_data="unlock")
    ]])
    if update.callback_query:
        await update.callback_query.answer("Session expired — please unlock.", show_alert=True)
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await update.effective_chat.send_message(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
# HOME / START
# ──────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    if not rate_ok(uid):
        return

    upsert_user(uid, user.username, user.first_name)
    session_touch(uid)

    count = accounts_col.count_documents({"user_id": uid})
    text  = (
        f"⚡ *NexAuth — 2FA Vault*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {user.first_name}  |  🔐 {count} account{'s' if count != 1 else ''} saved\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Share a *QR code image*, an `otpauth://` URI, or a *raw base32 secret* "
        f"and I will generate live TOTP codes for you — no QR codes sent from me.\n\n"
        f"Session auto-locks after *{SESSION_TIMEOUT_SECONDS // 60} min* of inactivity."
    )
    await update.message.reply_text(text, reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
# CALLBACK ROUTER
# ──────────────────────────────────────────────
async def button_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    uid  = q.from_user.id
    data = q.data

    await q.answer()

    # Unlock doesn't need session
    if data == "unlock":
        session_touch(uid)
        await _show_home(q, ctx, uid)
        return

    if not rate_ok(uid):
        await q.answer("⚠️ Rate limit. Wait a moment.", show_alert=True)
        return

    if not session_active(uid):
        await _prompt_unlock(update, ctx)
        return

    session_touch(uid)

    if data == "home":
        await _show_home(q, ctx, uid)
    elif data == "add":
        await _btn_add(q, ctx)
    elif data == "otp_list":
        await _btn_otp_list(q, ctx, uid)
    elif data.startswith("otp_get:"):
        await _btn_otp_get(q, ctx, uid, data.split(":", 1)[1])
    elif data == "list":
        await _btn_list(q, ctx, uid)
    elif data == "delete_list":
        await _btn_delete_list(q, ctx, uid)
    elif data.startswith("delete_ask:"):
        await _btn_delete_ask(q, ctx, uid, data.split(":", 1)[1])
    elif data.startswith("delete_confirm:"):
        await _btn_delete_confirm(q, ctx, uid, data.split(":", 1)[1])
    elif data == "backup":
        await _btn_backup(q, ctx, uid)
    elif data == "restore":
        await _btn_restore(q, ctx)
    elif data == "lock":
        await _btn_lock(q, ctx, uid)

async def _show_home(q, ctx, uid: int):
    count = accounts_col.count_documents({"user_id": uid})
    text  = (
        f"⚡ *NexAuth Dashboard*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔐 {count} account{'s' if count != 1 else ''} in vault\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"What would you like to do?"
    )
    try:
        await q.edit_message_text(text, reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        pass

# ──────────────────────────────────────────────
# ADD — conversation entry point
# ──────────────────────────────────────────────
async def _btn_add(q, ctx):
    text = (
        "➕ *Add New Account*\n\n"
        "Send me any of the following:\n\n"
        "① A *QR code image* from your app\n"
        "② An `otpauth://totp/...` URI string\n"
        "③ A *raw base32 secret key*\n\n"
        "📌 I will *never* generate or send QR codes — "
        "you provide the secret, I give you codes."
    )
    ctx.user_data["state"] = AWAIT_SECRET
    await q.edit_message_text(text, reply_markup=kb_cancel(), parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
# OTP LIST
# ──────────────────────────────────────────────
async def _btn_otp_list(q, ctx, uid: int):
    accounts = list_accounts(uid)
    if not accounts:
        await q.edit_message_text(
            "📭 *No accounts yet.*\n\nUse ➕ Add Account to get started.",
            reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN
        )
        return
    await q.edit_message_text(
        "🔑 *Get OTP Code*\n\nChoose a service:",
        reply_markup=kb_service_list(accounts, "otp_get"),
        parse_mode=ParseMode.MARKDOWN,
    )

async def _btn_otp_get(q, ctx, uid: int, service: str):
    doc = get_account(uid, service)
    if not doc:
        await q.answer("Account not found.", show_alert=True)
        return

    # Animate
    frames = ["◐", "◓", "◑", "◒"]
    for f in frames:
        try:
            await q.edit_message_text(
                f"`{f}` Generating code…", parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            pass
        await asyncio.sleep(0.15)

    try:
        secret = decrypt(doc["encrypted_secret"])
    except Exception:
        await q.edit_message_text("🔐 Decryption error.", reply_markup=kb_back())
        return

    totp      = pyotp.TOTP(secret)
    code      = totp.now()
    remaining = 30 - (int(time.time()) % 30)
    progress  = "█" * int(remaining / 3) + "░" * (10 - int(remaining / 3))
    issuer    = doc.get("issuer", service)

    text = (
        f"🔐 *{service}*\n"
        f"🏢 {issuer}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"```\n{code}\n```\n\n"
        f"⏱ `{progress}` {remaining}s remaining\n\n"
        f"_Tap Refresh for a new code_"
    )
    await q.edit_message_text(
        text,
        reply_markup=kb_otp_refresh(service),
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────
# LIST
# ──────────────────────────────────────────────
async def _btn_list(q, ctx, uid: int):
    accounts = list_accounts(uid)
    if not accounts:
        await q.edit_message_text(
            "📭 *No accounts saved yet.*",
            reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = []
    for i, a in enumerate(sorted(accounts, key=lambda x: x["service_name"]), 1):
        age = (datetime.now(timezone.utc) - a["created_at"]).days
        lines.append(f"`{i:02d}.` *{a['service_name']}*  _{age}d ago_")

    text = "📋 *Your Vault*\n━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines)
    await q.edit_message_text(text, reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
# DELETE
# ──────────────────────────────────────────────
async def _btn_delete_list(q, ctx, uid: int):
    accounts = list_accounts(uid)
    if not accounts:
        await q.edit_message_text(
            "📭 Nothing to delete.",
            reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN
        )
        return
    await q.edit_message_text(
        "🗑 *Delete Account*\n\nChoose which service to remove:",
        reply_markup=kb_service_list(accounts, "delete_ask"),
        parse_mode=ParseMode.MARKDOWN,
    )

async def _btn_delete_ask(q, ctx, uid: int, service: str):
    await q.edit_message_text(
        f"⚠️ *Are you sure?*\n\nYou are about to permanently delete:\n\n*{service}*\n\n"
        "_This action cannot be undone._",
        reply_markup=kb_confirm_delete(service),
        parse_mode=ParseMode.MARKDOWN,
    )

async def _btn_delete_confirm(q, ctx, uid: int, service: str):
    frames = ["💥", "🔥", "🗑", "✅"]
    for f in frames:
        try:
            await q.edit_message_text(f"`{f}` Deleting…", parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            pass
        await asyncio.sleep(0.2)

    ok = delete_account(uid, service)
    if ok:
        await q.edit_message_text(
            f"✅ *{service}* has been permanently removed from your vault.",
            reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN
        )
        log.info("Deleted: uid=%s service=%s", uid, service)
    else:
        await q.edit_message_text(
            "❌ Account not found.", reply_markup=kb_back(), parse_mode=ParseMode.MARKDOWN
        )

# ──────────────────────────────────────────────
# BACKUP
# ──────────────────────────────────────────────
async def _btn_backup(q, ctx, uid: int):
    docs = list(accounts_col.find(
        {"user_id": uid},
        {"service_name": 1, "encrypted_secret": 1, "issuer": 1, "created_at": 1, "_id": 0},
    ))
    if not docs:
        await q.edit_message_text("📭 Nothing to back up.", reply_markup=kb_back())
        return

    frames = ["📦", "🔒", "🔐", "✅"]
    for f in frames:
        try:
            await q.edit_message_text(f"`{f}` Encrypting backup…", parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            pass
        await asyncio.sleep(0.2)

    payload = json.dumps([
        {
            "service_name":     d["service_name"],
            "encrypted_secret": d["encrypted_secret"],
            "issuer":           d.get("issuer", ""),
            "created_at":       d["created_at"].isoformat(),
        }
        for d in docs
    ])
    token = encrypt(payload)

    await q.edit_message_text(
        f"💾 *Encrypted Backup*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔐 {len(docs)} account{'s' if len(docs) != 1 else ''} included\n\n"
        f"Copy this token and use *Restore* on any device:\n\n"
        f"`{token}`\n\n"
        f"⚠️ _Keep this private — it is AES-256-GCM encrypted._",
        reply_markup=kb_back(),
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────
# RESTORE
# ──────────────────────────────────────────────
async def _btn_restore(q, ctx):
    ctx.user_data["state"] = AWAIT_RESTORE_DATA
    await q.edit_message_text(
        "📥 *Restore Backup*\n\n"
        "Paste your encrypted backup token below.",
        reply_markup=kb_cancel(),
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────
# LOCK
# ──────────────────────────────────────────────
async def _btn_lock(q, ctx, uid: int):
    session_end(uid)
    await q.edit_message_text(
        "🔒 *Session Locked*\n\n"
        "Your vault has been locked. Press Unlock to continue.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓  Unlock", callback_data="unlock")
        ]]),
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────
# MESSAGE HANDLER — secret input & restore
# ──────────────────────────────────────────────
async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = ctx.user_data.get("state")

    if not rate_ok(uid):
        await update.message.reply_text("⚠️ Rate limit. Please slow down.")
        return

    if not session_active(uid):
        await _prompt_unlock(update, ctx)
        return

    session_touch(uid)

    if state == AWAIT_SECRET:
        await _handle_secret_input(update, ctx, uid)
    elif state == AWAIT_RESTORE_DATA:
        await _handle_restore_input(update, ctx, uid)
    else:
        # Unsolicited text — check if it looks like a secret anyway
        text = (update.message.text or "").strip()
        if text.startswith("otpauth://") or re.fullmatch(r"[A-Z2-7]{16,64}=*", text.upper().replace(" ", "")):
            ctx.user_data["state"] = AWAIT_SECRET
            await _handle_secret_input(update, ctx, uid)
        else:
            await update.message.reply_text(
                "👆 Use the buttons below to navigate.",
                reply_markup=kb_main(),
            )

async def _handle_secret_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    ctx.user_data.pop("state", None)
    text = (update.message.text or "").strip()

    # Delete user message immediately (contains sensitive secret)
    try:
        await update.message.delete()
    except Exception:
        pass

    await update.effective_chat.send_chat_action(ChatAction.TYPING)

    service, secret, issuer = parse_secret_input(text)

    if not secret:
        await update.effective_chat.send_message(
            "❌ *Invalid input.*\n\n"
            "Please send:\n"
            "• An `otpauth://totp/...` URI\n"
            "• A raw base32 secret (e.g. `JBSWY3DPEHPK3PXP`)",
            reply_markup=kb_back(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Validate secret works
    try:
        pyotp.TOTP(secret).now()
    except Exception:
        await update.effective_chat.send_message(
            "❌ Invalid TOTP secret. Please check and try again.",
            reply_markup=kb_back(),
        )
        return

    if not service:
        # Prompt for service name
        ctx.user_data["pending_secret"] = secret
        ctx.user_data["state"]          = "AWAIT_SERVICE_NAME"
        await update.effective_chat.send_message(
            "🏷 *What service is this for?*\n\n"
            "Enter a name (e.g. `GitHub`, `Gmail`, `AWS`):",
            reply_markup=kb_cancel(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _save_account(update.effective_chat, ctx, uid, service, secret, issuer or service)

async def _save_account(chat, ctx, uid: int, service: str, secret: str, issuer: str):
    # Animate save
    msg = await chat.send_message("`◐` Encrypting…", parse_mode=ParseMode.MARKDOWN)
    for frame in ["◓", "◑", "◒", "🔐"]:
        try:
            await msg.edit_text(f"`{frame}` Encrypting…", parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            pass
        await asyncio.sleep(0.18)

    enc = encrypt(secret)
    try:
        accounts_col.insert_one({
            "user_id":          uid,
            "service_name":     service,
            "encrypted_secret": enc,
            "issuer":           issuer,
            "created_at":       datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        await msg.edit_text(
            f"⚠️ *{service}* already exists in your vault.",
            reply_markup=kb_back(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Show first OTP immediately
    totp      = pyotp.TOTP(secret)
    code      = totp.now()
    remaining = 30 - (int(time.time()) % 30)
    progress  = "█" * int(remaining / 3) + "░" * (10 - int(remaining / 3))

    await msg.edit_text(
        f"✅ *{service}* added to vault!\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔐 *First OTP Code:*\n\n"
        f"```\n{code}\n```\n\n"
        f"⏱ `{progress}` {remaining}s remaining",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄  Refresh", callback_data=f"otp_get:{service}")],
            [InlineKeyboardButton("🏠  Home",    callback_data="home")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )
    log.info("Account added: uid=%s service=%s", uid, service)

async def _handle_restore_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    ctx.user_data.pop("state", None)
    token = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.effective_chat.send_message("`🔓` Decrypting backup…", parse_mode=ParseMode.MARKDOWN)

    try:
        payload = decrypt(token)
        entries = json.loads(payload)
    except Exception:
        await msg.edit_text("❌ Invalid or corrupted backup token.", reply_markup=kb_back())
        return

    restored = skipped = 0
    for entry in entries:
        svc = entry.get("service_name", "")
        enc = entry.get("encrypted_secret", "")
        iss = entry.get("issuer", svc)
        if not svc or not enc:
            skipped += 1
            continue
        try:
            accounts_col.update_one(
                {"user_id": uid, "service_name": svc},
                {"$setOnInsert": {
                    "user_id":          uid,
                    "service_name":     svc,
                    "encrypted_secret": enc,
                    "issuer":           iss,
                    "created_at":       datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            restored += 1
        except Exception:
            skipped += 1

    await msg.edit_text(
        f"✅ *Restore Complete*\n\n"
        f"• Restored: {restored}\n"
        f"• Skipped:  {skipped}",
        reply_markup=kb_back(),
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────
# Service name input (when raw secret given)
# ──────────────────────────────────────────────
async def service_name_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = ctx.user_data.get("state")

    if state != "AWAIT_SERVICE_NAME":
        return

    if not session_active(uid):
        await _prompt_unlock(update, ctx)
        return

    session_touch(uid)
    service = (update.message.text or "").strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    if not SERVICE_RE.match(service):
        await update.effective_chat.send_message(
            "❌ Invalid name. Use letters, digits, spaces, `-`, `_`, `.` (max 64 chars).\n\nTry again:",
            reply_markup=kb_cancel(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    secret = ctx.user_data.pop("pending_secret", None)
    ctx.user_data.pop("state", None)

    if not secret:
        await update.effective_chat.send_message("⚠️ Session lost. Please try again.", reply_markup=kb_main())
        return

    await _save_account(update.effective_chat, ctx, uid, service, secret, service)

# ──────────────────────────────────────────────
# QR image handler
# ──────────────────────────────────────────────
async def photo_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not rate_ok(uid):
        return
    if not session_active(uid):
        await _prompt_unlock(update, ctx)
        return

    session_touch(uid)

    # We can't decode QR from image directly without zbar/pyzbar.
    # Prompt user to copy the URI text instead.
    await update.message.reply_text(
        "📷 *QR Image Received*\n\n"
        "To decode it, open your camera app or a QR scanner, "
        "scan the code, copy the `otpauth://totp/...` text, "
        "and paste it here.\n\n"
        "_Alternatively, tap ➕ Add Account and paste the secret key directly._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_back(),
    )

# ──────────────────────────────────────────────
# Session watchdog — background task
# ──────────────────────────────────────────────
async def session_watchdog(app: Application):
    """Periodically clean up expired sessions."""
    while True:
        await asyncio.sleep(30)
        cutoff = datetime.now(timezone.utc).timestamp() - SESSION_TIMEOUT_SECONDS
        try:
            sessions_col.delete_many({
                "last_active": {"$lt": datetime.fromtimestamp(cutoff, tz=timezone.utc)}
            })
        except Exception as e:
            log.warning("Watchdog error: %s", e)

# ──────────────────────────────────────────────
# Error handler
# ──────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception: %s", ctx.error)

# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Command (only /start — everything else is buttons)
    app.add_handler(CommandHandler("start", cmd_start))

    # Inline button router
    app.add_handler(CallbackQueryHandler(button_router))

    # Message handlers (order matters)
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"^AWAIT_SERVICE_NAME"),
            service_name_handler,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    app.add_error_handler(error_handler)

    # Background watchdog
    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(session_watchdog(app)),
        interval=60,
        first=10,
    )

    log.info("🚀 NexAuth Bot is live.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
