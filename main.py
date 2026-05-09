import asyncio
import re

from bson import ObjectId

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import BOT_TOKEN, ADMIN_IDS

from database import (
    users,
    tasks,
    submissions,
    withdrawals,
    supports,
    transactions
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CURRENT_SUBMISSION = {}


# ================= STATES =================

class CreateTask(StatesGroup):
    title = State()
    reward = State()
    description = State()
    questions = State()


class SubmitTask(StatesGroup):
    answering = State()


class WithdrawState(StatesGroup):
    amount = State()
    address = State()


class SupportState(StatesGroup):
    message = State()


# ================= KEYBOARDS =================

user_menu = ReplyKeyboardMarkup(
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
            KeyboardButton(text="📋 Tasks")
        ],
        [
            KeyboardButton(text="📊 Stats"),
            KeyboardButton(text="📜 History")
        ]
    ],
    resize_keyboard=True
)


def approve_buttons(submission_id):

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


def withdraw_buttons(withdraw_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Approve",
                    callback_data=f"wapprove:{withdraw_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Reject",
                    callback_data=f"wreject:{withdraw_id}"
                )
            ]
        ]
    )


# ================= START =================

@dp.message(CommandStart())
async def start(message: Message):

    user = await users.find_one({
        "user_id": message.from_user.id
    })

    if not user:

        await users.insert_one({
            "user_id": message.from_user.id,
            "username": message.from_user.username,
            "name": message.from_user.full_name,
            "balance": 0.0,
            "total_earnings": 0.0,
            "banned": False
        })

    if message.from_user.id in ADMIN_IDS:

        await message.answer(
            "👑 Admin Panel",
            reply_markup=admin_menu
        )

    else:

        await message.answer(
            "🎉 Welcome",
            reply_markup=user_menu
        )


# ================= BALANCE =================

@dp.message(F.text == "💰 Balance")
async def balance(message: Message):

    user = await users.find_one({
        "user_id": message.from_user.id
    })

    await message.answer(
        (
            f"💰 Balance: {user['balance']}\n"
            f"💵 Total Earnings: {user['total_earnings']}"
        )
    )


# ================= TASKS =================

@dp.message(F.text == "📋 Tasks")
async def show_tasks(message: Message):

    keyboard = []

    found = False

    async for task in tasks.find({"active": True}):

        found = True

        keyboard.append([
            KeyboardButton(
                text=f"📝 {task['title']}"
            )
        ])

    if not found:

        await message.answer(
            "❌ No Tasks Available"
        )

        return

    keyboard.append([
        KeyboardButton(text="🏠 Home")
    ])

    task_keyboard = ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True
    )

    await message.answer(
        "📋 Select Task",
        reply_markup=task_keyboard
    )


# ================= SELECT TASK =================

@dp.message(F.text.startswith("📝 "))
async def select_task(
    message: Message,
    state: FSMContext
):

    title = message.text.replace(
        "📝 ",
        ""
    ).strip()

    task = await tasks.find_one({
        "title": title
    })

    if not task:

        await message.answer(
            "❌ Task Not Found"
        )

        return

    CURRENT_SUBMISSION[
        message.from_user.id
    ] = {
        "task_id": str(task['_id']),
        "answers": [],
        "index": 0
    }

    await state.set_state(
        SubmitTask.answering
    )

    await message.answer(
        (
            f"📌 {task['title']}\n\n"
            f"💰 Reward: {task['reward']}\n\n"
            f"📄 {task['description']}\n\n"
            f"Question 1:\n"
            f"{task['questions'][0]}"
        ),
        reply_markup=user_menu
    )


# ================= SUBMIT ANSWERS =================

@dp.message(SubmitTask.answering)
async def process_answers(
    message: Message,
    state: FSMContext
):

    if message.from_user.id not in CURRENT_SUBMISSION:
        return

    current = CURRENT_SUBMISSION[
        message.from_user.id
    ]

    task = await tasks.find_one({
        "_id": ObjectId(
            current['task_id']
        )
    })

    index = current['index']

    # FIRST QUESTION EMAIL

    if index == 0:

        email = message.text.lower().strip()

        email_regex = (
            r"^[a-zA-Z0-9_.+-]+@"
            r"[a-zA-Z0-9-]+\."
            r"[a-zA-Z0-9-.]+$"
        )

        if not re.match(email_regex, email):

            await message.answer(
                "❌ Invalid Email"
            )

            return

        exists = await submissions.find_one({
            "task_id": current['task_id'],
            "unique_email": email
        })

        if exists:

            await message.answer(
                "❌ Email Already Used"
            )

            return

        current['answers'].append(email)

    else:

        current['answers'].append(
            message.text
        )

    current['index'] += 1

    # FINISHED

    if current['index'] >= len(task['questions']):

        result = await submissions.insert_one({
            "user_id": message.from_user.id,
            "task_id": current['task_id'],
            "answers": current['answers'],
            "unique_email": current['answers'][0],
            "status": "pending"
        })

        answers_text = ""

        for i, ans in enumerate(
            current['answers'],
            start=1
        ):

            answers_text += (
                f"Q{i}: {ans}\n"
            )

        sid = str(result.inserted_id)

        for admin in ADMIN_IDS:

            await bot.send_message(
                admin,
                (
                    f"📥 New Submission\n\n"
                    f"👤 {message.from_user.full_name}\n"
                    f"🆔 {message.from_user.id}\n\n"
                    f"{answers_text}"
                ),
                reply_markup=approve_buttons(sid)
            )

        del CURRENT_SUBMISSION[
            message.from_user.id
        ]

        await state.clear()

        await message.answer(
            (
                "✅ Task Submitted\n"
                "⏳ Waiting For Approval"
            ),
            reply_markup=user_menu
        )

        return

    await message.answer(
        (
            f"Question "
            f"{current['index']+1}\n\n"
            f"{task['questions'][current['index']]}"
        )
    )


# ================= CREATE TASK =================

@dp.message(F.text == "➕ Create Task")
async def create_task_start(
    message: Message,
    state: FSMContext
):

    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    await state.set_state(
        CreateTask.title
    )

    await message.answer(
        "📌 Send Task Title"
    )


@dp.message(CreateTask.title)
async def create_title(
    message: Message,
    state: FSMContext
):

    await state.update_data(
        title=message.text
    )

    await state.set_state(
        CreateTask.reward
    )

    await message.answer(
        "💰 Send Reward\nExample: 1.5"
    )


@dp.message(CreateTask.reward)
async def create_reward(
    message: Message,
    state: FSMContext
):

    try:
        reward = float(message.text)

    except:

        await message.answer(
            "❌ Invalid Number"
        )

        return

    await state.update_data(
        reward=reward
    )

    await state.set_state(
        CreateTask.description
    )

    await message.answer(
        "📝 Send Description"
    )


@dp.message(CreateTask.description)
async def create_description(
    message: Message,
    state: FSMContext
):

    await state.update_data(
        description=message.text,
        questions=[]
    )

    await state.set_state(
        CreateTask.questions
    )

    await message.answer(
        (
            "❓ Send Question 1\n\n"
            "Type done when finished"
        )
    )


@dp.message(CreateTask.questions)
async def create_questions(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    questions = data['questions']

    if message.text.lower() == "done":

        if len(questions) < 1:

            await message.answer(
                "❌ Add Minimum 1 Question"
            )

            return

        await tasks.insert_one({
            "title": data['title'],
            "reward": data['reward'],
            "description": data['description'],
            "questions": questions,
            "active": True
        })

        await state.clear()

        await message.answer(
            "✅ Task Created",
            reply_markup=admin_menu
        )

        return

    questions.append(message.text)

    await state.update_data(
        questions=questions
    )

    await message.answer(
        (
            f"✅ Question {len(questions)} Saved\n\n"
            f"Send Question {len(questions)+1}\n"
            f"OR type done"
        )
    )


# ================= APPROVE SUBMISSION =================

@dp.callback_query(
    F.data.startswith("approve:")
)
async def approve_submission(
    call: CallbackQuery
):

    sid = call.data.split(":")[1]

    submission = await submissions.find_one({
        "_id": ObjectId(sid)
    })

    if not submission:
        return

    if submission['status'] != "pending":
        return

    task = await tasks.find_one({
        "_id": ObjectId(
            submission['task_id']
        )
    })

    reward = float(task['reward'])

    await users.update_one(
        {
            "user_id": submission['user_id']
        },
        {
            "$inc": {
                "balance": reward,
                "total_earnings": reward
            }
        }
    )

    await submissions.update_one(
        {
            "_id": ObjectId(sid)
        },
        {
            "$set": {
                "status": "approved"
            }
        }
    )

    await transactions.insert_one({
        "user_id": submission['user_id'],
        "amount": reward,
        "type": "task_reward"
    })

    await bot.send_message(
        submission['user_id'],
        (
            f"✅ Submission Approved\n"
            f"💰 Reward Added: {reward}"
        )
    )

    await call.message.edit_reply_markup()

    await call.answer(
        "Approved"
    )


# ================= REJECT SUBMISSION =================

@dp.callback_query(
    F.data.startswith("reject:")
)
async def reject_submission(
    call: CallbackQuery
):

    sid = call.data.split(":")[1]

    submission = await submissions.find_one({
        "_id": ObjectId(sid)
    })

    if not submission:
        return

    await submissions.update_one(
        {
            "_id": ObjectId(sid)
        },
        {
            "$set": {
                "status": "rejected"
            }
        }
    )

    await bot.send_message(
        submission['user_id'],
        "❌ Submission Rejected"
    )

    await call.message.edit_reply_markup()

    await call.answer(
        "Rejected"
    )


# ================= WITHDRAW =================

@dp.message(F.text == "💸 Withdraw")
async def withdraw_start(
    message: Message,
    state: FSMContext
):

    await state.set_state(
        WithdrawState.amount
    )

    await message.answer(
        "💰 Send Amount"
    )


@dp.message(WithdrawState.amount)
async def withdraw_amount(
    message: Message,
    state: FSMContext
):

    try:
        amount = float(message.text)

    except:

        await message.answer(
            "❌ Invalid Amount"
        )

        return

    user = await users.find_one({
        "user_id": message.from_user.id
    })

    if amount > user['balance']:

        await message.answer(
            "❌ Low Balance"
        )

        return

    await state.update_data(
        amount=amount
    )

    await state.set_state(
        WithdrawState.address
    )

    await message.answer(
        "🏦 Send USDT BEP20 Address"
    )


@dp.message(WithdrawState.address)
async def withdraw_address(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    amount = data['amount']

    await users.update_one(
        {
            "user_id": message.from_user.id
        },
        {
            "$inc": {
                "balance": -amount
            }
        }
    )

    result = await withdrawals.insert_one({
        "user_id": message.from_user.id,
        "amount": amount,
        "address": message.text,
        "status": "pending"
    })

    wid = str(result.inserted_id)

    for admin in ADMIN_IDS:

        await bot.send_message(
            admin,
            (
                f"💸 Withdrawal Request\n\n"
                f"👤 User ID: {message.from_user.id}\n"
                f"💰 Amount: {amount}\n"
                f"🏦 Address:\n{message.text}"
            ),
            reply_markup=withdraw_buttons(wid)
        )

    await state.clear()

    await message.answer(
        "✅ Withdrawal Requested"
    )


# ================= APPROVE WITHDRAW =================

@dp.callback_query(
    F.data.startswith("wapprove:")
)
async def approve_withdraw(
    call: CallbackQuery
):

    wid = call.data.split(":")[1]

    withdrawal = await withdrawals.find_one({
        "_id": ObjectId(wid)
    })

    if not withdrawal:
        return

    await withdrawals.update_one(
        {
            "_id": ObjectId(wid)
        },
        {
            "$set": {
                "status": "approved"
            }
        }
    )

    await bot.send_message(
        withdrawal['user_id'],
        "✅ Withdrawal Approved"
    )

    await call.message.edit_reply_markup()

    await call.answer(
        "Approved"
    )


# ================= REJECT WITHDRAW =================

@dp.callback_query(
    F.data.startswith("wreject:")
)
async def reject_withdraw(
    call: CallbackQuery
):

    wid = call.data.split(":")[1]

    withdrawal = await withdrawals.find_one({
        "_id": ObjectId(wid)
    })

    if not withdrawal:
        return

    await users.update_one(
        {
            "user_id": withdrawal['user_id']
        },
        {
            "$inc": {
                "balance": withdrawal['amount']
            }
        }
    )

    await withdrawals.update_one(
        {
            "_id": ObjectId(wid)
        },
        {
            "$set": {
                "status": "rejected"
            }
        }
    )

    await bot.send_message(
        withdrawal['user_id'],
        "❌ Withdrawal Rejected"
    )

    await call.message.edit_reply_markup()

    await call.answer(
        "Rejected"
    )


# ================= HISTORY =================

@dp.message(F.text == "📜 History")
async def history(message: Message):

    text = "📜 Your History\n\n"

    found = False

    async for tx in transactions.find({
        "user_id": message.from_user.id
    }).sort("_id", -1):

        found = True

        text += (
            f"💰 {tx['amount']}\n"
            f"📌 {tx['type']}\n\n"
        )

    if not found:

        text += "❌ No History"

    await message.answer(text)


# ================= STATS =================

@dp.message(F.text == "📊 Stats")
async def stats(message: Message):

    if message.from_user.id not in ADMIN_IDS:
        return

    total_users = await users.count_documents({})
    total_tasks = await tasks.count_documents({})
    total_submissions = await submissions.count_documents({})
    total_withdrawals = await withdrawals.count_documents({})

    await message.answer(
        (
            f"📊 Bot Statistics\n\n"
            f"👥 Users: {total_users}\n"
            f"📋 Tasks: {total_tasks}\n"
            f"📥 Submissions: {total_submissions}\n"
            f"💸 Withdrawals: {total_withdrawals}"
        )
    )


# ================= SUPPORT =================

@dp.message(F.text == "☎ Support")
async def support_start(
    message: Message,
    state: FSMContext
):

    await state.set_state(
        SupportState.message
    )

    await message.answer(
        "✉ Send Support Message"
    )


@dp.message(SupportState.message)
async def support_message(
    message: Message,
    state: FSMContext
):

    await supports.insert_one({
        "user_id": message.from_user.id,
        "message": message.text
    })

    for admin in ADMIN_IDS:

        await bot.send_message(
            admin,
            (
                f"☎ Support Ticket\n\n"
                f"👤 User ID: {message.from_user.id}\n\n"
                f"{message.text}"
            )
        )

    await state.clear()

    await message.answer(
        "✅ Support Message Sent"
    )


# ================= HOME =================

@dp.message(F.text == "🏠 Home")
async def home(message: Message):

    if message.from_user.id in ADMIN_IDS:

        await message.answer(
            "👑 Admin Panel",
            reply_markup=admin_menu
        )

    else:

        await message.answer(
            "🏠 Home",
            reply_markup=user_menu
        )


# ================= MAIN =================

async def main():

    print("Bot Started")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
