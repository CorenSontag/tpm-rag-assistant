"""API-level behaviour that is invisible until it costs someone something.

Three things live here:

  * what an error response says. Interpolating the caught exception into the
    body handed the caller the Qdrant URL, the OpenAI error detail and
    whatever else the exception happened to carry. The operator needs that;
    the internet does not. It goes to the log, and the response carries a
    correlation id that ties the two together.

  * that /escalations declares its charset. It returned a bare JSONResponse,
    bypassing UTF8JSONResponse - so Thai names arrive as mojibake in Windows
    PowerShell 5.1, which is the exact client that class exists to protect
    and the exact way an operator reads this endpoint.

  * the startup warning when no contact route is configured. A deploy with
    blank PRICING_URL / CONTACT_URL / CONTACT_EMAIL is currently SILENT: the
    assistant declines questions it cannot answer and offers no next step,
    and an escalation has nowhere to point anyone. The values are the
    operator's to supply; being told they are missing is ours.

  * _contact_line()'s joining, because that string is read by the model at
    decision time and a malformed one is a behaviour change, not a typo.

No network, no Qdrant, no OpenAI call, no cost. stdlib unittest only.

    python -m unittest test_api -v
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

# app/api.py and app/agent.py both build an OpenAI client at import time,
# which raises without a key present. Neither is ever called here.
os.environ.setdefault("OPENAI_API_KEY", "test-key-never-used")


class _FakeStore:
    """Stands in for QdrantStorage.

    The store is built on first use by app/store.py, not at module scope.
    Seeding app.store._store below keeps this test hermetic and fast whether
    or not the tpm-qdrant container happens to be running.
    """

    def __init__(self, *a, **kw):
        self.client = None
        self.collection = "docs"

    def search(self, *a, **kw):
        raise AssertionError("no test in this file may touch the vector store")


import tpm_rag  # noqa: E402
from tpm_rag.store import QdrantStorage as _RealQdrantStorage  # noqa: E402

with mock.patch.object(tpm_rag, "QdrantStorage", _FakeStore):
    from app import api as api_mod  # noqa: E402
    from app import prompts as prompts_mod  # noqa: E402
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

from fastapi.testclient import TestClient  # noqa: E402

# Things that must never reach a caller. Each one is real: the Qdrant URL is
# in every connection error, and an OpenAI error message can carry the tail of
# the key that failed.
SECRETS = (
    "localhost:6333",
    "qdrant",
    "sk-live-abcdef",
    "Connection refused",
)


class ContactConfigWarningTest(unittest.TestCase):
    """The unblocked half of defect #4: say something when nothing is set."""

    def _with_contact(self, pricing="", contact="", email=""):
        return mock.patch.multiple(
            api_mod,
            PRICING_URL=pricing,
            CONTACT_URL=contact,
            CONTACT_EMAIL=email,
        )

    def test_warns_when_no_contact_route_is_configured(self):
        with self._with_contact():
            with self.assertLogs("tpm-api", level="WARNING") as captured:
                api_mod.warn_if_no_contact_configured()
        message = "\n".join(captured.output)
        self.assertIn("CONTACT_URL", message)
        self.assertIn("PRICING_URL", message)
        self.assertIn("CONTACT_EMAIL", message)

    def test_silent_when_any_one_is_configured(self):
        for kwargs in (
            {"email": "hello@example.com"},
            {"contact": "https://example.com/contact"},
            {"pricing": "https://example.com/pricing"},
        ):
            with self.subTest(**kwargs):
                with self._with_contact(**kwargs):
                    # assertNoLogs is the assertion; a warning here would be
                    # noise on a correctly configured deploy.
                    with self.assertNoLogs("tpm-api", level="WARNING"):
                        api_mod.warn_if_no_contact_configured()


class ContactLineTest(unittest.TestCase):
    """_contact_line() is prompt text. Its joining is behaviour."""

    def _with_contact(self, pricing="", contact="", email=""):
        return mock.patch.multiple(
            prompts_mod,
            PRICING_URL=pricing,
            CONTACT_URL=contact,
            CONTACT_EMAIL=email,
        )

    def test_email_only(self):
        with self._with_contact(email="hello@example.com"):
            self.assertEqual(
                prompts_mod._contact_line(), "email: hello@example.com"
            )

    def test_url_only(self):
        with self._with_contact(contact="https://example.com/contact"):
            self.assertEqual(
                prompts_mod._contact_line(),
                "contact page: https://example.com/contact",
            )

    def test_all_three_join_in_a_fixed_order(self):
        with self._with_contact(
            pricing="https://example.com/pricing",
            contact="https://example.com/contact",
            email="hello@example.com",
        ):
            self.assertEqual(
                prompts_mod._contact_line(),
                "pricing page: https://example.com/pricing; "
                "contact page: https://example.com/contact; "
                "email: hello@example.com",
            )

    def test_none_set_tells_the_model_it_has_nowhere_to_point(self):
        with self._with_contact():
            line = prompts_mod._contact_line()
        self.assertIn("no contact links configured", line)
        self.assertNotIn(";", line)


class ResponseHygieneTest(unittest.TestCase):
    """Defects #10 and #13: what leaks out, and what arrives mangled."""

    def setUp(self):
        self.client = TestClient(api_mod.app)
        # The rate limiter is process-global and every test here shares one
        # client host. Left alone, an earlier test spends a later one's budget.
        api_mod._hits.clear()
        api_mod._day["turns"] = 0
        patcher = mock.patch.object(api_mod, "ADMIN_TOKEN", "test-token")
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertNoLeak(self, body: str):
        for secret in SECRETS:
            self.assertNotIn(secret, body)

    # ---------------------------------------------------------- /query 503
    def test_retrieval_failure_leaks_nothing_and_is_logged(self):
        boom = RuntimeError("Connection refused to qdrant at localhost:6333")
        store = mock.Mock()
        store.search.side_effect = boom

        with mock.patch.object(store_mod, "_store", store), \
                mock.patch.object(api_mod, "embed_texts", lambda t: [[0.1]]):
            with self.assertLogs("tpm-api", level="ERROR") as captured:
                resp = self.client.post("/query", json={"question": "OEE"})

        self.assertEqual(resp.status_code, 503)
        self.assertNoLeak(resp.text)

        # The operator still gets everything, and can join the two by ref.
        logged = "\n".join(captured.output)
        self.assertIn("Connection refused to qdrant at localhost:6333", logged)
        ref = resp.json()["detail"].split("ref ")[-1].strip(") ")
        self.assertTrue(ref)
        self.assertIn(ref, logged)

    # ---------------------------------------------------------- /chat 502
    def test_chat_failure_leaks_nothing_and_is_logged(self):
        boom = RuntimeError("openai rejected key sk-live-abcdef")
        with mock.patch.object(api_mod, "run_chat", side_effect=boom):
            with self.assertLogs("tpm-api", level="ERROR") as captured:
                resp = self.client.post(
                    "/chat",
                    json={"messages": [{"role": "user", "content": "TPM"}]},
                )

        self.assertEqual(resp.status_code, 502)
        self.assertNoLeak(resp.text)
        self.assertIn("sk-live-abcdef", "\n".join(captured.output))

    # ---------------------------------------------------------- /health 503
    def test_health_failure_leaks_nothing(self):
        store = mock.Mock()
        store.client.get_collection.side_effect = RuntimeError(
            "Connection refused to qdrant at localhost:6333"
        )
        with mock.patch.object(store_mod, "_store", store):
            with self.assertLogs("tpm-api", level="ERROR"):
                resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 503)
        self.assertNoLeak(resp.text)
        # Must be the outage message, not the missing-collection one. The two
        # 503s have different fixes, and the test below is the other half.
        self.assertIn("unreachable", resp.json()["detail"])

    def test_health_tells_a_missing_collection_apart_from_an_outage(self):
        # get_collection raises the same way for a refused socket and for a
        # 404, and the handler used to blame the network for both. The serving
        # path passes create_if_missing=False, so nothing creates the
        # collection anymore - which makes this exactly the state a fresh
        # Qdrant Cloud is in before the first ingest. An operator told
        # "unreachable" then goes hunting a firewall problem that is not there.
        store = mock.Mock()
        store.collection = "docs"
        store.client.get_collection.side_effect = RuntimeError("404 not found")
        store.client.collection_exists.return_value = False

        with mock.patch.object(store_mod, "_store", store):
            with self.assertLogs("tpm-api", level="ERROR"):
                resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 503)
        detail = resp.json()["detail"]
        self.assertIn("not found", detail)
        self.assertIn("ingest", detail)
        self.assertNotIn("unreachable", detail)
        self.assertNoLeak(resp.text)

    def test_health_success_declares_utf8(self):
        store = mock.Mock()
        store.client.get_collection.return_value = mock.Mock(points_count=1415)
        with mock.patch.object(store_mod, "_store", store):
            resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["content-type"], "application/json; charset=utf-8"
        )

    # ---------------------------------------------------- /escalations utf-8
    def _seed_escalations_db(self):
        tmpdir = tempfile.mkdtemp(prefix="tpm-esc-api-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        db = os.path.join(tmpdir, "escalations.db")
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE escalations (id INTEGER PRIMARY KEY, session_id TEXT,"
            " name TEXT, contact TEXT, reason TEXT, summary TEXT,"
            " transcript TEXT, ts REAL)"
        )
        con.execute(
            "INSERT INTO escalations (session_id, name, contact, reason,"
            " summary, transcript, ts) VALUES (?,?,?,?,?,?,?)",
            ("s1", "สมชาย ใจดี", "somchai@example.com", "asked_for_person",
             "ขอคุยกับเจ้าหน้าที่", "u: ...", 1.0),
        )
        con.commit()
        con.close()
        patcher = mock.patch.dict(os.environ, {"ESCALATIONS_DB": db})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_thai_name_round_trips_with_a_declared_charset(self):
        self._seed_escalations_db()
        resp = self.client.get(
            "/escalations", headers={"X-Admin-Token": "test-token"}
        )

        self.assertEqual(resp.status_code, 200)
        # Without this header PowerShell 5.1 decodes as ISO-8859-1 and the
        # operator reads mojibake instead of the name of a real person.
        self.assertEqual(
            resp.headers["content-type"], "application/json; charset=utf-8"
        )
        self.assertEqual(resp.json()[0]["name"], "สมชาย ใจดี")

    # ------------------------------------------------------------------
    # MUST NOT REGRESS. These paths are correct and this commit is not
    # allowed to disturb them.
    # ------------------------------------------------------------------
    def test_unset_admin_token_still_denies_with_503(self):
        with mock.patch.object(api_mod, "ADMIN_TOKEN", ""):
            resp = self.client.get(
                "/escalations", headers={"X-Admin-Token": "anything"}
            )
        self.assertEqual(resp.status_code, 503)

    def test_bad_admin_token_still_401(self):
        resp = self.client.get(
            "/escalations", headers={"X-Admin-Token": "wrong"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_unknown_source_ids_still_400(self):
        resp = self.client.post(
            "/query", json={"question": "OEE", "source_ids": ["not_a_book"]}
        )
        self.assertEqual(resp.status_code, 400)

    def test_rate_limit_still_429(self):
        with mock.patch.object(api_mod, "rate_limited", return_value=True):
            resp = self.client.post("/query", json={"question": "OEE"})
        self.assertEqual(resp.status_code, 429)

    def test_last_message_must_be_from_the_user_still_400(self):
        resp = self.client.post(
            "/chat",
            json={"messages": [{"role": "assistant", "content": "hi"}]},
        )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
