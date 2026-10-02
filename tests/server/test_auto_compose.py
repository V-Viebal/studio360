from pathlib import Path

from server.services.auto_compose import _safe_output_name


def test_auto_compose_output_name_is_safe_and_stable() -> None:
    assert _safe_output_name("scripts/episode_1.json") == "auto_episode_1_final.mp4"
    assert _safe_output_name("scripts/episode 01 (draft).json") == "auto_episode_01_draft_final.mp4"
