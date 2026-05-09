from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📋 Tasks"),
            KeyboardButton(text="💰 Balance")
        ],
        [
            KeyboardButton(text="💸 Withdraw"),
            KeyboardButton(text="📜 History")
        ],
        [
            KeyboardButton(text="☎ Support")
        ]
    ],
    resize_keyboard=True
)

admin_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="➕ Create Task"),
            KeyboardButton(text="📊 Stats")
        ],
        [
            KeyboardButton(text="👥 Users"),
            KeyboardButton(text="📨 Broadcast")
        ],
        [
            KeyboardButton(text="📋 Tasks")
        ]
    ],
    resize_keyboard=True
)


def admin_submission_buttons(submission_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Approve",
                    callback_data=f"approve:{submission_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Reject",
                    callback_data=f"reject:{submission_id}"
                )
            ]
        ]
    )


def withdrawal_buttons(withdrawal_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Approve",
                    callback_data=f"wapprove:{withdrawal_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Reject",
                    callback_data=f"wreject:{withdrawal_id}"
                )
            ]
        ]
    )
