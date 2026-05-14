from database.mongodb import tasks_collection
from datetime import datetime


async def create_task(data: dict):
    data["created_at"] = datetime.utcnow()

    result = await tasks_collection.insert_one(data)

    return str(result.inserted_id)


async def get_user_tasks(user_id: int):
    cursor = tasks_collection.find(
        {
            "user_id": user_id
        }
    ).sort("task_datetime", 1)

    return await cursor.to_list(length=100)
