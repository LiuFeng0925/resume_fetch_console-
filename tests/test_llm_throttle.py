import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from resume_parser import llm_client


def test_is_rate_limit_error_detects_common_shapes():
    class RateLimitError(Exception):
        pass

    assert llm_client._is_rate_limit_error(RateLimitError("429"))
    err429 = Exception("Error code: 429 - rate limited")
    err429.status_code = 429
    assert llm_client._is_rate_limit_error(err429)
    assert not llm_client._is_rate_limit_error(ValueError("bad json"))


def test_acquire_llm_throttle_slot_enforces_min_interval():
    llm_client._reset_llm_throttle_for_tests()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch.object(llm_client.time, "sleep", fake_sleep):
        with patch.object(llm_client.time, "monotonic", side_effect=[0.0, 0.5, 0.5]):
            llm_client._acquire_llm_throttle_slot()
            llm_client._acquire_llm_throttle_slot()

    assert sleeps == [pytest.approx(1.0)]


def test_extract_fields_rate_limit_backoff_before_retry():
    llm_client._reset_llm_throttle_for_tests()
    client = MagicMock()
    rate_err = Exception("Error code: 429 - rate limited")
    rate_err.status_code = 429
    ok_resp = MagicMock()
    ok_resp.choices = [MagicMock(message=MagicMock(content='{"name":"张三","phone":"1"}'))]

    call_count = {"n": 0}

    def fake_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise rate_err
        return ok_resp

    sleeps: list[float] = []

    with patch.object(llm_client, "_call_chat_completion", side_effect=fake_call):
        with patch.object(llm_client.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            result = llm_client.extract_fields(
                "姓名：张三\n电话：1",
                "test-model",
                client,
                max_retries=0,
            )

    assert result["name"] == "张三"
    assert sleeps == [2]
    assert call_count["n"] == 2


def test_concurrent_throttle_serializes_request_starts():
    llm_client._reset_llm_throttle_for_tests()
    starts: list[float] = []
    lock = threading.Lock()

    def mark_start():
        llm_client._acquire_llm_throttle_slot()
        with lock:
            starts.append(time.monotonic())

    threads = [threading.Thread(target=mark_start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(starts) == 2
    gap = starts[1] - starts[0]
    assert gap >= llm_client.LLM_MIN_REQUEST_INTERVAL - 0.05
