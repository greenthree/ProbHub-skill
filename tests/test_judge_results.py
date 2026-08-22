import unittest

from probhub.judge_results import (
    classify_contestant_failure,
    classify_process_status,
    infer_failed_status,
    protocol_outcome,
)


class JudgeResultNormalizationTests(unittest.TestCase):
    def test_memory_verdict_requires_positive_enforced_evidence(self):
        self.assertEqual(infer_failed_status(-9, "", True, 255, 256), "MLE")
        self.assertEqual(infer_failed_status(-9, "", True, None, 256), "RE")
        self.assertEqual(infer_failed_status(-9, "", False, 300, 256), "RE")

    def test_process_status_maps_supervisor_reasons(self):
        self.assertEqual(classify_process_status({"reason": "time_limit"}, 256), "TLE")
        self.assertEqual(classify_process_status({"reason": "output_limit"}, 256), "OLE")
        self.assertEqual(classify_process_status({"reason": "memory_limit"}, 256), "MLE")
        self.assertEqual(
            classify_process_status({"reason": "process_cleanup_failed"}, 256),
            "FAIL",
        )
        self.assertEqual(
            classify_process_status({"reason": "completed", "returncode": 0}, 256),
            "AC",
        )

    def test_protocol_exit_codes_are_stable(self):
        self.assertEqual(protocol_outcome(0, " accepted ", "checker"), {
            "status": "AC",
            "verdict": "AC",
            "execution_status": "completed",
            "failure_kind": None,
            "actor": "session",
            "termination_reason": "completed",
            "message": "accepted",
        })
        self.assertEqual(protocol_outcome(43, "wrong", "interactor")["verdict"], "WA")
        failure = protocol_outcome(7, "", "interactor")
        self.assertEqual(failure["status"], "FAIL")
        self.assertEqual(failure["actor"], "interactor")
        self.assertIn("exited with code 7", failure["message"])

    def test_contestant_failure_fields_match_legacy_adapter(self):
        self.assertEqual(
            classify_contestant_failure("MLE", "inferred_memory_limit"),
            {
                "verdict": "MLE",
                "execution_status": "memory_limit",
                "failure_kind": "resource_limit",
                "actor": "contestant",
                "termination_reason": "inferred_memory_limit",
            },
        )
        self.assertEqual(
            classify_contestant_failure("FAIL", None),
            {
                "verdict": None,
                "execution_status": "start_error",
                "failure_kind": "control_failure",
                "actor": "supervisor",
                "termination_reason": "start_error",
            },
        )


if __name__ == "__main__":
    unittest.main()
