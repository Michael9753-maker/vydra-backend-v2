from app.core.redis import redis_client, get_today_key


class UsageService:

    DOWNLOAD_LIMIT_FREE = 50
    DOWNLOAD_LIMIT_PREMIUM = 1000  # effectively unlimited

    @staticmethod
    def check_and_increment_download(user_id: str, is_premium: bool):
        key = get_today_key("download", user_id)

        limit = (
            UsageService.DOWNLOAD_LIMIT_PREMIUM
            if is_premium
            else UsageService.DOWNLOAD_LIMIT_FREE
        )

        # 🚨 If Redis is not available → allow request (fail-safe mode)
        if redis_client is None:
            print("⚠️ Redis unavailable → bypassing usage limits")
            return True, 1, limit

        try:
            current = redis_client.get(key)
            current = int(current) if current else 0

            if current >= limit:
                return False, current, limit

            pipe = redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 86400)  # 24 hours
            pipe.execute()

            return True, current + 1, limit, "redis"

        except Exception as e:
            # 🚨 If Redis crashes during operation → fallback
            print("❌ Redis error in UsageService:", str(e))
            print("⚠️ Switching to safe fallback mode")

            return True, 1, limit, "fallback"