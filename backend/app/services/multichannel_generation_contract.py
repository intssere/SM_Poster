"""Phase C review-only social channel generation contract.

This module extends the existing AI creative service's validation allowlist without
changing provider selection, publishing, scheduling, approval, or reconciliation
behavior. Importing it is intentionally side-effectful so the legacy service keeps
one validation path while Phase C adds LinkedIn as a review-only target.
"""
from __future__ import annotations

from app.services.ai_creative_generation import CHANNELS as AI_GENERATION_CHANNELS


REVIEW_GENERATION_CHANNELS = frozenset({
    "pinterest",
    "instagram",
    "facebook",
    "linkedin",
    "tiktok",
    "youtube_shorts",
})


def install_multichannel_generation_contract() -> frozenset[str]:
    AI_GENERATION_CHANNELS.update(REVIEW_GENERATION_CHANNELS)
    return REVIEW_GENERATION_CHANNELS


install_multichannel_generation_contract()
