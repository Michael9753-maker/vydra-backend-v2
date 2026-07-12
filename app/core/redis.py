import os
import redis

REDIS_URL = os.getenv("REDIS_URL")

if not REDIS_URL:
    raise Exception("❌ REDIS_URL is not set")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

print("✅ Connected to Redis:", REDIS_URL)