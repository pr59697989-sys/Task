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
# AI SESSION
# ==========================================

# LIGHTWEIGHT MODEL
# Better for Railway RAM

session = new_session(
    "u2netp"
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
# SETTINGS
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
        "• AI BG Removal\n"
        "• Automatic Blue BG\n"
        "• HD Enhancement\n"
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
        "1. Create Passport PDF\n"
        "2. Select options\n"
        "3. Send photo\n"
        "4. Receive printable PDF"
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
# REMOVE BG
# ==========================================

def remove_background(
    input_path
):

    image = Image.open(
        input_path
    ).convert(
        "RGBA"
    )

    # Reduce size for Railway RAM

    image.thumbnail(
        (1500, 1500)
    )

    # Slight enhancement

    image = ImageEnhance.Sharpness(
        image
    ).enhance(1.1)

    # AI remove

    output = remove(
        image,
        session=session
    )

    output = output.convert(
        "RGBA"
    )

    # Smooth edges

    r, g, b, a = output.split()

    a = a.filter(
        ImageFilter.GaussianBlur(1)
    )

    output.putalpha(a)

    # AUTO BLUE BG

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

    output_path = input_path.replace(
        ".jpg",
        "_blue.jpg"
    )

    final.save(
        output_path,
        quality=95
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

    # LOWER MEMORY SIZE

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
        quality=90
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
        # BG REMOVE
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
                f"BG failed:\n{e}\nUsing original image."
            )

            final_image = input_path

        # ==================================
        # SHEET
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
