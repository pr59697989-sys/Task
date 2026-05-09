from aiogram.fsm.state import State, StatesGroup


class CreateTask(StatesGroup):
    title = State()
    reward = State()
    description = State()
    question1 = State()
    question2 = State()
    question3 = State()
    question4 = State()


class SubmitTask(StatesGroup):
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()


class WithdrawState(StatesGroup):
    amount = State()
    address = State()


class SupportState(StatesGroup):
    message = State()
