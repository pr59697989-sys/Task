import os
import gc
import cv2
import uuid
import asyncio
import logging
import traceback
import requests
import numpy as np

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile
)

from aiogram.filters import CommandStart

from aiogram.fsm.state import (
    State,
    StatesGroup
)

from aiogram.fsm.context import FSMContext

from aiogram.fsm.storage.memory import (
    MemoryStorage
)

from PIL import (
    Image,
    ImageEnhance,
    ImageFilter,
    ImageOps
)

from reportlab.pdfgen import canvas

# =========================================================
# ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv(
    "BOT_TOKEN"
)

REMOVEBG_API_KEY = os.getenv(
    "REMOVEBG_API_KEY"
)

if not BOT_TOKEN:
    raise Exception(
        "BOT_TOKEN missing"
    )

if not REMOVEBG_API_KEY:
    raise Exception(
        "REMOVEBG_API_KEY missing"
    )

# =========================================================
# TEMP
# =========================================================

TEMP_DIR = "temp"

os.makedirs(
    TEMP_DIR,
    exist_ok=True
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO
)

# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher(
    storage=MemoryStorage()
)

# =========================================================
# STATES
# =========================================================

class PhotoState(StatesGroup):

    waiting_page = State()

    waiting_count = State()

    waiting_bg = State()

    waiting_enhance = State()

    waiting_photo = State()

# =========================================================
# USER SETTINGS
# =========================================================

user_settings = {}

# =========================================================
# KEYBOARD
# =========================================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="📷 Create Passport PDF"
            )
        ],
        [
            KeyboardButton(
                text="ℹ Help"
            )
        ]
    ],
    resize_keyboard=True
)

# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):

    await message.answer(
        "🔥 ULTRA AI PASSPORT BOT\n\n"
        "✅ remove.bg API AI\n"
        "✅ Ultra Precise Hair Safe Removal\n"
        "✅ HD Face Enhancement\n"
        "✅ Smart Edge Smoothing\n"
        "✅ Blue / White Background\n"
        "✅ A4 & 4x6 PDF\n"
        "✅ Printable Passport Sheet\n"
        "✅ High Quality Output",
        reply_markup=main_keyboard
    )

# =========================================================
# HELP
# =========================================================

@dp.message(F.text == "ℹ Help")
async def help_handler(message: Message):

    await message.answer(
        "HOW TO USE:\n\n"
        "1️⃣ Click Create Passport PDF\n"
        "2️⃣ Select paper size\n"
        "3️⃣ Select copies\n"
        "4️⃣ Select background\n"
        "5️⃣ Send clear front face photo\n"
        "6️⃣ Receive printable PDF\n\n"
        "BEST RESULTS:\n"
        "• Use bright photo\n"
        "• Avoid blurry image\n"
        "• Keep face visible"
    )

# =========================================================
# CREATE START
# =========================================================

@dp.message(
    F.text == "📷 Create Passport PDF"
)
async def create_start(
    message: Message,
    state: FSMContext
):

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="A4")
            ],
            [
                KeyboardButton(text="4x6")
            ]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_page
    )

    await message.answer(
        "📄 Select page size:",
        reply_markup=keyboard
    )

# =========================================================
# PAGE
# =========================================================

@dp.message(PhotoState.waiting_page)
async def page_select(
    message: Message,
    state: FSMContext
):

    if message.text not in [
        "A4",
        "4x6"
    ]:

        return await message.answer(
            "Choose A4 or 4x6"
        )

    user_settings[
        message.from_user.id
    ] = {
        "page": message.text
    }

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="2"),
                KeyboardButton(text="4")
            ],
            [
                KeyboardButton(text="6"),
                KeyboardButton(text="8")
            ]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_count
    )

    await message.answer(
        "🖼 Select copies:",
        reply_markup=keyboard
    )

# =========================================================
# COUNT
# =========================================================

@dp.message(PhotoState.waiting_count)
async def count_select(
    message: Message,
    state: FSMContext
):

    try:

        count = int(message.text)

    except:

        return await message.answer(
            "Invalid number"
        )

    user_settings[
        message.from_user.id
    ]["count"] = count

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Blue")
            ],
            [
                KeyboardButton(text="White")
            ]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_bg
    )

    await message.answer(
        "🎨 Select background:",
        reply_markup=keyboard
    )

# =========================================================
# BG
# =========================================================

@dp.message(PhotoState.waiting_bg)
async def bg_select(
    message: Message,
    state: FSMContext
):

    if message.text not in [
        "Blue",
        "White"
    ]:

        return await message.answer(
            "Choose Blue or White"
        )

    user_settings[
        message.from_user.id
    ]["bg"] = message.text

    await state.set_state(
        PhotoState.waiting_photo
    )

    await message.answer(
        "📸 Send your photo now.",
        reply_markup=main_keyboard
    )

# =========================================================
# SAFE DELETE
# =========================================================

def safe_delete(path):

    try:

        if path and os.path.exists(path):

            os.remove(path)

    except:
        pass

# =========================================================
# API REMOVE BG
# =========================================================

def remove_background(
    input_path,
    bg_type
):

    # ==========================================
    # API REQUEST
    # ==========================================

    with open(input_path, "rb") as image_file:

        response = requests.post(

            "https://api.remove.bg/v1.0/removebg",

            files={
                "image_file": image_file
            },

            data={
                "size": "auto"
            },

            headers={
                "X-Api-Key": REMOVEBG_API_KEY
            },

            timeout=120
        )

    if response.status_code != 200:

        raise Exception(
            response.text
        )

    temp_png = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}.png"
    )

    with open(temp_png, "wb") as out:

        out.write(response.content)

    # ==========================================
    # OPEN PNG
    # ==========================================

    image = Image.open(
        temp_png
    ).convert("RGBA")

    image.thumbnail(
        (1400, 1400)
    )

    # ==========================================
    # EDGE SMOOTH
    # ==========================================

    r, g, b, a = image.split()

    alpha = np.array(a)

    alpha = cv2.GaussianBlur(
        alpha,
        (3, 3),
        0
    )

    alpha = cv2.medianBlur(
        alpha,
        3
    )

    alpha = cv2.dilate(
        alpha,
        np.ones((2, 2), np.uint8),
        iterations=1
    )

    final_alpha = Image.fromarray(alpha)

    image.putalpha(final_alpha)

    # ==========================================
    # BACKGROUND
    # ==========================================

    if bg_type == "Blue":

        bg_color = (
            67,
            142,
            219,
            255
        )

    else:

        bg_color = (
            255,
            255,
            255,
            255
        )

    background = Image.new(
        "RGBA",
        image.size,
        bg_color
    )

    final = Image.alpha_composite(
        background,
        image
    )

    final = final.convert("RGB")

    # ==========================================
    # HD ENHANCE
    # ==========================================

    final = ImageEnhance.Sharpness(
        final
    ).enhance(1.18)

    final = ImageEnhance.Contrast(
        final
    ).enhance(1.04)

    final = ImageEnhance.Color(
        final
    ).enhance(1.03)

    # ==========================================
    # SAVE
    # ==========================================

    final_path = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}.jpg"
    )

    final.save(
        final_path,
        quality=95
    )

    image.close()
    final.close()

    safe_delete(temp_png)

    gc.collect()

    return final_path

# =========================================================
# CREATE SHEET
# =========================================================

def create_sheet(
    image_path,
    settings
):

    page = settings["page"]

    copies = settings["count"]

    if page == "A4":

        sheet_w = 1000
        sheet_h = 1400

    else:

        sheet_w = 800
        sheet_h = 1200

    sheet = Image.new(
        "RGB",
        (sheet_w, sheet_h),
        (255, 255, 255)
    )

    img = Image.open(
        image_path
    ).convert("RGB")

    passport = ImageOps.fit(
        img,
        (220, 300),
        method=Image.LANCZOS
    )

    spacing = 25

    cols = max(
        1,
        sheet_w // (220 + spacing)
    )

    for i in range(copies):

        row = i // cols

        col = i % cols

        x = spacing + col * (
            220 + spacing
        )

        y = spacing + row * (
            300 + spacing
        )

        sheet.paste(
            passport,
            (x, y)
        )

    output_sheet = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}_sheet.jpg"
    )

    sheet.save(
        output_sheet,
        "JPEG",
        quality=80
    )

    img.close()
    passport.close()
    sheet.close()

    gc.collect()

    return output_sheet

# =========================================================
# CREATE PDF
# =========================================================

def create_pdf(
    image_path
):

    pdf_path = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}.pdf"
    )

    image = Image.open(
        image_path
    ).convert("RGB")

    image.save(
        pdf_path,
        "PDF",
        resolution=100.0
    )

    image.close()

    gc.collect()

    return pdf_path

# =========================================================
# PHOTO HANDLER
# =========================================================

@dp.message(
    PhotoState.waiting_photo,
    F.photo
)
async def photo_handler(
    message: Message,
    state: FSMContext
):

    input_path = None
    final_image = None
    sheet = None
    pdf = None

    try:

        await message.answer(
            "⬇ Downloading photo..."
        )

        photo = message.photo[-1]

        file = await bot.get_file(
            photo.file_id
        )

        input_path = os.path.join(
            TEMP_DIR,
            f"{uuid.uuid4().hex}.jpg"
        )

        await bot.download_file(
            file.file_path,
            input_path
        )

        settings = user_settings[
            message.from_user.id
        ]

        # ======================================
        # REMOVE BG
        # ======================================

        await message.answer(
            "🤖 Ultra AI removing background..."
        )

        final_image = remove_background(
            input_path,
            settings["bg"]
        )

        # ======================================
        # SHEET
        # ======================================

        await message.answer(
            "🖨 Creating printable sheet..."
        )

        sheet = create_sheet(
            final_image,
            settings
        )

        # ======================================
        # PDF
        # ======================================

        await message.answer(
            "📄 Generating PDF..."
        )

        pdf = create_pdf(
            sheet
        )

        # ======================================
        # SEND PDF
        # ======================================

        await message.answer_document(
            FSInputFile(pdf),
            caption=(
                "✅ Passport PDF Ready\n\n"
                "✨ Ultra AI Processed"
            )
        )

        # ======================================
        # SEND PHOTO
        # ======================================

        await message.answer_photo(
            FSInputFile(final_image),
            caption="✨ Final Passport Photo"
        )

        await state.clear()

    except Exception as e:

        traceback.print_exc()

        await message.answer(
            f"❌ Error:\n{e}"
        )

    finally:

        safe_delete(input_path)
        safe_delete(final_image)
        safe_delete(sheet)
        safe_delete(pdf)

        gc.collect()

# =========================================================
# INVALID
# =========================================================

@dp.message(PhotoState.waiting_photo)
async def invalid_photo(message: Message):

    await message.answer(
        "❌ Please send photo only"
    )

# =========================================================
# MAIN
# =========================================================

async def main():

    print("BOT STARTED")

    await dp.start_polling(bot)

# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())
