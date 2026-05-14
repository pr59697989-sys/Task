from database.mongodb import users_collection


async def create_user(user_id: int, username: str):
    existing = await users_collection.find_one({"user_id": user_id})

    if existing:
        return

    await users_collection.insert_one(
        {
            "user_id": user_id,
            "username": username,
            "timezone": "Asia/Kolkata",
            "created_at": None,
            "stats": {
                "completed": 0,
                "missed": 0,
                "streak": 0
            }
        }
    )
