"""Escalation: who gets told, and what the model is told to say (defect #2).

WHAT THIS PROVES
----------------
tool_escalate wrote a row to SQLite and returned "Confirm to the visitor that
someone will be in touch" - unconditionally, with nothing anywhere that told a
person. The row sat in a file nobody opens. The shipped escalations.db holds
one real member of the public who handed over their email on 2026-08-11 and
whom nobody has been told about, so this is not a hypothetical.

Two separate things are wrong and they are fixed separately:

  1. Nobody is notified. There is now a webhook, defaulting to unset.
  2. The model was instructed to promise a callback whether or not anyone
     could possibly make one. With no notifier configured that promise is a
     lie the model then repeats, warmly and convincingly, to a real human
     being. The note now says what is actually true.

Ordering is load-bearing: SAVE FIRST, NOTIFY SECOND. A webhook that times out
must never cost us the row.

The no-contact refusal path is pinned here as a REGRESSION test. It is correct
today and this change must not disturb it.

No network, no Qdrant, no OpenAI call, no cost. stdlib unittest only.

    python -m unittest test_tools_escalation -v
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

os.environ.setdefault("OPENAI_API_KEY", "test-key-never-used")


class _FakeStore:
    """app/store.py builds the QdrantStorage on first use. Seeded below so
    no test here can reach the real one. No test here touches retrieval."""

    def __init__(self, *a, **kw):
        pass

    def search(self, *a, **kw):
        raise AssertionError("no test in this file may touch the vector store")


import tpm_rag  # noqa: E402
from tpm_rag.store import QdrantStorage as _RealQdrantStorage  # noqa: E402

with mock.patch.object(tpm_rag, "QdrantStorage", _FakeStore):
    from app import prompts as prompts_mod  # noqa: E402
    from app import tools as tools_mod  # noqa: E402
    from app import store as store_mod  # noqa: E402

# app/store.py builds the store on first use rather than at import. The patch
# above DOES still reach it, for a reason no reader would guess: app.store is
# imported inside the block, and its `from tpm_rag import QdrantStorage` binds
# the fake permanently. That holds only while app.store keeps being imported
# in here - so assert it rather than trust it, and seed the cache as well.
# Either mechanism alone is enough; the assert is what fails closed if a later
# edit moves the import out and get_store() starts dialling QDRANT_URL for
# real, which is how an "offline" suite ends up reading the live corpus.
# Not `is _FakeStore`: `python -m unittest a b c` loads every module into one
# interpreter, so whichever file imports app.store first is the file whose fake
# is bound there. All of them raise on search(), so any fake is fine - what
# must never happen is the real class surviving into app.store.
assert store_mod.QdrantStorage is not _RealQdrantStorage, (
    "app.store captured the real QdrantStorage - it must be imported inside "
    "the mock.patch.object block above, or get_store() will connect for real"
)
store_mod._store = _FakeStore()

# A stand-in, never the deployed address. The real one lives in .env and
# nothing tracked in git may assert it.
TEST_CONTACT_EMAIL = "someone@example.invalid"


ARGS = {
    "summary": "wants a person",
    "name": "เรย์",
    "contact": "rei@example.com",
    "reason": "asked_for_person",
}
WEBHOOK = "https://hooks.example.com/tpm"


class EscalationNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tpm-escalation-")
        self.db = os.path.join(self.tmpdir, "escalations.db")
        patcher = mock.patch.object(tools_mod, "ESCALATIONS_DB", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        tools_mod.init_escalations()

    def rows(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT session_id, name, contact, reason, summary FROM"
                " escalations ORDER BY id"
            ).fetchall()
        finally:
            con.close()

    def test_row_saved_and_notifier_called(self):
        posted = []
        with mock.patch.object(tools_mod, "ESCALATION_WEBHOOK_URL", WEBHOOK), \
                mock.patch.object(
                    tools_mod, "_post_json",
                    lambda url, payload: posted.append((url, payload))):
            result = tools_mod.tool_escalate(ARGS, "sess-1", "u: ขอคุยกับคน")

        self.assertIs(result["saved"], True)
        self.assertEqual(len(self.rows()), 1)

        self.assertEqual(len(posted), 1)
        url, payload = posted[0]
        self.assertEqual(url, WEBHOOK)
        self.assertEqual(payload["contact"], "rei@example.com")
        self.assertEqual(payload["name"], "เรย์")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["reason"], "asked_for_person")
        self.assertEqual(payload["summary"], "wants a person")

        # Someone really was told, so the promise is now true.
        self.assertIn("in touch", result["note"])

    def test_row_survives_a_notifier_that_raises(self):
        """The row is the thing that must not be lost. Save first, notify
        second, and never let the second undo the first."""

        def boom(url, payload):
            raise OSError("connection refused")

        with mock.patch.object(tools_mod, "ESCALATION_WEBHOOK_URL", WEBHOOK), \
                mock.patch.object(tools_mod, "_post_json", boom):
            with self.assertLogs("app.tools", level="WARNING") as captured:
                result = tools_mod.tool_escalate(ARGS, "sess-2", "u: hello")

        self.assertIs(result["saved"], True)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "rei@example.com")

        logged = "\n".join(captured.output)
        self.assertIn("connection refused", logged)

        # The webhook failed, so nobody was told. The model must not say
        # otherwise just because a notifier was configured.
        self.assertNotIn("in touch", result["note"])

    def test_no_notifier_configured_means_no_callback_promise(self):
        with mock.patch.object(tools_mod, "ESCALATION_WEBHOOK_URL", ""), \
                mock.patch.object(
                    tools_mod, "_post_json",
                    mock.Mock(side_effect=AssertionError("must not be called"))):
            result = tools_mod.tool_escalate(ARGS, "sess-3", "u: hello")

        self.assertIs(result["saved"], True)
        self.assertEqual(len(self.rows()), 1)

        note = result["note"]
        self.assertNotIn("in touch", note)
        self.assertNotIn("will contact", note)
        self.assertIn("Do NOT promise", note)
        # ...and it must point somewhere instead of just refusing to promise.
        self.assertIn("contact", note.lower())

    def test_contact_route_configured_but_no_notifier_hands_over_the_route(self):
        """The deployed state: a contact route is set, no webhook is.

        This is the combination that actually ships. Nothing will deliver a
        callback, so the model must not offer one - but it does have somewhere
        real to send the visitor, and it must be told exactly where rather
        than left to remember that the system prompt mentioned it.
        """
        with mock.patch.multiple(
            prompts_mod,
            PRICING_URL="",
            CONTACT_URL="",
            CONTACT_EMAIL=TEST_CONTACT_EMAIL,
        ), mock.patch.object(tools_mod, "ESCALATION_WEBHOOK_URL", ""):
            result = tools_mod.tool_escalate(ARGS, "sess-5", "u: hello")

        note = result["note"]
        self.assertIs(result["saved"], True)
        self.assertIn(TEST_CONTACT_EMAIL, note)
        self.assertNotIn("in touch", note)
        self.assertIn("Do NOT promise", note)

    def test_note_carries_whatever_route_is_configured(self):
        """Whichever of the three is set is the one handed over."""
        with mock.patch.multiple(
            prompts_mod,
            PRICING_URL="",
            CONTACT_URL="https://example.invalid/contact",
            CONTACT_EMAIL="",
        ), mock.patch.object(tools_mod, "ESCALATION_WEBHOOK_URL", ""):
            note = tools_mod.tool_escalate(ARGS, "sess-6", "u: hello")["note"]

        self.assertIn("https://example.invalid/contact", note)

    # ------------------------------------------------------------------
    # REGRESSION ONLY. tools.py's no-contact refusal is correct and is
    # deliberately not changed by this commit.
    # ------------------------------------------------------------------
    def test_no_contact_still_refuses_and_writes_nothing(self):
        for contact in (None, "", "   "):
            with self.subTest(contact=contact):
                args = dict(ARGS, contact=contact)
                with mock.patch.object(
                        tools_mod, "ESCALATION_WEBHOOK_URL", WEBHOOK), \
                        mock.patch.object(
                            tools_mod, "_post_json",
                            mock.Mock(side_effect=AssertionError(
                                "nothing was saved, so nobody may be told"))):
                    result = tools_mod.tool_escalate(args, "sess-4", "u: hello")

                self.assertIs(result["saved"], False)
                self.assertIn("No contact given", result["note"])
                self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
