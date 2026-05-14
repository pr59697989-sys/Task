from aiogram.types import InlineKeyboardMarkup
from aiogram.types import InlineKeyboardButton


priority_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔴 High",
                callback_data="priority_high"
            )
        ],
        [
            InlineKeyboardButton(
                text="🟡 Medium",
                callback_data="priority_medium"
            )
        ],
        [
            InlineKeyboardButton(
                text="🟢 Low",
                callback_data="priority_low"
            )
        ]
    ]
)


category_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📚 Study",
                callback_data="category_study"
            )
        ],
        [
            InlineKeyboardButton(
                text="💼 Work",
                callback_data="category_work"
            )
        ],
        [
            InlineKeyboardButton(
                text="🏃 Health",
                callback_data="category_health"
            )
        ],
        [
            InlineKeyboardButton(
                text="🏠 Personal",
                callback_data="category_personal"
            )
        ]
    ]
)
