from __future__ import annotations

import logging
import os

from app.core.redis import get_today_key, redis_client

logger = logging.getLogger(__name__)


class UsageService:
    DOWNLOAD_LIMIT_FREE = int(os.getenv("DOWNLOAD_LIMIT_FREE", 25))
    DOWNLOAD_LIMIT_PREMIUM = int(os.getenv("DOWNLOAD_LIMIT_PREMIUM", 1000))

    @staticmethod
    def check_and_increment_download(user_id: str, is_premium: bool):
        if not user_id:
            user_id = "guest_user"

        if redis_client is None:
            raise Exception("Redis is required but not initialized")

        key = get_today_key("download", user_id)

        limit = (
            UsageService.DOWNLOAD_LIMIT_PREMIUM
            if is_premium
            else UsageService.DOWNLOAD_LIMIT_FREE
        )

        current = 0

        try:
            raw_current = redis_client.get(key)
            current = int(raw_current) if raw_current else 0

            if current >= limit:
                logger.info("📊 Usage limit reached: %s/%s | user=%s", current, limit, user_id)
                print(f"📊 Usage limit reached: {current}/{limit} | user={user_id}")
                return False, current, limit, "redis"

            pipe = redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 86400)  # 24 hours
            pipe.execute()

            new_usage = current + 1

            logger.info("📊 Usage: %s/%s | user=%s", new_usage, limit, user_id)
            print(f"📊 Usage: {new_usage}/{limit} | user={user_id}")

            return True, new_usage, limit, "redis"

        except Exception as e:
            logger.exception("❌ Redis failure in UsageService: %s", e)
            print("❌ Redis failure (blocking request):", str(e))
            return False, current, limit, "error"