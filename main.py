import os
import io
import cv2
import gc
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
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from PIL import (
    Image,
    ImageFilter,
    ImageEnhance,
    ImageOps
)

from rembg import remove, new_session

from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# =========================================================
# LOAD ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env")

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
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================================================
# AI SESSION
# =========================================================

# u2net = BEST QUALITY
# u2netp = LOW RAM FAST
# isnet-general-use = VERY GOOD

session = new_session(
    "isnet-general-use"
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
        "🔥 ULTRA AI PASSPORT PHOTO BOT\n\n"
        "✅ Ultra Precise AI BG Removal\n"
        "✅ Keeps Main Face Safe\n"
        "✅ HD Enhancement\n"
        "✅ Smooth Hair Edges\n"
        "✅ Auto Passport Resize\n"
        "✅ Blue / White Background\n"
        "✅ A4 & 4x6 PDF\n"
        "✅ High Quality Print Ready\n",
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
        "4️⃣ Select background color\n"
        "5️⃣ Send clear front face photo\n"
        "6️⃣ Receive printable PDF\n\n"
        "Tips:\n"
        "• Use bright photo\n"
        "• Avoid blurry image\n"
        "• Face should be visible"
    )

# =========================================================
# START CREATE
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
# PAGE SELECT
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

    user_settings[message.from_user.id] = {
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
            "Select valid number."
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
# BG COLOR
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
        "📸 Now send your photo.",
        reply_markup=main_keyboard
    )

# =========================================================
# FACE SAFE AI BG REMOVE
# =========================================================

def ultra_remove_background(
    input_path,
    bg_type="Blue"
):

    image = Image.open(
        input_path
    ).convert("RGBA")

    # ======================================
    # LARGE QUALITY
    # ======================================

    image.thumbnail(
        (2200, 2200)
    )

    # ======================================
    # PRE SHARPEN
    # ======================================

    image = ImageEnhance.Sharpness(
        image
    ).enhance(1.25)

    image = ImageEnhance.Contrast(
        image
    ).enhance(1.05)

    # ======================================
    # REMBG
    # ======================================

    output = remove(
        image,
        session=session
    )

    output = output.convert("RGBA")

    # ======================================
    # MASK PROCESSING
    # ======================================

    r, g, b, a = output.split()

    # smooth alpha

    a = a.filter(
        ImageFilter.MedianFilter(size=3)
    )

    a = a.filter(
        ImageFilter.GaussianBlur(radius=0.6)
    )

    # increase edge precision

    alpha_np = np.array(a)

    alpha_np = cv2.morphologyEx(
        alpha_np,
        cv2.MORPH_CLOSE,
        np.ones((2, 2), np.uint8)
    )

    alpha_np = cv2.GaussianBlur(
        alpha_np,
        (3, 3),
        0
    )

    a = Image.fromarray(alpha_np)

    output.putalpha(a)

    # ======================================
    # BACKGROUND
    # ======================================

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

    # ======================================
    # HD UPSCALE SHARP
    # ======================================

    final = final.convert("RGB")

    final = ImageEnhance.Sharpness(
        final
    ).enhance(1.18)

    final = ImageEnhance.Color(
        final
    ).enhance(1.02)

    # ======================================
    # SAVE
    # ======================================

    output_path = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}_final.jpg"
    )

    final.save(
        output_path,
        quality=100,
        subsampling=0
    )

    gc.collect()

    return output_path

# =========================================================
# PASSPORT SIZE
# =========================================================

def fit_passport(img):

    return ImageOps.fit(
        img,
        (413, 531),
        method=Image.LANCZOS
    )

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

        sheet_w = 2480
        sheet_h = 3508

    else:

        sheet_w = 1200
        sheet_h = 1800

    sheet = Image.new(
        "RGB",
        (sheet_w, sheet_h),
        "white"
    )

    img = Image.open(
        image_path
    ).convert("RGB")

    img = fit_passport(img)

    spacing = 50

    cols = max(
        1,
        sheet_w // (413 + spacing)
    )

    for i in range(copies):

        row = i // cols
        col = i % cols

        x = spacing + col * (413 + spacing)
        y = spacing + row * (531 + spacing)

        sheet.paste(
            img,
            (x, y)
        )

    output_sheet = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}_sheet.jpg"
    )

    sheet.save(
        output_sheet,
        quality=100
    )

    gc.collect()

    return output_sheet

# =========================================================
# PDF
# =========================================================

def create_pdf(
    image_path,
    page
):

    if page == "A4":

        pdf_w = 595
        pdf_h = 842

    else:

        pdf_w = 288
        pdf_h = 432

    pdf_path = os.path.join(
        TEMP_DIR,
        f"{uuid.uuid4().hex}.pdf"
    )

    c = canvas.Canvas(
        pdf_path,
        pagesize=(pdf_w, pdf_h)
    )

    img = Image.open(image_path)

    img_reader = ImageReader(img)

    c.drawImage(
        img_reader,
        0,
        0,
        width=pdf_w,
        height=pdf_h
    )

    c.save()

    gc.collect()

    return pdf_path

# =========================================================
# CLEANUP
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

        settings = user_settings.get(
            message.from_user.id
        )

        # ======================================
        # BG REMOVE
        # ======================================

        await message.answer(
            "🤖 AI removing background...\n"
            "Please wait..."
        )

        final_image = ultra_remove_background(
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
            sheet,
            settings["page"]
        )

        # ======================================
        # SEND
        # ======================================

        await message.answer_document(
            FSInputFile(pdf),
            caption=(
                "✅ Passport PDF Ready\n\n"
                "• Ultra AI BG Removed\n"
                "• HD Enhanced\n"
                "• Print Ready"
            )
        )

        # also send processed image

        await message.answer_photo(
            FSInputFile(final_image),
            caption="✨ Final HD Passport Photo"
        )

        await state.clear()

    except Exception as e:

        traceback.print_exc()

        await message.answer(
            "❌ Error processing image.\n\n"
            f"{e}"
        )

    finally:

        safe_delete(input_path)
        safe_delete(final_image)
        safe_delete(sheet)
        safe_delete(pdf)

        gc.collect()

# =========================================================
# INVALID PHOTO
# =========================================================

@dp.message(
    PhotoState.waiting_photo
)
async def invalid_photo(message: Message):

    await message.answer(
        "❌ Please send image/photo only."
    )

# =========================================================
# MAIN
# =========================================================

async def main():

    logging.info(
        "BOT STARTED"
    )

    await dp.start_polling(bot)

# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())
