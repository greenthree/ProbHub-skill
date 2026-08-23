import json
import unittest

from probhub.webui_judge_protocol import (
    apply_sandbox_event,
    consume_jsonl_chunk,
    empty_sandbox_result,
    finalize_judge_protocol,
    sandbox_log_line,
    submission_verdict,
)


class _Log:
    def __init__(self):
        self.lines = []

    def append(self, value):
        self.lines.append(str(value))


def _event(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"


class WebUiJudgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.result = empty_sandbox_result({"limits": {"time": 2, "memory": 512}})
        self.log = _Log()
        self.state = {}

    def test_split_utf8_event_is_reassembled_without_replacement(self):
        payload = {
            "type": "case",
            "kind": "std",
            "program": "answer.cpp",
            "case": "sample/1",
            "status": "AC",
            "time": 0.125,
            "message": "中文输出",
        }
        encoded = _event(payload)
        split = encoded.index("中".encode("utf-8")) + 1
        consume_jsonl_chunk(self.result, self.log, self.state, encoded[:split])
        self.assertEqual(self.result["cases"], [])
        consume_jsonl_chunk(self.result, self.log, self.state, encoded[split:], final=True)
        self.assertEqual(self.result["cases"][0]["message"], "中文输出")
        self.assertNotIn("�", "\n".join(self.log.lines))

    def test_nonfinal_event_budget_truncates_details_but_keeps_final(self):
        detail = _event({"type": "case", "status": "AC", "message": "x" * 100})
        final = _event({"type": "final", "ok": True, "status": "passed", "code": "ok"})
        consume_jsonl_chunk(
            self.result,
            self.log,
            self.state,
            detail + final,
            final=True,
            event_limit_bytes=len(detail) - 1,
        )
        self.assertTrue(self.result["details_truncated"])
        self.assertEqual(self.result["cases"], [])
        self.assertEqual(self.result["final"]["code"], "ok")

    def test_malformed_json_is_sticky_and_cannot_be_followed_by_success(self):
        consume_jsonl_chunk(
            self.result,
            self.log,
            self.state,
            b"not-json\n" + _event({"type": "final", "ok": True, "code": "ok"}),
            final=True,
        )
        self.assertEqual(self.state["protocol_error"], "malformed_json")
        self.assertFalse(self.result["final"]["ok"])
        self.assertEqual(self.result["final"]["code"], "judge_protocol_error")

    def test_non_object_json_is_rejected(self):
        consume_jsonl_chunk(self.result, self.log, self.state, b"[]\n", final=True)
        self.assertEqual(self.state["protocol_error"], "event_not_object")
        self.assertEqual(self.result["final"]["code"], "judge_protocol_error")

    def test_invalid_utf8_is_rejected(self):
        consume_jsonl_chunk(self.result, self.log, self.state, b"\xff\n", final=True)
        self.assertEqual(self.state["protocol_error"], "invalid_utf8")
        self.assertEqual(self.result["final"]["code"], "judge_protocol_error")

    def test_duplicate_final_is_rejected(self):
        final = _event({"type": "final", "ok": True, "code": "ok"})
        consume_jsonl_chunk(self.result, self.log, self.state, final + final, final=True)
        self.assertEqual(self.state["protocol_error"], "duplicate_final_event")
        self.assertFalse(self.result["final"]["ok"])
        self.assertEqual(self.result["final"]["code"], "judge_protocol_error")

    def test_oversized_final_is_rejected(self):
        final = _event({"type": "final", "ok": True, "message": "x" * 100})
        consume_jsonl_chunk(
            self.result,
            self.log,
            self.state,
            final,
            final=True,
            final_event_limit_bytes=32,
        )
        self.assertEqual(self.state["protocol_error"], "task_event_limit")
        self.assertEqual(self.result["final"]["code"], "task_event_limit")

    def test_finalize_rejects_missing_final_and_exit_mismatch(self):
        self.assertEqual(
            finalize_judge_protocol(self.result, self.state, 0),
            "protocol_error",
        )
        self.assertEqual(self.result["final"]["code"], "missing_final_event")

        result = empty_sandbox_result({})
        state = {}
        consume_jsonl_chunk(
            result,
            _Log(),
            state,
            _event({"type": "final", "ok": True, "code": "ok"}),
            final=True,
        )
        self.assertEqual(
            finalize_judge_protocol(result, state, 1),
            "protocol_error",
        )
        self.assertEqual(result["final"]["code"], "judge_protocol_error")

    def test_finalize_maps_pipe_read_failure(self):
        self.assertEqual(
            finalize_judge_protocol(self.result, self.state, 0, read_error=True),
            "protocol_error",
        )
        self.assertEqual(self.result["final"]["code"], "judge_protocol_error")

    def test_result_normalization_and_log_format_are_core_owned(self):
        apply_sandbox_event(
            self.result,
            {
                "type": "compile",
                "kind": "std",
                "file": "code/std.cpp",
                "ok": True,
            },
        )
        apply_sandbox_event(
            self.result,
            {
                "type": "case",
                "kind": "std",
                "program": "answer.cpp",
                "case": "secret/1",
                "status": "AC",
                "time": 0.01,
            },
        )
        self.assertEqual(submission_verdict(self.result), "AC")
        self.assertIn("compile:OK", sandbox_log_line({"type": "compile", "ok": True}))

    def test_malformed_event_fields_are_bounded_without_crashing_consumer(self):
        payload = {
            "type": "case",
            "time": "not-a-number",
            "status": "AC",
        }
        consume_jsonl_chunk(self.result, self.log, self.state, _event(payload), final=True)
        self.assertEqual(self.result["cases"][0]["time"], 0.0)
        self.assertIn("0.000s", sandbox_log_line(payload))


if __name__ == "__main__":
    unittest.main()
