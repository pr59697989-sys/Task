import os
import io
import math
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
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# =========================
# LOAD ENV
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
REMOVEBG_API = os.getenv("REMOVEBG_API")

# =========================
# LOGGING
# =========================

logging.basicConfig(level=logging.INFO)

# =========================
# BOT SETUP
# =========================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# =========================
# KEYBOARD
# =========================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📷 Create Passport Sheet")],
        [KeyboardButton(text="ℹ Help")]
    ],
    resize_keyboard=True
)

# =========================
# START
# =========================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Send photo and get printable 4×6 passport photo PDF.",
        reply_markup=main_keyboard
    )

# =========================
# HELP
# =========================

@dp.message(F.text == "ℹ Help")
async def help_handler(message: Message):
    await message.answer(
        "1. Click Create Passport Sheet\n"
        "2. Send clear photo\n"
        "3. Bot removes background\n"
        "4. Bot creates printable PDF"
    )

# =========================
# BUTTON CLICK
# =========================

@dp.message(F.text == "📷 Create Passport Sheet")
async def ask_photo(message: Message):
    await message.answer(
        "Now send your photo."
    )

# =========================
# REMOVE BG
# =========================

def remove_background(image_path):
    with open(image_path, 'rb') as image_file:
        response = requests.post(
            'https://api.remove.bg/v1.0/removebg',
            files={'image_file': image_file},
            data={'size': 'auto'},
            headers={'X-Api-Key': REMOVEBG_API},
        )

    if response.status_code != requests.codes.ok:
        raise Exception(
            f"Remove.bg Error: {response.status_code} {response.text}"
        )

    output_path = image_path.replace('.jpg', '_nobg.png')

    with open(output_path, 'wb') as out:
        out.write(response.content)

    return output_path

# =========================
# CREATE 4x6 IMAGE SHEET
# =========================

def create_sheet(image_path):

    dpi = 300

    sheet_width = 4 * dpi
    sheet_height = 6 * dpi

    passport_w = 413
    passport_h = 531

    cols = 2
    rows = 3

    margin_x = 60
    margin_y = 60

    sheet = Image.new(
        'RGB',
        (sheet_width, sheet_height),
        'white'
    )

    img = Image.open(image_path).convert("RGBA")

    bg = Image.new("RGBA", img.size, "white")
    img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")

    img = img.resize((passport_w, passport_h))

    for row in range(rows):
        for col in range(cols):

            x = margin_x + col * (passport_w + 40)
            y = margin_y + row * (passport_h + 20)

            sheet.paste(img, (x, y))

    output_sheet = image_path.replace('.png', '_sheet.jpg')

    sheet.save(output_sheet, quality=100)

    return output_sheet

# =========================
# CREATE PDF
# =========================

def create_pdf(image_path):

    pdf_path = image_path.replace('.jpg', '.pdf')

    c = canvas.Canvas(pdf_path, pagesize=(288, 432))

    img = Image.open(image_path)

    img_reader = ImageReader(img)

    c.drawImage(
        img_reader,
        0,
        0,
        width=288,
        height=432
    )

    c.save()

    return pdf_path

# =========================
# PHOTO HANDLER
# =========================

@dp.message(F.photo)
async def photo_handler(message: Message):

    try:

        await message.answer("Processing photo...")

        photo = message.photo[-1]

        file = await bot.get_file(photo.file_id)

        user_id = message.from_user.id

        input_path = f"temp/{user_id}.jpg"

        await bot.download_file(file.file_path, input_path)

        await message.answer("Removing background...")

        nobg = remove_background(input_path)

        await message.answer("Creating print sheet...")

        sheet = create_sheet(nobg)

        await message.answer("Generating PDF...")

        pdf = create_pdf(sheet)

        await message.answer_document(
            FSInputFile(pdf),
            caption="Your printable passport photo PDF is ready."
        )

    except Exception as e:
        await message.answer(f"Error: {e}")

# =========================
# MAIN
# =========================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
