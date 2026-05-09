import asyncio
from bson import ObjectId

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
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

CURRENT_TASK = {}


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

def user_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Tasks",
                    callback_data="tasks"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Balance",
                    callback_data="balance"
                ),
                InlineKeyboardButton(
                    text="💸 Withdraw",
                    callback_data="withdraw"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 History",
                    callback_data="history"
                ),
                InlineKeyboardButton(
                    text="☎ Support",
                    callback_data="support"
                )
            ]
        ]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Create Task",
                    callback_data="create_task"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Tasks",
                    callback_data="tasks"
                ),
                InlineKeyboardButton(
                    text="📊 Stats",
                    callback_data="stats"
                )
            ]
        ]
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
            reply_markup=admin_menu()
        )

    else:

        await message.answer(
            "🎉 Welcome",
            reply_markup=user_menu()
        )


# ================= BALANCE =================

@dp.callback_query(F.data == "balance")
async def balance(call: CallbackQuery):

    user = await users.find_one({
        "user_id": call.from_user.id
    })

    await call.message.answer(
        (
            f"💰 Balance: {user['balance']}\n"
            f"💵 Total Earnings: {user['total_earnings']}"
        )
    )

    await call.answer()


# ================= SHOW TASKS =================

@dp.callback_query(F.data == "tasks")
async def tasks_list(call: CallbackQuery):

    text = "📋 Available Tasks\n\n"

    async for task in tasks.find({"active": True}):

        text += (
            f"🆔 {task['_id']}\n"
            f"📌 {task['title']}\n"
            f"💰 Reward: {task['reward']}\n\n"
        )

    text += "Send Task ID to start."

    await call.message.answer(text)

    await call.answer()


# ================= CREATE TASK =================

@dp.callback_query(F.data == "create_task")
async def create_task_start(
    call: CallbackQuery,
    state: FSMContext
):

    if call.from_user.id not in ADMIN_IDS:
        return

    await state.clear()

    await state.set_state(CreateTask.title)

    await call.message.answer(
        "📌 Send Task Title"
    )

    await call.answer()


@dp.message(CreateTask.title)
async def task_title(
    message: Message,
    state: FSMContext
):

    await state.update_data(
        title=message.text
    )

    await state.set_state(CreateTask.reward)

    await message.answer(
        "💰 Send Reward Amount"
    )


@dp.message(CreateTask.reward)
async def task_reward(
    message: Message,
    state: FSMContext
):

    if not message.text.isdigit():

        await message.answer(
            "❌ Enter valid number"
        )

        return

    await state.update_data(
        reward=int(message.text)
    )

    await state.set_state(
        CreateTask.description
    )

    await message.answer(
        "📝 Send Task Description"
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
            "Type 'done' when finished"
        )
    )


@dp.message(CreateTask.questions)
async def task_questions(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    questions = data.get(
        "questions",
        []
    )

    if message.text.lower() == "done":

        if len(questions) < 1:

            await message.answer(
                "❌ Add at least 1 question"
            )

            return

        result = await tasks.insert_one({
            "title": data['title'],
            "reward": data['reward'],
            "description": data['description'],
            "questions": questions,
            "active": True
        })

        await state.clear()

        await message.answer(
            (
                "✅ Task Created\n\n"
                f"🆔 {result.inserted_id}\n"
                f"❓ Questions: {len(questions)}"
            )
        )

        return

    questions.append(message.text)

    await state.update_data(
        questions=questions
    )

    await message.answer(
        (
            f"✅ Question {len(questions)} Saved\n\n"
            f"❓ Send Question {len(questions)+1}\n"
            f"OR type done"
        )
    )


# ================= START SUBMISSION =================

@dp.message(F.text.regexp(r"^[0-9a-fA-F]{24}$"))
async def start_submit(
    message: Message,
    state: FSMContext
):

    task = await tasks.find_one({
        "_id": ObjectId(message.text),
        "active": True
    })

    if not task:
        return

    CURRENT_TASK[
        message.from_user.id
    ] = {
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
            f"{task['questions'][0]}"
        )
    )


# ================= SUBMIT ANSWERS =================

@dp.message(SubmitTask.answers)
async def process_answers(
    message: Message,
    state: FSMContext
):

    current = CURRENT_TASK[
        message.from_user.id
    ]

    task = await tasks.find_one({
        "_id": ObjectId(
            current['task_id']
        )
    })

    index = current['index']

    if index == 0:

        unique = message.text.lower().strip()

        exists = await submissions.find_one({
            "task_id": current['task_id'],
            "unique_key": unique
        })

        if exists:

            await message.answer(
                "❌ First answer already used"
            )

            return

    current['answers'].append(
        message.text
    )

    current['index'] += 1

    if current['index'] >= len(task['questions']):

        result = await submissions.insert_one({
            "user_id": message.from_user.id,
            "task_id": current['task_id'],
            "answers": current['answers'],
            "unique_key": current['answers'][0].lower().strip(),
            "status": "pending"
        })

        submission_id = str(
            result.inserted_id
        )

        answers_text = ""

        for i, ans in enumerate(
            current['answers'],
            start=1
        ):

            answers_text += (
                f"Q{i}: {ans}\n"
            )

        for admin in ADMIN_IDS:

            await bot.send_message(
                admin,
                (
                    f"📥 New Submission\n\n"
                    f"👤 {message.from_user.full_name}\n"
                    f"🆔 {message.from_user.id}\n\n"
                    f"{answers_text}"
                ),
                reply_markup=approve_buttons(
                    submission_id
                )
            )

        await state.clear()

        del CURRENT_TASK[
            message.from_user.id
        ]

        await message.answer(
            "✅ Task Submitted"
        )

        return

    await message.answer(
        task['questions'][
            current['index']
        ]
    )


# ================= APPROVE =================

@dp.callback_query(
    F.data.startswith("approve:")
)
async def approve_submission(
    call: CallbackQuery
):

    if call.from_user.id not in ADMIN_IDS:
        return

    submission_id = call.data.split(":")[1]

    submission = await submissions.find_one({
        "_id": ObjectId(submission_id)
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

    reward = task['reward']

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
            "_id": ObjectId(submission_id)
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
        "type": "reward"
    })

    await bot.send_message(
        submission['user_id'],
        (
            f"✅ Approved\n"
            f"💰 Reward Added: {reward}"
        )
    )

    await call.message.edit_text(
        call.message.text +
        "\n\n✅ APPROVED"
    )

    await call.answer()


# ================= REJECT =================

@dp.callback_query(
    F.data.startswith("reject:")
)
async def reject_submission(
    call: CallbackQuery
):

    if call.from_user.id not in ADMIN_IDS:
        return

    submission_id = call.data.split(":")[1]

    submission = await submissions.find_one({
        "_id": ObjectId(submission_id)
    })

    if not submission:
        return

    await submissions.update_one(
        {
            "_id": ObjectId(submission_id)
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

    await call.message.edit_text(
        call.message.text +
        "\n\n❌ REJECTED"
    )

    await call.answer()


# ================= WITHDRAW =================

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(
    call: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        WithdrawState.amount
    )

    await call.message.answer(
        "💰 Send Withdraw Amount"
    )

    await call.answer()


@dp.message(WithdrawState.amount)
async def withdraw_amount(
    message: Message,
    state: FSMContext
):

    if not message.text.isdigit():

        await message.answer(
            "❌ Invalid Amount"
        )

        return

    amount = int(message.text)

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

    result = await withdrawals.insert_one({
        "user_id": message.from_user.id,
        "amount": amount,
        "address": message.text,
        "status": "pending"
    })

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

    wid = str(result.inserted_id)

    for admin in ADMIN_IDS:

        await bot.send_message(
            admin,
            (
                f"💸 Withdrawal Request\n\n"
                f"👤 {message.from_user.id}\n"
                f"💰 {amount}\n"
                f"🏦 {message.text}"
            ),
            reply_markup=withdraw_buttons(
                wid
            )
        )

    await state.clear()

    await message.answer(
        "✅ Withdrawal Requested"
    )


# ================= SUPPORT =================

@dp.callback_query(F.data == "support")
async def support_start(
    call: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        SupportState.message
    )

    await call.message.answer(
        "✉ Send Support Message"
    )

    await call.answer()


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
