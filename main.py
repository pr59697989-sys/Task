import os
import asyncio
import logging

from dotenv import load_dotenv

from aiogram import (
    Bot,
    Dispatcher,
    F
)

from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile
)

from aiogram.filters import (
    CommandStart
)

from aiogram.fsm.state import (
    State,
    StatesGroup
)

from aiogram.fsm.context import (
    FSMContext
)

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

from reportlab.pdfgen import (
    canvas
)

from reportlab.lib.utils import (
    ImageReader
)

# ==========================================
# LOAD ENV
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
# AI SESSION
# ==========================================

# BEST MODEL FOR HUMAN PHOTOS

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

# ==========================================
# START
# ==========================================

@dp.message(CommandStart())
async def start(message: Message):

    await message.answer(
        "Ultra Passport Photo Bot\n\n"
        "• Precise AI Background Removal\n"
        "• Automatic Blue Background\n"
        "• HD Passport Sheet\n"
        "• A4 / 4x6 PDF",
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
        "1. Click Create Passport PDF\n"
        "2. Select page size\n"
        "3. Select copies\n"
        "4. Send photo\n"
        "5. Receive printable PDF"
    )

# ==========================================
# START CREATE
# ==========================================

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
        "Select page size:",
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

def remove_background(
    input_path
):

    # OPEN IMAGE

    image = Image.open(
        input_path
    ).convert(
        "RGBA"
    )

    # ======================================
    # LIMIT SIZE FOR RAILWAY RAM
    # ======================================

    image.thumbnail(
        (
            2000,
            2000
        )
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

    image = ImageEnhance.Color(
        image
    ).enhance(1.02)

    # ======================================
    # AI REMOVE
    # ======================================

    output = remove(

        image,

        session=session,

        alpha_matting=True,

        alpha_matting_foreground_threshold=250,

        alpha_matting_background_threshold=5,

        alpha_matting_erode_size=3
    )

    output = output.convert(
        "RGBA"
    )

    # ======================================
    # REFINE MASK
    # ======================================

    r, g, b, a = output.split()

    a = a.filter(
        ImageFilter.GaussianBlur(0.8)
    )

    a = ImageEnhance.Contrast(
        a
    ).enhance(1.4)

    output.putalpha(a)

    # ======================================
    # BLUE BG
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
        "_passport.jpg"
    )

    final.save(
        output_path,
        quality=100
    )

    return output_path

# ==========================================
# FIT PASSPORT
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

    if page == "A4":

        sheet_w = 1800
        sheet_h = 2600

    else:

        sheet_w = 1000
        sheet_h = 1500

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

    # HD ENHANCE

    img = ImageEnhance.Sharpness(
        img
    ).enhance(1.6)

    img = ImageEnhance.Contrast(
        img
    ).enhance(1.08)

    spacing = 30

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
        quality=95
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

        # ==================================
        # AI BG REMOVE
        # ==================================

        await message.answer(
            "AI removing background..."
        )

        try:

            final_image = remove_background(
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
            "Creating printable sheet..."
        )

        sheet = create_sheet(
            final_image,
            user_settings[user_id]
        )

        # ==================================
        # PDF
        # ==================================

        await message.answer(
            "Generating PDF..."
        )

        pdf = create_pdf(
            sheet,
            user_settings[user_id]["page"]
        )

        # ==================================
        # SEND
        # ==================================

        await message.answer_document(
            FSInputFile(pdf),
            caption="Printable passport PDF ready."
        )

        # ==================================
        # CLEANUP
        # ==================================

        files_to_delete = [
            input_path,
            final_image,
            sheet,
            pdf
        ]

        for file_path in files_to_delete:

            try:

                if os.path.exists(file_path):

                    os.remove(file_path)

            except:
                pass

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
