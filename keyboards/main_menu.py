from aiogram.types import ReplyKeyboardMarkup
from aiogram.types import KeyboardButton


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="➕ Add Task"),
            KeyboardButton(text="📋 My Tasks")
        ],
        [
            KeyboardButton(text="⏰ Today Tasks"),
            KeyboardButton(text="📊 Stats")
        ],
        [
            KeyboardButton(text="⚙️ Settings")
        ]
    ],
    resize_keyboard=True
)
