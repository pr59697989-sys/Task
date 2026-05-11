import os
import asyncio
import logging

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
    ImageFilter,
    ImageEnhance,
    ImageOps
)

from rembg import (
    remove,
    new_session
)

from reportlab.pdfgen import canvas

from reportlab.lib.utils import (
    ImageReader
)

# ==========================================
# ENV
# ==========================================

load_dotenv()

BOT_TOKEN = os.getenv(
    "BOT_TOKEN"
)

# ==========================================
# TEMP
# ==========================================

os.makedirs(
    "temp",
    exist_ok=True
)

# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO
)

# ==========================================
# AI MODEL
# ==========================================

# BEST MODEL FOR:
# - hair
# - clothes
# - face edges
# - passport photos

session = new_session(
    "isnet-general-use"
)

# ==========================================
# BOT
# ==========================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher(
    storage=MemoryStorage()
)

# ==========================================
# STATES
# ==========================================

class PhotoState(StatesGroup):

    waiting_page = State()

    waiting_count = State()

    waiting_quality = State()

    waiting_photo = State()

# ==========================================
# USER SETTINGS
# ==========================================

user_settings = {}

# ==========================================
# KEYBOARD
# ==========================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="📷 Create Passport Sheet"
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

# ==========================================
# START
# ==========================================

@dp.message(CommandStart())
async def start(message: Message):

    await message.answer(
        "Ultra Advanced Passport Photo Bot\n\n"
        "• AI Background Removal\n"
        "• Smart Hair Protection\n"
        "• Automatic Blue Background\n"
        "• HD Enhancement\n"
        "• A4 / 4x6 Printable PDF",
        reply_markup=main_keyboard
    )

# ==========================================
# HELP
# ==========================================

@dp.message(F.text == "ℹ Help")
async def help_handler(
    message: Message
):

    await message.answer(
        "1. Click Create Passport Sheet\n"
        "2. Select options\n"
        "3. Send photo\n"
        "4. Receive printable PDF"
    )

# ==========================================
# START CREATE
# ==========================================

@dp.message(
    F.text == "📷 Create Passport Sheet"
)

async def create_start(
    message: Message,
    state: FSMContext
):

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="4x6"
                )
            ],
            [
                KeyboardButton(
                    text="A4"
                )
            ]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_page
    )

    await message.answer(
        "Select paper size:",
        reply_markup=keyboard
    )

# ==========================================
# PAGE
# ==========================================

@dp.message(
    PhotoState.waiting_page
)

async def page_select(
    message: Message,
    state: FSMContext
):

    user_settings[
        message.from_user.id
    ] = {
        "page": message.text
    }

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="2"
                )
            ],
            [
                KeyboardButton(
                    text="4"
                )
            ],
            [
                KeyboardButton(
                    text="6"
                )
            ],
            [
                KeyboardButton(
                    text="8"
                )
            ]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_count
    )

    await message.answer(
        "Select copies:",
        reply_markup=keyboard
    )

# ==========================================
# COUNT
# ==========================================

@dp.message(
    PhotoState.waiting_count
)

async def count_select(
    message: Message,
    state: FSMContext
):

    user_settings[
        message.from_user.id
    ]["count"] = int(
        message.text
    )

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="HD"
                )
            ],
            [
                KeyboardButton(
                    text="Ultra HD"
                )
            ]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_quality
    )

    await message.answer(
        "Select quality:",
        reply_markup=keyboard
    )

# ==========================================
# QUALITY
# ==========================================

@dp.message(
    PhotoState.waiting_quality
)

async def quality_select(
    message: Message,
    state: FSMContext
):

    user_settings[
        message.from_user.id
    ]["quality"] = message.text

    await state.set_state(
        PhotoState.waiting_photo
    )

    await message.answer(
        "Now send your photo.",
        reply_markup=main_keyboard
    )

# ==========================================
# ADVANCED BG REMOVE
# ==========================================

def ultra_remove_bg(
    input_path
):

    # ======================================
    # OPEN
    # ======================================

    image = Image.open(
        input_path
    ).convert(
        "RGBA"
    )

    # ======================================
    # PRE ENHANCE
    # ======================================

    image = ImageEnhance.Sharpness(
        image
    ).enhance(1.15)

    image = ImageEnhance.Contrast(
        image
    ).enhance(1.08)

    # ======================================
    # AI REMOVE
    # ======================================

    output = remove(

        image,

        session=session,

        alpha_matting=True,

        alpha_matting_foreground_threshold=240,

        alpha_matting_background_threshold=10,

        alpha_matting_erode_size=8
    )

    output = output.convert(
        "RGBA"
    )

    # ======================================
    # IMPROVE MASK
    # ======================================

    r, g, b, a = output.split()

    # smooth edges

    a = a.filter(
        ImageFilter.GaussianBlur(1)
    )

    # remove noise

    a = a.filter(
        ImageFilter.MedianFilter(3)
    )

    # improve edges

    a = ImageEnhance.Contrast(
        a
    ).enhance(1.3)

    output.putalpha(a)

    # ======================================
    # AUTO BLUE BG
    # ======================================

    blue_bg = Image.new(

        "RGBA",

        output.size,

        (
            67,
            142,
            219,
            255
        )
    )

    final = Image.alpha_composite(
        blue_bg,
        output
    )

    final = final.convert(
        "RGB"
    )

    # ======================================
    # SAVE
    # ======================================

    output_path = input_path.replace(
        ".jpg",
        "_blue.jpg"
    )

    final.save(
        output_path,
        quality=100
    )

    return output_path

# ==========================================
# ENHANCE
# ==========================================

def enhance_image(
    img,
    quality
):

    if quality == "HD":

        img = ImageEnhance.Sharpness(
            img
        ).enhance(1.6)

        img = ImageEnhance.Contrast(
            img
        ).enhance(1.08)

    elif quality == "Ultra HD":

        img = ImageEnhance.Sharpness(
            img
        ).enhance(2.4)

        img = ImageEnhance.Contrast(
            img
        ).enhance(1.18)

        img = img.filter(
            ImageFilter.DETAIL
        )

    return img

# ==========================================
# PASSPORT FIT
# ==========================================

def fit_passport(img):

    return ImageOps.fit(
        img,
        (
            413,
            531
        )
    )

# ==========================================
# CREATE SHEET
# ==========================================

def create_sheet(
    image_path,
    settings
):

    page = settings["page"]

    copies = settings["count"]

    quality = settings["quality"]

    if page == "A4":

        sheet_w = 2480
        sheet_h = 3508

    else:

        sheet_w = 1200
        sheet_h = 1800

    sheet = Image.new(
        "RGB",
        (
            sheet_w,
            sheet_h
        ),
        "white"
    )

    img = Image.open(
        image_path
    ).convert(
        "RGB"
    )

    img = fit_passport(
        img
    )

    img = enhance_image(
        img,
        quality
    )

    spacing = 40

    cols = max(
        1,
        sheet_w // (
            413 + spacing
        )
    )

    for i in range(copies):

        row = i // cols

        col = i % cols

        x = spacing + col * (
            413 + spacing
        )

        y = spacing + row * (
            531 + spacing
        )

        sheet.paste(
            img,
            (
                x,
                y
            )
        )

    output_sheet = image_path.replace(
        ".jpg",
        "_sheet.jpg"
    )

    sheet.save(
        output_sheet,
        quality=100
    )

    return output_sheet

# ==========================================
# PDF
# ==========================================

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

    pdf_path = image_path.replace(
        ".jpg",
        ".pdf"
    )

    c = canvas.Canvas(
        pdf_path,
        pagesize=(
            pdf_w,
            pdf_h
        )
    )

    img = Image.open(
        image_path
    )

    img_reader = ImageReader(
        img
    )

    c.drawImage(
        img_reader,
        0,
        0,
        width=pdf_w,
        height=pdf_h
    )

    c.save()

    return pdf_path

# ==========================================
# PHOTO HANDLER
# ==========================================

@dp.message(
    PhotoState.waiting_photo,
    F.photo
)

async def photo_handler(
    message: Message,
    state: FSMContext
):

    try:

        await message.answer(
            "Downloading photo..."
        )

        photo = message.photo[-1]

        file = await bot.get_file(
            photo.file_id
        )

        user_id = message.from_user.id

        input_path = (
            f"temp/{user_id}.jpg"
        )

        await bot.download_file(
            file.file_path,
            input_path
        )

        settings = user_settings[
            user_id
        ]

        # ==================================
        # AI REMOVE BG
        # ==================================

        await message.answer(
            "Ultra AI removing background..."
        )

        try:

            final_image = ultra_remove_bg(
                input_path
            )

        except Exception as e:

            await message.answer(
                f"AI failed:\n{e}\nUsing original image."
            )

            final_image = input_path

        # ==================================
        # CREATE SHEET
        # ==================================

        await message.answer(
            "Creating HD printable sheet..."
        )

        sheet = create_sheet(
            final_image,
            settings
        )

        # ==================================
        # PDF
        # ==================================

        await message.answer(
            "Generating PDF..."
        )

        pdf = create_pdf(
            sheet,
            settings["page"]
        )

        # ==================================
        # SEND
        # ==================================

        await message.answer_document(
            FSInputFile(pdf),
            caption=(
                "Printable passport PDF ready."
            )
        )

        await state.clear()

    except Exception as e:

        await message.answer(
            f"Error:\n{e}"
        )

# ==========================================
# MAIN
# ==========================================

async def main():

    await dp.start_polling(
        bot
    )

if __name__ == "__main__":

    asyncio.run(main())
