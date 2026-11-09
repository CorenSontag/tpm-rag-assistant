"""run_eval.py's empty-context guard, and the populated path it must not move.

WHY THIS EXISTS
---------------
run_eval builds `context_block` by joining the retrieved contexts. With zero
contexts that join produces an empty string, and the eval went on to ask the
model to answer from nothing - fifteen paid calls that can only ever fail,
which is exactly the state you are in when the collection is empty or a
filter matches nothing. Failing fast costs nothing and says why.

The populated path is the eval's whole value as a regression guard, so it is
pinned here byte-for-byte: same prompt, same model, same temperature, same
max_tokens as /query.

No network, no Qdrant, no OpenAI call, no cost. stdlib unittest only.

    python -m unittest test_run_eval -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# run_eval builds an OpenAI client at import time. It is replaced by a fake in
# every test below, so this value never authenticates anything.
os.environ.setdefault("OPENAI_API_KEY", "test-key-never-used")

import run_eval  # noqa: E402


class _Store:
    """Stands in for QdrantStorage. run_eval constructs one inside run_query."""

    def __init__(self, found):
        self.found = found

    def __call__(self, *a, **kw):
        return self

    def search(self, *a, **kw):
        return self.found


class _Client:
    """An OpenAI client that records calls. Raising is the point in one test."""

    def __init__(self, content=None):
        self.calls = []
        self.content = content
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.content is None:
            raise AssertionError("run_eval must not pay for an empty context")
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class RunEvalTest(unittest.TestCase):
    def run_query(self, found, content=None):
        client = _Client(content)
        with mock.patch.object(run_eval, "QdrantStorage", _Store(found)), \
                mock.patch.object(run_eval, "embed_texts", lambda t: [[0.0, 0.0]]), \
                mock.patch.object(run_eval, "client", client):
            result = run_eval.run_query("OEE คืออะไร")
        return result, client

    def test_empty_context_makes_no_model_call(self):
        found = {"contexts": [], "pages": [], "sources": []}
        result, client = self.run_query(found)

        self.assertEqual(client.calls, [], "no paid call may be made")
        self.assertEqual(result["contexts"], [])
        self.assertEqual(result["answer"], "")
        self.assertEqual(result["skipped"], "empty context")

    def test_empty_context_is_reported_as_a_fail_with_a_reason(self):
        """main() must score it FAIL and say why, not silently as a miss.

        main() reads eval_q.json from the working directory, so it gets a
        one-question set in a temp directory rather than the real fifteen.
        """
        found = {"contexts": [], "pages": [], "sources": []}
        tmpdir = tempfile.mkdtemp(prefix="tpm-eval-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        with open(os.path.join(tmpdir, "eval_q.json"), "w", encoding="utf-8") as f:
            json.dump(
                [{"question": "OEE คืออะไร", "expected_keywords": ["OEE"]}], f
            )
        cwd = os.getcwd()
        os.chdir(tmpdir)
        self.addCleanup(os.chdir, cwd)

        printed = []
        with mock.patch.object(run_eval, "QdrantStorage", _Store(found)), \
                mock.patch.object(run_eval, "embed_texts", lambda t: [[0.0]]), \
                mock.patch.object(run_eval, "client", _Client()), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(
                    " ".join(str(x) for x in a))):
            run_eval.main()

        out = "\n".join(printed)
        self.assertIn("[FAIL]", out)
        self.assertIn("empty context", out)
        self.assertIn("0/1 passed", out)

    def test_populated_path_is_unchanged(self):
        """The prompt the eval sends is the regression guard. Pin it exactly."""
        found = {
            "contexts": ["[บทที่ 3] OEE วัดประสิทธิผลโดยรวม", "ไม่มีหน้า"],
            "pages": [42, None],
            "sources": ["tpm_collab", "content_am"],
        }
        result, client = self.run_query(found, content="  OEE คือ...  ")

        self.assertEqual(len(client.calls), 1)
        sent = client.calls[0]
        self.assertEqual(sent["model"], "gpt-4o-mini")
        self.assertEqual(sent["max_tokens"], 1024)
        self.assertEqual(sent["temperature"], 0.2)
        self.assertEqual(sent["messages"][0]["content"], run_eval.SYSTEM_PROMPT)
        self.assertEqual(
            sent["messages"][1]["content"],
            "Use the following context to answer the question.\n\n"
            "Context:\n"
            "- [บทที่ 3] OEE วัดประสิทธิผลโดยรวม (หน้า 42)\n\n"
            "- ไม่มีหน้า\n\n"
            "Question: OEE คืออะไร\n"
            "Answer concisely using the context above.",
        )
        self.assertEqual(result["answer"], "OEE คือ...")
        self.assertEqual(result["contexts"], found["contexts"])
        self.assertNotIn("skipped", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
