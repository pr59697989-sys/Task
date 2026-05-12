import os
import gc
import cv2
import uuid
import asyncio
import logging
import traceback
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

from rembg import (
    remove,
    new_session
)

from reportlab.pdfgen import canvas

# =========================================================
# ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN not found")

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

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher(
    storage=MemoryStorage()
)

# =========================================================
# AI SESSION
# =========================================================

# BEST BALANCE:
# PRECISE + STABLE

session = new_session(
    "u2net"
)

# =========================================================
# STATES
# =========================================================

class PhotoState(StatesGroup):

    waiting_page = State()
    waiting_count = State()
    waiting_bg = State()
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
        "🔥 ULTRA PASSPORT PHOTO BOT\n\n"
        "✅ Precise AI Background Removal\n"
        "✅ Hair Safe AI\n"
        "✅ HD Enhancement\n"
        "✅ Blue / White Background\n"
        "✅ Printable Passport PDF\n"
        "✅ A4 & 4x6 Support",
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
        "2️⃣ Select page size\n"
        "3️⃣ Select copies\n"
        "4️⃣ Select background\n"
        "5️⃣ Send clear photo\n"
        "6️⃣ Receive printable PDF"
    )

# =========================================================
# CREATE START
# =========================================================

@dp.message(F.text == "📷 Create Passport PDF")
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

    if message.text not in ["A4", "4x6"]:

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

    if message.text not in ["Blue", "White"]:

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
# ULTRA REMOVE BG
# =========================================================

def remove_background(
    input_path,
    bg_type
):

    image = Image.open(
        input_path
    ).convert("RGBA")

    # ==========================================
    # SAFE SIZE
    # ==========================================

    image.thumbnail(
        (1600, 1600)
    )

    # ==========================================
    # ENHANCE
    # ==========================================

    image = ImageEnhance.Sharpness(
        image
    ).enhance(1.2)

    image = ImageEnhance.Contrast(
        image
    ).enhance(1.05)

    image = ImageEnhance.Color(
        image
    ).enhance(1.02)

    # ==========================================
    # AI REMOVE
    # ==========================================

    output = remove(
        image,
        session=session,
        alpha_matting=True,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=10,
        alpha_matting_erode_size=5
    )

    output = output.convert("RGBA")

    # ==========================================
    # MASK PROCESS
    # ==========================================

    r, g, b, a = output.split()

    alpha = np.array(a)

    alpha = cv2.GaussianBlur(
        alpha,
        (3, 3),
        0
    )

    alpha = cv2.morphologyEx(
        alpha,
        cv2.MORPH_CLOSE,
        np.ones((2, 2), np.uint8)
    )

    alpha = cv2.dilate(
        alpha,
        np.ones((2, 2), np.uint8),
        iterations=1
    )

    alpha = cv2.medianBlur(
        alpha,
        3
    )

    final_alpha = Image.fromarray(alpha)

    output.putalpha(final_alpha)

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
        output.size,
        bg_color
    )

    final = Image.alpha_composite(
        background,
        output
    )

    final = final.convert("RGB")

    # ==========================================
    # FINAL ENHANCE
    # ==========================================

    final = ImageEnhance.Sharpness(
        final
    ).enhance(1.15)

    output_path = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}.jpg"
    )

    final.save(
        output_path,
        quality=95
    )

    image.close()
    output.close()
    final.close()

    gc.collect()

    return output_path

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

        sheet_w = 1240
        sheet_h = 1754

    else:

        sheet_w = 900
        sheet_h = 1300

    sheet = Image.new(
        "RGB",
        (sheet_w, sheet_h),
        "white"
    )

    img = Image.open(
        image_path
    ).convert("RGB")

    passport = ImageOps.fit(
        img,
        (295, 413),
        method=Image.LANCZOS
    )

    spacing = 30

    cols = max(
        1,
        sheet_w // (295 + spacing)
    )

    for i in range(copies):

        row = i // cols
        col = i % cols

        x = spacing + col * (
            295 + spacing
        )

        y = spacing + row * (
            413 + spacing
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
        quality=85
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
    image_path,
    page
):

    pdf_path = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}.pdf"
    )

    if page == "A4":

        pdf_w = 595
        pdf_h = 842

    else:

        pdf_w = 288
        pdf_h = 432

    c = canvas.Canvas(
        pdf_path,
        pagesize=(pdf_w, pdf_h)
    )

    c.drawImage(
        image_path,
        0,
        0,
        width=pdf_w,
        height=pdf_h
    )

    c.save()

    gc.collect()

    return pdf_path

# =========================================================
# DELETE
# =========================================================

def safe_delete(path):

    try:

        if path and os.path.exists(path):

            os.remove(path)

    except:
        pass

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
        # BG REMOVE
        # ======================================

        await message.answer(
            "🤖 AI removing background..."
        )

        final_image = remove_background(
            input_path,
            settings["bg"]
        )

        if not os.path.exists(final_image):

            return await message.answer(
                "❌ BG remove failed"
            )

        # ======================================
        # SHEET
        # ======================================

        await message.answer(
            "🖨 Creating sheet..."
        )

        sheet = create_sheet(
            final_image,
            settings
        )

        if not os.path.exists(sheet):

            return await message.answer(
                "❌ Sheet failed"
            )

        # ======================================
        # PDF
        # ======================================

        await message.answer(
            "📄 Generating PDF..."
        )

        pdf = create_pdf(
            sheet,
            settings["page"]
        )

        if not os.path.exists(pdf):

            return await message.answer(
                "❌ PDF failed"
            )

        # ======================================
        # SEND PDF
        # ======================================

        await message.answer_document(
            FSInputFile(pdf),
            caption="✅ Passport PDF Ready"
        )

        # ======================================
        # SEND FINAL PHOTO
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
        "❌ Send image only"
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
