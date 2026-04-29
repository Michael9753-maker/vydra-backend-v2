"""
reward_manager.py

Mock reward system for VYDRA.
Rewards are disabled for now (startup phase).

When REWARDS_LIVE = False:
- No database access
- No claims
- Safe mock responses
"""

from config import REWARDS_LIVE


def get_reward_overview(db=None) -> dict:
    """
    Returns reward stats for admin dashboard.
    Currently mocked.
    """
    if not REWARDS_LIVE:
        return {
            "enabled": False,
            "total_claims": 0,
            "claimed_today": 0,
            "pending_claims": 0,
            "reward_types": {
                "invite": 0,
                "visit": 0
            }
        }

    # Future real implementation goes here
    raise NotImplementedError("Live rewards not enabled yet")


def can_claim_reward(user_id: str, reward_key: str) -> bool:
    """
    Check if user can claim a reward.
    Always False while rewards are mocked.
    """
    if not REWARDS_LIVE:
        return False

    raise NotImplementedError("Live rewards not enabled yet")


def claim_reward(user_id: str, reward_key: str, reward_type: str) -> dict:
    """
    Claim a reward.
    Disabled while rewards are mocked.
    """
    if not REWARDS_LIVE:
        return {
            "success": False,
            "reason": "Rewards are not live yet"
        }

    raise NotImplementedError("Live rewards not enabled yet")
