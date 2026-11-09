"""`import tpm_rag` must not drag the OCR and ingestion stack in with it.

Incident this prevents: the production image weighed 919 MB. The cause was one
line - `tpm_rag/__init__.py` imported `.loader` eagerly, and `loader.py`
imports fitz (PyMuPDF), pytesseract, PIL and llama_index.core at module scope.
So the FastAPI serving path, which calls exactly QdrantStorage, embed_texts,
fetch_width and maybe_rerank and never OCRs anything, paid for the entire
ingestion toolchain. On Container Apps scaled to zero that weight is pulled and
unpacked on *every* cold start, so it is latency on the first visitor's
message, not just disk. The tell was a PyMuPDF deprecation warning printed by a
bare `python -c "import tpm_rag"`.

This runs in a subprocess on purpose. pytest has already imported app.api via
tests/_bootstrap.py, which imports tpm_rag, and sys.modules is cached for the
life of the interpreter - an in-process check would be measuring this process's
history, not what a fresh `import tpm_rag` costs.

The child needs a dummy OPENAI_API_KEY: tpm_rag/embed.py constructs OpenAI() at
import and it raises without one. Nothing here reaches the model, so nothing is
spent.

Note the environment requirement in _probe(): these assertions are only
meaningful where the heavy packages are actually installed. In an environment
without them "fitz is not in sys.modules" is vacuously true, so the probe
reports whether they are importable at all and the test fails rather than
passing for the wrong reason.
"""

import functools
import json
import os
import subprocess
import sys

from tests._bootstrap import REPO_ROOT

# Three facts from one child: what a bare `import tpm_rag` loaded, that the
# serving names are still eagerly available, and whether the ingestion stack is
# even installed here (see the module docstring). Printed as JSON on a marked
# line so a stray warning on stdout - the PyMuPDF one, if the regression is
# back - cannot be mistaken for the result.
HEAVY = ("fitz", "pymupdf", "pytesseract", "PIL", "llama_index")

CHILD = """
import importlib.util, json, sys
import tpm_rag
heavy = %r
print("RESULT:" + json.dumps({
    "loaded": [m for m in heavy if m in sys.modules],
    "installed": [m for m in heavy if importlib.util.find_spec(m) is not None],
    "serving": [
        n for n in ("QdrantStorage", "embed_texts", "fetch_width", "maybe_rerank")
        if not hasattr(tpm_rag, n)
    ],
}))
""" % (HEAVY,)

# Ingestion resolves load_and_chunk_pdf through the module __getattr__, which
# is where a typo'd module name or a renamed symbol would hide: the lazy path
# raises only on first attribute access, and nothing on the serving path ever
# touches it. Exit code is the whole assertion.
CHILD_INGEST = """
from tpm_rag import load_and_chunk_pdf
assert callable(load_and_chunk_pdf), load_and_chunk_pdf
"""


def _run(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # tpm_rag/embed.py builds OpenAI() at import and it raises on a missing
    # key. load_dotenv() does not override what is already set, so this wins
    # over any .env on the developer's machine.
    env["OPENAI_API_KEY"] = "test-key-never-used"
    env["PYTHONPATH"] = REPO_ROOT
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@functools.lru_cache(maxsize=1)
def _probe() -> dict:
    proc = _run(CHILD)
    assert proc.returncode == 0, (
        f"`import tpm_rag` failed in a clean interpreter.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert line, f"child produced no result.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return json.loads(line[-1][len("RESULT:"):])


def test_importing_tpm_rag_does_not_load_the_ingestion_stack():
    probe = _probe()

    # Guard against a vacuous pass: with none of these installed there is
    # nothing for a regression to load, and this test would go green on an
    # image that has the eager import back.
    assert probe["installed"], (
        "none of the ingestion packages are installed here, so this test "
        "cannot prove anything. Install the extra that CI and setup/2-SETUP.ps1 "
        'use: pip install "./tpm-rag[ingest]"'
    )

    assert probe["loaded"] == [], (
        "`import tpm_rag` pulled in the ingestion stack: "
        f"{probe['loaded']}. tpm_rag/__init__.py is importing .loader eagerly "
        "again, or something on the serving path imports it - that is the "
        "919 MB image, paid on every cold start."
    )


def test_the_serving_names_are_still_eagerly_importable():
    # Making __init__ lazy must not make the hot path lazy. These four are what
    # app/api.py, app/store.py, app/tools.py and run_eval.py import by name.
    assert _probe()["serving"] == [], (
        f"tpm_rag no longer exposes {_probe()['serving']} - the serving path "
        "imports these by name and will fail at startup."
    )


def test_load_and_chunk_pdf_is_still_importable_for_ingestion():
    proc = _run(CHILD_INGEST)
    assert proc.returncode == 0, (
        "`from tpm_rag import load_and_chunk_pdf` broke - re-ingest is dead, "
        "and it would only surface hours into a run.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
