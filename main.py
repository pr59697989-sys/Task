import os
import asyncio
import logging
import requests

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

from PIL import Image, ImageEnhance, ImageFilter

from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ==========================================
# LOAD ENV
# ==========================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
REMOVEBG_API = os.getenv("REMOVEBG_API")

# ==========================================
# TEMP FOLDER
# ==========================================

os.makedirs("temp", exist_ok=True)

# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(level=logging.INFO)

# ==========================================
# BOT
# ==========================================

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# STATES
# ==========================================

class PhotoState(StatesGroup):

    waiting_for_page = State()
    waiting_for_count = State()
    waiting_for_removebg = State()
    waiting_for_bgcolor = State()
    waiting_for_quality = State()
    waiting_for_size = State()
    waiting_for_photo = State()

# ==========================================
# USER DATA
# ==========================================

user_settings = {}

# ==========================================
# MAIN KEYBOARD
# ==========================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📷 Create Passport Sheet")],
        [KeyboardButton(text="ℹ Help")]
    ],
    resize_keyboard=True
)

# ==========================================
# START
# ==========================================

@dp.message(CommandStart())
async def start(message: Message):

    await message.answer(
        "Advanced Passport Photo Generator Bot\n\n"
        "Features:\n"
        "• HD PDF\n"
        "• A4 / 4x6\n"
        "• BG Removal\n"
        "• BG Colors\n"
        "• Multiple Copies\n"
        "• Image Enhancement\n"
        "• Printable Quality",
        reply_markup=main_keyboard
    )

# ==========================================
# HELP
# ==========================================

@dp.message(F.text == "ℹ Help")
async def help_handler(message: Message):

    await message.answer(
        "Steps:\n\n"
        "1. Click Create Passport Sheet\n"
        "2. Select settings\n"
        "3. Send photo\n"
        "4. Receive HD printable PDF"
    )

# ==========================================
# START CREATE
# ==========================================

@dp.message(F.text == "📷 Create Passport Sheet")
async def create_start(
    message: Message,
    state: FSMContext
):

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="4x6")],
            [KeyboardButton(text="A4")]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_for_page
    )

    await message.answer(
        "Select paper size:",
        reply_markup=keyboard
    )

# ==========================================
# PAGE SIZE
# ==========================================

@dp.message(PhotoState.waiting_for_page)
async def select_page(
    message: Message,
    state: FSMContext
):

    user_settings[message.from_user.id] = {
        "page": message.text
    }

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="2")],
            [KeyboardButton(text="4")],
            [KeyboardButton(text="6")],
            [KeyboardButton(text="8")],
            [KeyboardButton(text="12")]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_for_count
    )

    await message.answer(
        "Select number of copies:",
        reply_markup=keyboard
    )

# ==========================================
# PHOTO COUNT
# ==========================================

@dp.message(PhotoState.waiting_for_count)
async def select_count(
    message: Message,
    state: FSMContext
):

    user_settings[message.from_user.id]["count"] = int(message.text)

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Remove BG")],
            [KeyboardButton(text="Keep Original")]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_for_removebg
    )

    await message.answer(
        "Background option:",
        reply_markup=keyboard
    )

# ==========================================
# REMOVE BG
# ==========================================

@dp.message(PhotoState.waiting_for_removebg)
async def remove_bg_option(
    message: Message,
    state: FSMContext
):

    remove_bg = message.text == "Remove BG"

    user_settings[message.from_user.id]["remove_bg"] = remove_bg

    if remove_bg:

        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="White")],
                [KeyboardButton(text="Blue")],
                [KeyboardButton(text="Red")],
                [KeyboardButton(text="Gray")]
            ],
            resize_keyboard=True
        )

        await state.set_state(
            PhotoState.waiting_for_bgcolor
        )

        await message.answer(
            "Select background color:",
            reply_markup=keyboard
        )

    else:

        user_settings[message.from_user.id]["bg_color"] = (255,255,255)

        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Standard")],
                [KeyboardButton(text="HD")],
                [KeyboardButton(text="Ultra HD")]
            ],
            resize_keyboard=True
        )

        await state.set_state(
            PhotoState.waiting_for_quality
        )

        await message.answer(
            "Select quality:",
            reply_markup=keyboard
        )

# ==========================================
# BG COLOR
# ==========================================

@dp.message(PhotoState.waiting_for_bgcolor)
async def select_bgcolor(
    message: Message,
    state: FSMContext
):

    colors = {
        "White": (255,255,255),
        "Blue": (67,142,219),
        "Red": (255,0,0),
        "Gray": (180,180,180)
    }

    user_settings[message.from_user.id]["bg_color"] = colors.get(
        message.text,
        (255,255,255)
    )

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Standard")],
            [KeyboardButton(text="HD")],
            [KeyboardButton(text="Ultra HD")]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_for_quality
    )

    await message.answer(
        "Select quality:",
        reply_markup=keyboard
    )

# ==========================================
# QUALITY
# ==========================================

@dp.message(PhotoState.waiting_for_quality)
async def select_quality(
    message: Message,
    state: FSMContext
):

    user_settings[message.from_user.id]["quality"] = message.text

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Passport Size")],
            [KeyboardButton(text="35x45 mm")],
            [KeyboardButton(text="2x2 inch")]
        ],
        resize_keyboard=True
    )

    await state.set_state(
        PhotoState.waiting_for_size
    )

    await message.answer(
        "Select photo size:",
        reply_markup=keyboard
    )

# ==========================================
# PHOTO SIZE
# ==========================================

@dp.message(PhotoState.waiting_for_size)
async def select_size(
    message: Message,
    state: FSMContext
):

    user_settings[message.from_user.id]["photo_size"] = message.text

    await state.set_state(
        PhotoState.waiting_for_photo
    )

    await message.answer(
        "Now send your photo.",
        reply_markup=main_keyboard
    )

# ==========================================
# REMOVE BG FUNCTION
# ==========================================

def remove_background(image_path):

    with open(image_path, 'rb') as image_file:

        response = requests.post(
            'https://api.remove.bg/v1.0/removebg',
            files={
                'image_file': image_file
            },
            data={
                'size': 'auto'
            },
            headers={
                'X-Api-Key': REMOVEBG_API
            },
        )

    if response.status_code != requests.codes.ok:

        raise Exception(
            f"Remove.bg Error:\n{response.text}"
        )

    output_path = image_path.replace(
        '.jpg',
        '_nobg.png'
    )

    with open(output_path, 'wb') as out:
        out.write(response.content)

    return output_path

# ==========================================
# ENHANCE IMAGE
# ==========================================

def enhance_image(img, quality):

    if quality == "HD":

        img = ImageEnhance.Sharpness(img).enhance(1.5)
        img = ImageEnhance.Contrast(img).enhance(1.1)

    elif quality == "Ultra HD":

        img = ImageEnhance.Sharpness(img).enhance(2.0)
        img = ImageEnhance.Contrast(img).enhance(1.2)
        img = img.filter(ImageFilter.DETAIL)

    return img

# ==========================================
# CREATE SHEET
# ==========================================

def create_sheet(
    image_path,
    settings
):

    page_type = settings["page"]
    copies = settings["count"]
    bg_color = settings["bg_color"]
    quality = settings["quality"]
    size = settings["photo_size"]

    # DPI

    dpi = 300

    # PAGE SIZE

    if page_type == "A4":

        sheet_width = 2480
        sheet_height = 3508

    else:

        sheet_width = 1200
        sheet_height = 1800

    # PHOTO SIZE

    if size == "35x45 mm":

        passport_w = 413
        passport_h = 531

    elif size == "2x2 inch":

        passport_w = 600
        passport_h = 600

    else:

        passport_w = 413
        passport_h = 531

    # SHEET

    sheet = Image.new(
        'RGB',
        (sheet_width, sheet_height),
        'white'
    )

    # IMAGE

    img = Image.open(image_path)

    if img.mode == "RGBA":

        bg = Image.new(
            "RGBA",
            img.size,
            bg_color
        )

        img = Image.alpha_composite(
            bg,
            img
        )

    img = img.convert("RGB")

    # ENHANCE

    img = enhance_image(
        img,
        quality
    )

    # RESIZE

    img = img.resize(
        (passport_w, passport_h)
    )

    # GRID

    spacing = 40

    cols = max(
        1,
        sheet_width // (passport_w + spacing)
    )

    # PASTE

    for i in range(copies):

        row = i // cols
        col = i % cols

        x = spacing + col * (passport_w + spacing)
        y = spacing + row * (passport_h + spacing)

        sheet.paste(img, (x, y))

    output_sheet = image_path.replace(
        '.png',
        '_sheet.jpg'
    ).replace(
        '.jpg',
        '_sheet.jpg'
    )

    sheet.save(
        output_sheet,
        quality=100
    )

    return output_sheet

# ==========================================
# CREATE PDF
# ==========================================

def create_pdf(
    image_path,
    page_type
):

    if page_type == "A4":

        pdf_width = 595
        pdf_height = 842

    else:

        pdf_width = 288
        pdf_height = 432

    pdf_path = image_path.replace(
        '.jpg',
        '.pdf'
    )

    c = canvas.Canvas(
        pdf_path,
        pagesize=(pdf_width, pdf_height)
    )

    img = Image.open(image_path)

    img_reader = ImageReader(img)

    c.drawImage(
        img_reader,
        0,
        0,
        width=pdf_width,
        height=pdf_height
    )

    c.save()

    return pdf_path

# ==========================================
# PHOTO HANDLER
# ==========================================

@dp.message(
    PhotoState.waiting_for_photo,
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

        input_path = f"temp/{user_id}.jpg"

        await bot.download_file(
            file.file_path,
            input_path
        )

        settings = user_settings[user_id]

        final_image = input_path

        # REMOVE BG

        if settings["remove_bg"]:

            await message.answer(
                "Removing background..."
            )

            final_image = remove_background(
                input_path
            )

        # CREATE SHEET

        await message.answer(
            "Creating HD photo sheet..."
        )

        sheet = create_sheet(
            final_image,
            settings
        )

        # CREATE PDF

        await message.answer(
            "Generating printable PDF..."
        )

        pdf = create_pdf(
            sheet,
            settings["page"]
        )

        # SEND

        await message.answer_document(
            FSInputFile(pdf),
            caption=(
                "Passport photo PDF ready."
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

    await dp.start_polling(bot)

if __name__ == "__main__":

    asyncio.run(main())
