import asyncio
import re

from bson import ObjectId
from email_validator import validate_email, EmailNotValidError

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
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
    answers = State()


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
            "balance": 0,
            "total_earnings": 0,
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

    async for task in tasks.find({"active": True}):

        keyboard.append([
            KeyboardButton(
                text=f"{task['title']} | ${task['reward']}"
            )
        ])

    task_menu = ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True
    )

    await message.answer(
        "📋 Select Task",
        reply_markup=task_menu
    )


# ================= TASK SELECT =================

@dp.message()
async def select_task(message: Message, state: FSMContext):

    task = await tasks.find_one({
        "title": {
            "$regex": f"^{re.escape(message.text.split('|')[0].strip())}$",
            "$options": "i"
        }
    })

    if not task:
        return

    CURRENT_SUBMISSION[message.from_user.id] = {
        "task_id": str(task['_id']),
        "index": 0,
        "answers": []
    }

    await state.set_state(
        SubmitTask.answers
    )

    await message.answer(
        (
            f"📌 {task['title']}\n\n"
            f"💰 Reward: ${task['reward']}\n\n"
            f"Question 1 (Email Required)\n\n"
            f"{task['questions'][0]}"
        ),
        reply_markup=user_menu
    )


# ================= SUBMIT ANSWERS =================

@dp.message(SubmitTask.answers)
async def process_answers(
    message: Message,
    state: FSMContext
):

    current = CURRENT_SUBMISSION[
        message.from_user.id
    ]

    task = await tasks.find_one({
        "_id": ObjectId(current['task_id'])
    })

    index = current['index']

    # EMAIL VALIDATION

    if index == 0:

        try:
            valid = validate_email(
                message.text
            )

            email = valid.email.lower()

        except EmailNotValidError:

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

        await state.clear()

        del CURRENT_SUBMISSION[
            message.from_user.id
        ]

        await message.answer(
            "✅ Task Submitted",
            reply_markup=user_menu
        )

        return

    await message.answer(
        task['questions'][
            current['index']
        ]
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
async def task_title(
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
async def task_reward(
    message: Message,
    state: FSMContext
):

    try:
        reward = float(message.text)

    except:
        await message.answer(
            "❌ Invalid Reward"
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
async def task_description(
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
async def task_questions(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    questions = data['questions']

    if message.text.lower() == "done":

        if len(questions) < 1:

            await message.answer(
                "❌ Minimum 1 question"
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
            "✅ Task Created"
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


# ================= APPROVE =================

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


# ================= REJECT =================

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

    result = await withdrawals.insert_one({
        "user_id": message.from_user.id,
        "amount": data['amount'],
        "address": message.text,
        "status": "pending"
    })

    wid = str(result.inserted_id)

    for admin in ADMIN_IDS:

        await bot.send_message(
            admin,
            (
                f"💸 Withdrawal Request\n\n"
                f"👤 {message.from_user.id}\n"
                f"💰 {data['amount']}\n"
                f"🏦 {message.text}"
            ),
            reply_markup=withdraw_buttons(wid)
        )

    await state.clear()

    await message.answer(
        "✅ Withdrawal Requested"
    )


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

    text = "📜 History\n\n"

    async for tx in transactions.find({
        "user_id": message.from_user.id
    }).sort("_id", -1).limit(10):

        text += (
            f"💰 {tx['amount']} | "
            f"{tx['type']}\n"
        )

    await message.answer(text)


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
                f"👤 {message.from_user.id}\n\n"
                f"{message.text}"
            )
        )

    await state.clear()

    await message.answer(
        "✅ Support Sent"
    )


# ================= MAIN =================

async def main():

    print("Bot Started")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
