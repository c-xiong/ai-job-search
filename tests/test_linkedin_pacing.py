import subprocess
import unittest
from unittest import mock

from tools import collectors


class LinkedInPacing(unittest.TestCase):
    def setUp(self):
        collectors._linkedin_last[0] = 0.0
        env = mock.patch.dict("os.environ", {"LINKEDIN_PACE": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_second_request_waits_for_the_gap(self):
        with mock.patch.object(collectors.time, "sleep") as sleep, \
                mock.patch.object(collectors.random, "uniform", return_value=4.0):
            collectors.linkedin_pace()
            sleep.assert_not_called()
            collectors.linkedin_pace()
            self.assertEqual(sleep.call_count, 1)
            self.assertGreater(sleep.call_args[0][0], 3.5)

    def test_detail_429_cools_down_once_then_retries(self):
        limited = subprocess.CompletedProcess([], 1, "", '{"code":"RATE_LIMITED"}')
        ok = subprocess.CompletedProcess([], 0, '{"description":"English only role"}', "")
        budget = collectors.Budget(5)
        record = {"portal": "linkedin-search", "id": "123"}
        with mock.patch.object(collectors.time, "sleep") as sleep, \
                mock.patch.object(collectors.subprocess, "run", side_effect=[limited, ok]):
            status, _note, text = collectors.screen(record, lambda *_: None, budget)
        self.assertEqual(text, "English only role")
        self.assertIn(mock.call(collectors.LINKEDIN_COOLDOWN), sleep.call_args_list)
        self.assertTrue(budget.cooled)
        self.assertFalse(budget.blocked)
        # A second 429 in the same run blocks instead of cooling down again.
        with mock.patch.object(collectors.time, "sleep"), \
                mock.patch.object(collectors.subprocess, "run", return_value=limited):
            collectors.screen(record, lambda *_: None, budget)
        self.assertTrue(budget.blocked)


if __name__ == "__main__":
    unittest.main()
