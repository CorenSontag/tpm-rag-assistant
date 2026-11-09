# tpm-chat-widget

A dependency-free chat bubble for a bilingual Thai/English RAG assistant. One
script tag, ~330 lines of vanilla JavaScript, no build step.

```html
<script src="https://yoursite.com/tpm-widget.js"
        data-api="https://your-api-host.com"></script>
```

That's the whole integration. It appends two elements to `<body>`, reads one
`localStorage` key, and conflicts with nothing.

Backend: [tpm-chatbot-api](../tpm-chatbot-api).

## Why no framework

It's designed to drop into someone else's WordPress site. A build step means a
toolchain the site owner has to maintain; a framework means a bundle competing
with whatever the theme already loads. A single file they paste in has no tail.

Everything is inline-styled and sits at `z-index: 2147483000` so no theme can
bury it or restyle it by accident.

## Thai typography is the hard part

Thai has no spaces between words and stacks diacritics above and below the
baseline. Three consequences, all handled:

**`word-break: normal` with `overflow-wrap: anywhere`.** The common
`break-word` splits Thai mid-syllable and detaches combining marks from their
base characters.

**`line-height: 1.8`.** Tighter and diacritics collide with the line above.

**The widget loads Noto Sans Thai itself.** Fallbacks are uneven — Windows has
Leelawadee UI, macOS has Thonburi, older Android has neither — and a font
without Thai coverage produces collisions rather than degrading gracefully.
Relying on the host page to have loaded a suitable font is a bet you lose
quietly, on someone else's device.

It also never lowercases or transliterates input. Lowercasing is meaningless
for Thai and breaks embedded English terms like `OEE`.

## Behaviour

**No streaming.** The backend may make several model calls per turn, so a reply
can take 4–6 seconds. The waiting state says "thinking", then upgrades to
"searching the documents" after 1.2 seconds — describing what is happening
rather than showing an undifferentiated spinner.

**UI language follows the reply.** The backend returns a `language` field; the
placeholder, labels and citation headers switch with it.

**Citations collapsed by default.** The corpus is OCR'd scans, so passage text
is sometimes visibly garbled. Page numbers lead; the text is secondary.

**History is capped at 16 turns.** The whole array is re-sent every request, so
an uncapped conversation quietly becomes an expensive one.

## Robustness, learned the hard way

**`localStorage` is wrapped in try/catch.** It throws in Safari private mode and
wherever storage is blocked. Because the widget is a top-level IIFE, an
unguarded throw kills the entire script — those visitors see no bubble at all,
and nothing appears in your server logs, because the request never left their
browser.

**Empty replies show an error** rather than a blank bubble, which reads as a
broken widget.

**`aria-live="polite"` on the message log**, so screen readers announce replies.

**A visible send button**, because mobile keyboards make Enter awkward.

**`maxLength` matches the server's limit**, so an over-long message is prevented
rather than returning a raw 422.

## Local demo

```bash
python -m http.server 5173
# open http://localhost:5173/demo.html
```

Serve it over HTTP. Opening the file directly gives an origin of `null` and CORS
rejects every request.

## Configuration

| Attribute | Required | Notes |
|---|---|---|
| `data-api` | yes | Base URL of the backend. **Must be https in production** — an https page cannot call an http API |

Strings live in the `STR` object at the top of the file. The backend must allow
your page's exact origin via `ALLOWED_ORIGINS` — `https://` vs `http://` and
`www.` vs bare all count as different origins.

## Licence

MIT.
