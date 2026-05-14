from aiogram.fsm.state import State
from aiogram.fsm.state import StatesGroup


class AddTaskState(StatesGroup):
    waiting_for_title = State()

    waiting_for_description = State()

    waiting_for_category = State()

    waiting_for_priority = State()

    waiting_for_date = State()

    waiting_for_time = State()

    waiting_for_repeat = State()
