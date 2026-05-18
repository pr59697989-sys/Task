"""
NexAuth — Telegram TOTP Authenticator Bot  (v3 — Full Rewrite)
===============================================================
Features
────────
• Reply keyboard buttons (bottom of chat, always visible)
• Inline buttons only for confirm/back/action prompts
• Add TOTP via QR photo  (zxingcpp + Pillow)
• Add TOTP via otpauth:// URI  (paste)
• Add TOTP via raw base32 secret key  (paste)
• Search / find account by name
• Account detail view  (issuer · algorithm · digits · period · added)
• Rename account
• Delete account button  (fully restored, with confirmation)
• Grouped OTP list  (alphabetical, paginated 8 per page)
• OTP auto-refresh animation — code updates every second until period ends or session out
• PASSCODE LOCK — set a 4-8 digit PIN; vault stays locked until correct PIN
• Change / disable passcode
• Encrypted backup & restore
• AES-256-GCM encryption at rest (MongoDB)
• Auto-lock after inactivity with animated spinner
• Per-user rate limiting
• Session watchdog
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv
import zxingcpp
from PIL import Image
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────
# 1.  ENV & LOGGING
# ─────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("NexAuth")

BOT_TOKEN      = os.environ["BOT_TOKEN"]
MONGO_URI      = os.environ["MONGO_URI"]
ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"]
SESSION_TTL    = int(os.getenv("SESSION_TIMEOUT", "120"))   # seconds
PAGE_SIZE      = 8                                           # accounts per page

# ─────────────────────────────────────────────────────────────
# 2.  AES-256-GCM  &  PIN HASHING
# ─────────────────────────────────────────────────────────────
def _build_key(raw: str) -> bytes:
    for decoder in (bytes.fromhex, base64.b64decode):
        try:
            k = decoder(raw)
            if len(k) == 32:
                return k
        except Exception:
            pass
    return hashlib.sha256(raw.encode()).digest()


_AESKEY = _build_key(ENCRYPTION_KEY)


def aes_encrypt(plaintext: str) -> str:
    nonce = os.urandom(12)
    ct    = AESGCM(_AESKEY).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def aes_decrypt(token: str) -> str:
    raw       = base64.urlsafe_b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_AESKEY).decrypt(nonce, ct, None).decode()


def hash_pin(pin: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode(), _AESKEY[:16], 100_000
    ).hex()


def verify_pin(pin: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_pin(pin), stored_hash)


# ─────────────────────────────────────────────────────────────
# 3.  MONGODB
# ─────────────────────────────────────────────────────────────
_client      = MongoClient(MONGO_URI, serverSelectionTimeoutMS=6_000)
_db          = _client["nexauth"]
col_users    = _db["users"]
col_accounts = _db["otp_accounts"]
col_sessions = _db["sessions"]

col_users.create_index("uid", unique=True)
col_accounts.create_index([("uid", ASCENDING), ("svc", ASCENDING)], unique=True)
col_sessions.create_index("uid", unique=True)
log.info("MongoDB connected.")


def db_upsert_user(uid: int, name: str, username: Optional[str]) -> None:
    col_users.update_one(
        {"uid": uid},
        {"$setOnInsert": {
            "uid": uid, "name": name, "username": username,
            "joined": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def db_get(uid: int, svc: str) -> Optional[dict]:
    return col_accounts.find_one({"uid": uid, "svc": svc})


def db_list(uid: int) -> list:
    return list(col_accounts.find(
        {"uid": uid},
        {"svc": 1, "issuer": 1, "created": 1, "digits": 1, "period": 1, "_id": 0},
    ))


def db_add(uid: int, svc: str, issuer: str, enc_secret: str,
           digits: int = 6, period: int = 30, algorithm: str = "SHA1") -> bool:
    try:
        col_accounts.insert_one({
            "uid":       uid,
            "svc":       svc,
            "issuer":    issuer,
            "enc":       enc_secret,
            "digits":    digits,
            "period":    period,
            "algorithm": algorithm,
            "created":   datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


def db_delete(uid: int, svc: str) -> bool:
    return col_accounts.delete_one({"uid": uid, "svc": svc}).deleted_count > 0


def db_rename(uid: int, old: str, new: str) -> bool:
    try:
        col_accounts.update_one(
            {"uid": uid, "svc": old},
            {"$set": {"svc": new}},
        )
        return True
    except Exception:
        return False


def db_search(uid: int, query: str) -> list:
    regex = re.compile(re.escape(query), re.IGNORECASE)
    return list(col_accounts.find(
        {"uid": uid, "$or": [{"svc": regex}, {"issuer": regex}]},
        {"svc": 1, "issuer": 1, "created": 1, "_id": 0},
    ))


def db_get_pin(uid: int) -> Optional[str]:
    doc = col_users.find_one({"uid": uid}, {"pin_hash": 1})
    return (doc or {}).get("pin_hash")


def db_set_pin(uid: int, pin_hash: Optional[str]) -> None:
    col_users.update_one({"uid": uid}, {"$set": {"pin_hash": pin_hash}})


# ─────────────────────────────────────────────────────────────
# 4.  SESSION
# ─────────────────────────────────────────────────────────────
_session_cache: dict = {}


def session_touch(uid: int) -> None:
    _session_cache[uid] = time.monotonic()
    col_sessions.update_one(
        {"uid": uid},
        {"$set": {"last": datetime.now(timezone.utc)}},
        upsert=True,
    )


def session_alive(uid: int) -> bool:
    t = _session_cache.get(uid)
    if t and (time.monotonic() - t) < SESSION_TTL:
        return True
    doc = col_sessions.find_one({"uid": uid})
    if not doc:
        return False
    alive = (datetime.now(timezone.utc) - doc["last"]).total_seconds() < SESSION_TTL
    if alive:
        _session_cache[uid] = time.monotonic()
    else:
        _session_cache.pop(uid, None)
    return alive


def session_kill(uid: int) -> None:
    _session_cache.pop(uid, None)
    col_sessions.delete_one({"uid": uid})


# ─────────────────────────────────────────────────────────────
# 5.  RATE LIMITER
# ─────────────────────────────────────────────────────────────
_hits: dict = defaultdict(list)
RATE_WIN, RATE_MAX = 60, 30


def rate_ok(uid: int) -> bool:
    now  = time.monotonic()
    prev = [t for t in _hits[uid] if now - t < RATE_WIN]
    if len(prev) >= RATE_MAX:
        return False
    prev.append(now)
    _hits[uid] = prev
    return True


# ─────────────────────────────────────────────────────────────
# 6.  SECRET / URI PARSING
# ─────────────────────────────────────────────────────────────
_URI_RE = re.compile(r"otpauth://totp/([^?]+)\?([^#\s]+)", re.I)
_B32_RE = re.compile(r"^[A-Z2-7]{16,512}=*$")
_SVC_RE = re.compile(r"^[A-Za-z0-9 ._\-]{1,64}$")
_PIN_RE = re.compile(r"^\d{4,8}$")


def parse_otpauth(uri: str) -> Optional[dict]:
    m = _URI_RE.search(uri)
    if not m:
        return None
    label  = m.group(1).strip()
    params = dict(p.split("=", 1) for p in m.group(2).split("&") if "=" in p)
    secret = params.get("secret", "").upper().replace(" ", "").replace("-", "")
    if not _B32_RE.match(secret):
        return None
    if ":" in label:
        issuer, account = label.split(":", 1)
    else:
        issuer  = params.get("issuer", label)
        account = label
    return {
        "svc":       (account.strip() or issuer.strip())[:64],
        "issuer":    issuer.strip()[:64],
        "secret":    secret,
        "digits":    int(params.get("digits", 6)),
        "period":    int(params.get("period", 30)),
        "algorithm": params.get("algorithm", "SHA1").upper(),
    }


def parse_b32(text: str) -> Optional[str]:
    candidate = text.upper().replace(" ", "").replace("-", "")
    return candidate if _B32_RE.match(candidate) else None


def decode_qr_image(image_bytes: bytes) -> Optional[str]:
    try:
        img     = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        results = zxingcpp.read_barcodes(img)
        for r in results:
            if r.text.lower().startswith("otpauth://"):
                return r.text
    except Exception as e:
        log.warning("QR decode error: %s", e)
    return None


# ─────────────────────────────────────────────────────────────
# 7.  SPINNER ANIMATION
# ─────────────────────────────────────────────────────────────
_SPIN = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


async def spin(msg, label: str, rounds: int = 6) -> None:
    for i in range(rounds):
        try:
            await msg.edit_text(
                f"`{_SPIN[i % len(_SPIN)]}` _{label}_",
                parse_mode=ParseMode.MARKDOWN,
            )
        except (BadRequest, TelegramError):
            pass
        await asyncio.sleep(0.15)


# ─────────────────────────────────────────────────────────────
# 8.  REPLY KEYBOARDS  (always visible at bottom of chat)
# ─────────────────────────────────────────────────────────────

def rkb_home() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Add Account"),    KeyboardButton("🔑 Get OTP")],
            [KeyboardButton("📋 My Accounts"),    KeyboardButton("🔍 Search")],
            [KeyboardButton("🗑 Delete Account"), KeyboardButton("✏️ Rename")],
            [KeyboardButton("💾 Backup"),          KeyboardButton("📥 Restore")],
            [KeyboardButton("🔒 Lock Vault"),      KeyboardButton("⚙️ Settings")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def rkb_add_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📷 Scan QR Code")],
            [KeyboardButton("🔗 Paste URI"),     KeyboardButton("🔐 Enter Secret Key")],
            [KeyboardButton("🏠 Home")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def rkb_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔑 Set Passcode"),   KeyboardButton("🔓 Remove Passcode")],
            [KeyboardButton("📊 My Stats"),        KeyboardButton("🏠 Home")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def rkb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Cancel")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def rkb_unlock() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔓 Unlock Vault")]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ─────────────────────────────────────────────────────────────
# 9.  INLINE KEYBOARDS  (per-item actions only)
# ─────────────────────────────────────────────────────────────

def ikb_otp_view(svc: str) -> InlineKeyboardMarkup:
    """Shown under live OTP message — refresh, stop, detail, delete."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"OTP_GET:{svc}"),
            InlineKeyboardButton("⏹ Stop",     callback_data=f"OTP_STOP:{svc}"),
        ],
        [
            InlineKeyboardButton("ℹ️ Details", callback_data=f"DETAIL:{svc}"),
            InlineKeyboardButton("🗑 Delete",   callback_data=f"DEL_ASK:{svc}"),
        ],
    ])


def ikb_detail(svc: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔑 Get OTP", callback_data=f"OTP_GET:{svc}"),
            InlineKeyboardButton("✏️ Rename",  callback_data=f"RENAME_CB:{svc}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete",  callback_data=f"DEL_ASK:{svc}"),
        ],
    ])


def ikb_del_confirm(svc: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"DEL_OK:{svc}"),
            InlineKeyboardButton("❌ Cancel",       callback_data="DEL_CANCEL"),
        ]
    ])


def ikb_accounts(docs: list, prefix: str, page: int = 0) -> InlineKeyboardMarkup:
    sorted_docs = sorted(docs, key=lambda x: x["svc"].lower())
    total       = len(sorted_docs)
    start       = page * PAGE_SIZE
    end         = start + PAGE_SIZE
    page_docs   = sorted_docs[start:end]

    rows = [
        [InlineKeyboardButton(f"🔐 {d['svc']}", callback_data=f"{prefix}:{d['svc']}")]
        for d in page_docs
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"PAGE:{prefix}:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"PAGE:{prefix}:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────
# 10.  TEXT HELPERS
# ─────────────────────────────────────────────────────────────

def home_text(uid: int, name: str) -> str:
    count   = col_accounts.count_documents({"uid": uid})
    pin_set = bool(db_get_pin(uid))
    return (
        f"⚡ *NexAuth — 2FA Vault*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}   🔐 {count} service{'s' if count != 1 else ''}\n"
        f"{'🔑' if pin_set else '🔓'} Passcode: {'Set' if pin_set else 'Not set'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Auto-locks after *{SESSION_TTL // 60} min* inactivity."
    )


def otp_text(svc: str, issuer: str, secret: str,
             digits: int = 6, period: int = 30, algorithm: str = "SHA1") -> str:
    totp      = pyotp.TOTP(secret, digits=digits, interval=period)
    code      = totp.now()
    remaining = period - (int(time.time()) % period)
    filled    = int(remaining / (period / 10))
    bar       = "█" * filled + "░" * (10 - filled)
    half      = digits // 2
    pretty    = f"{code[:half]} {code[half:]}" if digits == 6 else code
    return (
        f"🔐 *{svc}*\n"
        f"🏢 _{issuer}_\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"```\n{pretty}\n```\n\n"
        f"`{bar}` _{remaining}s left_"
    )


def detail_text(doc: dict) -> str:
    age   = (datetime.now(timezone.utc) - doc["created"]).days
    added = doc["created"].strftime("%Y-%m-%d")
    return (
        f"ℹ️ *Account Details*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏷 *Name:*    `{doc['svc']}`\n"
        f"🏢 *Issuer:*  `{doc.get('issuer', '—')}`\n"
        f"🔢 *Digits:*  `{doc.get('digits', 6)}`\n"
        f"⏱ *Period:*  `{doc.get('period', 30)}s`\n"
        f"🔑 *Algo:*    `{doc.get('algorithm', 'SHA1')}`\n"
        f"📅 *Added:*   `{added}` _{age}d ago_"
    )


def locked_text() -> str:
    return (
        f"🔒 *Vault Locked*\n\n"
        f"Session expired after {SESSION_TTL // 60} min of inactivity.\n"
        f"Tap *🔓 Unlock Vault* or enter your passcode."
    )


async def send_locked(update: Update) -> None:
    await update.effective_chat.send_message(
        locked_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=rkb_unlock(),
    )


# ─────────────────────────────────────────────────────────────
# 11.  AUTO-REFRESH OTP LOOP
# ─────────────────────────────────────────────────────────────
_refresh_tasks: dict[int, asyncio.Task] = {}


def _cancel_refresh(uid: int) -> None:
    task = _refresh_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()


async def _otp_refresh_loop(
    chat_id: int, message_id: int, uid: int,
    svc: str, doc: dict, secret: str, bot,
) -> None:
    """Edit the OTP message every second with a live countdown bar."""
    digits    = doc.get("digits", 6)
    period    = doc.get("period", 30)
    issuer    = doc.get("issuer", svc)
    algorithm = doc.get("algorithm", "SHA1")

    try:
        while True:
            if not session_alive(uid):
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=locked_text(),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=None,
                    )
                except (BadRequest, TelegramError):
                    pass
                break

            text = otp_text(svc, issuer, secret, digits, period, algorithm)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=ikb_otp_view(svc),
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    log.warning("OTP refresh edit: %s", e)
            except TelegramError as e:
                log.warning("OTP refresh error: %s", e)
                break

            await asyncio.sleep(1)

    except asyncio.CancelledError:
        pass
    finally:
        _refresh_tasks.pop(uid, None)


async def start_otp_refresh(chat_id: int, message_id: int,
                             uid: int, svc: str, doc: dict, secret: str, bot) -> None:
    _cancel_refresh(uid)
    task = asyncio.create_task(
        _otp_refresh_loop(chat_id, message_id, uid, svc, doc, secret, bot)
    )
    _refresh_tasks[uid] = task


# ─────────────────────────────────────────────────────────────
# 12.  /start
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid  = user.id
    if not rate_ok(uid):
        await update.message.reply_text("⚠️ Too many requests. Please wait.")
        return
    db_upsert_user(uid, user.first_name, user.username)
    session_touch(uid)
    ctx.user_data.clear()
    _cancel_refresh(uid)
    await update.message.reply_text(
        home_text(uid, user.first_name),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=rkb_home(),
    )


# ─────────────────────────────────────────────────────────────
# 13.  REPLY KEYBOARD MESSAGE ROUTER
# ─────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = update.effective_user.id
    text  = (update.message.text or "").strip()
    state = ctx.user_data.get("state", "")

    if not rate_ok(uid):
        await update.message.reply_text("⚠️ Too many requests. Please wait.")
        return

    # ── CANCEL  ─────────────────────────────────────────────
    if text == "❌ Cancel":
        ctx.user_data.clear()
        _cancel_refresh(uid)
        if session_alive(uid):
            await update.message.reply_text("Cancelled.", reply_markup=rkb_home())
        else:
            await send_locked(update)
        return

    # ── UNLOCK VAULT  ───────────────────────────────────────
    if text == "🔓 Unlock Vault":
        pin_hash = db_get_pin(uid)
        if pin_hash:
            ctx.user_data["state"] = "WAIT_PIN_UNLOCK"
            await update.message.reply_text(
                "🔑 *Enter your passcode to unlock:*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=rkb_cancel(),
            )
        else:
            session_touch(uid)
            ctx.user_data.clear()
            await update.message.reply_text(
                home_text(uid, update.effective_user.first_name),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=rkb_home(),
            )
        return

    # ── PIN ENTRY (unlock)  ─────────────────────────────────
    if state == "WAIT_PIN_UNLOCK":
        try:
            await update.message.delete()
        except TelegramError:
            pass
        pin_hash = db_get_pin(uid)
        if pin_hash and verify_pin(text, pin_hash):
            session_touch(uid)
            ctx.user_data.clear()
            await update.effective_chat.send_message(
                "✅ *Vault unlocked!*\n\n" + home_text(uid, update.effective_user.first_name),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=rkb_home(),
            )
        else:
            attempts = ctx.user_data.get("pin_attempts", 0) + 1
            ctx.user_data["pin_attempts"] = attempts
            if attempts >= 5:
                ctx.user_data.clear()
                await update.effective_chat.send_message(
                    "🚫 *Too many wrong attempts.* Try again later.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=rkb_unlock(),
                )
            else:
                await update.effective_chat.send_message(
                    f"❌ *Wrong passcode.* {5 - attempts} attempt{'s' if 5-attempts!=1 else ''} left.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=rkb_cancel(),
                )
        return

    # ── SESSION CHECK for everything else  ──────────────────
    if not session_alive(uid):
        await send_locked(update)
        return

    session_touch(uid)

    # ── WAIT-STATE FLOWS  ───────────────────────────────────
    if state == "WAIT_RESTORE":
        await _do_restore(update, ctx, uid, text); return
    if state == "WAIT_SVC_NAME":
        await _do_save_svc_name(update, ctx, uid, text); return
    if state == "WAIT_RENAME":
        await _do_rename(update, ctx, uid, text); return
    if state == "WAIT_SEARCH":
        await _do_search(update, ctx, uid, text); return
    if state == "WAIT_URI":
        try: await update.message.delete()
        except TelegramError: pass
        await _do_add_uri(update, ctx, uid, text); return
    if state == "WAIT_KEY":
        try: await update.message.delete()
        except TelegramError: pass
        await _do_add_key(update, ctx, uid, text); return
    if state == "WAIT_SET_PIN":
        try: await update.message.delete()
        except TelegramError: pass
        await _do_set_pin(update, ctx, uid, text); return
    if state == "WAIT_CONFIRM_PIN":
        try: await update.message.delete()
        except TelegramError: pass
        await _do_confirm_pin(update, ctx, uid, text); return

    # ── MAIN MENU BUTTONS  ──────────────────────────────────
    if text == "➕ Add Account":
        ctx.user_data.clear()
        _cancel_refresh(uid)
        await update.message.reply_text(
            "➕ *Add New TOTP Account*\n\nChoose how to add:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_add_menu(),
        )

    elif text == "📷 Scan QR Code":
        ctx.user_data["state"] = "WAIT_QR"
        await update.message.reply_text(
            "📷 *Scan QR Code*\n\nSend the QR code image now.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )

    elif text == "🔗 Paste URI":
        ctx.user_data["state"] = "WAIT_URI"
        await update.message.reply_text(
            "🔗 *Paste otpauth URI*\n\nSend your `otpauth://totp/...` string.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )

    elif text == "🔐 Enter Secret Key":
        ctx.user_data["state"] = "WAIT_KEY"
        await update.message.reply_text(
            "🔐 *Enter Secret Key*\n\nSend your base32 secret key.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )

    elif text == "🔑 Get OTP":
        docs = db_list(uid)
        if not docs:
            await update.message.reply_text("📭 *No accounts yet.* Add one first.",
                                            parse_mode=ParseMode.MARKDOWN, reply_markup=rkb_home())
            return
        await update.message.reply_text(
            "🔑 *Get OTP Code*\n\nChoose a service:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb_accounts(docs, "OTP_GET"),
        )

    elif text == "📋 My Accounts":
        docs = db_list(uid)
        if not docs:
            await update.message.reply_text("📭 *Vault is empty.*",
                                            parse_mode=ParseMode.MARKDOWN, reply_markup=rkb_home())
            return
        lines = []
        for i, d in enumerate(sorted(docs, key=lambda x: x["svc"].lower()), 1):
            age = (datetime.now(timezone.utc) - d["created"]).days
            lines.append(f"`{i:02}.` *{d['svc']}*  _{age}d ago_")
        await update.message.reply_text(
            "📋 *Your Vault*\n━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_home(),
        )

    elif text == "🔍 Search":
        ctx.user_data["state"] = "WAIT_SEARCH"
        await update.message.reply_text(
            "🔍 *Search Accounts*\n\nType any part of the name or issuer.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )

    elif text == "🗑 Delete Account":
        docs = db_list(uid)
        if not docs:
            await update.message.reply_text("📭 *Nothing to delete.*",
                                            parse_mode=ParseMode.MARKDOWN, reply_markup=rkb_home())
            return
        await update.message.reply_text(
            "🗑 *Delete Account*\n\nChoose a service:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb_accounts(docs, "DEL_ASK"),
        )

    elif text == "✏️ Rename":
        docs = db_list(uid)
        if not docs:
            await update.message.reply_text("📭 *No accounts to rename.*",
                                            parse_mode=ParseMode.MARKDOWN, reply_markup=rkb_home())
            return
        await update.message.reply_text(
            "✏️ *Rename Account*\n\nChoose a service:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb_accounts(docs, "RENAME_CB"),
        )

    elif text == "💾 Backup":
        await _do_backup(update, uid)

    elif text == "📥 Restore":
        ctx.user_data["state"] = "WAIT_RESTORE"
        await update.message.reply_text(
            "📥 *Restore Backup*\n\nPaste your encrypted backup string.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )

    elif text == "🔒 Lock Vault":
        _cancel_refresh(uid)
        session_kill(uid)
        ctx.user_data.clear()
        await update.message.reply_text(
            "🔒 *Vault Locked.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_unlock(),
        )

    elif text == "⚙️ Settings":
        ctx.user_data.clear()
        pin_set = bool(db_get_pin(uid))
        await update.message.reply_text(
            f"⚙️ *Settings*\n\n🔑 Passcode: {'✅ Set' if pin_set else '❌ Not set'}\n\nChoose an option:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_settings(),
        )

    elif text == "🔑 Set Passcode":
        ctx.user_data["state"] = "WAIT_SET_PIN"
        await update.message.reply_text(
            "🔑 *Set Passcode*\n\nSend a 4–8 digit PIN.\n_Message deleted immediately for security._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )

    elif text == "🔓 Remove Passcode":
        if not db_get_pin(uid):
            await update.message.reply_text("ℹ️ No passcode is set.", reply_markup=rkb_settings())
        else:
            db_set_pin(uid, None)
            await update.message.reply_text(
                "✅ *Passcode removed.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=rkb_settings(),
            )

    elif text == "📊 My Stats":
        await _do_stats(update, uid)

    elif text == "🏠 Home":
        ctx.user_data.clear()
        _cancel_refresh(uid)
        await update.message.reply_text(
            home_text(uid, update.effective_user.first_name),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_home(),
        )

    else:
        await update.message.reply_text(
            "👆 Use the keyboard buttons to navigate.",
            reply_markup=rkb_home(),
        )


# ─────────────────────────────────────────────────────────────
# 14.  PHOTO HANDLER — QR scan
# ─────────────────────────────────────────────────────────────
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = update.effective_user.id
    state = ctx.user_data.get("state", "")

    if not rate_ok(uid):
        await update.message.reply_text("⚠️ Too many requests. Please wait.")
        return
    if not session_alive(uid):
        await send_locked(update)
        return
    session_touch(uid)

    if state != "WAIT_QR":
        await update.message.reply_text(
            "📷 Tap *➕ Add Account* → *📷 Scan QR Code* first.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_home(),
        )
        return

    await update.effective_chat.send_chat_action(ChatAction.TYPING)
    msg = await update.message.reply_text("`⠋` _Scanning QR…_", parse_mode=ParseMode.MARKDOWN)

    photo_file  = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()
    await spin(msg, "Decoding QR", rounds=4)

    uri = decode_qr_image(bytes(image_bytes))
    if not uri:
        ctx.user_data.clear()
        await msg.edit_text(
            "❌ *No TOTP QR found.*\n\nMake sure the QR code is clear and well-lit.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.effective_chat.send_message("Go back to add menu.", reply_markup=rkb_add_menu())
        return

    parsed = parse_otpauth(uri)
    if not parsed:
        ctx.user_data.clear()
        await msg.edit_text("❌ *QR found but URI is invalid.*", parse_mode=ParseMode.MARKDOWN)
        await update.effective_chat.send_message("Go back to add menu.", reply_markup=rkb_add_menu())
        return

    ctx.user_data.clear()
    otp_msg = await _save_and_show(msg, uid, parsed)
    if otp_msg:
        doc = db_get(uid, parsed["svc"]) or {}
        await start_otp_refresh(
            otp_msg.chat_id, otp_msg.message_id,
            uid, parsed["svc"], doc, parsed["secret"], ctx.bot
        )
    log.info("QR-add uid=%s svc=%s", uid, parsed["svc"])


# ─────────────────────────────────────────────────────────────
# 15.  INLINE CALLBACK ROUTER
# ─────────────────────────────────────────────────────────────
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    uid  = q.from_user.id
    data = q.data or ""

    await q.answer()

    if not rate_ok(uid):
        await q.answer("⚠️ Rate limit — slow down.", show_alert=True)
        return

    if not session_alive(uid):
        try:
            await q.edit_message_text(locked_text(), parse_mode=ParseMode.MARKDOWN)
        except (BadRequest, TelegramError):
            pass
        await q.message.reply_text(locked_text(), parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=rkb_unlock())
        return

    session_touch(uid)

    # ── OTP GET ─────────────────────────────────────────────
    if data.startswith("OTP_GET:"):
        svc = data.split(":", 1)[1]
        _cancel_refresh(uid)
        for i in range(6):
            try:
                await q.edit_message_text(
                    f"`{_SPIN[i % len(_SPIN)]}` _Generating…_",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except (BadRequest, TelegramError):
                pass
            await asyncio.sleep(0.14)

        doc = db_get(uid, svc)
        if not doc:
            await q.edit_message_text("❌ Account not found."); return
        try:
            secret = aes_decrypt(doc["enc"])
        except Exception:
            log.exception("Decrypt error uid=%s svc=%s", uid, svc)
            await q.edit_message_text("🔐 Decryption failed."); return

        await q.edit_message_text(
            otp_text(svc, doc.get("issuer", svc), secret,
                     doc.get("digits", 6), doc.get("period", 30), doc.get("algorithm", "SHA1")),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb_otp_view(svc),
        )
        await start_otp_refresh(
            q.message.chat_id, q.message.message_id,
            uid, svc, doc, secret, ctx.bot
        )

    # ── OTP STOP ────────────────────────────────────────────
    elif data.startswith("OTP_STOP:"):
        _cancel_refresh(uid)
        svc = data.split(":", 1)[1]
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except (BadRequest, TelegramError):
            pass

    # ── DETAIL ──────────────────────────────────────────────
    elif data.startswith("DETAIL:"):
        svc = data.split(":", 1)[1]
        _cancel_refresh(uid)
        doc = db_get(uid, svc)
        if not doc:
            await q.edit_message_text("❌ Account not found."); return
        await q.edit_message_text(
            detail_text(doc),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb_detail(svc),
        )

    # ── RENAME from inline (detail view) ────────────────────
    elif data.startswith("RENAME_CB:"):
        svc = data.split(":", 1)[1]
        _cancel_refresh(uid)
        ctx.user_data["state"]      = "WAIT_RENAME"
        ctx.user_data["rename_svc"] = svc
        await q.edit_message_text(
            f"✏️ *Rename* `{svc}`\n\nSend the new name.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await q.message.reply_text("Type the new name:", reply_markup=rkb_cancel())

    # ── DELETE ASK ──────────────────────────────────────────
    elif data.startswith("DEL_ASK:"):
        svc = data.split(":", 1)[1]
        _cancel_refresh(uid)
        await q.edit_message_text(
            f"⚠️ *Confirm Delete*\n\nPermanently remove *{svc}*?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb_del_confirm(svc),
        )

    # ── DELETE CANCEL ───────────────────────────────────────
    elif data == "DEL_CANCEL":
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except (BadRequest, TelegramError):
            pass

    # ── DELETE CONFIRM ──────────────────────────────────────
    elif data.startswith("DEL_OK:"):
        svc = data.split(":", 1)[1]
        _cancel_refresh(uid)
        for f in ["💥", "🔥", "🗑", "✅"]:
            try:
                await q.edit_message_text(f"`{f}` _Deleting…_", parse_mode=ParseMode.MARKDOWN)
            except (BadRequest, TelegramError):
                pass
            await asyncio.sleep(0.2)
        if db_delete(uid, svc):
            await q.edit_message_text(f"✅ *{svc}* removed from vault.", parse_mode=ParseMode.MARKDOWN)
            log.info("Deleted uid=%s svc=%s", uid, svc)
        else:
            await q.edit_message_text("❌ Account not found.")

    # ── PAGINATION ──────────────────────────────────────────
    elif data.startswith("PAGE:"):
        _, prefix, pg = data.split(":", 2)
        page = int(pg)
        docs = db_list(uid)
        label = "🔑 *Get OTP Code*" if prefix == "OTP_GET" else "🗑 *Select Account*"
        await q.edit_message_text(
            f"{label}\n\nChoose a service:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ikb_accounts(docs, prefix, page),
        )


# ─────────────────────────────────────────────────────────────
# 16.  ADD FLOWS
# ─────────────────────────────────────────────────────────────
async def _do_add_uri(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      uid: int, text: str) -> None:
    parsed = parse_otpauth(text)
    if not parsed:
        await update.effective_chat.send_message(
            "❌ *Invalid URI.*\n\nFormat: `otpauth://totp/Label?secret=XXX&issuer=YYY`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_add_menu(),
        )
        return
    try:
        pyotp.TOTP(parsed["secret"], digits=parsed["digits"], interval=parsed["period"]).now()
    except Exception:
        await update.effective_chat.send_message(
            "❌ *Secret in URI is invalid.*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_add_menu(),
        )
        return
    ctx.user_data.clear()
    msg     = await update.effective_chat.send_message("`⠋` _Saving…_", parse_mode=ParseMode.MARKDOWN)
    otp_msg = await _save_and_show(msg, uid, parsed)
    if otp_msg:
        doc = db_get(uid, parsed["svc"]) or {}
        await start_otp_refresh(otp_msg.chat_id, otp_msg.message_id,
                                 uid, parsed["svc"], doc, parsed["secret"], ctx.bot)


async def _do_add_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      uid: int, text: str) -> None:
    secret = parse_b32(text)
    if not secret:
        await update.effective_chat.send_message(
            "❌ *Invalid base32 secret.*\n\nOnly A–Z and 2–7 characters are valid.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_add_menu(),
        )
        return
    try:
        pyotp.TOTP(secret).now()
    except Exception:
        await update.effective_chat.send_message(
            "❌ *Secret is invalid.*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_add_menu(),
        )
        return
    ctx.user_data["pending_secret"] = secret
    ctx.user_data["state"]          = "WAIT_SVC_NAME"
    await update.effective_chat.send_message(
        "🏷 *Name this account*\n\nSend a label (e.g. `GitHub`, `Gmail`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=rkb_cancel(),
    )


async def _do_save_svc_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                             uid: int, text: str) -> None:
    try:
        await update.message.delete()
    except TelegramError:
        pass
    svc = text.strip()[:64]
    if not _SVC_RE.match(svc):
        await update.effective_chat.send_message(
            "❌ *Invalid name.* Use letters, digits, spaces, `-`, `_`, `.`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )
        return
    secret = ctx.user_data.pop("pending_secret", None)
    ctx.user_data.clear()
    if not secret:
        await update.effective_chat.send_message("⚠️ Session lost.", reply_markup=rkb_home())
        return
    parsed  = {"svc": svc, "issuer": svc, "secret": secret,
                "digits": 6, "period": 30, "algorithm": "SHA1"}
    msg     = await update.effective_chat.send_message("`⠋` _Saving…_", parse_mode=ParseMode.MARKDOWN)
    otp_msg = await _save_and_show(msg, uid, parsed)
    if otp_msg:
        doc = db_get(uid, svc) or {}
        await start_otp_refresh(otp_msg.chat_id, otp_msg.message_id,
                                 uid, svc, doc, secret, ctx.bot)


async def _save_and_show(msg, uid: int, parsed: dict):
    """Encrypt, save, show first OTP. Returns msg on success, None on duplicate."""
    await spin(msg, "Encrypting & saving")
    ok = db_add(
        uid, parsed["svc"], parsed["issuer"],
        aes_encrypt(parsed["secret"]),
        parsed.get("digits", 6), parsed.get("period", 30), parsed.get("algorithm", "SHA1"),
    )
    if not ok:
        await msg.edit_text(
            f"⚠️ *{parsed['svc']}* already exists.\n\nUse 🗑 Delete Account to remove it first.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return None
    otp_str = otp_text(parsed["svc"], parsed["issuer"], parsed["secret"],
                        parsed.get("digits", 6), parsed.get("period", 30))
    await msg.edit_text(
        f"✅ *{parsed['svc']}* added!\n━━━━━━━━━━━━━━━━\n\n{otp_str}\n\n"
        f"🔄 _Auto-refreshing…_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb_otp_view(parsed["svc"]),
    )
    log.info("Added uid=%s svc=%s", uid, parsed["svc"])
    return msg


# ─────────────────────────────────────────────────────────────
# 17.  RENAME FLOW
# ─────────────────────────────────────────────────────────────
async def _do_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                     uid: int, text: str) -> None:
    try:
        await update.message.delete()
    except TelegramError:
        pass
    new_svc = text.strip()[:64]
    if not _SVC_RE.match(new_svc):
        await update.effective_chat.send_message(
            "❌ *Invalid name.*", parse_mode=ParseMode.MARKDOWN, reply_markup=rkb_cancel()
        )
        return
    old_svc = ctx.user_data.pop("rename_svc", None)
    ctx.user_data.clear()
    if not old_svc:
        await update.effective_chat.send_message("⚠️ Session lost.", reply_markup=rkb_home())
        return
    if db_rename(uid, old_svc, new_svc):
        await update.effective_chat.send_message(
            f"✅ Renamed *{old_svc}* → *{new_svc}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_home(),
        )
        log.info("Renamed uid=%s %s->%s", uid, old_svc, new_svc)
    else:
        await update.effective_chat.send_message("❌ Rename failed.", reply_markup=rkb_home())


# ─────────────────────────────────────────────────────────────
# 18.  SEARCH FLOW
# ─────────────────────────────────────────────────────────────
async def _do_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                     uid: int, query: str) -> None:
    ctx.user_data.clear()
    results = db_search(uid, query)
    if not results:
        await update.message.reply_text(
            f"🔍 No accounts matching *{query}*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_home(),
        )
        return
    await update.message.reply_text(
        f"🔍 Found *{len(results)}* result{'s' if len(results)!=1 else ''} for `{query}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ikb_accounts(results, "OTP_GET"),
    )


# ─────────────────────────────────────────────────────────────
# 19.  BACKUP & RESTORE
# ─────────────────────────────────────────────────────────────
async def _do_backup(update: Update, uid: int) -> None:
    docs = list(col_accounts.find(
        {"uid": uid},
        {"svc": 1, "enc": 1, "issuer": 1, "digits": 1, "period": 1,
         "algorithm": 1, "created": 1, "_id": 0},
    ))
    if not docs:
        await update.message.reply_text("📭 Nothing to back up.", reply_markup=rkb_home())
        return
    msg = await update.message.reply_text("`⠋` _Encrypting…_", parse_mode=ParseMode.MARKDOWN)
    await spin(msg, "Encrypting backup")
    payload = json.dumps([
        {"svc": d["svc"], "enc": d["enc"], "issuer": d.get("issuer", ""),
         "digits": d.get("digits", 6), "period": d.get("period", 30),
         "algorithm": d.get("algorithm", "SHA1"), "ts": d["created"].isoformat()}
        for d in docs
    ])
    token = aes_encrypt(payload)
    await msg.edit_text(
        f"💾 *Encrypted Backup*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🔐 {len(docs)} account{'s' if len(docs)!=1 else ''}\n\n"
        f"`{token}`\n\n⚠️ _Keep this private._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      uid: int, token: str) -> None:
    try:
        await update.message.delete()
    except TelegramError:
        pass
    ctx.user_data.clear()
    msg = await update.effective_chat.send_message("`⠋` _Decrypting…_", parse_mode=ParseMode.MARKDOWN)
    await spin(msg, "Restoring backup")
    try:
        entries = json.loads(aes_decrypt(token))
    except Exception:
        await msg.edit_text("❌ Invalid or corrupted backup token.")
        await update.effective_chat.send_message("Back to home.", reply_markup=rkb_home())
        return
    restored = skipped = 0
    for e in entries:
        if not e.get("svc") or not e.get("enc"):
            skipped += 1
            continue
        try:
            col_accounts.update_one(
                {"uid": uid, "svc": e["svc"]},
                {"$setOnInsert": {
                    "uid": uid, "svc": e["svc"], "enc": e["enc"],
                    "issuer": e.get("issuer", e["svc"]),
                    "digits": e.get("digits", 6), "period": e.get("period", 30),
                    "algorithm": e.get("algorithm", "SHA1"),
                    "created": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            restored += 1
        except Exception:
            skipped += 1
    await msg.edit_text(
        f"✅ *Restore Complete*\n\n• Restored : {restored}\n• Skipped  : {skipped}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.effective_chat.send_message("Back to home.", reply_markup=rkb_home())
    log.info("Restore uid=%s ok=%d skip=%d", uid, restored, skipped)


# ─────────────────────────────────────────────────────────────
# 20.  PASSCODE FLOWS
# ─────────────────────────────────────────────────────────────
async def _do_set_pin(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      uid: int, text: str) -> None:
    if not _PIN_RE.match(text):
        await update.effective_chat.send_message(
            "❌ *Invalid PIN.* Enter 4–8 digits only.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_cancel(),
        )
        return
    ctx.user_data["pending_pin"] = text
    ctx.user_data["state"]       = "WAIT_CONFIRM_PIN"
    await update.effective_chat.send_message(
        "🔁 *Confirm passcode*\n\nEnter the same PIN again:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=rkb_cancel(),
    )


async def _do_confirm_pin(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          uid: int, text: str) -> None:
    pending = ctx.user_data.pop("pending_pin", None)
    ctx.user_data.clear()
    if not pending:
        await update.effective_chat.send_message("⚠️ Session lost.", reply_markup=rkb_home())
        return
    if text != pending:
        await update.effective_chat.send_message(
            "❌ *PINs do not match.* Try again via ⚙️ Settings → 🔑 Set Passcode.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rkb_settings(),
        )
        return
    db_set_pin(uid, hash_pin(pending))
    await update.effective_chat.send_message(
        "✅ *Passcode set!* Vault will require this PIN to unlock.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=rkb_settings(),
    )
    log.info("PIN set uid=%s", uid)


# ─────────────────────────────────────────────────────────────
# 21.  STATS
# ─────────────────────────────────────────────────────────────
async def _do_stats(update: Update, uid: int) -> None:
    docs     = db_list(uid)
    total    = len(docs)
    oldest   = newest = None
    if docs:
        dates  = [d["created"] for d in docs]
        oldest = min(dates).strftime("%Y-%m-%d")
        newest = max(dates).strftime("%Y-%m-%d")
    user_doc  = col_users.find_one({"uid": uid}, {"joined": 1}) or {}
    joined    = user_doc.get("joined")
    joined_s  = joined.strftime("%Y-%m-%d") if joined else "Unknown"
    pin_set   = bool(db_get_pin(uid))
    await update.message.reply_text(
        f"📊 *Your Stats*\n━━━━━━━━━━━━━━━━\n"
        f"🔐 Total accounts : `{total}`\n"
        f"📅 Joined         : `{joined_s}`\n"
        f"🗓 Oldest account : `{oldest or '—'}`\n"
        f"🆕 Newest account : `{newest or '—'}`\n"
        f"🔑 Passcode       : {'✅ Set' if pin_set else '❌ Not set'}\n"
        f"⏱ Session TTL    : `{SESSION_TTL}s`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=rkb_settings(),
    )


# ─────────────────────────────────────────────────────────────
# 22.  SESSION WATCHDOG
# ─────────────────────────────────────────────────────────────
async def watchdog(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SESSION_TTL)
    try:
        r = col_sessions.delete_many({"last": {"$lt": cutoff}})
        if r.deleted_count:
            log.info("Watchdog: removed %d expired session(s)", r.deleted_count)
    except Exception as e:
        log.warning("Watchdog error: %s", e)


# ─────────────────────────────────────────────────────────────
# 23.  ERROR HANDLER
# ─────────────────────────────────────────────────────────────
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", ctx.error)


# ─────────────────────────────────────────────────────────────
# 24.  MAIN
# ─────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(watchdog, interval=30, first=10)

    log.info("NexAuth v3 started — polling.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
