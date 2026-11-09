# Architecture

The architecture document lives with the code it describes:

**[`repos/tpm-chatbot-api/ARCHITECTURE.md`](../repos/tpm-chatbot-api/ARCHITECTURE.md)**

## Why this is a pointer and not a copy

It used to be a byte-identical duplicate. Both copies then drifted away from the
code together, and by 2026-08 the "Traps" section was giving two instructions
that were actively wrong — it told you `sources` was a deduplicated set that
must not be zipped with `contexts` (it is positional, `store.py:55-70`), and
that the reranker was never wired in (it is, `tools.py:16,151,169`). Following
that section produced worse code than the code already there.

Two copies of a document are two things to update and one thing that gets
forgotten. There is now one.
