from motor.motor_asyncio import AsyncIOMotorClient
from config.settings import MONGO_URI, DATABASE_NAME

client = AsyncIOMotorClient(MONGO_URI)

db = client[DATABASE_NAME]

users_collection = db.users

tasks_collection = db.tasks
