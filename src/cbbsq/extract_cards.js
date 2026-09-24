// Extracts ShotQuality ScoreCenter game cards from the rendered page.
//
// The extractor is deliberately layout-driven instead of CSS-class-driven:
// class names in a React/Tailwind app change with every deploy, but the visual
// layout of a card does not. Each card is a stack of rows where a centered
// label ("Score", "SQ Score", "Live SQ", ...) has the away value on its left and
// the home value on its right. We therefore collect every visible text fragment
// with its on-screen geometry, anchor on the labels, and read the numbers that
// sit on the same line to the left/right of each label.
//
// Evaluated with page.evaluate(); returns a plain JSON-serialisable object.
() => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const low = (s) => norm(s).toLowerCase().replace(/:$/, "");
  const NUM = /^[-+]?\d+(?:\.\d+)?%?$/;
  const RECORD = /^\(?\d{1,2}-\d{1,2}(?:,.*)?\)?$/;
  const STATUS = /^(live|final|halftime|half|scheduled|postponed|canceled|cancelled|delayed|suspended|in progress|pre-?game|upcoming)\b/i;

  // ---- 1. every visible text fragment, with page coordinates -------------
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "TITLE"]);
  const raw = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = norm(node.nodeValue);
    const el = node.parentElement;
    if (!text || !el || SKIP.has(el.tagName)) continue;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    let r = range.getBoundingClientRect();
    if (!r || (r.width === 0 && r.height === 0)) r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const x = r.left + window.scrollX;
    const y = r.top + window.scrollY;
    raw.push({ text, el, els: [el], x, y, r: x + r.width, b: y + r.height, h: r.height });
  }

  // ---- 2. merge fragments that form one phrase on one line ---------------
  // ("SQ" + "Score" in two spans, or "Pre-Game" + "BOS -6.5" inside a chip).
  raw.sort((a, b) => a.y - b.y || a.x - b.x);
  const used = new Array(raw.length).fill(false);
  const frags = [];
  for (let i = 0; i < raw.length; i++) {
    if (used[i]) continue;
    let cur = { ...raw[i], els: [raw[i].el] };
    used[i] = true;
    let merged = true;
    while (merged) {
      merged = false;
      for (let j = i + 1; j < raw.length; j++) {
        const f = raw[j];
        if (f.y > cur.b) break; // sorted by y: nothing further down shares the line
        if (used[j]) continue;
        const hMax = Math.max(cur.h, f.h);
        const sameLine = Math.abs((cur.y + cur.b) / 2 - (f.y + f.b) / 2) < 0.3 * hMax;
        const gap = f.x - cur.r;
        const bothNumeric = NUM.test(cur.text) && NUM.test(f.text);
        if (sameLine && gap > -2 && gap < 0.45 * hMax && !bothNumeric) {
          cur = {
            text: norm(cur.text + " " + f.text),
            el: cur.el,
            els: cur.els.concat([f.el]),
            x: Math.min(cur.x, f.x), y: Math.min(cur.y, f.y),
            r: Math.max(cur.r, f.r), b: Math.max(cur.b, f.b), h: hMax,
          };
          used[j] = true;
          merged = true;
        }
      }
    }
    cur.cx = (cur.x + cur.r) / 2;
    cur.cy = (cur.y + cur.b) / 2;
    frags.push(cur);
  }

  // ---- 3. page-level facts -------------------------------------------------
  let showing = null, total = null;
  const chrome = [];
  for (const f of frags) {
    const m = f.text.match(/^showing (\d+) of (\d+) games?/i);
    if (m) { showing = +m[1]; total = +m[2]; chrome.push(f.el); }
    if (/^scorecenter$/i.test(f.text) || /^all games$/i.test(f.text)) chrome.push(f.el);
  }
  const DATE = /^[^\w]*(\d{1,2}\/\d{1,2}\/\d{4}|\d{4}-\d{2}-\d{2})[^\w]*$/u;
  let displayedDate = null;
  for (const inp of document.querySelectorAll("input")) {
    const m = norm(inp.value).match(DATE);
    if (m) { displayedDate = m[1]; break; }
  }
  if (!displayedDate) {
    const d = frags.find((f) => DATE.test(f.text));
    if (d) displayedDate = d.text.match(DATE)[1];
  }

  // ---- 4. locate cards: one container per "SQ Score" label ----------------
  const sqLabels = frags.filter((f) => low(f.text) === "sq score");
  const containsAny = (el, list) => list.some((x) => el !== x && el.contains(x));
  const cardEls = [];
  for (const lab of sqLabels) {
    let cur = lab.el;
    while (cur.parentElement && cur.parentElement !== document.body) {
      const p = cur.parentElement;
      const n = sqLabels.filter((l) => p.contains(l.el)).length;
      if (n > 1 || containsAny(p, chrome)) break;
      cur = p;
    }
    // With a single game on the page the climb reaches the grid itself; step back down
    // through wrappers whose only text-bearing child holds all of their text.
    for (;;) {
      const full = norm(cur.textContent);
      const kids = [...cur.children].filter((k) => norm(k.textContent));
      if (kids.length === 1 && norm(kids[0].textContent) === full && kids[0].contains(lab.el)) cur = kids[0];
      else break;
    }
    if (!cardEls.includes(cur)) cardEls.push(cur);
  }

  // ---- 5. read each card ---------------------------------------------------
  const cards = [];
  for (const card of cardEls) {
    const rect = card.getBoundingClientRect();
    const box = { x: rect.left + window.scrollX, y: rect.top + window.scrollY, w: rect.width, h: rect.height };
    const midX = box.x + box.w / 2;
    const band = box.w * 0.12;
    const cf = frags.filter((f) => f.els.some((e) => card.contains(e)));
    const find = (pred, after) =>
      cf.filter((f) => pred(low(f.text)) && (after == null || f.cy > after)).sort((a, b) => a.cy - b.cy)[0] || null;

    const scoreL = find((t) => t === "score");
    const sqL = find((t) => t === "sq score");
    const pctL = find((t) => t === "sq percentile" || t === "sq %ile" || t === "sq pct");
    const pppH = find((t) => /^(pts|points)\s*\/\s*poss(ession)?s?$/.test(t) || t === "ppp");
    const below = pppH ? pppH.cy : null;
    const liveL = find((t) => t === "live" || t === "actual", below);
    const liveSqL = find((t) => t === "live sq" || t === "sq", below);
    const preL = find((t) => /^pre-?game sq$/.test(t), below);
    const lineH = find((t) => t === "game line" || t === "spread");
    const ouH = find((t) => /^over\s*\/\s*under$/.test(t) || t === "o/u" || t === "total");

    const rowValues = (lab) => {
      if (!lab) return [null, null];
      const same = cf.filter((f) => f !== lab && NUM.test(f.text) &&
        Math.abs(f.cy - lab.cy) <= Math.max(8, 0.6 * Math.max(f.h, lab.h)));
      const left = same.filter((f) => f.cx < lab.x).sort((a, b) => a.cx - b.cx);
      const right = same.filter((f) => f.cx > lab.r).sort((a, b) => b.cx - a.cx);
      return [left.length ? left[0].text : null, right.length ? right[0].text : null];
    };

    // Section text for chips (game line / over-under): everything between the
    // section header and the next header below it, in reading order.
    const headers = [pppH, lineH, ouH].filter(Boolean).sort((a, b) => a.cy - b.cy);
    const sectionText = (h) => {
      if (!h) return null;
      const next = headers.find((o) => o.cy > h.cy + 1);
      const end = next ? next.y : box.y + box.h + 1;
      const parts = cf.filter((f) => f !== h && f.cy > h.b - 1 && f.cy < end)
        .sort((a, b) => (Math.abs(a.cy - b.cy) < 4 ? a.x - b.x : a.cy - b.cy));
      return norm(parts.map((f) => f.text).join(" ")) || null;
    };

    // Top of card: status/clock (centered) and team names (left/right).
    const firstRow = [scoreL, sqL, pctL].filter(Boolean).sort((a, b) => a.y - b.y)[0];
    const topLimit = firstRow ? firstRow.y - 2 : box.y + box.h * 0.3;
    const top = cf.filter((f) => f.cy < topLimit).sort((a, b) => a.cy - b.cy || a.x - b.x);
    const header = [], sides = { away: [], home: [] };
    for (const f of top) {
      if (Math.abs(f.cx - midX) <= band && !(f.x < midX - band && f.r > midX + band)) header.push(f.text);
      else (f.cx < midX ? sides.away : sides.home).push(f);
    }
    const team = (list) => {
      const nameParts = [], extra = [];
      for (const f of list) {
        if (RECORD.test(f.text) || NUM.test(f.text) || /^#\d+$/.test(f.text)) extra.push(f.text);
        else nameParts.push(f.text);
      }
      return { name: norm(nameParts.join(" ")) || null, extra };
    };
    const logos = { away: null, home: null };
    for (const img of card.querySelectorAll("img")) {
      const r = img.getBoundingClientRect();
      const cy = r.top + window.scrollY + r.height / 2;
      if (r.width === 0 || cy > topLimit) continue;
      const side = r.left + window.scrollX + r.width / 2 < midX ? "away" : "home";
      if (!logos[side]) logos[side] = { src: img.currentSrc || img.src || null, alt: img.alt || null };
    }
    const statusIdx = header.findIndex((t) => STATUS.test(t));
    const status = statusIdx >= 0 ? header[statusIdx] : (header[0] || null);
    const detail = header.filter((_, i) => i !== (statusIdx >= 0 ? statusIdx : 0));

    const a = team(sides.away), h = team(sides.home);
    cards.push({
      status,
      detail: detail.length ? detail.join(" | ") : null,
      away: { name: a.name, extra: a.extra, logo: logos.away },
      home: { name: h.name, extra: h.extra, logo: logos.home },
      rows: {
        score: rowValues(scoreL),
        sq_score: rowValues(sqL),
        sq_pct: rowValues(pctL),
        ppp_live: rowValues(liveL),
        ppp_live_sq: rowValues(liveSqL),
        ppp_pregame_sq: rowValues(preL),
      },
      line_text: sectionText(lineH),
      ou_text: sectionText(ouH),
      text: norm(card.innerText),
      box,
    });
  }
  cards.sort((p, q) => (Math.abs(p.box.y - q.box.y) < 5 ? p.box.x - q.box.x : p.box.y - q.box.y));
  return { cards, showing, total, displayedDate, url: location.href };
}
