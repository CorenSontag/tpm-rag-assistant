# Prompt for the website-building agent

Fill in the four bracketed values, then paste everything below the line.

Values you need first:

- `[WIDGET_URL]` — where `tpm-widget.js` is hosted, e.g.
  `https://yoursite.com/wp-content/uploads/2026/08/tpm-widget.js`
- `[API_URL]` — the deployed API, https, no trailing slash
- `[SITE_URL]` — the site's exact public origin, `www.` included if it uses one
- `[CMS]` — WordPress, or whatever it actually is

---

## Prompt

I need you to embed an existing chat widget into my website. The widget and its
backend are already built, tested and deployed. **Your task is to place one
script tag correctly and verify it works — not to build or modify a chatbot.**

### The tag

```html
<script src="[WIDGET_URL]" data-api="[API_URL]"></script>
```

Place it site-wide in the **footer**, after page content. On [CMS], use whatever
mechanism is idiomatic — for WordPress that is WPCode, the theme's footer
setting, or `wp_enqueue_script` with `in_footer = true`.

### Hard constraints

Do not violate these. If one seems to require breaking, stop and tell me.

1. **Do not modify the widget file.** It is maintained in its own repository.
   Report problems instead of patching locally.
2. **Do not write any API key, token or credential into the frontend.** The
   widget needs none. If you think it does, something is wrong — ask.
3. **Do not proxy the API through PHP or any server-side route.** It is a
   direct browser-to-API call by design. A proxy breaks the 4–6 second response
   times under typical `max_execution_time`.
4. **Do not use `defer`, `async`, or load it inside a shadow DOM.** The widget
   reads `document.currentScript` at parse time to find `data-api`; all three
   break that.
5. **Do not rebuild the UI.** No React wrapper, no re-theming, no custom launcher.
   It is deliberately dependency-free vanilla JavaScript that inline-styles
   everything so no theme can interfere.
6. **`data-api` must be https.** An https page cannot call http — browsers block
   it and every message fails silently.

### If the site has a Content Security Policy

Add exactly these, and nothing broader:

```
connect-src  [API_URL]
style-src    https://fonts.googleapis.com
font-src     https://fonts.gstatic.com
```

The font entries are required: the widget loads Noto Sans Thai itself because
Thai stacks diacritics above and below the baseline and renders badly in fonts
without proper coverage.

### Verify, in a private window, on the live site

1. Page loads with a dark round button bottom-right, nothing overlapping it
2. Send `สวัสดีครับ` — expect a friendly greeting and **no** sources panel
3. Send `OEE คืออะไร` — expect a Thai answer with a `หน้า` page citation
4. Then send `แล้วมีกี่ขั้นตอน` — expect a sensible reply using the previous turn
5. Send `How much does training cost?` — expect an English reply that declines
   and links a pricing page
6. Check a mobile viewport — panel fits, send button reachable, Thai text not
   clipped or overlapping

A private window is required: logged-in admin sessions render differently and
page caches will serve you a stale script.

### If something fails

Open the browser console and read the error before changing anything.

- **CORS / `Access-Control-Allow-Origin`** — the API does not yet allow this
  origin. **You cannot fix this from the frontend.** Report the exact origin
  the site is served from and stop.
- **Mixed content blocked** — `data-api` is http. Fix it.
- **CSP violation** — add the three entries above.
- **Thai renders as boxes or overlapping marks** — the font is blocked; check
  `font-src`.
- **Nothing appears at all** — the script URL is not resolving, or a page cache
  is serving an old version. Purge and retest.

Do not work around a failure by rewriting the widget or adding a proxy. Report
it.

### Out of scope

Do not attempt to change these — they are backend-controlled:

- Answer quality, tone, refusals, or which documents get searched
- The 4–6 second response time (the widget already shows a searching state)
- Escalation handling
- Adding languages

### Report back

1. The exact origin the site is served from, `www.` included or not — I need it
   for the API's allow-list
2. Whether a CSP is in force and whether you added the three entries
3. Pass/fail for each of the six checks
4. Any theme element overlapping the bubble, and its z-index
5. Where exactly you placed the tag, so I can find it later
