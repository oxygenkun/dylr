from __future__ import annotations

import time
from dataclasses import FrozenInstanceError

import pytest

from src.danmaku.plan import DanmakuPlan
from src.recording.plan import RecordPlan
from src.recording.signals import LiveHint, RoomAdded, RoomConfig, RoomObservation


def test_room_config_and_observation_carry_required_fields() -> None:
    config = RoomConfig(url="https://live.douyin.com/1", quality="原画", anchor_name="主播")
    observation = RoomObservation(
        room_key="douyin:1",
        url=config.url,
        quality=config.quality,
        platform="抖音直播",
        live=True,
        observed_at=time.monotonic(),
        anchor_name=config.anchor_name,
        port_info={"flv_url": "https://example.com/live.flv"},
    )

    assert observation.room_key == "douyin:1"
    assert observation.port_info == {"flv_url": "https://example.com/live.flv"}
    assert observation.source == "room"


def test_hint_and_added_keep_source_semantics() -> None:
    hint = LiveHint(room_key="douyin:1", live=True, revision=3, observed_at=1.0)
    added = RoomAdded(url="https://live.douyin.com/2", quality="高清", anchor_name="另一位主播")

    assert hint.source == "follow"
    assert added.source == "dm"
    assert added.persist is True


def test_signals_are_immutable() -> None:
    hint = LiveHint(room_key="douyin:1", live=False, revision=0, observed_at=1.0)

    with pytest.raises(FrozenInstanceError):
        hint.live = True  # type: ignore[misc]


def test_record_plan_links_observation_and_danmaku_plan() -> None:
    observation = RoomObservation(
        room_key="douyin:1",
        url="https://live.douyin.com/1",
        quality="原画",
        platform="抖音直播",
        live=True,
        observed_at=1.0,
    )
    danmaku = DanmakuPlan(
        write_srt=True,
        monitor_only=False,
        split_seconds=None,
        room_name="序号1 主播",
        base_filename="/tmp/room",
        args={"room_id": "1"},
    )
    plan = RecordPlan(
        observation=observation,
        source_url="https://example.com/live.flv",
        user_agent="ua",
        headers=None,
        quality_code="origin",
        danmaku=danmaku,
    )

    assert plan.observation is observation
    assert plan.danmaku == danmaku
    assert plan.danmaku.args["room_id"] == "1"
