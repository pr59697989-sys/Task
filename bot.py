"""
NexAuth — Telegram TOTP Authenticator Bot  (Advanced Edition)
==============================================================
Features
────────
• 100 % button-driven — zero inline text instructions
• Add TOTP via QR photo  (pyzbar + Pillow decode on-device)
• Add TOTP via otpauth:// URI  (paste)
• Add TOTP via raw base32 secret key  (paste)
• Search / find account by name
• Account detail view  (issuer · algorithm · digits · period · added)
• Rename account
• Grouped OTP list  (alphabetical, paginated 8 per page)
• OTP code shown as  123 456  with visual countdown bar
• Copy-friendly format (tap code → copies automatically on mobile)
• Encrypted backup & restore
• AES-256-GCM encryption at rest (MongoDB)
• 2-minute auto session-lock with animated spinner
• Per-user rate limiting
• Session watchdog
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
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
from PIL import Image
from pyzbar.pyzbar import decode as qr_decode
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
PAGE_SIZE      = 8                                           # accounts per page

# ─────────────────────────────────────────────────────────────
# 2.  AES-256-GCM
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
RATE_WIN, RATE_MAX = 60, 25


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
_URI_RE  = re.compile(
    r"otpauth://totp/([^?]+)\?([^#\s]+)", re.I
)
_B32_RE  = re.compile(r"^[A-Z2-7]{16,512}=*$")
_SVC_RE  = re.compile(r"^[A-Za-z0-9 ._\-]{1,64}$")


def parse_otpauth(uri: str) -> Optional[dict]:
    """Parse an otpauth://totp/... URI into a dict with all TOTP params."""
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
    if _B32_RE.match(candidate):
        return candidate
    return None


def decode_qr_image(image_bytes: bytes) -> Optional[str]:
    """Decode QR code from image bytes; return otpauth URI or None."""
    try:
        img     = Image.open(io.BytesIO(image_bytes))
        results = qr_decode(img)
        for r in results:
            data = r.data.decode("utf-8", errors="ignore")
            if data.lower().startswith("otpauth://"):
                return data
    except Exception as e:
        log.warning("QR decode error: %s", e)
    return None


# ─────────────────────────────────────────────────────────────
# 7.  SPINNER
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
# 8.  KEYBOARDS
# ─────────────────────────────────────────────────────────────
def kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Account",  callback_data="ADD_MENU"),
            InlineKeyboardButton("🔑 Get OTP",       callback_data="OTP_LIST"),
        ],
        [
            InlineKeyboardButton("📋 My Accounts",   callback_data="LIST"),
            InlineKeyboardButton("🔍 Search",         callback_data="SEARCH"),
        ],
        [
            InlineKeyboardButton("💾 Backup",         callback_data="BACKUP"),
            InlineKeyboardButton("📥 Restore",        callback_data="RESTORE"),
        ],
        [InlineKeyboardButton("🔒 Lock Vault",        callback_data="LOCK")],
    ])


def kb_add_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Scan QR Code",      callback_data="ADD_QR")],
        [InlineKeyboardButton("🔗 Paste URI",          callback_data="ADD_URI")],
        [InlineKeyboardButton("🔐 Enter Secret Key",   callback_data="ADD_KEY")],
        [InlineKeyboardButton("⬅️ Back",               callback_data="HOME")],
    ])


def kb_back(to: str = "HOME") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=to)]])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="HOME")]])


def kb_unlock() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Unlock Vault", callback_data="UNLOCK")]])


def kb_services(docs: list, prefix: str, back: str = "HOME",
                page: int = 0) -> InlineKeyboardMarkup:
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

    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def kb_otp_view(svc: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Code",    callback_data=f"OTP_GET:{svc}")],
        [InlineKeyboardButton("ℹ️ Details",          callback_data=f"DETAIL:{svc}")],
        [InlineKeyboardButton("⬅️ Back",             callback_data="OTP_LIST")],
    ])


def kb_detail(svc: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename",           callback_data=f"RENAME:{svc}")],
        [InlineKeyboardButton("🗑 Delete",            callback_data=f"DEL_ASK:{svc}")],
        [InlineKeyboardButton("🔑 Get OTP",           callback_data=f"OTP_GET:{svc}")],
        [InlineKeyboardButton("⬅️ Back",              callback_data="OTP_LIST")],
    ])


def kb_del_confirm(svc: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"DEL_OK:{svc}"),
            InlineKeyboardButton("❌ Cancel",       callback_data="HOME"),
        ]
    ])


def kb_search_results(docs: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🔐 {d['svc']}", callback_data=f"OTP_GET:{d['svc']}")]
        for d in sorted(docs, key=lambda x: x["svc"].lower())
    ]
    rows.append([InlineKeyboardButton("🔍 New Search", callback_data="SEARCH")])
    rows.append([InlineKeyboardButton("⬅️ Back",       callback_data="HOME")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────
# 9.  TEXT HELPERS
# ─────────────────────────────────────────────────────────────
def home_text(uid: int, name: str) -> str:
    count = col_accounts.count_documents({"uid": uid})
    return (
        f"⚡ *NexAuth — 2FA Vault*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}   🔐 {count} service{'s' if count != 1 else ''}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Auto-locks after *{SESSION_TTL // 60} min* inactivity."
    )


def otp_text(svc: str, issuer: str, secret: str,
             digits: int = 6, period: int = 30, algorithm: str = "SHA1") -> str:
    algo_map = {"SHA1": pyotp.TOTP, "SHA256": pyotp.TOTP, "SHA512": pyotp.TOTP}
    totp      = pyotp.TOTP(secret, digits=digits, interval=period)
    code      = totp.now()
    remaining = period - (int(time.time()) % period)
    filled    = int(remaining / (period / 10))
    bar       = "█" * filled + "░" * (10 - filled)

    # Split code into halves for readability
    half   = digits // 2
    pretty = f"{code[:half]} {code[half:]}" if digits == 6 else code

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


async def show_locked(target) -> None:
    text = (
        f"🔒 *Vault Locked*\n\n"
        f"Session expired after {SESSION_TTL // 60} min of inactivity."
    )
    try:
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_unlock()
            )
        else:
            await target.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_unlock()
            )
    except (BadRequest, TelegramError) as e:
        log.warning("show_locked: %s", e)


# ─────────────────────────────────────────────────────────────
# 10. /start
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
        await q.answer("⚠️ Rate limit — slow down.", show_alert=True)
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

    # ── ADD MENU ──────────────────────────────────────────────
    elif data == "ADD_MENU":
        ctx.user_data.clear()
        await q.edit_message_text(
            "➕ *Add New TOTP Account*\n\nChoose how to add:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_add_menu(),
        )

    # ── ADD VIA QR PHOTO ──────────────────────────────────────
    elif data == "ADD_QR":
        ctx.user_data["state"] = "WAIT_QR"
        await q.edit_message_text(
            "📷 *Scan QR Code*\n\nSend the QR code image now.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── ADD VIA URI ───────────────────────────────────────────
    elif data == "ADD_URI":
        ctx.user_data["state"] = "WAIT_URI"
        await q.edit_message_text(
            "🔗 *Paste otpauth URI*\n\nSend your `otpauth://totp/...` string.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── ADD VIA SECRET KEY ────────────────────────────────────
    elif data == "ADD_KEY":
        ctx.user_data["state"] = "WAIT_KEY"
        await q.edit_message_text(
            "🔐 *Enter Secret Key*\n\nSend your base32 secret key.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── OTP LIST ──────────────────────────────────────────────
    elif data == "OTP_LIST":
        docs = db_list(uid)
        if not docs:
            await q.edit_message_text(
                "📭 *No accounts yet.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Account", callback_data="ADD_MENU")],
                    [InlineKeyboardButton("⬅️ Back",        callback_data="HOME")],
                ]),
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
        try:
            await q.edit_message_text(f"`{_SPIN[0]}` _Generating…_", parse_mode=ParseMode.MARKDOWN)
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
            otp_text(svc, doc.get("issuer", svc), secret,
                     doc.get("digits", 6), doc.get("period", 30), doc.get("algorithm", "SHA1")),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_otp_view(svc),
        )

    # ── DETAIL VIEW ───────────────────────────────────────────
    elif data.startswith("DETAIL:"):
        svc = data.split(":", 1)[1]
        doc = db_get(uid, svc)
        if not doc:
            await q.edit_message_text("❌ Account not found.", reply_markup=kb_back("OTP_LIST"))
            return
        await q.edit_message_text(
            detail_text(doc),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_detail(svc),
        )

    # ── RENAME ────────────────────────────────────────────────
    elif data.startswith("RENAME:"):
        svc = data.split(":", 1)[1]
        ctx.user_data["state"]      = "WAIT_RENAME"
        ctx.user_data["rename_svc"] = svc
        await q.edit_message_text(
            f"✏️ *Rename* `{svc}`\n\nSend the new name.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── SEARCH ────────────────────────────────────────────────
    elif data == "SEARCH":
        ctx.user_data["state"] = "WAIT_SEARCH"
        await q.edit_message_text(
            "🔍 *Search Accounts*\n\nType any part of the name or issuer.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── LIST ──────────────────────────────────────────────────
    elif data == "LIST":
        docs = db_list(uid)
        if not docs:
            await q.edit_message_text(
                "📭 *Vault is empty.*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Account", callback_data="ADD_MENU")],
                    [InlineKeyboardButton("⬅️ Back",        callback_data="HOME")],
                ]),
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

    # ── PAGINATION ────────────────────────────────────────────
    elif data.startswith("PAGE:"):
        _, prefix, pg = data.split(":", 2)
        page = int(pg)
        docs = db_list(uid)
        back = "HOME" if prefix == "OTP_GET" else "HOME"
        await q.edit_message_text(
            "🔑 *Get OTP Code*\n\nChoose a service:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_services(docs, prefix, back, page),
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
            "🗑 *Delete Account*\n\nSelect a service:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_services(docs, "DEL_ASK", "HOME"),
        )

    # ── DELETE ASK ────────────────────────────────────────────
    elif data.startswith("DEL_ASK:"):
        svc = data.split(":", 1)[1]
        await q.edit_message_text(
            f"⚠️ *Confirm Delete*\n\nPermanently remove *{svc}* from vault?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_del_confirm(svc),
        )

    # ── DELETE CONFIRM ────────────────────────────────────────
    elif data.startswith("DEL_OK:"):
        svc    = data.split(":", 1)[1]
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
            {"svc": 1, "enc": 1, "issuer": 1, "digits": 1, "period": 1,
             "algorithm": 1, "created": 1, "_id": 0},
        ))
        if not docs:
            await q.edit_message_text("📭 Nothing to back up.", reply_markup=kb_back())
            return
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
            {
                "svc":       d["svc"],
                "enc":       d["enc"],
                "issuer":    d.get("issuer", ""),
                "digits":    d.get("digits", 6),
                "period":    d.get("period", 30),
                "algorithm": d.get("algorithm", "SHA1"),
                "ts":        d["created"].isoformat(),
            }
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
            "📥 *Restore Backup*\n\nPaste your encrypted backup string.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )

    # ── LOCK ──────────────────────────────────────────────────
    elif data == "LOCK":
        session_kill(uid)
        ctx.user_data.clear()
        await q.edit_message_text(
            "🔒 *Vault Locked*",
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

    if state == "WAIT_RESTORE":
        await _do_restore(update, ctx, uid, text)
        return

    if state == "WAIT_SVC_NAME":
        await _do_save_svc_name(update, ctx, uid, text)
        return

    if state == "WAIT_RENAME":
        await _do_rename(update, ctx, uid, text)
        return

    if state == "WAIT_SEARCH":
        await _do_search(update, ctx, uid, text)
        return

    if state == "WAIT_URI":
        try:
            await update.message.delete()
        except TelegramError:
            pass
        await _do_add_uri(update, ctx, uid, text)
        return

    if state == "WAIT_KEY":
        try:
            await update.message.delete()
        except TelegramError:
            pass
        await _do_add_key(update, ctx, uid, text)
        return

    # Fallback — guide to buttons
    await update.message.reply_text(
        "👆 Use the buttons to navigate.",
        reply_markup=kb_home(),
    )


# ─────────────────────────────────────────────────────────────
# 13. PHOTO HANDLER  — QR scan
# ─────────────────────────────────────────────────────────────
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = update.effective_user.id
    state = ctx.user_data.get("state", "")

    if not rate_ok(uid):
        await update.message.reply_text("⚠️ Too many requests. Please wait.")
        return

    if not session_alive(uid):
        await show_locked(update.message)
        return

    session_touch(uid)

    if state != "WAIT_QR":
        await update.message.reply_text(
            "📷 To scan a QR code, tap ➕ Add Account → 📷 Scan QR Code first.",
            reply_markup=kb_home(),
        )
        return

    await update.effective_chat.send_chat_action(ChatAction.TYPING)
    msg = await update.message.reply_text("`⠋` _Scanning QR…_", parse_mode=ParseMode.MARKDOWN)

    # Download the highest-resolution photo
    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()

    await spin(msg, "Decoding QR", rounds=4)

    uri = decode_qr_image(bytes(image_bytes))
    if not uri:
        ctx.user_data.clear()
        await msg.edit_text(
            "❌ *No TOTP QR found in image.*\n\n"
            "Make sure the QR code is clear and well-lit.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="ADD_QR")],
                [InlineKeyboardButton("⬅️ Back",       callback_data="ADD_MENU")],
            ]),
        )
        return

    parsed = parse_otpauth(uri)
    if not parsed:
        ctx.user_data.clear()
        await msg.edit_text(
            "❌ *QR found but URI is invalid.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back("ADD_MENU"),
        )
        return

    ctx.user_data.clear()
    await _save_and_show(msg, uid, parsed)
    log.info("QR-add uid=%s svc=%s", uid, parsed["svc"])


# ─────────────────────────────────────────────────────────────
# 14. ADD FLOWS
# ─────────────────────────────────────────────────────────────
async def _do_add_uri(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      uid: int, text: str) -> None:
    await update.effective_chat.send_chat_action(ChatAction.TYPING)
    parsed = parse_otpauth(text)
    if not parsed:
        await update.effective_chat.send_message(
            "❌ *Invalid URI.*\n\nFormat: `otpauth://totp/Label?secret=XXX&issuer=YYY`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="ADD_URI")],
                [InlineKeyboardButton("⬅️ Back",       callback_data="ADD_MENU")],
            ]),
        )
        return

    # Validate
    try:
        pyotp.TOTP(parsed["secret"], digits=parsed["digits"], interval=parsed["period"]).now()
    except Exception:
        await update.effective_chat.send_message(
            "❌ *Secret in URI is invalid.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back("ADD_MENU"),
        )
        return

    ctx.user_data.clear()
    msg = await update.effective_chat.send_message("`⠋` _Saving…_", parse_mode=ParseMode.MARKDOWN)
    await _save_and_show(msg, uid, parsed)


async def _do_add_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      uid: int, text: str) -> None:
    await update.effective_chat.send_chat_action(ChatAction.TYPING)
    secret = parse_b32(text)
    if not secret:
        await update.effective_chat.send_message(
            "❌ *Invalid base32 secret.*\n\nOnly A–Z and 2–7 characters are valid.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="ADD_KEY")],
                [InlineKeyboardButton("⬅️ Back",       callback_data="ADD_MENU")],
            ]),
        )
        return

    try:
        pyotp.TOTP(secret).now()
    except Exception:
        await update.effective_chat.send_message(
            "❌ *Secret is invalid — cannot generate OTP.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back("ADD_MENU"),
        )
        return

    ctx.user_data["pending_secret"] = secret
    ctx.user_data["state"]          = "WAIT_SVC_NAME"
    await update.effective_chat.send_message(
        "🏷 *Name this account*\n\nSend a label (e.g. `GitHub`, `Gmail`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_cancel(),
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

    parsed = {"svc": svc, "issuer": svc, "secret": secret, "digits": 6, "period": 30, "algorithm": "SHA1"}
    msg = await update.effective_chat.send_message("`⠋` _Saving…_", parse_mode=ParseMode.MARKDOWN)
    await _save_and_show(msg, uid, parsed)


async def _save_and_show(msg, uid: int, parsed: dict) -> None:
    await spin(msg, "Encrypting & saving")

    ok = db_add(
        uid,
        parsed["svc"],
        parsed["issuer"],
        aes_encrypt(parsed["secret"]),
        parsed.get("digits", 6),
        parsed.get("period", 30),
        parsed.get("algorithm", "SHA1"),
    )

    if not ok:
        await msg.edit_text(
            f"⚠️ *{parsed['svc']}* already exists. Delete it first.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Delete It",  callback_data=f"DEL_ASK:{parsed['svc']}")],
                [InlineKeyboardButton("⬅️ Back",        callback_data="HOME")],
            ]),
        )
        return

    await msg.edit_text(
        f"✅ *{parsed['svc']}* added!\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🔑 *First OTP Code:*\n\n"
        f"{otp_text(parsed['svc'], parsed['issuer'], parsed['secret'], parsed.get('digits', 6), parsed.get('period', 30))}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"OTP_GET:{parsed['svc']}")],
            [InlineKeyboardButton("🏠 Home",    callback_data="HOME")],
        ]),
    )
    log.info("Added uid=%s svc=%s", uid, parsed["svc"])


# ─────────────────────────────────────────────────────────────
# 15. RENAME FLOW
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
            "❌ *Invalid name.* Use letters, digits, spaces, `-`, `_`, `.`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_cancel(),
        )
        return

    old_svc = ctx.user_data.pop("rename_svc", None)
    ctx.user_data.clear()

    if not old_svc:
        await update.effective_chat.send_message("⚠️ Session lost. Start over.", reply_markup=kb_home())
        return

    if db_rename(uid, old_svc, new_svc):
        await update.effective_chat.send_message(
            f"✅ Renamed *{old_svc}* → *{new_svc}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_back("OTP_LIST"),
        )
        log.info("Renamed uid=%s %s->%s", uid, old_svc, new_svc)
    else:
        await update.effective_chat.send_message(
            "❌ Rename failed.",
            reply_markup=kb_back("OTP_LIST"),
        )


# ─────────────────────────────────────────────────────────────
# 16. SEARCH FLOW
# ─────────────────────────────────────────────────────────────
async def _do_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                     uid: int, query: str) -> None:
    ctx.user_data.clear()
    results = db_search(uid, query)
    if not results:
        await update.message.reply_text(
            f"🔍 No accounts matching *{query}*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Try Again", callback_data="SEARCH")],
                [InlineKeyboardButton("⬅️ Back",       callback_data="HOME")],
            ]),
        )
        return
    await update.message.reply_text(
        f"🔍 Found *{len(results)}* result{'s' if len(results) != 1 else ''} for `{query}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_search_results(results),
    )


# ─────────────────────────────────────────────────────────────
# 17. RESTORE FLOW
# ─────────────────────────────────────────────────────────────
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
                    "uid":       uid,
                    "svc":       e["svc"],
                    "enc":       e["enc"],
                    "issuer":    e.get("issuer", e["svc"]),
                    "digits":    e.get("digits", 6),
                    "period":    e.get("period", 30),
                    "algorithm": e.get("algorithm", "SHA1"),
                    "created":   datetime.now(timezone.utc),
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
# 18. SESSION WATCHDOG
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
# 19. ERROR HANDLER
# ─────────────────────────────────────────────────────────────
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", ctx.error)


# ─────────────────────────────────────────────────────────────
# 20. MAIN
# ─────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(watchdog, interval=30, first=10)

    log.info("NexAuth (Advanced) started — polling.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
