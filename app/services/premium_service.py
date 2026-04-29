from app.repositories.supabase_repository import SupabaseRepository

class PremiumService:
    def __init__(self):
        self.repo = SupabaseRepository()

    def is_premium(self, user_id: str):
        if not user_id:
            return False

        user = self.repo.get_user(user_id)

        if not user:
            return False

        return user.get("plan") == "premium"