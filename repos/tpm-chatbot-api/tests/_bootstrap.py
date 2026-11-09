"""Import app.api without a key, a container, or a network.

app/api.py and app/tools.py both build an OpenAI client at import time, which
raises without OPENAI_API_KEY. Nothing in this package calls it, but it runs
before the first test function does.

The vector store is no longer built at import - app/store.py defers it to
first use so the service can boot while Qdrant is unreachable. Patching
tpm_rag.QdrantStorage around the import still reaches it, because app.store is
imported inside that block and binds the fake permanently; but that depends on
an import order a reader would never infer, so below we assert it holds AND
seed app.store._store directly.

test_api.py already solved this for stdlib unittest. This module is the same
trick factored out, so every pytest module shares one patched import instead of
racing to be the one that performs it - module objects are cached after the
first import, so whichever test module got there first would silently decide
whether the whole suite was hermetic.

The environment is set here rather than in a fixture for the same reason:
app/config.py and app/api.py read os.getenv at import time, so a fixture that
runs later is too late to matter. Values are assigned, not setdefault-ed,
because a test suite that inherits ALLOWED_ORIGINS from whatever happens to be
in the developer's shell is a test suite that passes for the wrong reason.
"""

import os
import sys
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Never used - app/api.py and app/agent.py construct OpenAI() at import and it
# raises on a missing key. No test here lets a request reach the model.
os.environ["OPENAI_API_KEY"] = "test-key-never-used"

# The four origins the deployed service must accept: both domains, apex and
# www, exact-matched. tpmconsulting.org is primary; tpmconsults.org serves the
# site until DNS moves. The space after the first comma is deliberate - it is
# what a human types, and it is what the old .split(",") silently broke.
TEST_ORIGINS = (
    "https://tpmconsulting.org, https://www.tpmconsulting.org,"
    "https://tpmconsults.org,https://www.tpmconsults.org"
)
os.environ["ALLOWED_ORIGINS"] = TEST_ORIGINS

# A known contact route, so _contact_line() has something deterministic to
# join. The bot never quotes a price; it routes here instead.
os.environ["CONTACT_EMAIL"] = "admin@tpmconsulting.org"
os.environ["CONTACT_URL"] = "https://tpmconsulting.org/contact"
os.environ["PRICING_URL"] = ""

os.environ["ADMIN_TOKEN"] = ""


class FakeStore:
    """Stands in for QdrantStorage.

    Both app/api.py and app/tools.py instantiate one at module scope. Any call
    to search() from this suite means a test wandered into the retrieval path
    it was supposed to be isolated from, so it fails loudly rather than
    quietly returning nothing.
    """

    def __init__(self, *a, **kw):
        self.client = None
        self.collection = "docs"

    def search(self, *a, **kw):
        raise AssertionError("no test in this package may touch the vector store")


import tpm_rag  # noqa: E402
from tpm_rag.store import QdrantStorage as _RealQdrantStorage  # noqa: E402

with mock.patch.object(tpm_rag, "QdrantStorage", FakeStore):
    from app import api as api_mod  # noqa: E402,F401
    from app import config as config_mod  # noqa: E402,F401
    from app import prompts as prompts_mod  # noqa: E402,F401
    from app import store as store_mod  # noqa: E402,F401

# See the module docstring. Both mechanisms are live and either alone would
# do; the assert is what fails closed if a later edit moves the app.store
# import out of the patch block and get_store() starts dialling QDRANT_URL for
# real - the way an "offline" suite quietly starts reading the live corpus.
assert store_mod.QdrantStorage is not _RealQdrantStorage, (
    "app.store captured the real QdrantStorage - it must be imported inside "
    "the mock.patch.object block above, or get_store() will connect for real"
)
store_mod._store = FakeStore()

APP_DIR = os.path.join(REPO_ROOT, "app")

__all__ = [
    "api_mod",
    "config_mod",
    "prompts_mod",
    "store_mod",
    "APP_DIR",
    "REPO_ROOT",
    "TEST_ORIGINS",
]
