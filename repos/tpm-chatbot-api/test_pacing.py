"""The behavioural suite must stay under the server's own rate limit.

This exists because it already went wrong. `test_chat.py` fired ~20 requests at
/chat back to back while the API shipped CHAT_RATE_LIMIT_PER_MIN=6, so the last
two thirds returned HTTP 429 and the suite could not pass in a single run. It
had been that way since the limiter was added and stayed invisible until
VERIFY.ps1 learned to read exit codes - at which point it looked like a
regression in the agent rather than a collision between two correct components.

Prose in a docstring would have rotted. This asserts it.

Free: simulated clock, no API, no network, no spend.
"""

import pathlib
import re
import unittest

import test_chat


class PacingStaysUnderTheServerLimit(unittest.TestCase):
    def setUp(self):
        self._real_time, self._real_sleep = test_chat.time.time, test_chat.time.sleep
        self.clock = [1000.0]
        test_chat.time.time = lambda: self.clock[0]
        test_chat.time.sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        test_chat._sent.clear()

    def tearDown(self):
        test_chat.time.time, test_chat.time.sleep = self._real_time, self._real_sleep
        test_chat._sent.clear()

    def _land(self, n):
        """Send n paced requests; return the time each one landed."""
        out = []
        for _ in range(n):
            test_chat._pace()
            out.append(self.clock[0])
        return out

    def _worst_window(self, landed):
        """The most requests that ever fall inside one 60s window."""
        return max(sum(1 for u in landed if t - 60.0 < u <= t) for t in landed)

    def test_full_run_never_exceeds_the_limit(self):
        # A full run is 12 cases and ~20 requests, since several cases are
        # multi-turn and every turn is its own POST.
        landed = self._land(20)
        self.assertLessEqual(self._worst_window(landed), test_chat.RATE)

    def test_a_short_run_does_not_wait_at_all(self):
        # --only, and the first few cases of any run, must stay instant. A pacer
        # that sleeps when it does not need to would make every developer
        # disable it, which is how this protection gets lost.
        landed = self._land(test_chat.RATE)
        self.assertEqual(landed[0], landed[-1])

    def test_the_limit_is_read_from_RATE_not_hardcoded(self):
        # --rate exists so the suite can follow a limit raised server-side.
        # If _pace ignored RATE, this would pace to 6 and pass by accident.
        original = test_chat.RATE
        try:
            test_chat.RATE = 3
            self.assertLessEqual(self._worst_window(self._land(10)), 3)
        finally:
            test_chat.RATE = original

    def test_it_never_paces_faster_than_the_server_allows(self):
        # Read the server's own default rather than keeping a second copy of the
        # number here. If the API's default drops and this does not follow, the
        # suite starts 429ing again - the failure this file exists to prevent.
        #
        # Read from the source text, not `from app.api import ...`. Importing
        # that module used to construct a QdrantStorage and open a connection,
        # putting a live database behind an offline test - doing exactly that
        # is how this assertion was first written, and it passed only because
        # test_api happened to be imported first. app/store.py has since made
        # the store lazy, so the connection is gone, but the import still
        # pulls in OpenAI, FastAPI and the whole agent to read one integer.
        #
        # Less-than-or-equal, not equal: pacing slower than the server allows
        # only wastes time, while pacing faster is the defect.
        source = pathlib.Path(__file__).with_name("app") / "api.py"
        found = re.search(
            r'CHAT_RATE_LIMIT_PER_MIN\s*=\s*int\(\s*os\.getenv\(\s*'
            r'"CHAT_RATE_LIMIT_PER_MIN"\s*,\s*"(\d+)"',
            source.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(
            found,
            f"could not find the CHAT_RATE_LIMIT_PER_MIN default in {source} - "
            "if it moved or was rewritten, update this check rather than "
            "deleting it",
        )
        server_default = int(found.group(1))
        self.assertLessEqual(
            test_chat.RATE,
            server_default,
            f"test_chat paces at {test_chat.RATE}/min but the API's default is "
            f"{server_default}/min - the suite will 429",
        )


if __name__ == "__main__":
    unittest.main()
