def count_user_downloads_today(self, user_id, date):
    response = (
        self.client
        .table("downloads")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("created_at", f"{date}T00:00:00")
        .lte("created_at", f"{date}T23:59:59")
        .execute()
    )

    return response.count or 0