"""The agent loop.

Ask the model. If it replies with text, done. If it asks for a tool, run the
tool, append the result to the conversation, ask again. Repeat up to
MAX_TOOL_ROUNDS. If the rounds run out with no text, ask once more for words
only - see _final_answer.

That is the whole thing - a for loop, an if, and a list that grows. No agent
framework: with three tools there is nothing for one to abstract, and a
hand-written loop means every failure is visible in a stack trace rather than
in someone else's control flow.
"""

import json
import logging
import re

from openai import OpenAI

from app.config import CHAT_MODEL, MAX_HISTORY, MAX_TOOL_ROUNDS
from app.prompts import AGENT_SYSTEM, _contact_line
from app.tools import (
    TOOLS,
    has_open_escalation,
    init_escalations,
    tool_escalate,
    tool_recommend,
    tool_search,
)

log = logging.getLogger(__name__)

# A stalled connection otherwise holds a FastAPI threadpool worker until the
# OS gives up. A few of those and the service stops answering anyone.
client = OpenAI(timeout=30.0, max_retries=2)

THAI = re.compile(r"[฀-๿]")

# Reached only when the tool rounds ran out AND the terminal answer call below
# failed or came back empty. Before the terminal call existed this was the
# reply for EVERY exhausted turn, which is defect #3: nothing has timed out
# here, but with no words from the model and nothing saved, there is genuinely
# nothing else to say.
# Keyed by visitor language, like ESCALATION_RECORDED below. It was a Thai-only
# constant, so an English visitor whose turn hit this branch got a Thai reply -
# and because api.py derives the reported `language` from the answer text, the
# turn came back tagged "th" no matter what they typed. Applying the language to
# one branch and not the other is how that survived the original fix.
ROUND_LIMIT_APOLOGY = {
    "th": "ขออภัยครับ ระบบใช้เวลานานเกินไป กรุณาลองถามใหม่อีกครั้ง",
    "en": "Sorry - I could not put an answer together for that one. "
          "Please try asking again.",
}

# The same dead end, but with an escalation committed to SQLite. Confirms only
# what is certainly true - the request is recorded. It deliberately promises no
# callback and no timeframe, because whether anyone is actually notified
# depends on ESCALATION_WEBHOOK_URL being configured (see tools.tool_escalate).
ESCALATION_RECORDED = {
    "th": "บันทึกคำขอของคุณไว้เรียบร้อยแล้วครับ",
    "en": "Your request has been recorded.",
}

# Appended for the terminal call only. Without it the model sees a conversation
# that ends in tool output and no instruction to stop using tools, and can
# answer as though another round were coming.
FINAL_TURN_NOTE = (
    "The tool budget for this turn is spent - no further tool calls are "
    "possible. Answer the visitor now, in their own language, using only what "
    "the tool results above already gave you. If those results do not answer "
    "the question, say so plainly and offer the contact route. Do not promise "
    "to look anything up later."
)


# Conversation history is client-supplied and carries no server-side state, so
# an escalation from an earlier turn is invisible to the model on the next one:
# it will ask a visitor who has already given their name and email for both
# again, and escalate the same request twice. The escalations table is the only
# durable memory this service has of that handoff, so it is read back in.
PENDING_ESCALATION_NOTE = (
    "STATE: earlier in this conversation this visitor was already handed to a "
    "person, and their name and contact details are recorded. Do NOT call "
    "escalate_to_human again for the same request and do NOT ask for their "
    "contact details a second time. If they ask about it, acknowledge that the "
    "handoff is already pending. Otherwise carry on helping normally."
)


def _visitor_language(history) -> str:
    """"th" or "en", from what the visitor last wrote - not from the reply.

    The response `language` field is derived from the answer text, which works
    while the model obeys "reply in the visitor's language" but says nothing
    useful about a canned string. Anything canned picks its language here
    instead.
    """
    for m in reversed(history):
        if m["role"] == "user":
            return "th" if THAI.search(m["content"]) else "en"
    return "th"


def _final_answer(convo, escalated: bool, language: str) -> str:
    """The terminal answer step: ask for words, with the tools switched off.

    The loop only ever produced an answer when the model returned text. A turn
    that asked for a tool on every round therefore ended with no answer at all,
    and the reply was replaced by an apology for a timeout that never happened
    - while the work of the turn, including a saved escalation, had in fact
    succeeded. This is that missing step.

    Cost is one extra call on turns that already spent MAX_TOOL_ROUNDS, not one
    per turn. `tools` is still passed because `convo` contains tool calls and
    their results; `tool_choice="none"` is what forbids another one.

    Never raises. A failure here must not turn an otherwise successful turn
    into a 502, so it degrades to a deterministic reply instead.
    """
    try:
        res = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=convo + [{"role": "system", "content": FINAL_TURN_NOTE}],
            tools=TOOLS,
            tool_choice="none",
            temperature=0.3,
            max_tokens=900,
        )
        text = (res.choices[0].message.content or "").strip()
    except Exception:
        log.exception("terminal answer call failed after exhausting tool rounds")
        text = ""

    if text:
        return text
    return ESCALATION_RECORDED[language] if escalated else ROUND_LIMIT_APOLOGY[language]


def run_chat(messages, session_id="anon") -> dict:
    """One conversational turn. Returns reply, citations and metadata."""
    init_escalations()

    history = [
        {"role": m["role"], "content": str(m["content"])[:4000]}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ][-MAX_HISTORY:]

    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    convo = [
        {"role": "system", "content": AGENT_SYSTEM.format(contact_line=_contact_line())}
    ] + history

    # Beside the system prompt rather than after the visitor's last message:
    # it qualifies the escalation instructions, and belongs where they are.
    if has_open_escalation(session_id):
        convo.insert(1, {"role": "system", "content": PENDING_ESCALATION_NOTE})

    collected = []
    escalated = False
    searched = False

    for _ in range(MAX_TOOL_ROUNDS):
        res = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=convo,
            tools=TOOLS,
            temperature=0.3,
            max_tokens=900,
        )
        msg = res.choices[0].message

        if not msg.tool_calls:
            answer = (msg.content or "").strip()
            break

        # Rebuild the assistant turn explicitly rather than msg.model_dump().
        # The SDK object carries fields the API will not accept back on a
        # later request (refusal, annotations, audio, the deprecated
        # function_call), and which fields exist changes between SDK versions.
        convo.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.function.name,
                            "arguments": c.function.arguments,
                        },
                    }
                    for c in msg.tool_calls
                ],
            }
        )

        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if name == "search_tpm_docs":
                searched = True
                result = tool_search(args, collected)
            elif name == "recommend_reading":
                result = tool_recommend(args, collected)
            elif name == "escalate_to_human":
                result = tool_escalate(args, session_id, transcript)
                escalated = escalated or result.get("saved", False)
            else:
                result = {"error": f"unknown tool {name}"}

            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    else:
        # Rounds exhausted with no text. Assign, never return - the citation
        # block below still has to run, and an early return here silently
        # drops every citation the turn collected.
        answer = _final_answer(convo, escalated, _visitor_language(history))

    # Deduplicate citations, preserving order.
    #
    # LOCAL PATCH (website source) - upstream owns the real fix.
    #
    # The key was (source, page). Every book chunk carries a distinct page, so
    # that worked. Every *website* chunk has source "website" and page None, so
    # the key is identical for all of them and the whole set collapses to one
    # citation - which is why a reply naming three different pages reported
    # citations=1. url is what actually distinguishes a web chunk.
    #
    # url and title were also being dropped here. They are set on the payload,
    # returned by store.search, carried through _retrieve, and rendered by the
    # widget - but never copied into the citation, so the widget could not
    # receive them and a web source could never render as a link.
    seen = set()
    citations = []
    for h in collected:
        key = (h["source"], h["page"], h.get("url"))
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {"text": h["display"], "source": h["source"], "page": h["page"],
             "url": h.get("url"), "title": h.get("title")}
        )

    return {
        "reply": answer,
        "citations": citations[:8],
        "searched": searched,
        "escalated": escalated,
        "language": "th" if THAI.search(answer or "") else "en",
    }
