"""The service must boot when Qdrant is unreachable.

Incident this prevents: `QdrantStorage.__init__` calls `collection_exists()`,
a real network round-trip, and `app/api.py` and `app/tools.py` each built one
at module scope. An unreachable vector store was therefore not a degraded
service - the process exited 1 before uvicorn bound its socket. On Container
Apps scaled to zero, every cold start depends on Qdrant Cloud answering, so a
transient outage became a crash loop with a failing health probe instead of a
service that reports unhealthy and recovers by itself.

This runs in a subprocess on purpose. tests/_bootstrap.py has already imported
app.api with a fake store, and module objects are cached for the life of the
interpreter, so there is no way to re-import it hermetically in-process. The
child gets a QDRANT_URL pointing at a closed loopback port, which refuses the
connection immediately - no network, no waiting, no spend.
"""

import json
import os
import socket
import subprocess
import sys

from tests._bootstrap import REPO_ROOT

# Boot the app and ask the one endpoint whose whole job is to report on the
# vector store. Printed as JSON on a marked line so a stray warning on stdout
# cannot be mistaken for the result.
CHILD = """
import json
from fastapi.testclient import TestClient
import app.api
with TestClient(app.api.app, raise_server_exceptions=False) as c:
    r = c.get("/health")
print("RESULT:" + json.dumps({"status": r.status_code, "body": r.text[:200]}))
"""


def _closed_port() -> int:
    """A port with nothing listening: bind to get one, then release it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _boot_with_qdrant_down():
    env = dict(os.environ)
    env["QDRANT_URL"] = f"http://127.0.0.1:{_closed_port()}"
    # load_dotenv() does not override variables already in the environment, so
    # these win over any .env on the developer's machine. The key is a dummy
    # because app/api.py and app/agent.py construct OpenAI() at import and it
    # raises without one; /health never calls the model, so nothing is spent.
    env["OPENAI_API_KEY"] = "test-key-never-used"
    env["ALLOWED_ORIGINS"] = "https://tpmconsulting.org"
    env["ADMIN_TOKEN"] = ""
    env["PYTHONPATH"] = REPO_ROOT

    return subprocess.run(
        [sys.executable, "-c", CHILD],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_app_boots_and_health_reports_unhealthy_when_qdrant_is_down():
    proc = _boot_with_qdrant_down()

    assert proc.returncode == 0, (
        "importing app.api died with Qdrant unreachable - the store is being "
        f"built at import time again.\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )

    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert line, f"child produced no result.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    result = json.loads(line[-1][len("RESULT:"):])

    # 503 is the point: the process is alive and telling the truth about the
    # store. A 200 here would mean /health stopped checking Qdrant at all.
    assert result["status"] == 503, result

    # And it must be the 503 this test means. /health also returns 503 for a
    # reachable Qdrant with no collection, which is a different outage with a
    # different fix; without this the test would pass on either and prove
    # neither.
    assert "unreachable" in result["body"], result
