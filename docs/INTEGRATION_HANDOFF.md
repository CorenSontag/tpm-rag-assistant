# Handoff — embedding the TPM assistant on the website

> **Read this if you are putting the chat assistant onto the site.**
> The work is one script tag. This document exists because the ways it goes
> wrong are not obvious, and most of them fail silently.

---

## 1. What you are integrating

A chat bubble that answers questions about Total Productive Maintenance from
three Thai manuals, cites page numbers, replies in Thai or English depending on
what the visitor typed, and hands off to a human when asked.

Two pieces already exist and are not your responsibility:

| Piece | Where it runs | Status |
|---|---|---|
| `tpm-widget.js` | The visitor's browser | Built, tested |
| The API | Its own host, separate from WordPress | Built; deployment is the site owner's task |

**You are adding the script tag. That is the whole job.**

---

## 2. The integration

```html
<script src="https://YOURSITE/wp-content/uploads/tpm-widget.js"
        data-api="https://YOUR-API-HOST"></script>
```

Site-wide footer. Use WPCode, your theme's footer setting, or
`wp_enqueue_script` with `in_footer = true`.

Footer rather than head, so the widget never delays first paint.

Values to fill in:

| Placeholder | What it is |
|---|---|
| `YOURSITE/wp-content/uploads/tpm-widget.js` | Wherever you host the file. The media library is fine |
| `YOUR-API-HOST` | The deployed API. **Must be https** |

---

## 3. Hard requirements

**`data-api` must be https.** An https page cannot call an http endpoint —
browsers block mixed content and every message fails with nothing visible in
the UI.

**Do not proxy the API through PHP.** It is a direct browser-to-API call by
design. A PHP proxy adds a hop, breaks the 4–6 second responses under typical
`max_execution_time`, and gains nothing.

**Do not put any API key anywhere in the frontend.** The widget needs no
credentials. If you find yourself adding one, something has been
misunderstood — stop and ask.

**Do not load it inside a shadow DOM, or defer/async it.** The widget reads
`document.currentScript` at parse time to find `data-api`. Both break that.

**Do not modify `tpm-widget.js`.** It is maintained in its own repository. If
something needs changing, report it rather than patching in place — local edits
will be lost on the next update.

---

## 4. Things that will bite you

### Content Security Policy

If the site sets a CSP, the widget needs three allowances. This is the most
common cause of "it works locally but not on the real site":

```
connect-src  https://YOUR-API-HOST
style-src    https://fonts.googleapis.com
font-src     https://fonts.gstatic.com
```

The font entries are needed because the widget loads Noto Sans Thai itself —
Thai stacks diacritics and renders badly in fonts without proper coverage, and
we cannot assume the theme provides one.

### Cookie consent

The widget writes one `localStorage` key, `tpm_session`, to keep a conversation
id. If the site has a consent banner covering local storage, this is
functional-category storage. It is already wrapped in try/catch, so blocking it
degrades gracefully rather than breaking the widget.

### Caching

Page caches (WP Rocket, LiteSpeed, Cloudflare) will serve a stale
`tpm-widget.js` after any update. Purge after changes, and test in a private
window.

### Theme conflicts

The widget inline-styles everything and sits at `z-index: 2147483000`. If
another fixed element overlaps it, that element is above 2147483000 — lower it
rather than raising the widget.

---

## 5. Verify it works

In a **private window**, on the live site:

| Test | Expected |
|---|---|
| Page loads | Dark round button, bottom-right, nothing overlapping |
| `สวัสดีครับ` | Friendly greeting, **no** sources panel |
| `OEE คืออะไร` | Thai answer with a `หน้า` citation |
| Then `แล้วมีกี่ขั้นตอน` | Sensible — it uses the previous turn |
| `How much does training cost?` | English reply, declines, links the pricing page |
| Mobile viewport | Panel fits, send button reachable, Thai renders without clipping |

A private window matters: logged-in admins see a different page, and cached
scripts will lie to you.

---

## 6. If something is wrong

Open the console (F12). The message tells you which layer failed.

| Console message | Cause | Who fixes it |
|---|---|---|
| CORS policy / `Access-Control-Allow-Origin` | `ALLOWED_ORIGINS` on the API doesn't match this exact domain | Site owner |
| Mixed content blocked | `data-api` is http on an https page | You |
| `Refused to connect` / CSP | `connect-src` missing | You |
| Thai renders as boxes or overlaps | Font blocked — check `font-src` | You |
| 429 | Rate limited. Expected under load testing | Site owner |
| Nothing appears at all | Script not loading. Check the URL resolves, and purge caches | You |

**Do not attempt to fix CORS from the frontend.** It is enforced by the
browser and can only be resolved on the API. Report the exact origin the site
is served from and let the site owner set it.

---

## 7. What to report back

- The exact origin the site is served from, including `www.` or not — needed
  for `ALLOWED_ORIGINS`
- Whether a CSP is in force, and whether you could add the three entries
- Results of the six tests in section 5
- Any theme element that overlaps the bubble

---

## 8. Out of scope

Not your problem, listed so you know not to chase them:

- Answer quality, tone or refusals — controlled by the API's system prompt
- Which documents get searched — the model decides
- Response time of 4–6 seconds — the agent makes multiple model calls; the
  widget already shows a "searching" state
- Escalation emails — stored server-side; no frontend involvement
- Adding new languages — Thai and English are handled by the backend

---

## 9. Source

| Repo | Contents |
|---|---|
| `tpm-chat-widget` | The widget, a demo page, its own README |
| `tpm-chatbot-api` | The agent and HTTP API |
| `tpm-rag` | Retrieval over the document corpus |

`demo.html` in the widget repo is a working reference implementation. If in
doubt about placement, copy what it does.
