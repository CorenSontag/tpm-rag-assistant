/*
  tpm-widget.js - bilingual chat bubble over the TPM agent.

    <script src=".../tpm-widget.js" data-api="https://your-api-host.com"></script>

  Talks to POST /chat, which keeps conversation history and decides for itself
  whether a message needs a document lookup. Greetings no longer trigger a
  search; follow-ups like "what about chapter 8" work because the whole
  history goes with every request.

  Design notes:
    - No streaming. /chat may make several model calls, so a turn can take
      4-6 seconds. The waiting state says what it is doing, not just "...".
    - Thai has no spaces between words and uses stacked diacritics, so the
      font stack and line-height are load-bearing, not cosmetic.
    - UI language follows the reply, so an English visitor sees English labels.
*/
(function () {
  var script = document.currentScript;
  var API = script && script.dataset.api;

  /* Refuse to render rather than fall back to a default. A localhost fallback
     is connection-refused on a visitor's machine, which reads as a broken bot
     instead of a missing attribute - and a missing attribute is a deploy
     mistake that should be loud, not silent. */
  if (!API) {
    console.error("[tpm-widget] no data-api attribute; widget not rendered");
    return;
  }
  if (location.protocol === "https:" && API.indexOf("https://") !== 0) {
    console.error("[tpm-widget] data-api must be https on an https page; " +
                  "browsers block mixed content. Widget not rendered.");
    return;
  }
  API = API.replace(/\/+$/, "");

  var THAI_FONT =
    "'Noto Sans Thai','IBM Plex Sans Thai',Sarabun,'Leelawadee UI'," +
    "system-ui,sans-serif";

  var STR = {
    th: {
      greeting: "สวัสดีครับ ถามเกี่ยวกับ TPM ได้เลยครับ",
      placeholder: "พิมพ์คำถามของคุณ",
      thinking: "กำลังคิด…",
      searching: "กำลังค้นหาเอกสาร…",
      citations: "อ้างอิง",
      page: "หน้า",
      error: "เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง",
      rate: "ส่งคำถามถี่เกินไป กรุณารอสักครู่",
      escalated: "ส่งต่อให้ทีมงานแล้ว",
      reset: "เริ่มใหม่",
      askLabel: "ถามอะไรก็ได้ • ผู้ช่วย AI",
    },
    en: {
      greeting: "Hello. Ask me anything about TPM.",
      placeholder: "Type your question",
      thinking: "Thinking…",
      searching: "Searching the documents…",
      citations: "Sources",
      page: "page",
      error: "Something went wrong. Please try again.",
      rate: "Too many questions at once. Please wait a moment.",
      escalated: "Passed to the team",
      reset: "Start over",
      askLabel: "Ask anything • AI assistant",
    },
  };

  var lang = "th";
  function T(key) {
    return STR[lang][key];
  }

  /* localStorage throws when storage is blocked - Safari private mode, some
     corporate policies, cookie-consent walls. Unguarded, that exception kills
     this whole script and the visitor sees no bubble at all. A per-page
     session id is a fine fallback. */
  var sessionId;
  try {
    sessionId = localStorage.getItem("tpm_session");
    if (!sessionId) {
      sessionId = Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem("tpm_session", sessionId);
    }
  } catch (e) {
    sessionId = "nostore-" + Math.random().toString(36).slice(2);
  }

  /* Load the Thai font ourselves. The test page includes it, a customer's
     WordPress theme almost certainly does not, and the fallbacks are uneven:
     Windows has Leelawadee UI, macOS has Thonburi, older Android has neither.
     Thai stacks diacritics above and below the baseline, so a font without
     proper coverage produces collisions rather than a graceful fallback. */
  if (!document.querySelector('link[href*="Noto+Sans+Thai"]')) {
    var fontLink = document.createElement("link");
    fontLink.rel = "stylesheet";
    fontLink.href =
      "https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;500&display=swap";
    document.head.appendChild(fontLink);
  }

  var messages = [];
  var busy = false;

  /* Cap the history the browser keeps. Without this the request body grows
     with every turn, and a long conversation quietly becomes an expensive
     one - the whole array is re-sent and re-read by the model each time. */
  var MAX_TURNS = 16;

  var TEXT_BASE =
    "font-family:" + THAI_FONT + ";line-height:1.8;" +
    "word-break:normal;overflow-wrap:anywhere;";

  // A robot face wearing a headset reads as "AI support chat" on sight,
  // where a plain speech bubble reads as "contact us" just as easily. Eyes
  // are punched through with the button's own background colour rather than
  // currentColor, since a filled circle needs to read as a hole, not a dot -
  // this only works because the button's background is the fixed #1a1a1a
  // used below, not a themable value.
  var ICON_CHAT =
    '<svg width="30" height="30" viewBox="0 0 24 24" fill="currentColor">' +
    '<path d="M5.5 12.5a6.5 6.5 0 0 1 13 0" fill="none" stroke="currentColor" ' +
    'stroke-width="2.2" stroke-linecap="round"/>' +
    '<rect x="3.6" y="10" width="3" height="6.4" rx="1.5"/>' +
    '<rect x="17.4" y="10" width="3" height="6.4" rx="1.5"/>' +
    '<rect x="7.3" y="7.8" width="9.4" height="8.8" rx="4"/>' +
    '<circle cx="10.3" cy="11.8" r="1.1" fill="#1a1a1a"/>' +
    '<circle cx="13.7" cy="11.8" r="1.1" fill="#1a1a1a"/>' +
    '<path d="M4.3 16.4c0 2.6 1.6 4.2 3.6 4.6" fill="none" stroke="currentColor" ' +
    'stroke-width="1.8" stroke-linecap="round"/>' +
    '<rect x="7.6" y="20" width="3.6" height="1.9" rx=".95"/>' +
    '</svg>';
  var ICON_CLOSE =
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

  // A ring pulse for a few seconds after load, so the bubble catches the eye
  // on a page it hasn't appeared on before. Iteration count is finite - a
  // looping pulse reads as a notification badge that never clears.
  if (!document.getElementById("tpmc-widget-style")) {
    var pulseStyle = document.createElement("style");
    pulseStyle.id = "tpmc-widget-style";
    pulseStyle.textContent =
      "@keyframes tpmc-pulse{" +
      "0%{box-shadow:0 2px 12px rgba(0,0,0,.25),0 0 0 0 rgba(26,26,26,.35)}" +
      "70%{box-shadow:0 2px 12px rgba(0,0,0,.25),0 0 0 14px rgba(26,26,26,0)}" +
      "100%{box-shadow:0 2px 12px rgba(0,0,0,.25),0 0 0 0 rgba(26,26,26,0)}}";
    document.head.appendChild(pulseStyle);
  }

  var button = document.createElement("button");
  button.setAttribute("aria-label", "Open chat");
  button.style.cssText =
    "position:fixed;bottom:24px;right:24px;width:60px;height:60px;" +
    "border-radius:30px;background:#1a1a1a;color:#fff;border:none;" +
    "cursor:pointer;display:flex;align-items:center;justify-content:center;" +
    "z-index:2147483000;animation:tpmc-pulse 1.8s ease-out 3;" +
    "box-shadow:0 2px 12px rgba(0,0,0,.25)";

  // The icon lives in its own span so it can be swapped independently of the
  // "AI" badge, which is a sibling and stays put across the open/close toggle.
  var iconWrap = document.createElement("span");
  iconWrap.style.cssText = "display:flex;pointer-events:none";
  iconWrap.innerHTML = ICON_CHAT;
  button.appendChild(iconWrap);

  // A generic speech bubble reads as "contact us" as easily as "AI chat".
  // This badge is the one-word disambiguator. button's position:fixed already
  // establishes the containing block an absolute child positions against.
  var aiBadge = document.createElement("span");
  aiBadge.textContent = "AI";
  aiBadge.setAttribute("aria-hidden", "true");
  aiBadge.style.cssText =
    "position:absolute;top:-5px;right:-5px;background:#2f6fed;color:#fff;" +
    "font-size:10px;font-weight:700;line-height:1;padding:4px 6px;" +
    "border-radius:9px;border:2px solid #fff;pointer-events:none;" +
    "font-family:system-ui,sans-serif;letter-spacing:.02em";
  button.appendChild(aiBadge);

  // A standing label beside the bubble, so the widget reads as "AI chat" on
  // sight rather than depending on a visitor noticing and clicking an icon.
  // Dismissible and remembered per browser - loud on first visit, not a
  // permanent tax on screen space for a returning one.
  var ASK_LABEL_KEY = "tpm_ask_label_dismissed";
  var askLabelDismissed = false;
  try {
    askLabelDismissed = localStorage.getItem(ASK_LABEL_KEY) === "1";
  } catch (e) {}

  var askBubble = document.createElement("div");
  askBubble.style.cssText =
    "position:fixed;bottom:34px;right:92px;max-width:calc(100vw - 132px);" +
    "background:#fff;color:#1a1a1a;padding:9px 10px 9px 14px;" +
    "border-radius:20px;font-size:14px;font-weight:500;white-space:nowrap;" +
    "overflow:hidden;text-overflow:ellipsis;border:1px solid #e3e3e0;" +
    "box-shadow:0 2px 10px rgba(0,0,0,.16);z-index:2147483000;" +
    "cursor:pointer;display:" + (askLabelDismissed ? "none" : "flex") + ";" +
    "align-items:center;gap:6px;" + TEXT_BASE;

  var askText = document.createElement("span");
  askText.textContent = T("askLabel");
  askBubble.appendChild(askText);

  var askDismiss = document.createElement("span");
  askDismiss.textContent = "×";
  askDismiss.setAttribute("aria-label", "Dismiss");
  askDismiss.setAttribute("role", "button");
  askDismiss.style.cssText =
    "color:#9b9a95;font-size:17px;line-height:1;padding:0 2px;flex:0 0 auto";
  askDismiss.onclick = function (e) {
    e.stopPropagation();
    askBubble.style.display = "none";
    try {
      localStorage.setItem(ASK_LABEL_KEY, "1");
    } catch (err) {}
  };
  askBubble.appendChild(askDismiss);
  askBubble.onclick = function () {
    button.click();
  };

  var panel = document.createElement("div");
  panel.lang = "th";
  panel.style.cssText =
    "position:fixed;bottom:92px;right:24px;width:400px;" +
    "max-width:calc(100vw - 32px);height:560px;" +
    "max-height:calc(100vh - 130px);background:#fff;border-radius:12px;" +
    "border:1px solid #e3e3e0;display:none;flex-direction:column;" +
    "overflow:hidden;z-index:2147483000;" +
    "box-shadow:0 4px 24px rgba(0,0,0,.18);" + TEXT_BASE;

  var bar = document.createElement("div");
  bar.style.cssText =
    "padding:10px 16px;border-bottom:1px solid #e3e3e0;background:#faf9f5;" +
    "display:flex;justify-content:flex-end";

  var resetBtn = document.createElement("button");
  resetBtn.textContent = STR.th.reset;
  resetBtn.style.cssText =
    "background:none;border:none;color:#5f5e5a;cursor:pointer;font-size:13px;" +
    TEXT_BASE;
  bar.appendChild(resetBtn);

  var log = document.createElement("div");
  // Screen readers announce replies as they arrive. Without this the panel is
  // silent to anyone not looking at it.
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");
  log.setAttribute("aria-label", "Conversation");
  log.style.cssText =
    "flex:1;overflow-y:auto;padding:16px;display:flex;" +
    "flex-direction:column;gap:14px";

  // A visible send button, because on mobile there is no comfortable Enter
  // key and tapping "Go" is not obvious.
  var row = document.createElement("div");
  row.style.cssText =
    "padding:16px;border-top:1px solid #e3e3e0;display:flex;gap:8px";

  var input = document.createElement("input");
  input.placeholder = STR.th.placeholder;
  // Matches the server's Field(max_length=4000). Without it an over-long
  // message returns a raw 422 that the visitor sees as a generic error.
  input.maxLength = 3500;
  input.style.cssText =
    "flex:1;min-width:0;box-sizing:border-box;padding:10px 12px;" +
    "border:1px solid #d4d4d0;border-radius:8px;font-size:15px;" +
    "outline:none;" + TEXT_BASE;

  var sendBtn = document.createElement("button");
  sendBtn.type = "button";
  sendBtn.setAttribute("aria-label", "Send");
  sendBtn.textContent = "→";
  sendBtn.style.cssText =
    "flex:0 0 auto;padding:10px 14px;border-radius:8px;border:none;" +
    "background:#1a1a1a;color:#fff;cursor:pointer;font-size:16px";
  sendBtn.onclick = function () {
    if (busy || !input.value.trim()) return;
    send(input.value.trim());
    input.value = "";
  };

  row.appendChild(input);
  row.appendChild(sendBtn);

  panel.appendChild(bar);
  panel.appendChild(log);
  panel.appendChild(row);
  document.body.appendChild(button);
  document.body.appendChild(askBubble);
  document.body.appendChild(panel);

  function bubble(role, text, tone) {
    var el = document.createElement("div");
    var skin =
      role === "user"
        ? "align-self:flex-end;background:#1a1a1a;color:#fff"
        : tone === "wait"
        ? "align-self:flex-start;background:#faeeda;color:#633806"
        : tone === "good"
        ? "align-self:flex-start;background:#e1f5ee;color:#085041"
        : "align-self:flex-start;background:#f1efe8;color:#1a1a1a";
    el.style.cssText =
      "max-width:88%;padding:11px 13px;border-radius:12px;font-size:15px;" +
      "white-space:pre-wrap;" + TEXT_BASE + skin;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  /* Collapsed by default. OCR noise means chunk text is sometimes garbled,
     so the page number leads and the passage is secondary. */
  function citations(list) {
    if (!list || !list.length) return;
    var wrap = document.createElement("details");
    wrap.style.cssText =
      "align-self:flex-start;max-width:88%;font-size:13px;color:#5f5e5a;" +
      TEXT_BASE;

    var summary = document.createElement("summary");
    summary.textContent = T("citations") + " (" + list.length + ")";
    summary.style.cssText = "cursor:pointer;padding:4px 0";
    wrap.appendChild(summary);

    list.forEach(function (c) {
      var item = document.createElement("div");
      item.style.cssText =
        "margin-top:8px;padding:8px 10px;background:#f1efe8;" +
        "border-radius:8px;border-left:2px solid #b4b2a9";
      /* Book chunks have a page number; web chunks have a URL and no page.
         Build the label from whatever is present rather than assuming, and
         never render an empty header - a citation with no identifying
         information is worse than no citation. */
      var parts = [];
      if (c.page) parts.push(T("page") + " " + c.page);
      if (c.title) parts.push(c.title);          // web pages carry a title
      else if (c.source) parts.push(c.source);   // books fall back to the id
      var label = parts.join(" · ");

      var head;
      if (c.url) {
        head = document.createElement("a");
        head.href = c.url;
        head.target = "_blank";
        head.rel = "noopener noreferrer";
        head.textContent = label || c.url;
        head.style.cssText =
          "font-weight:500;color:#185fa5;margin-bottom:4px;display:block;" +
          "text-decoration:underline";
      } else {
        head = document.createElement("div");
        head.textContent = label;
        head.style.cssText =
          "font-weight:500;color:#2c2c2a;margin-bottom:4px" +
          (label ? "" : ";display:none");
      }
      var body = document.createElement("div");
      body.textContent =
        c.text.length > 320 ? c.text.slice(0, 320) + "…" : c.text;
      item.appendChild(head);
      item.appendChild(body);
      wrap.appendChild(item);
    });

    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  function applyLang(next) {
    if (next === lang) return;
    lang = next;
    panel.lang = lang;
    input.placeholder = T("placeholder");
    resetBtn.textContent = T("reset");
    askText.textContent = T("askLabel");
  }

  resetBtn.onclick = function () {
    messages = [];
    log.innerHTML = "";
    bubble("assistant", T("greeting"));
  };

  button.onclick = function () {
    var open = panel.style.display === "flex";
    panel.style.display = open ? "none" : "flex";
    iconWrap.innerHTML = open ? ICON_CHAT : ICON_CLOSE;
    button.setAttribute("aria-label", open ? "Open chat" : "Close chat");
    // The badge and label are the "notice me" state; once open, the panel
    // itself has already done that job, and re-showing the label under an
    // open panel would sit behind it uselessly rather than looking dismissed.
    aiBadge.style.display = open ? "" : "none";
    if (!askLabelDismissed) {
      askBubble.style.display = open ? "flex" : "none";
    }
    if (!open) {
      if (!log.childElementCount) bubble("assistant", T("greeting"));
      input.focus();
    }
  };

  input.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || busy || !input.value.trim()) return;
    send(input.value.trim());
    input.value = "";
  });

  async function send(text) {
    busy = true;
    bubble("user", text);
    messages.push({ role: "user", content: text });

    /* The agent may search, or may just reply. Start with the neutral wait
       message and upgrade it after a moment, since a lookup is the slow case. */
    var pending = bubble("assistant", T("thinking"), "wait");
    var timer = setTimeout(function () {
      pending.textContent = T("searching");
    }, 1200);

    try {
      var res = await fetch(API + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          messages: messages.slice(-MAX_TURNS),
        }),
      });

      clearTimeout(timer);

      if (res.status === 429) {
        pending.textContent = T("rate");
        busy = false;
        return;
      }
      if (!res.ok) throw new Error("http " + res.status);

      var data = await res.json();
      applyLang(data.language === "en" ? "en" : "th");

      /* An empty reply is rare but possible - a model that stops on a tool
         boundary, or max_tokens hit mid-thought. Showing a blank bubble looks
         like the widget broke; say something instead. */
      var reply = (data.reply || "").trim() || T("error");

      pending.textContent = reply;
      pending.style.background = data.escalated ? "#e1f5ee" : "#f1efe8";
      pending.style.color = data.escalated ? "#085041" : "#1a1a1a";

      /* Escalation used to be signalled by the tint above and nothing else -
         beige to pale green. That is invisible to a screen reader, and to
         anyone who cannot separate those two greens. STR.escalated was written
         and translated in both languages and then never rendered anywhere.

         Say it in words. No role="status" here on purpose: `log` is already
         aria-live="polite" (see its setup), and a nested live region makes
         some screen readers announce the same text twice. Appending into the
         existing region is enough. applyLang has already run above, so T()
         gives the language the reply is actually in. */
      if (data.escalated) {
        var handoff = document.createElement("div");
        handoff.textContent = "✓ " + T("escalated");
        handoff.style.cssText =
          "align-self:flex-start;max-width:88%;padding:0 4px;" +
          "font-size:13px;font-weight:600;color:#085041;" + TEXT_BASE;
        log.appendChild(handoff);
        log.scrollTop = log.scrollHeight;
      }

      messages.push({ role: "assistant", content: reply });
      citations(data.citations);
    } catch (err) {
      clearTimeout(timer);
      pending.textContent = T("error");
    }

    busy = false;
    input.focus();
  }
})();
