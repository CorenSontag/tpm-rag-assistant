"""Settings, all environment-driven.

Nothing here has a value that only works on one machine. Every default is
either safe or obviously a placeholder, so a fresh clone runs without edits
and a misconfigured deploy fails loudly rather than quietly.
"""

import os

from dotenv import load_dotenv

# Load .env here rather than relying on someone else having done it first.
#
# Every name below is read at import time, so this module must load .env before
# the first os.getenv - and it does, immediately below. That is the whole
# mechanism. It does not depend on any other module's load_dotenv().
#
# The comment that used to sit here said otherwise, and was wrong twice over.
# It claimed these values survived only because api.py imports tpm_rag first
# (tpm_rag.embed calls load_dotenv at its own import), and that a script
# importing app.config without touching tpm_rag would silently get
# CONTACT_EMAIL == "". Measured 2026-11-09: such a script resolves
# CONTACT_EMAIL correctly, because of the call below.
#
# Worth knowing how that false claim survived two attempts to verify it: under
# `python -c` and heredocs, find_dotenv() has no __file__ to walk up from and
# falls back to the CWD, so it "works" for the wrong reason and proves nothing
# either way. Only a real .py file exercises the frame-walk. Check it that way.
#
# The two calls are not redundant - they resolve DIFFERENT files. This one
# finds repos/tpm-chatbot-api/.env; tpm_rag.embed's finds the root
# TPM-SYSTEM/.env. Neither is a backstop for the other.
#
# load_dotenv does not overwrite variables already in the environment, so a real
# deployment that sets them properly is unaffected, and calling it twice is free.
load_dotenv()

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
ESCALATIONS_DB = os.getenv("ESCALATIONS_DB", "escalations.db")

# Where the assistant sends people for anything outside the documents.
# Blank means it declines with no next step - set these.
CONTACT_URL = os.getenv("CONTACT_URL", "")
PRICING_URL = os.getenv("PRICING_URL", "")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")

# Where an escalation is announced. Any endpoint that accepts a JSON POST -
# a Slack or Teams incoming webhook, a Zapier catch hook, your own handler.
#
# Blank by default, and blank is a real, supported state: the escalation is
# still written to SQLite and still readable at /escalations. What changes is
# what the assistant is allowed to say. Unset, it must not promise a callback
# nobody has been asked to make.
ESCALATION_WEBHOOK_URL = os.getenv("ESCALATION_WEBHOOK_URL", "")

# Ceiling on tool round trips in one turn. Without it, a model that keeps
# requesting tools loops until the bill notices.
MAX_TOOL_ROUNDS = 4

# Conversation memory. The whole window is re-sent every turn, so this is
# both a quality knob and a cost knob.
MAX_HISTORY = 12

KNOWN_SOURCES = {"tpm_lean_factory", "tpm_collab", "content_am", "website"}

# The books only. run_eval.py filters to these so its score keeps measuring
# retrieval over the same corpus it was baselined on.
BOOK_SOURCES = ["tpm_lean_factory", "tpm_collab", "content_am"]

SOURCE_LABELS = {
    "tpm_lean_factory": "TPM สำหรับโรงงานแบบลีน (TPM for the Lean Factory)",
    "tpm_collab": "TPM การมีส่วนร่วม (TPM participation)",
    "content_am": "AM — การบำรุงรักษาด้วยตนเอง (Autonomous Maintenance)",
    "website": "เว็บไซต์บริษัท (company website)",
}
