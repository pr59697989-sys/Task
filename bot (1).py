"""
NexAuth — Telegram TOTP Authenticator Bot
==========================================
- No commands (except /start).  Everything is inline buttons.
- User provides QR-URI / base32 secret  →  Bot stores it & returns OTP codes.
- AES-256-GCM encryption at rest (MongoDB).
- 2-minute auto session-lock with animated spinner feedback.
- Rate limiting per user.
"""

import asyncio
import base64
import hashlib
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
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# ─────────────────────────────────────────────────────────────
# 2.  AES-256-GCM  (nonce prepended, base64url encoded)
# ─────────────────────────────────────────────────────────────
def _build_key(raw: str) -> bytes:
    """Accept 64-char hex, base64-encoded 32 bytes, or a passphrase."""
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
    return list(col_accounts.find({"uid": uid}, {"svc": 1, "issuer": 1, "created": 1, "_id": 0}))


def db_add(uid: int, svc: str, issuer: str, enc_secret: str) -> bool:
    try:
        col_accounts.insert_one({
            "uid":     uid,
            "svc":     svc,
            "issuer":  issuer,
            "enc":     enc_secret,
            "created": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


def db_delete(uid: int, svc: str) -> bool:
    return col_accounts.delete_one({"uid": uid, "svc": svc}).deleted_count > 0


# ─────────────────────────────────────────────────────────────
# 4.  SESSION
# ─────────────────────────────────────────────────────────────
_session_cache: dict = {}   # uid → last_active monotonic time


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
RATE_WIN, RATE_MAX = 60, 20


def rate_ok(uid: int) -> bool:
    now  = time.monotonic()
    prev = [t for t in _hits[uid] if now - t < RATE_WIN]
    if len(prev) >= RATE_MAX:
        return False
    prev.append(now)
    _hits[uid] = prev
    return True


# ─────────────────────────────────────────────────────────────
# 6.  SECRET PARSING
# ─────────────────────────────────────────────────────────────
_URI_RE = re.compile(r"otpauth://totp/([^?]+)\?.*?secret=([A-Z2-7a-z]+)", re.I)
_B32_RE = re.compile(r"^[A-Z2-7]{16,128}=*$")
_SVC_RE = re.compile(r"^[A-Za-z0-9 ._\-]{1,64}$")


def parse_input(text: str):
    """Returns (service_name, raw_secret, issuer). All None if not recognised."""
    text = text.strip()
    m = _URI_RE.search(text)
    if m:
        label  = m.group(1).strip()
        secret = m.group(2).upper()
        if ":" in label:
            issuer, account = label.split(":", 1)
        else:
            issuer = account = label
        svc = (account.strip() or issuer.strip())[:64]
        return svc, secret, issuer.strip()
    candidate = text.upper().replace(" ", "").replace("-", "")
    if _B32_RE.match(candidate):
        return None, candidate, None
    return None, None, None


# ─────────────────────────────────────────────────────────────
# 7.  KEYBOARDS
# ─────────────────────────────────────────────────────────────
def kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Account", callback_data="ADD"),
            InlineKeyboardButton("🔑 Get OTP",     callback_data="OTP_LIST"),
        ],
        [
            InlineKeyboardButton("📋 My Accounts", callback_data="LIST"),
            InlineKeyboardButton("🗑 Delete",       callback_data="DEL_LIST"),
        ],
        [
            InlineKeyboardButton("💾 Backup",       callback_data="BACKUP"),
            InlineKeyboardButton("📥 Restore",      callback_data="RESTORE"),
        ],
        [InlineKeyboardButton("🔒 Lock Vault",     callback_data="LOCK")],
    ])


def kb_back(to: str = "HOME") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=to)]])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="HOME")]])


def kb_unlock() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Unlock Vault", callback_data="UNLOCK")]])


def kb_services(docs: list, prefix: str, back: str = "HOME") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🔐 {d['svc']}", callback_data=f"{prefix}:{d['svc']}")]
        for d in sorted(docs, key=lambda x: x["svc"].lower())
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def kb_otp_view(svc: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Code", callback_data=f"OTP_GET:{svc}")],
        [InlineKeyboardButton("⬅️ Back",          callback_data="OTP_LIST")],
    ])


def kb_del_confirm(svc: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"DEL_OK:{svc}"),
            InlineKeyboardButton("❌ Cancel",       callback_data="HOME"),
        ]
    ])


# ─────────────────────────────────────────────────────────────
# 8.  SPINNER ANIMATION
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
# 9.  HELPERS
# ─────────────────────────────────────────────────────────────
def home_text(uid: int, name: str) -> str:
    count = col_accounts.count_documents({"uid": uid})
    return (
        f"⚡ *NexAuth — 2FA Vault*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}   🔐 {count} service{'s' if count != 1 else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Paste a QR-URI or base32 secret to add an account.\n"
        f"Vault auto-locks after *{SESSION_TTL // 60} min* of inactivity."
    )


def otp_text(svc: str, issuer: str, secret: str) -> str:
    code      = pyotp.TOTP(secret).now()
    remaining = 30 - (int(time.time()) % 30)
    filled    = int(remaining / 3)
    bar       = "█" * filled + "░" * (10 - filled)
    return (
        f"🔐 *{svc}*\n"
        f"🏢 _{issuer}_\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"```\n{code}\n```\n\n"
        f"`{bar}` _{remaining}s left_"
    )


async def show_locked(target) -> None:
    text = (
        "🔒 *Vault Locked*\n\n"
        f"Session expired after {SESSION_TTL // 60} min of inactivity.\n"
        "Press the button below to unlock."
    )
    try:
        if hasattr(target, "edit_message_text"):          # CallbackQuery
            await target.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_unlock()
            )
        else:                                              # Message
            await target.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_unlock()
            )
    except (BadRequest, TelegramError) as e:
        log.warning("show_locked: %s", e)


# ─────────────────────────────────────────────────────────────
# 10. /start  COMMAND
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

    await update.message.reply_text(
        home_text(uid, user.first_name),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_home(),
    )


# ─────────────────────────────────────────────────────────────
# 11. CALLBACK QUERY ROUTER
# ─────────────────────────────────────────────────────────────
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    uid  = q.from_user.id
    data = q.data or ""

    await q.answer()

    if not rate_ok(uid):
        await q.answer("⚠️ Rate limit hit. Slow down.", show_alert=True)
        return

    # UNLOCK bypasses session check
    if data == "UNLOCK":
        session_touch(uid)
        ctx.user_data.clear()
        try:
            await q.edit_message_text(
                home_text(uid, q.from_user.first_name),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_home(),
            )
        except (BadRequest, TelegramError):
            await q.message.reply_text(
                home_text(uid, q.from_user.first_name),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_home(),
            )
        return

    if not session_alive(uid):
        await show_locked(q)
        return

    session_touch(uid)

    # ── HOME ──────────────────────────────────────────────────
    if data == "HOME":
        ctx.user_data.clear()
        try:
            await q.edit_message_text(
                home_text(uid, q.from_user.first_name),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_home(),
            )
        except (BadRequest, TelegramError):
            pass

    # ── ADD ───────────────────────────────────────────────────
    elif data == "ADD":
        ctx.user_data["state"] = "WAIT_SECRET"
        await q.edit_message_text(
            "➕ *Add New Account*\n\n"
            "Send me any of the following:\n\n"
            "① An `otpauth://totp/...` URI\n"
            "② A raw *base32 secret key*\n\n"
            "I store it encrypted and give you live OTP codes.\n"
            "_I never generate or send QR codes — you provide the secret._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── OTP LIST ──────────────────────────────────────────────
    elif data == "OTP_LIST":
        docs = db_list(uid)
        if not docs:
            await q.edit_message_text(
                "📭 *No accounts yet.*\n\nUse ➕ Add Account to get started.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_back(),
            )
            return
        await q.edit_message_text(
            "🔑 *Get OTP Code*\n\nChoose a service:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_services(docs, "OTP_GET", "HOME"),
        )

    # ── OTP GET ───────────────────────────────────────────────
    elif data.startswith("OTP_GET:"):
        svc = data.split(":", 1)[1]
        # animate
        try:
            await q.edit_message_text("`⠋` _Generating…_", parse_mode=ParseMode.MARKDOWN)
        except (BadRequest, TelegramError):
            pass
        for i in range(1, 7):
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
            await q.edit_message_text("❌ Account not found.", reply_markup=kb_back("OTP_LIST"))
            return
        try:
            secret = aes_decrypt(doc["enc"])
        except Exception:
            log.exception("Decrypt error uid=%s svc=%s", uid, svc)
            await q.edit_message_text("🔐 Decryption failed.", reply_markup=kb_back("OTP_LIST"))
            return

        await q.edit_message_text(
            otp_text(svc, doc.get("issuer", svc), secret),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_otp_view(svc),
        )

    # ── LIST ──────────────────────────────────────────────────
    elif data == "LIST":
        docs = db_list(uid)
        if not docs:
            await q.edit_message_text(
                "📭 *Vault is empty.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_back(),
            )
            return
        lines = []
        for i, d in enumerate(sorted(docs, key=lambda x: x["svc"].lower()), 1):
            age = (datetime.now(timezone.utc) - d["created"]).days
            lines.append(f"`{i:02}.` *{d['svc']}*  _{age}d ago_")
        await q.edit_message_text(
            "📋 *Your Vault*\n━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back(),
        )

    # ── DELETE LIST ───────────────────────────────────────────
    elif data == "DEL_LIST":
        docs = db_list(uid)
        if not docs:
            await q.edit_message_text(
                "📭 *Nothing to delete.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_back(),
            )
            return
        await q.edit_message_text(
            "🗑 *Delete Account*\n\nSelect a service to remove:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_services(docs, "DEL_ASK", "HOME"),
        )

    # ── DELETE ASK ────────────────────────────────────────────
    elif data.startswith("DEL_ASK:"):
        svc = data.split(":", 1)[1]
        await q.edit_message_text(
            f"⚠️ *Confirm Delete*\n\nPermanently remove *{svc}* from your vault?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_del_confirm(svc),
        )

    # ── DELETE CONFIRM ────────────────────────────────────────
    elif data.startswith("DEL_OK:"):
        svc = data.split(":", 1)[1]
        frames = ["💥", "🔥", "🗑", "✅"]
        for f in frames:
            try:
                await q.edit_message_text(f"`{f}` _Deleting…_", parse_mode=ParseMode.MARKDOWN)
            except (BadRequest, TelegramError):
                pass
            await asyncio.sleep(0.2)
        if db_delete(uid, svc):
            await q.edit_message_text(
                f"✅ *{svc}* removed from vault.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_back(),
            )
            log.info("Deleted uid=%s svc=%s", uid, svc)
        else:
            await q.edit_message_text("❌ Account not found.", reply_markup=kb_back())

    # ── BACKUP ────────────────────────────────────────────────
    elif data == "BACKUP":
        docs = list(col_accounts.find(
            {"uid": uid},
            {"svc": 1, "enc": 1, "issuer": 1, "created": 1, "_id": 0},
        ))
        if not docs:
            await q.edit_message_text("📭 Nothing to back up.", reply_markup=kb_back())
            return
        # spin
        for i in range(6):
            try:
                await q.edit_message_text(
                    f"`{_SPIN[i % len(_SPIN)]}` _Encrypting…_",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except (BadRequest, TelegramError):
                pass
            await asyncio.sleep(0.15)
        payload = json.dumps([
            {"svc": d["svc"], "enc": d["enc"], "issuer": d.get("issuer", ""), "ts": d["created"].isoformat()}
            for d in docs
        ])
        token = aes_encrypt(payload)
        await q.edit_message_text(
            f"💾 *Encrypted Backup*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔐 {len(docs)} account{'s' if len(docs) != 1 else ''}\n\n"
            f"`{token}`\n\n"
            f"⚠️ _Keep this private. Use 📥 Restore to import._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back(),
        )

    # ── RESTORE ───────────────────────────────────────────────
    elif data == "RESTORE":
        ctx.user_data["state"] = "WAIT_RESTORE"
        await q.edit_message_text(
            "📥 *Restore Backup*\n\n"
            "Paste your encrypted backup string below.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── LOCK ──────────────────────────────────────────────────
    elif data == "LOCK":
        session_kill(uid)
        ctx.user_data.clear()
        await q.edit_message_text(
            "🔒 *Vault Locked*\n\nPress Unlock to continue.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_unlock(),
        )


# ─────────────────────────────────────────────────────────────
# 12. TEXT MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = update.effective_user.id
    text  = (update.message.text or "").strip()
    state = ctx.user_data.get("state", "")

    if not rate_ok(uid):
        await update.message.reply_text("⚠️ Too many requests. Please wait.")
        return

    if not session_alive(uid):
        await show_locked(update.message)
        return

    session_touch(uid)

    # RESTORE flow
    if state == "WAIT_RESTORE":
        await _do_restore(update, ctx, uid, text)
        return

    # Service-name flow (after raw base32 with no svc)
    if state == "WAIT_SVC_NAME":
        await _do_save_svc_name(update, ctx, uid, text)
        return

    # Secret / URI input — either from ADD flow or pasted directly
    candidate = text.upper().replace(" ", "").replace("-", "")
    if state == "WAIT_SECRET" or text.lower().startswith("otpauth://") or _B32_RE.match(candidate):
        ctx.user_data["state"] = "WAIT_SECRET"
        await _do_add_secret(update, ctx, uid, text)
        return

    # Anything else
    await update.message.reply_text(
        "👆 Use the buttons to navigate.",
        reply_markup=kb_home(),
    )


# ─────────────────────────────────────────────────────────────
# 13. ADD SECRET FLOW
# ─────────────────────────────────────────────────────────────
async def _do_add_secret(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, text: str) -> None:
    # Delete immediately — contains sensitive data
    try:
        await update.message.delete()
    except TelegramError:
        pass

    await update.effective_chat.send_chat_action(ChatAction.TYPING)

    svc, secret, issuer = parse_input(text)

    if not secret:
        ctx.user_data.clear()
        await update.effective_chat.send_message(
            "❌ *Unrecognised format.*\n\n"
            "Please send:\n"
            "• `otpauth://totp/...` URI\n"
            "• Base32 secret (e.g. `JBSWY3DPEHPK3PXP`)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )
        return

    # Validate secret
    try:
        pyotp.TOTP(secret).now()
    except Exception:
        ctx.user_data.clear()
        await update.effective_chat.send_message(
            "❌ Invalid TOTP secret. Please double-check and try again.",
            reply_markup=kb_cancel(),
        )
        return

    if not svc:
        ctx.user_data["pending_secret"] = secret
        ctx.user_data["state"]          = "WAIT_SVC_NAME"
        await update.effective_chat.send_message(
            "🏷 *Name this account*\n\n"
            "Enter a label (e.g. `GitHub`, `Gmail`, `AWS`):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )
        return

    ctx.user_data.clear()
    await _save_and_show(update.effective_chat, uid, svc, secret, issuer or svc)


async def _do_save_svc_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, text: str) -> None:
    try:
        await update.message.delete()
    except TelegramError:
        pass

    svc = text.strip()[:64]
    if not _SVC_RE.match(svc):
        await update.effective_chat.send_message(
            "❌ Invalid name. Use letters, digits, spaces, `-`, `_`, `.` (max 64 chars). Try again:",
            reply_markup=kb_cancel(),
        )
        return

    secret = ctx.user_data.pop("pending_secret", None)
    ctx.user_data.clear()

    if not secret:
        await update.effective_chat.send_message(
            "⚠️ Session data lost. Please start over.",
            reply_markup=kb_home(),
        )
        return

    await _save_and_show(update.effective_chat, uid, svc, secret, svc)


async def _save_and_show(chat, uid: int, svc: str, secret: str, issuer: str) -> None:
    msg = await chat.send_message("`⠋` _Saving…_", parse_mode=ParseMode.MARKDOWN)
    await spin(msg, "Encrypting & saving")

    ok = db_add(uid, svc, issuer, aes_encrypt(secret))
    if not ok:
        await msg.edit_text(
            f"⚠️ *{svc}* already exists in your vault. Delete it first.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back(),
        )
        return

    await msg.edit_text(
        f"✅ *{svc}* added!\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🔑 *First OTP Code:*\n\n"
        f"{otp_text(svc, issuer, secret)}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"OTP_GET:{svc}")],
            [InlineKeyboardButton("🏠 Home",    callback_data="HOME")],
        ]),
    )
    log.info("Added uid=%s svc=%s", uid, svc)


# ─────────────────────────────────────────────────────────────
# 14. RESTORE FLOW
# ─────────────────────────────────────────────────────────────
async def _do_restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, token: str) -> None:
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
        await msg.edit_text("❌ Invalid or corrupted backup token.", reply_markup=kb_back())
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
                    "uid":     uid,
                    "svc":     e["svc"],
                    "enc":     e["enc"],
                    "issuer":  e.get("issuer", e["svc"]),
                    "created": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            restored += 1
        except Exception:
            skipped += 1

    await msg.edit_text(
        f"✅ *Restore Complete*\n\n"
        f"• Restored : {restored}\n"
        f"• Skipped  : {skipped}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_back(),
    )
    log.info("Restore uid=%s ok=%d skip=%d", uid, restored, skipped)


# ─────────────────────────────────────────────────────────────
# 15. PHOTO HANDLER
# ─────────────────────────────────────────────────────────────
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not session_alive(uid):
        await show_locked(update.message)
        return
    session_touch(uid)
    await update.message.reply_text(
        "📷 *QR Image Received*\n\n"
        "To use it:\n"
        "1. Scan the QR with your phone camera or any QR scanner app\n"
        "2. Copy the `otpauth://totp/...` text that appears\n"
        "3. Paste it here\n\n"
        "_Or tap ➕ Add Account and paste your base32 secret directly._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_back(),
    )


# ─────────────────────────────────────────────────────────────
# 16. SESSION WATCHDOG  (cleans expired sessions from Mongo)
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
# 17. ERROR HANDLER
# ─────────────────────────────────────────────────────────────
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", ctx.error)


# ─────────────────────────────────────────────────────────────
# 18. MAIN
# ─────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    # Session watchdog every 30 s
    app.job_queue.run_repeating(watchdog, interval=30, first=10)

    log.info("NexAuth bot started — polling.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
