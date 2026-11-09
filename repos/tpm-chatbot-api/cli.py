"""Talk to the chatbot from the terminal.

    uv run python chat_cli.py

Only needs Qdrant running and OPENAI_API_KEY set. No uvicorn, no static
server, no browser, no CORS - it imports run_chat directly, so this is the
same agent the website will use, minus three moving parts.

Commands:
    /reset    start a fresh conversation
    /cite     show the sources for the last reply in full
    /raw      toggle the searched/latency/citation line
    /quit     leave
"""

import sys
import time

from app.agent import run_chat

# Windows terminals default to a codepage that mangles Thai.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BANNER = """
TPM chatbot - terminal
Ask in Thai or English. /reset  /cite  /raw  /quit
"""


def main() -> None:
    print(BANNER)

    messages = []
    last = None
    show_meta = True
    session = f"cli-{int(time.time())}"

    while True:
        try:
            text = input("\nyou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not text:
            continue

        if text in ("/quit", "/exit", "/q"):
            return

        if text == "/reset":
            messages = []
            last = None
            print("  (conversation cleared)")
            continue

        if text == "/raw":
            show_meta = not show_meta
            print(f"  (meta {'on' if show_meta else 'off'})")
            continue

        if text == "/cite":
            if not last or not last["citations"]:
                print("  (no citations on the last reply)")
                continue
            for c in last["citations"]:
                head = f"หน้า {c['page']}" if c["page"] else "(no page)"
                if c["source"]:
                    head += f"  [{c['source']}]"
                print(f"\n  {head}")
                print(f"  {c['text'][:500]}")
            continue

        messages.append({"role": "user", "content": text})

        started = time.time()
        try:
            last = run_chat(messages, session_id=session)
        except Exception as e:
            print(f"\n  error: {e}")
            messages.pop()
            continue
        elapsed = time.time() - started

        messages.append({"role": "assistant", "content": last["reply"]})

        print(f"\nbot > {last['reply']}")

        if show_meta:
            print(
                f"\n      searched={last['searched']}  "
                f"citations={len(last['citations'])}  "
                f"escalated={last['escalated']}  "
                f"lang={last['language']}  {elapsed:.1f}s"
            )
            if last["citations"]:
                pages = ", ".join(
                    f"หน้า {c['page']}" for c in last["citations"] if c["page"]
                )
                if pages:
                    print(f"      {pages}   (/cite for the text)")


if __name__ == "__main__":
    main()
