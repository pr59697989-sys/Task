from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URL

client = AsyncIOMotorClient(MONGO_URL)

db = client["task_bot"]

users = db["users"]
tasks = db["tasks"]
submissions = db["submissions"]
withdrawals = db["withdrawals"]
transactions = db["transactions"]
supports = db["supports"]
