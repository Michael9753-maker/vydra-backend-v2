import redis
from datetime import datetime

redis_client = redis.Redis(
    host="localhost",
    port=6379,
    db=0,
    decode_responses=True  # return strings not bytes
)


def get_today_key(prefix: str, user_id: str) -> str:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return f"usage:{prefix}:{user_id}:{today}"