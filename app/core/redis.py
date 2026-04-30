import os
import redis
from datetime import datetime

def get_redis_client():
    redis_url = os.getenv("REDIS_URL")

    if redis_url:
        try:
            print("✅ Connecting to Redis via REDIS_URL")
            return redis.from_url(redis_url, decode_responses=True)
        except Exception as e:
            print("❌ Failed to connect using REDIS_URL:", e)

    # Fallback (local development)
    try:
        print("⚠️ Falling back to localhost Redis")
        return redis.Redis(
            host="localhost",
            port=6379,
            db=0,
            decode_responses=True
        )
    except Exception as e:
        print("❌ Local Redis connection failed:", e)
        return None


redis_client = get_redis_client()


def get_today_key(prefix: str, user_id: str) -> str:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return f"usage:{prefix}:{user_id}:{today}"