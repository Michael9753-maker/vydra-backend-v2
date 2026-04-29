import json
import redis
from datetime import datetime

redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


class JobStore:

    @staticmethod
    def create_job(job_id: str, user_id: str, url: str):
        job_data = {
            "job_id": job_id,
            "user_id": user_id,
            "url": url,
            "status": "queued",
            "file": "",
            "error": "",
            "attempts": 0,
            "created_at": datetime.utcnow().isoformat(),
            "finished_at": ""
        }
        redis_client.set(f"job:{job_id}", json.dumps(job_data))

    @staticmethod
    def get_job(job_id: str):
        data = redis_client.get(f"job:{job_id}")
        if not data:
            return None
        return json.loads(data)

    @staticmethod
    def update_job(job_id: str, updates: dict):
        job = JobStore.get_job(job_id)
        if not job:
            return None

        job.update(updates)
        redis_client.set(f"job:{job_id}", json.dumps(job))
        return job