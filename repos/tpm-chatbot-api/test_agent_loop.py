"""The agent loop's round-exhaustion behaviour (defect #3), and its fix.

WHAT THIS PROVES
----------------
`run_chat` is a `for ... else`. The `else` fires when the loop finishes WITHOUT
`break`, i.e. when the model asked for a tool on every one of the
MAX_TOOL_ROUNDS (=4) rounds and therefore never produced a text answer.

BEFORE THE FIX that branch set `answer` to a hard-coded Thai apology claiming
the system "took too long". Nothing took too long: the tools all succeeded -
including the escalation, which is committed to SQLite. The turn's WORK was
done; only the WORDS were wrong. The visitor was told to try again while
`escalated=True` was returned alongside, and the widget paints that reply in
the green "you have been escalated" bubble. Worse, the apology is a Thai
constant and `language` is derived from the answer text, so an English visitor
got a Thai reply tagged `language="th"`.

The root cause was a MISSING TERMINAL ANSWER STEP, not the round ceiling
(raising it to 6 or 10 produces the same apology). The fix adds one: on
exhaustion the model is asked once more with `tool_choice="none"`, so it writes
the answer it never got to write - in the visitor's own language.

test_escalation_on_final_round_returns_a_real_answer is the former
reproduction, updated to assert the fixed reply. Its DB-row assertions are
unchanged from the reproduction: they are what proves the write path was always
correct and stayed correct.

test_text_answer_on_final_round_is_returned is the control: the same four
rounds, but the model emits text on the last one. That path was correct before
the fix and must stay correct after it - it bounds the blast radius.

No network, no Qdrant, no OpenAI call, no cost. stdlib unittest only (there is
no pytest in this environment).

    python -m unittest test_agent_loop -v
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# app/agent.py:30 builds an OpenAI client at import time, which raises without
# a key present. That client is replaced by a scripted fake in every test
# below, so this value never authenticates anything.
os.environ.setdefault("OPENAI_API_KEY", "test-key-never-used")


class _FakeStore:
    """Stands in for QdrantStorage.

    The store is built on first use by app/store.py, not at module scope.
    Seeding app.store._store below keeps this test hermetic and fast whether
    or not the tpm-qdrant container happens to be running.
    """

    def __init__(self, *a, **kw):
        pass

    def search(self, *a, **kw):
        raise AssertionError("no test in this file may touch the vector store")


import tpm_rag  # noqa: E402
from tpm_rag.store import QdrantStorage as _RealQdrantStorage  # noqa: E402

with mock.patch.object(tpm_rag, "QdrantStorage", _FakeStore):
    from app import agent as agent_mod  # noqa: E402
    from app import tools as tools_mod  # noqa: E402
    from app import store as store_mod  # noqa: E402
    from app.config import MAX_TOOL_ROUNDS  # noqa: E402
    from app.prompts import AGENT_SYSTEM, _contact_line  # noqa: E402

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


APOLOGY = "ขออภัยครับ ระบบใช้เวลานานเกินไป กรุณาลองถามใหม่อีกครั้ง"


# --------------------------------------------------------------------------
# Fakes shaped to exactly what app/agent.py reads off a completion:
#
#   agent.py:62  res.choices[0].message
#   agent.py:64  msg.tool_calls          (truthiness)
#   agent.py:65  msg.content
#   agent.py:74  msg.content             (rebuilding the assistant turn)
#   agent.py:78  call.id
#   agent.py:81  call.function.name
#   agent.py:82  call.function.arguments
#
# agent.py:68-88 rebuilds the assistant turn field by field instead of calling
# model_dump(), so a plain object carrying just these attributes is a faithful
# stand-in and no SDK model is required.
# --------------------------------------------------------------------------
def tool_call(call_id, name, arguments_json):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


def response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ScriptedCompletions:
    """Hands back a pre-written sequence of completions, recording each call.

    A script entry may be an Exception instance instead of a completion, which
    is raised rather than returned - that is how the terminal answer call is
    made to fail on purpose.
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError(
                "the loop asked the model for round %d, beyond the script"
                % len(self.requests)
            )
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ScriptedClient:
    def __init__(self, script):
        self.completions = ScriptedCompletions(script)
        self.chat = SimpleNamespace(completions=self.completions)


class _AgentTurn:
    """Temp-DB fixture and the scripted-turn helper, shared by both cases."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tpm-escalation-")
        self.db = os.path.join(self.tmpdir, "escalations.db")
        # app/tools.py reads ESCALATIONS_DB as a module global inside both
        # init_escalations() and tool_escalate(), so patching it here redirects
        # the real, unmocked SQLite write into this temp file.
        patcher = mock.patch.object(tools_mod, "ESCALATIONS_DB", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def rows(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT session_id, name, contact, reason, summary, transcript"
                " FROM escalations ORDER BY id"
            ).fetchall()
        finally:
            con.close()

    def seed_escalation(self, session_id):
        """Put an earlier turn's escalation in the temp DB, as tool_escalate
        would have."""
        tools_mod.init_escalations()
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO escalations (session_id, name, contact, reason,"
            " summary, transcript, ts) VALUES (?,?,?,?,?,?,?)",
            (session_id, "Rei", "rei@example.com", "asked_for_person",
             "wants a person", "user: earlier turn", 0.0),
        )
        con.commit()
        con.close()

    def run_scripted(self, script, messages, session_id="sess-repro"):
        """One turn against a scripted model, with the two search tools stubbed.

        tool_escalate is deliberately NOT stubbed - the SQLite write is the
        thing under examination.
        """
        client = ScriptedClient(script)
        searched = {"found": 0, "note": "Nothing matched. Do not invent an answer."}
        recommended = {"found": 0}
        with mock.patch.object(agent_mod, "client", client), \
                mock.patch.object(agent_mod, "tool_search", return_value=searched), \
                mock.patch.object(agent_mod, "tool_recommend", return_value=recommended):
            result = agent_mod.run_chat(messages, session_id=session_id)
        return result, client


class AgentLoopTest(_AgentTurn, unittest.TestCase):
    # ------------------------------------------------------------------
    # DEFECT #3 - was the reproduction, now asserts the fix.
    # ------------------------------------------------------------------
    def test_escalation_on_final_round_returns_a_real_answer(self):
        self.assertEqual(
            MAX_TOOL_ROUNDS, 4, "this script is written against a 4-round ceiling"
        )

        script = [
            response(tool_calls=[tool_call(
                "c1", "search_tpm_docs",
                '{"query": "TPM khue arai", "source_id": null}')]),
            response(tool_calls=[tool_call(
                "c2", "search_tpm_docs",
                '{"query": "what is TPM", "source_id": "website"}')]),
            response(tool_calls=[tool_call(
                "c3", "recommend_reading", '{"topic": "TPM"}')]),
            response(tool_calls=[tool_call(
                "c4", "escalate_to_human",
                '{"summary": "wants a person", "name": "Rei",'
                ' "contact": "rei@example.com", "reason": "asked_for_person"}')]),
            # The terminal answer step the loop was missing.
            response(content="  รับเรื่องแล้วครับ ทีมงานจะติดต่อกลับ  "),
        ]
        messages = [
            {"role": "user", "content": "ขอคุยกับคนจริงหน่อยครับ"},
            {"role": "assistant", "content": "ขอชื่อและอีเมลด้วยครับ"},
            {"role": "user", "content": "Rei, rei@example.com"},
        ]

        result, client = self.run_scripted(script, messages)

        # 1. The loop burned every round, then asked once more for words only.
        #    Exactly one extra call, and only on this already-rare path.
        requests = client.completions.requests
        self.assertEqual(len(requests), MAX_TOOL_ROUNDS + 1)
        self.assertEqual(requests[-1]["tool_choice"], "none")
        for earlier in requests[:-1]:
            self.assertNotIn("tool_choice", earlier)

        # 2. The escalation was recognised by the loop.
        self.assertIs(result["escalated"], True)

        # 3. The visitor gets the model's own words, not an apology for a
        #    timeout that never happened.
        self.assertEqual(result["reply"], "รับเรื่องแล้วครับ ทีมงานจะติดต่อกลับ")
        self.assertNotEqual(result["reply"], APOLOGY)
        self.assertEqual(result["language"], "th")

        # 4. LOAD-BEARING. The escalation really is in the database. Nothing
        #    was lost and nothing needed retrying: the apology was a DISPLAY
        #    defect, not data loss. The fix corrects the reply only - the
        #    write path was already right.
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        session_id, name, contact, reason, summary, transcript = rows[0]
        self.assertEqual(contact, "rei@example.com")
        self.assertEqual(name, "Rei")
        self.assertEqual(session_id, "sess-repro")
        self.assertEqual(reason, "asked_for_person")
        self.assertEqual(summary, "wants a person")
        self.assertIn("rei@example.com", transcript)

        # 5. Any fix must keep the citation block at agent.py:132-142 running.
        #    An early return in the exhausted branch would skip it. Here the
        #    stubbed tools collect nothing, so [] is the correct value - the
        #    point is that the key exists and the block executed.
        self.assertEqual(result["citations"], [])
        self.assertIs(result["searched"], True)

    # ------------------------------------------------------------------
    # CONTROL - the non-buggy path, at the same round boundary.
    # ------------------------------------------------------------------
    def test_text_answer_on_final_round_is_returned(self):
        script = [
            response(tool_calls=[tool_call(
                "c1", "search_tpm_docs", '{"query": "TPM", "source_id": null}')]),
            response(tool_calls=[tool_call(
                "c2", "search_tpm_docs",
                '{"query": "TPM website", "source_id": "website"}')]),
            response(tool_calls=[tool_call(
                "c3", "recommend_reading", '{"topic": "TPM"}')]),
            response(content="  TPM ย่อมาจาก Total Productive Maintenance ครับ  "),
        ]
        messages = [{"role": "user", "content": "TPM คืออะไร"}]

        result, client = self.run_scripted(script, messages)

        # Three tool rounds then text on the fourth: the loop breaks at
        # agent.py:66, the else never runs, the model's own words are returned.
        self.assertEqual(len(client.completions.requests), 4)
        self.assertEqual(
            result["reply"], "TPM ย่อมาจาก Total Productive Maintenance ครับ"
        )
        self.assertNotEqual(result["reply"], APOLOGY)
        self.assertIs(result["escalated"], False)
        self.assertIs(result["searched"], True)
        self.assertEqual(self.rows(), [])

    # ------------------------------------------------------------------
    # The language symptom of the same root cause.
    # ------------------------------------------------------------------
    def test_exhausted_english_turn_is_not_tagged_thai(self):
        """An English visitor who exhausts the rounds must not get Thai.

        `language` is derived from the answer text, so the old hard-coded Thai
        apology tagged every exhausted turn `th` no matter who was asking. The
        terminal answer step fixes this at the source: the model writes in the
        visitor's language, and the tag follows.
        """
        script = [
            response(tool_calls=[tool_call(
                "c1", "search_tpm_docs", '{"query": "TPM", "source_id": null}')]),
            response(tool_calls=[tool_call(
                "c2", "search_tpm_docs",
                '{"query": "TPM pillars", "source_id": "website"}')]),
            response(tool_calls=[tool_call(
                "c3", "recommend_reading", '{"topic": "TPM"}')]),
            response(tool_calls=[tool_call(
                "c4", "recommend_reading", '{"topic": "OEE"}')]),
            response(content="I could not find that in the documents, sorry."),
        ]
        messages = [{"role": "user", "content": "What are the eight pillars?"}]

        result, client = self.run_scripted(script, messages)

        self.assertEqual(len(client.completions.requests), MAX_TOOL_ROUNDS + 1)
        self.assertEqual(
            result["reply"], "I could not find that in the documents, sorry."
        )
        self.assertEqual(result["language"], "en")
        self.assertNotEqual(result["reply"], APOLOGY)

    # ------------------------------------------------------------------
    # The fallbacks. The extra call must never make the turn WORSE than it
    # was before the fix, so both of its failure modes are pinned here.
    # ------------------------------------------------------------------
    def test_terminal_call_failure_still_confirms_the_escalation(self):
        """If the extra call raises, the saved escalation is still confirmed.

        Re-raising here would turn a successful turn into an HTTP 502, and the
        old apology would tell someone who just handed over their email that
        nothing happened. Neither is acceptable when the row is committed.
        """
        script = [
            response(tool_calls=[tool_call(
                "c1", "search_tpm_docs", '{"query": "TPM", "source_id": null}')]),
            response(tool_calls=[tool_call(
                "c2", "recommend_reading", '{"topic": "TPM"}')]),
            response(tool_calls=[tool_call(
                "c3", "recommend_reading", '{"topic": "OEE"}')]),
            response(tool_calls=[tool_call(
                "c4", "escalate_to_human",
                '{"summary": "wants a person", "name": "Rei",'
                ' "contact": "rei@example.com", "reason": "asked_for_person"}')]),
            RuntimeError("upstream 500 on the terminal call"),
        ]
        messages = [{"role": "user", "content": "Can I speak to a person?"}]

        # Also captures the traceback so it does not print as test noise - and
        # asserts the failure is not swallowed silently.
        with self.assertLogs("app.agent", level="ERROR") as captured:
            result, client = self.run_scripted(script, messages)
        self.assertIn("upstream 500 on the terminal call", "\n".join(captured.output))

        self.assertEqual(len(client.completions.requests), MAX_TOOL_ROUNDS + 1)
        self.assertIs(result["escalated"], True)
        self.assertNotEqual(result["reply"], APOLOGY)
        # Deterministic, and in the language the visitor actually used.
        self.assertEqual(result["language"], "en")
        self.assertEqual(result["reply"], agent_mod.ESCALATION_RECORDED["en"])
        # The row survives the failed call - that is the whole point.
        self.assertEqual(len(self.rows()), 1)
        # And the citation block still ran.
        self.assertEqual(result["citations"], [])

    def test_terminal_call_empty_without_escalation_keeps_the_apology(self):
        """Nothing saved and no words produced: the old apology is still the
        best available reply, so the fix is never worse than what it replaced.
        """
        script = [
            response(tool_calls=[tool_call(
                "c1", "search_tpm_docs", '{"query": "TPM", "source_id": null}')]),
            response(tool_calls=[tool_call(
                "c2", "recommend_reading", '{"topic": "TPM"}')]),
            response(tool_calls=[tool_call(
                "c3", "recommend_reading", '{"topic": "OEE"}')]),
            response(tool_calls=[tool_call(
                "c4", "recommend_reading", '{"topic": "5S"}')]),
            response(content="   "),  # whitespace only - nothing to show
        ]
        messages = [{"role": "user", "content": "TPM คืออะไร"}]

        result, client = self.run_scripted(script, messages)

        self.assertEqual(len(client.completions.requests), MAX_TOOL_ROUNDS + 1)
        self.assertIs(result["escalated"], False)
        self.assertEqual(result["reply"], APOLOGY)
        self.assertEqual(result["language"], "th")


class PendingEscalationTest(_AgentTurn, unittest.TestCase):
    """Defect #5: the bot forgets a handoff the moment the turn ends.

    A visitor who has already given their name and email and been passed to a
    person comes back on the next turn to a bot behaving as though nothing
    happened - which, having no memory of it, will happily ask for their
    contact details again and escalate the same request a second time.

    The fix is deliberately minimal. It does not stop the bot chatting and it
    does not change what tools exist; it just tells the model the handoff has
    already happened.
    """

    def test_pending_escalation_is_announced_to_the_model(self):
        self.seed_escalation("sess-pending")
        script = [response(content="ทีมงานกำลังติดต่อกลับอยู่ครับ")]
        messages = [{"role": "user", "content": "มีคนติดต่อกลับหรือยังครับ"}]

        result, client = self.run_scripted(
            script, messages, session_id="sess-pending"
        )

        sent = client.completions.requests[0]["messages"]
        note = agent_mod.PENDING_ESCALATION_NOTE
        self.assertIn(
            note,
            [m.get("content") for m in sent],
            "the model was never told a handoff is already pending",
        )
        # It rides with the system prompt, not after the visitor's last turn.
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sent[1], {"role": "system", "content": note})
        self.assertEqual(sent[-1]["content"], "มีคนติดต่อกลับหรือยังครับ")

        # One read, not a rewrite: the turn still answers normally.
        self.assertEqual(result["reply"], "ทีมงานกำลังติดต่อกลับอยู่ครับ")

        # The note is state, not instruction creep - it must say the two
        # things the model would otherwise get wrong.
        self.assertIn("do not", note.lower())
        self.assertIn("escalate_to_human", note)

    def test_session_with_no_row_is_byte_identical_to_before(self):
        """No escalation, no change. The lookup must be invisible otherwise."""
        script = [response(content="TPM ย่อมาจาก Total Productive Maintenance")]
        messages = [
            {"role": "user", "content": "TPM คืออะไร"},
            {"role": "assistant", "content": "เป็นแนวคิดการบำรุงรักษาครับ"},
            {"role": "user", "content": "แล้วมีกี่เสาหลัก"},
        ]

        result, client = self.run_scripted(
            script, messages, session_id="sess-clean"
        )

        expected = [
            {
                "role": "system",
                "content": AGENT_SYSTEM.format(contact_line=_contact_line()),
            }
        ] + [{"role": m["role"], "content": m["content"]} for m in messages]

        self.assertEqual(client.completions.requests[0]["messages"], expected)
        self.assertIs(result["escalated"], False)

    def test_the_row_is_matched_by_session_not_shared_across_visitors(self):
        """Another visitor's escalation must not leak into this conversation."""
        self.seed_escalation("someone-else")
        script = [response(content="สวัสดีครับ")]
        messages = [{"role": "user", "content": "สวัสดีครับ"}]

        _, client = self.run_scripted(script, messages, session_id="sess-mine")

        sent = client.completions.requests[0]["messages"]
        self.assertNotIn(
            agent_mod.PENDING_ESCALATION_NOTE, [m.get("content") for m in sent]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
