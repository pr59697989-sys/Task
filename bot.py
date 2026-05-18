"""
Production-ready Telegram Authenticator Bot
Similar to Google Authenticator — TOTP-based 2FA via Telegram
"""

import os
import io
import re
import json
import base64
import logging
import hashlib
import time
from datetime import datetime, timezone
from functools import wraps
from collections import defaultdict

import pyotp
import qrcode
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("AuthBot")

BOT_TOKEN      = os.environ["BOT_TOKEN"]
MONGO_URI      = os.environ["MONGO_URI"]
ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"]          # hex string or raw 32 bytes


# ─────────────────────────────────────────────
# Crypto helpers  (AES-256-GCM)
# ─────────────────────────────────────────────
def _derive_key(raw: str) -> bytes:
    """Accept a hex string, a base-64 string, or a raw passphrase and return 32 bytes."""
    try:
        key = bytes.fromhex(raw)
        if len(key) == 32:
            return key
    except ValueError:
        pass
    try:
        key = base64.b64decode(raw)
        if len(key) == 32:
            return key
    except Exception:
        pass
    # Fall back: SHA-256 of the passphrase
    return hashlib.sha256(raw.encode()).digest()


_AES_KEY = _derive_key(ENCRYPTION_KEY)


def encrypt(plaintext: str) -> str:
    """AES-256-GCM encrypt → URL-safe base64 string (nonce‖ciphertext)."""
    aesgcm = AESGCM(_AES_KEY)
    nonce  = os.urandom(12)                          # 96-bit nonce
    ct     = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(token: str) -> str:
    """AES-256-GCM decrypt from URL-safe base64 string."""
    raw    = base64.urlsafe_b64decode(token.encode())
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(_AES_KEY)
    return aesgcm.decrypt(nonce, ct, None).decode()


# ─────────────────────────────────────────────
# MongoDB
# ─────────────────────────────────────────────
_mongo   = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5_000)
_db      = _mongo["authenticator_bot"]
users_col    = _db["users"]
accounts_col = _db["otp_accounts"]

# Indexes
users_col.create_index("user_id", unique=True)
accounts_col.create_index(
    [("user_id", ASCENDING), ("service_name", ASCENDING)], unique=True
)
log.info("MongoDB connected and indexes ensured.")


# ─────────────────────────────────────────────
# Rate limiter  (in-memory, per user)
# ─────────────────────────────────────────────
_rate: dict[int, list[float]] = defaultdict(list)
RATE_WINDOW  = 60        # seconds
RATE_MAX_REQ = 20        # max calls per window


def rate_limited(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid  = update.effective_user.id
        now  = time.monotonic()
        hits = [t for t in _rate[uid] if now - t < RATE_WINDOW]
        if len(hits) >= RATE_MAX_REQ:
            await update.message.reply_text(
                "⚠️ Rate limit reached. Please wait a moment before trying again."
            )
            return
        hits.append(now)
        _rate[uid] = hits
        return await func(update, ctx, *args, **kwargs)
    return wrapper


# ─────────────────────────────────────────────
# Input validation
# ─────────────────────────────────────────────
SERVICE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def valid_service(name: str) -> bool:
    return bool(SERVICE_RE.match(name))


# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────
def upsert_user(uid: int, username: str | None) -> None:
    users_col.update_one(
        {"user_id": uid},
        {"$setOnInsert": {
            "user_id":    uid,
            "username":   username,
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def get_account(uid: int, service: str):
    return accounts_col.find_one({"user_id": uid, "service_name": service})


def list_accounts(uid: int) -> list[str]:
    return [
        doc["service_name"]
        for doc in accounts_col.find({"user_id": uid}, {"service_name": 1})
    ]


def delete_account(uid: int, service: str) -> bool:
    result = accounts_col.delete_one({"user_id": uid, "service_name": service})
    return result.deleted_count > 0


# ─────────────────────────────────────────────
# QR code builder
# ─────────────────────────────────────────────
def make_qr_bytes(label: str, secret: str, issuer: str = "TGAuthBot") -> bytes:
    uri = pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name=issuer)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────

@rate_limited
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user.id, user.username)
    await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        "I'm your personal *2FA Authenticator Bot* — just like Google Authenticator, "
        "but right here in Telegram.\n\n"
        "📋 *Commands*\n"
        "/add `<service>` — add a new TOTP account\n"
        "/otp `<service>` — get current 6-digit code\n"
        "/list — show all saved services\n"
        "/delete `<service>` — remove a service\n"
        "/backup — export encrypted backup\n"
        "/restore — import from backup\n"
        "/help — show this message",
        parse_mode="Markdown",
    )


@rate_limited
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start.__wrapped__(update, ctx)   # reuse start text


@rate_limited
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "Usage: /add `<service_name>`\nExample: /add GitHub", parse_mode="Markdown"
        )
        return

    service = args[0].strip()
    if not valid_service(service):
        await update.message.reply_text(
            "❌ Invalid service name. Use only letters, digits, `.`, `-`, `_` (max 64 chars).",
            parse_mode="Markdown",
        )
        return

    if get_account(uid, service):
        await update.message.reply_text(
            f"⚠️ Service *{service}* already exists. Delete it first with /delete `{service}`.",
            parse_mode="Markdown",
        )
        return

    # Generate and store
    secret           = pyotp.random_base32()
    encrypted_secret = encrypt(secret)

    try:
        accounts_col.insert_one({
            "user_id":          uid,
            "service_name":     service,
            "encrypted_secret": encrypted_secret,
            "created_at":       datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        await update.message.reply_text("⚠️ Race condition — please try again.")
        return

    # Send QR code
    label    = f"{update.effective_user.username or uid}@{service}"
    qr_bytes = make_qr_bytes(label, secret)

    await update.message.reply_photo(
        photo=io.BytesIO(qr_bytes),
        caption=(
            f"✅ *{service}* added!\n\n"
            "1️⃣ Scan the QR code with any TOTP app *or* use the secret key below.\n"
            "2️⃣ The secret is shown *only once* — save it somewhere safe if needed.\n\n"
            f"🔑 Secret: `{secret}`\n\n"
            "⚠️ _This message will be your only chance to see the raw secret._"
        ),
        parse_mode="Markdown",
    )
    log.info("Account added: uid=%s service=%s", uid, service)


@rate_limited
async def cmd_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "Usage: /otp `<service_name>`", parse_mode="Markdown"
        )
        return

    service = args[0].strip()
    doc     = get_account(uid, service)

    if not doc:
        await update.message.reply_text(
            f"❌ No account found for *{service}*. Use /list to see saved services.",
            parse_mode="Markdown",
        )
        return

    try:
        secret = decrypt(doc["encrypted_secret"])
    except Exception:
        log.exception("Decryption failed for uid=%s service=%s", uid, service)
        await update.message.reply_text("🔐 Decryption error. Contact support.")
        return

    totp      = pyotp.TOTP(secret)
    code      = totp.now()
    remaining = 30 - (int(time.time()) % 30)

    await update.message.reply_text(
        f"🔐 *{service}*\n\n"
        f"Code: `{code}`\n"
        f"⏳ Valid for {remaining}s",
        parse_mode="Markdown",
    )


@rate_limited
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = update.effective_user.id
    services = list_accounts(uid)

    if not services:
        await update.message.reply_text(
            "📭 No accounts yet. Add one with /add `<service_name>`.",
            parse_mode="Markdown",
        )
        return

    lines = "\n".join(f"• `{s}`" for s in sorted(services))
    await update.message.reply_text(
        f"🗂 *Your saved services ({len(services)})*\n\n{lines}",
        parse_mode="Markdown",
    )


@rate_limited
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "Usage: /delete `<service_name>`", parse_mode="Markdown"
        )
        return

    service = args[0].strip()
    if delete_account(uid, service):
        await update.message.reply_text(f"🗑 *{service}* has been deleted.", parse_mode="Markdown")
        log.info("Account deleted: uid=%s service=%s", uid, service)
    else:
        await update.message.reply_text(
            f"❌ No account found for *{service}*.", parse_mode="Markdown"
        )


# ─────────────────────────────────────────────
# Backup / Restore
# ─────────────────────────────────────────────

@rate_limited
async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    docs = list(accounts_col.find(
        {"user_id": uid},
        {"service_name": 1, "encrypted_secret": 1, "created_at": 1, "_id": 0},
    ))

    if not docs:
        await update.message.reply_text("📭 Nothing to back up.")
        return

    # Serialize, then encrypt the whole blob
    payload = json.dumps(
        [
            {
                "service_name":     d["service_name"],
                "encrypted_secret": d["encrypted_secret"],
                "created_at":       d["created_at"].isoformat(),
            }
            for d in docs
        ]
    )
    backup_token = encrypt(payload)

    await update.message.reply_text(
        "🔒 *Encrypted Backup*\n\n"
        "Store this string safely. Use /restore to import it.\n\n"
        f"`{backup_token}`",
        parse_mode="Markdown",
    )
    log.info("Backup issued: uid=%s accounts=%d", uid, len(docs))


@rate_limited
async def cmd_restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /restore <backup_token>"""
    uid  = update.effective_user.id
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "Usage: /restore `<backup_token>`\n\nPaste the token from /backup.",
            parse_mode="Markdown",
        )
        return

    token = args[0].strip()
    try:
        payload = decrypt(token)
        entries = json.loads(payload)
    except Exception:
        await update.message.reply_text("❌ Invalid or corrupted backup token.")
        return

    if not isinstance(entries, list):
        await update.message.reply_text("❌ Backup format unrecognised.")
        return

    restored = skipped = 0
    for entry in entries:
        svc = entry.get("service_name", "")
        enc = entry.get("encrypted_secret", "")
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
                    "created_at":       datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            restored += 1
        except Exception:
            skipped += 1

    await update.message.reply_text(
        f"✅ Restore complete.\n• Restored: {restored}\n• Skipped: {skipped}"
    )
    log.info("Restore completed: uid=%s restored=%d skipped=%d", uid, restored, skipped)


# ─────────────────────────────────────────────
# Fallback
# ─────────────────────────────────────────────
async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "❓ Unknown command. Type /help to see available commands."
    )


# ─────────────────────────────────────────────
# App bootstrap
# ─────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("add",     cmd_add))
    app.add_handler(CommandHandler("otp",     cmd_otp))
    app.add_handler(CommandHandler("list",    cmd_list))
    app.add_handler(CommandHandler("delete",  cmd_delete))
    app.add_handler(CommandHandler("backup",  cmd_backup))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    log.info("Bot is polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
