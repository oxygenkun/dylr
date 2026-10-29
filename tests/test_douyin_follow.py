# The cache clock is monotonic so system clock changes cannot extend hint lifetime.
# Error tests check invalidation without retaining a prior positive snapshot.
# Feed snapshots are validated before replacing the published revision.
# Exercise the production parser and cache with deterministic account-feed responses.
# The injected fetcher replaces only the outbound HTTP boundary.
# No real cookies, signed URLs or production configuration are needed.
# Test time is explicit so cache expiry does not depend on wall-clock scheduling.
# Multiple cache reads must share one HTTP request across all room workers.
# Missing rooms must return None, preserving the individual resolver fallback.
# Recommended rooms must not be promoted into the monitored follow set.
# Explicitly unfollowed entries cannot become positive recording hints.
# A live-only response with protobuf-default status zero is still accepted.
# Both stream maps are needed because the fork's selector can use either transport.
# A failed or empty response must drop stale positive results immediately.
# Cookie changes invalidate old responses even when the endpoint is unchanged.
# Shutdown invalidates reads even if a fresh cache is already available.
# Copy isolation prevents quality selection in one worker from mutating another worker.
# URL tests cover exact host and room matching without network resolution.
# Each monitor is created per test, with no daemon thread left running afterwards.
import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.monitors.douyin_follow import DouyinFollowMonitor, parse_follow_feed, room_id


def entry(rid: Any = "681134154663", name: Any = "test", recommend: Any = 0, followed: Any = 1, status: Any = 0) -> Any:
    return {
        "web_rid": rid,
        "is_recommend": recommend,
        "room": {
            "status": status,
            "title": "live",
            "owner": {"nickname": name, "follow_info": {"follow_status": followed}},
            "stream_url": {
                "flv_pull_url": {"ORIGIN": "https://example.test/live.flv"},
                "hls_pull_url_map": {"ORIGIN": "https://example.test/live.m3u8"},
            },
        },
    }


def payload(*items: Any) -> Any:
    return {"status_code": 0, "data": {"data": list(items)}}


class FollowTests(unittest.TestCase):
    def monitor(self, fetch: Any) -> Any:
        self.now = 100
        self.stop = threading.Event()
        m = DouyinFollowMonitor(self.stop, log=lambda *a, **kw: None, fetch=fetch, clock=lambda: self.now)
        m.configure(True, "private-cookie", 60)
        return m

    def test_normalize_zero_status_live_feed(self) -> Any:
        room = parse_follow_feed(payload(entry()))["681134154663"]
        self.assertEqual(room["status"], 2)
        self.assertEqual(room["anchor_name"], "test")
        self.assertTrue(room["stream_url"]["flv_pull_url"])

    def test_recommended_unfollowed_offline_and_bad_rooms_are_ignored(self) -> Any:
        bad = entry("6")
        bad["room"]["stream_url"] = {}
        rooms = parse_follow_feed(
            payload(
                entry(), entry("2", recommend=1), entry("3", followed=0), entry("4", status=4), entry("", name=""), bad
            )
        )
        self.assertEqual(list(rooms), ["681134154663"])

    def test_invalid_and_logged_out_responses_fail_closed(self) -> Any:
        for value in [{"status_code": 8}, {"status_code": 0}, [], {"status_code": 0, "data": {"unexpected": []}}]:
            with self.assertRaises(ValueError):
                parse_follow_feed(value)

    def test_room_matching_is_exact(self) -> Any:
        self.assertEqual(room_id("https://live.douyin.com/123/?foo=1"), "123")
        self.assertIsNone(room_id("https://other.test/123"))
        self.assertIsNone(room_id("https://live.douyin.com/root/live/123"))

    def test_shared_cache_serves_all_rooms_without_additional_requests(self) -> Any:
        fetch = Mock(return_value=payload(entry(), entry("2")))
        m = self.monitor(fetch)
        m.poll_once()
        for _ in range(3):
            self.assertIsNotNone(m.get_room("https://live.douyin.com/681134154663")[1])
            self.assertIsNotNone(m.get_room("https://live.douyin.com/2")[1])
        fetch.assert_called_once_with("private-cookie", "")

    def test_missing_room_returns_none_for_individual_fallback(self) -> Any:
        m = self.monitor(lambda *a: payload(entry()))
        m.poll_once()
        self.assertIsNone(m.get_room("https://live.douyin.com/999")[1])

    def test_cache_expiry_and_errors_trigger_fallback(self) -> Any:
        fetch = Mock(return_value=payload(entry()))
        m = self.monitor(fetch)
        m.poll_once()
        self.now += 81
        self.assertIsNone(m.get_room("https://live.douyin.com/681134154663")[1])
        self.now = 100
        fetch.side_effect = ValueError("bad response")
        m.poll_once()
        self.assertIsNone(m.get_room("https://live.douyin.com/681134154663")[1])

    def test_empty_feed_drops_old_positive_hint(self) -> Any:
        fetch = Mock(side_effect=[payload(entry()), payload()])
        m = self.monitor(fetch)
        m.poll_once()
        revision = m.get_room("https://live.douyin.com/681134154663")[0]
        m.poll_once()
        new_revision, room = m.get_room("https://live.douyin.com/681134154663")
        self.assertGreater(new_revision, revision)
        self.assertIsNone(room)

    def test_configuration_and_shutdown_invalidate_cache(self) -> Any:
        m = self.monitor(lambda *a: payload(entry()))
        m.poll_once()
        m.configure(True, "different-cookie", 60)
        self.assertIsNone(m.get_room("https://live.douyin.com/681134154663")[1])
        m.poll_once()
        self.stop.set()
        self.assertIsNone(m.get_room("https://live.douyin.com/681134154663")[1])

    def test_results_are_copied_before_stream_parser_mutates_them(self) -> Any:
        m = self.monitor(lambda *a: payload(entry()))
        m.poll_once()
        _, room = m.get_room("https://live.douyin.com/681134154663")
        room["stream_url"]["flv_pull_url"].clear()
        self.assertTrue(m.get_room("https://live.douyin.com/681134154663")[1]["stream_url"]["flv_pull_url"])


if __name__ == "__main__":
    unittest.main()
