import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from bson import ObjectId

from config import BOT_TOKEN, ADMIN_IDS
from database import (
    users,
    tasks,
    submissions,
    withdrawals,
    transactions,
    supports
)
from keyboards import (
    main_menu,
    admin_menu,
    admin_submission_buttons,
    withdrawal_buttons
)
from states import (
    CreateTask,
    SubmitTask,
    WithdrawState,
    SupportState
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CURRENT_TASK = {}


@dp.message(CommandStart())
async def start(message: Message):

    user = await users.find_one({"user_id": message.from_user.id})

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
            reply_markup=main_menu
        )


@dp.message(F.text == "💰 Balance")
async def balance(message: Message):

    user = await users.find_one({"user_id": message.from_user.id})

    await message.answer(
        f"💰 Balance: {user['balance']}"
    )


@dp.message(F.text == "📋 Tasks")
async def show_tasks(message: Message):

    all_tasks = tasks.find({"active": True})

    text = "📋 Available Tasks\n\n"

    async for task in all_tasks:
        text += (
            f"🆔 {task['_id']}\n"
            f"📌 {task['title']}\n"
            f"💰 Reward: {task['reward']}\n\n"
        )

    text += "\nSend task ID to submit"

    await message.answer(text)


@dp.message(F.text == "➕ Create Task")
async def create_task_start(message: Message, state: FSMContext):

    if message.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(CreateTask.title)

    await message.answer("Enter task title")


@dp.message(CreateTask.title)
async def task_title(message: Message, state: FSMContext):

    await state.update_data(title=message.text)

    await state.set_state(CreateTask.reward)

    await message.answer("Enter reward")


@dp.message(CreateTask.reward)
async def task_reward(message: Message, state: FSMContext):

    await state.update_data(reward=int(message.text))

    await state.set_state(CreateTask.description)

    await message.answer("Enter description")


@dp.message(CreateTask.description)
async def task_desc(message: Message, state: FSMContext):

    await state.update_data(description=message.text)

    await state.set_state(CreateTask.question1)

    await message.answer("Enter Question 1")


@dp.message(CreateTask.question1)
async def q1(message: Message, state: FSMContext):

    await state.update_data(q1=message.text)

    await state.set_state(CreateTask.question2)

    await message.answer("Enter Question 2")


@dp.message(CreateTask.question2)
async def q2(message: Message, state: FSMContext):

    await state.update_data(q2=message.text)

    data = await state.get_data()

    await tasks.insert_one({
        "title": data['title'],
        "reward": data['reward'],
        "description": data['description'],
        "questions": [
            data['q1'],
            data['q2']
        ],
        "active": True
    })

    await state.clear()

    await message.answer("✅ Task Created")


@dp.message(F.text.regexp(r"^[0-9a-fA-F]{24}$"))
async def task_submit_start(message: Message, state: FSMContext):

    task = await tasks.find_one({
        "_id": ObjectId(message.text)
    })

    if not task:
        return

    CURRENT_TASK[message.from_user.id] = str(task['_id'])

    await state.set_state(SubmitTask.q1)

    await message.answer(task['questions'][0])


@dp.message(SubmitTask.q1)
async def submit_q1(message: Message, state: FSMContext):

    normalized = message.text.lower().strip()

    task_id = CURRENT_TASK[message.from_user.id]

    existing = await submissions.find_one({
        "task_id": task_id,
        "unique_key": normalized
    })

    if existing:
        await message.answer(
            "❌ First answer already used"
        )
        return

    await state.update_data(q1=message.text)

    task = await tasks.find_one({
        "_id": ObjectId(task_id)
    })

    await state.set_state(SubmitTask.q2)

    await message.answer(task['questions'][1])


@dp.message(SubmitTask.q2)
async def submit_q2(message: Message, state: FSMContext):

    data = await state.get_data()

    task_id = CURRENT_TASK[message.from_user.id]

    task = await tasks.find_one({
        "_id": ObjectId(task_id)
    })

    result = await submissions.insert_one({
        "user_id": message.from_user.id,
        "task_id": task_id,
        "answers": {
            "q1": data['q1'],
            "q2": message.text
        },
        "unique_key": data['q1'].lower().strip(),
        "status": "pending"
    })

    submission_id = str(result.inserted_id)

    for admin in ADMIN_IDS:

        await bot.send_message(
            admin,
            (
                f"📥 New Submission\n\n"
                f"👤 User: {message.from_user.full_name}\n"
                f"🆔 {message.from_user.id}\n"
                f"📌 Task: {task['title']}\n\n"
                f"Q1: {data['q1']}\n"
                f"Q2: {message.text}"
            ),
            reply_markup=admin_submission_buttons(submission_id)
        )

    await state.clear()

    await message.answer(
        "✅ Submitted Successfully"
    )


@dp.callback_query(F.data.startswith("approve:"))
async def approve_submission(call: CallbackQuery):

    if call.from_user.id not in ADMIN_IDS:
        return

    submission_id = call.data.split(":")[1]

    submission = await submissions.find_one({
        "_id": ObjectId(submission_id)
    })

    if not submission:
        return

    if submission['status'] != 'pending':
        return

    task = await tasks.find_one({
        "_id": ObjectId(submission['task_id'])
    })

    reward = task['reward']

    await users.update_one(
        {"user_id": submission['user_id']},
        {
            "$inc": {
                "balance": reward,
                "total_earnings": reward
            }
        }
    )

    await submissions.update_one(
        {"_id": ObjectId(submission_id)},
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
        f"✅ Submission Approved\n💰 Reward Added: {reward}"
    )

    await call.message.edit_text(
        call.message.text + "\n\n✅ APPROVED"
    )


@dp.callback_query(F.data.startswith("reject:"))
async def reject_submission(call: CallbackQuery):

    if call.from_user.id not in ADMIN_IDS:
        return

    submission_id = call.data.split(":")[1]

    submission = await submissions.find_one({
        "_id": ObjectId(submission_id)
    })

    await submissions.update_one(
        {"_id": ObjectId(submission_id)},
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
        call.message.text + "\n\n❌ REJECTED"
    )


@dp.message(F.text == "💸 Withdraw")
async def withdraw_start(message: Message, state: FSMContext):

    await state.set_state(WithdrawState.amount)

    await message.answer("Enter withdrawal amount")


@dp.message(WithdrawState.amount)
async def withdraw_amount(message: Message, state: FSMContext):

    amount = int(message.text)

    user = await users.find_one({
        "user_id": message.from_user.id
    })

    if user['balance'] < amount:
        await message.answer("❌ Low Balance")
        return

    await state.update_data(amount=amount)

    await state.set_state(WithdrawState.address)

    await message.answer(
        "Enter USDT BEP20 Address"
    )


@dp.message(WithdrawState.address)
async def withdraw_address(message: Message, state: FSMContext):

    data = await state.get_data()

    amount = data['amount']

    result = await withdrawals.insert_one({
        "user_id": message.from_user.id,
        "amount": amount,
        "address": message.text,
        "status": "pending"
    })

    await users.update_one(
        {"user_id": message.from_user.id},
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
                f"👤 User ID: {message.from_user.id}\n"
                f"💰 Amount: {amount}\n"
                f"🏦 Address: {message.text}"
            ),
            reply_markup=withdrawal_buttons(wid)
        )

    await state.clear()

    await message.answer(
        "✅ Withdrawal Requested"
    )


@dp.callback_query(F.data.startswith("wapprove:"))
async def approve_withdraw(call: CallbackQuery):

    wid = call.data.split(":")[1]

    await withdrawals.update_one(
        {"_id": ObjectId(wid)},
        {
            "$set": {
                "status": "approved"
            }
        }
    )

    await call.message.edit_text(
        call.message.text + "\n\n✅ WITHDRAW APPROVED"
    )


@dp.callback_query(F.data.startswith("wreject:"))
async def reject_withdraw(call: CallbackQuery):

    wid = call.data.split(":")[1]

    withdrawal = await withdrawals.find_one({
        "_id": ObjectId(wid)
    })

    await users.update_one(
        {"user_id": withdrawal['user_id']},
        {
            "$inc": {
                "balance": withdrawal['amount']
            }
        }
    )

    await withdrawals.update_one(
        {"_id": ObjectId(wid)},
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

    await call.message.edit_text(
        call.message.text + "\n\n❌ WITHDRAW REJECTED"
    )


@dp.message(F.text == "☎ Support")
async def support_start(message: Message, state: FSMContext):

    await state.set_state(SupportState.message)

    await message.answer(
        "✉ Send support message"
    )


@dp.message(SupportState.message)
async def support_message(message: Message, state: FSMContext):

    await supports.insert_one({
        "user_id": message.from_user.id,
        "message": message.text
    })

    for admin in ADMIN_IDS:

        await bot.send_message(
            admin,
            (
                f"☎ Support Ticket\n\n"
                f"👤 User: {message.from_user.id}\n\n"
                f"{message.text}"
            )
        )

    await state.clear()

    await message.answer(
        "✅ Support Message Sent"
    )


async def main():

    print("Bot Started")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
