"""Browser automation for the ShotQuality ScoreCenter (Playwright, sync API).

Uses a persistent browser profile (``.auth/profile``) so a login survives
between runs: cookies, localStorage and IndexedDB (where Firebase/Auth0-style
tokens live) are all kept, just like in your everyday browser.
"""

from __future__ import annotations

import calendar
import re
import time
from datetime import date
from importlib import resources
from typing import Callable, Optional
from urllib.parse import quote

from .config import Settings

EXTRACT_JS = resources.files("cbbsq").joinpath("extract_cards.js").read_text(encoding="utf-8")

_ANALYTICS = re.compile(
    r"google|doubleclick|segment|sentry|hotjar|intercom|mixpanel|amplitude|stripe|facebook|"
    r"clarity|posthog|launchdarkly|datadog|newrelic|fullstory|heap|gtag|cloudflareinsights",
    re.I,
)
_AUTHY = re.compile(r"login|logout|signin|sign-in|signup|auth|token|session|password|oauth|identity", re.I)

EMAIL_SELECTOR = ", ".join([
    'input[type="email"]', 'input[autocomplete="username"]', 'input[autocomplete="email"]',
    'input[name*="email" i]', 'input[id*="email" i]', 'input[placeholder*="email" i]',
    'input[name*="user" i]',
])
PASSWORD_SELECTOR = 'input[type="password"]'
SUBMIT_NAMES = re.compile(r"^\s*(log\s*in|sign\s*in|continue|next|submit)\s*$", re.I)
LOGGED_IN_TEXT = re.compile(r"^\s*(log\s*out|sign\s*out)\s*$", re.I)


class ScraperError(RuntimeError):
    pass


class LoginRequired(ScraperError):
    pass


def format_url(template: str, d: date) -> str:
    """``{date}`` -> 2026-01-10; any strftime spec works too, e.g. ``{date:%m/%d/%Y}``."""
    return template.format(date=d)


def date_formats(d: date) -> list[str]:
    return [
        d.isoformat(), d.strftime("%Y%m%d"), d.strftime("%m/%d/%Y"), quote(d.strftime("%m/%d/%Y"), safe=""),
        d.strftime("%m-%d-%Y"), f"{d.month}/{d.day}/{d.year}",
    ]


def suggest_url_template(url: str, d: date) -> Optional[str]:
    """If ``url`` embeds date ``d``, return it with the date swapped for a placeholder."""
    specs = [
        (d.isoformat(), "{date}"), (d.strftime("%Y%m%d"), "{date:%Y%m%d}"),
        (quote(d.strftime("%m/%d/%Y"), safe=""), "{date:%m%%2F%d%%2F%Y}"),
        (d.strftime("%m/%d/%Y"), "{date:%m/%d/%Y}"), (d.strftime("%m-%d-%Y"), "{date:%m-%d-%Y}"),
    ]
    for token, placeholder in specs:
        if token in url:
            return url.replace(token, placeholder)
    return None


# ---------------------------------------------------------------- JS helpers
_FIND_DATE_CONTROL_JS = r"""
() => {
  // the date, optionally next to an icon glyph: "📅 01/10/2026"
  const re = /^[^\w]*(\d{1,2}\/\d{1,2}\/\d{4}|\d{4}-\d{2}-\d{2})[^\w]*$/u;
  const dateOf = (t) => ((t || '').trim().match(re) || [])[1] || null;
  document.querySelectorAll('[data-cbbsq-date]').forEach(e => e.removeAttribute('data-cbbsq-date'));
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  for (const inp of document.querySelectorAll('input')) {
    if (visible(inp) && re.test(inp.value || '')) {
      inp.setAttribute('data-cbbsq-date', '1');
      return {kind: 'input', type: (inp.type || 'text').toLowerCase(), value: dateOf(inp.value)};
    }
  }
  const buttons = [...document.querySelectorAll('button, [role="button"], [aria-haspopup]')]
    .filter((e) => visible(e) && re.test(e.innerText || ''));
  let best = buttons.length ? buttons[buttons.length - 1] : null;
  if (!best) {
    best = [...document.querySelectorAll('body *')].find((e) => visible(e) && re.test(e.textContent || '')
      && ![...e.children].some((c) => re.test(c.textContent || ''))) || null;
  }
  if (!best) return null;
  const clickable = best.closest('button, [role="button"], [aria-haspopup]') || best;
  clickable.setAttribute('data-cbbsq-date', '1');
  return {kind: 'button', type: null, value: dateOf(best.innerText || best.textContent)};
}
"""

_CALENDAR_STATE_JS = r"""
([iso, y, m, d, monthName]) => {
  document.querySelectorAll('[data-cbbsq-day]').forEach(e => e.removeAttribute('data-cbbsq-day'));
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const outside = (e) => /outside|disabled|hidden/i.test(e.getAttribute('class') || '')
      || e.getAttribute('aria-disabled') === 'true' || e.disabled === true;
  const mark = (e) => { const b = e.matches('button') ? e : (e.querySelector('button') || e); b.setAttribute('data-cbbsq-day', '1'); return true; };
  // 1. explicit ISO data attribute (react-day-picker v9)
  for (const e of document.querySelectorAll('[data-day="' + iso + '"], [data-date="' + iso + '"]'))
    if (visible(e)) return {found: mark(e)};
  // 2. accessible label, e.g. "Saturday, January 10th, 2026" / "Jan 10, 2026"
  const full = new RegExp('(' + monthName + '|' + monthName.slice(0, 3) + ')\\.?\\s+' + d + '(st|nd|rd|th)?,?\\s+' + y, 'i');
  for (const e of document.querySelectorAll('[aria-label]'))
    if (visible(e) && full.test(e.getAttribute('aria-label')) && !outside(e)) return {found: mark(e)};
  // 3. caption says we're on the right month -> day cell by its number
  const capRe = /^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$/i;
  let caption = null;
  for (const e of document.querySelectorAll('body *')) {
    if (e.children.length === 0 && visible(e) && capRe.test((e.textContent || '').trim())) { caption = e.textContent.trim(); break; }
  }
  if (caption && caption.toLowerCase() === (monthName + ' ' + y).toLowerCase()) {
    const cells = document.querySelectorAll('[role="gridcell"], [name="day"], td button, .react-datepicker__day, [class*="day" i]');
    for (const e of cells) {
      if (visible(e) && (e.textContent || '').trim() === String(d) && !outside(e) && !(e.parentElement && outside(e.parentElement)))
        return {found: mark(e)};
    }
  }
  return {found: false, caption};
}
"""

_CLICK_NAV_JS = r"""
(dir) => {
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // accessible names: "Go to the Previous Month", "Next month", ...
  const labelRe = dir === 'prev' ? /prev|previous|back|earlier/i : /next|forward|later/i;
  // class names need whole words: Tailwind's "bg-background" must not read as "back"
  const classRe = dir === 'prev' ? /(^|[^a-z])prev(ious)?([^a-z]|$)/i : /(^|[^a-z])next([^a-z]|$)/i;
  const label = (e) => [e.getAttribute('aria-label'), e.getAttribute('name'), e.getAttribute('title')].join(' ');
  const cls = (e) => e.getAttribute('class') || '';
  const isDay = (e) => /^\d{1,2}$/.test((e.textContent || '').trim());
  // search outward from the calendar's "August 2026" caption, so page buttons never win
  const capRe = /^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}$/i;
  let caption = null;
  for (const e of document.querySelectorAll('body *')) {
    if (e.children.length === 0 && visible(e) && capRe.test((e.textContent || '').trim())) { caption = e; break; }
  }
  const roots = [];
  for (let a = caption && caption.parentElement; a && a !== document.body; a = a.parentElement) roots.push(a);
  roots.push(document.body);
  for (const root of roots) {
    const btns = [...root.querySelectorAll('button, [role="button"]')].filter((b) => visible(b) && !isDay(b));
    const hit = btns.find((b) => labelRe.test(label(b))) || btns.find((b) => classRe.test(cls(b)));
    if (hit) { hit.click(); return true; }
    // unlabeled arrows either side of the caption
    if (root !== document.body && btns.length === 2) { btns[dir === 'prev' ? 0 : 1].click(); return true; }
  }
  return false;
}
"""

_CALENDAR_OPEN_JS = r"""
() => {
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const capRe = /^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}$/i;
  for (const e of document.querySelectorAll('body *'))
    if (e.children.length === 0 && visible(e) && capRe.test((e.textContent || '').trim())) return true;
  return false;
}
"""

_SHOWING_JS = r"""
() => { const m = (document.body ? document.body.innerText : '').match(/Showing\s+\d+\s+of\s+\d+\s+games?/i);
        return m ? m[0] : null; }
"""

_HAS_CARDS_JS = "() => /SQ Score/i.test(document.body ? document.body.innerText : '')"

_WAIT_GAMES_JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  return /SQ Score/i.test(t) || /Showing\s+\d+\s+of\s+\d+\s+games?/i.test(t) || /no games/i.test(t);
}
"""

_SIGNATURE_JS = r"""
() => { const t = document.body ? document.body.innerText : ''; const i = t.search(/Showing\s+\d+/i);
        return t.slice(i < 0 ? 0 : i, (i < 0 ? 0 : i) + 3000); }
"""


class Scraper:
    def __init__(self, settings: Settings, *, headless: bool = True, capture_network: bool = True,
                 log: Callable[[str], None] = print):
        if not settings.scorecenter_url:
            raise ScraperError(
                "SQ_SCORECENTER_URL is not set. Open the CBB ScoreCenter in your browser and copy "
                "the address bar URL into .env (see .env.example).")
        self.settings = settings
        self.headless = headless
        self.capture_network = capture_network
        self.log = log
        self._responses: list = []
        self._fetched: list[str] = []  # every XHR/fetch URL, to see when a date's data arrives
        self._pw = None
        self.context = None
        self.page = None

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> "Scraper":
        self._pw = self._start_playwright()
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        kwargs = dict(headless=self.headless, viewport={"width": 1600, "height": 1000})
        if self.settings.browser_channel:
            kwargs["channel"] = self.settings.browser_channel
        self.context = self._pw.chromium.launch_persistent_context(str(self.settings.profile_dir), **kwargs)
        self.context.set_default_timeout(30_000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("response", self._on_response)
        return self

    def _start_playwright(self, attempts: int = 5):
        """Start Playwright's Node driver, retrying the intermittent startup crash
        ("Connection closed while reading from the driver") seen on some Macs."""
        from playwright.sync_api import sync_playwright

        for attempt in range(1, attempts + 1):
            try:
                return sync_playwright().start()
            except Exception as exc:
                if "Connection closed" not in str(exc) or attempt == attempts:
                    raise
                self.log(f"Playwright driver failed to start (try {attempt}/{attempts}); retrying...")
                time.sleep(attempt)

    def __exit__(self, *exc) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()

    # -------------------------------------------------------------- network
    def _on_response(self, response) -> None:
        try:
            req = response.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            self._fetched.append(response.url)
            if not self.capture_network:
                return
            if _ANALYTICS.search(response.url) or _AUTHY.search(response.url):
                return
            self._responses.append(response)
        except Exception:
            pass

    def drain_network(self) -> list[dict]:
        """JSON bodies of the XHR/fetch calls made since the last drain (auth calls excluded)."""
        out = []
        for resp in self._responses:
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype:
                    continue
                body = resp.body()
                if len(body) > 20_000_000:
                    continue
                post = resp.request.post_data
                if post and self.settings.password and self.settings.password in post:
                    post = None
                out.append({
                    "url": resp.url, "method": resp.request.method, "status": resp.status,
                    "post_data": post, "body": body.decode("utf-8", errors="replace"),
                })
            except Exception:
                continue
        self._responses = []
        return out

    # ---------------------------------------------------------------- login
    def _password_visible(self) -> bool:
        try:
            return self.page.locator(PASSWORD_SELECTOR).first.is_visible()
        except Exception:
            return False

    def needs_login(self) -> bool:
        if self._password_visible():
            return True
        path = self.page.url.split("?")[0].lower()
        return bool(re.search(r"/(login|signin|sign-in|auth)\b", path))

    def open_scorecenter(self) -> None:
        url = self.settings.scorecenter_url
        if "{date" in url:
            url = format_url(url, date.today())
        self.page.goto(url, wait_until="domcontentloaded")
        self._settle(4_000)

    def ensure_logged_in(self, interactive: bool = False) -> None:
        self.open_scorecenter()
        if not self.needs_login() and self._scorecenter_ready(10_000):
            return
        self.login(interactive=interactive)

    def login(self, interactive: bool = False) -> None:
        page = self.page
        if self.settings.login_url:
            page.goto(self.settings.login_url, wait_until="domcontentloaded")
            self._settle(3_000)
        if interactive:
            self.log("A browser window is open: log in to ShotQuality there (any method works).")
            self.log("Waiting up to 10 minutes for the ScoreCenter to appear...")
            deadline = time.time() + 600
            while time.time() < deadline:
                page.wait_for_timeout(2_000)
                try:
                    if not self.needs_login() and (self._has_logged_in_marker() or self._scorecenter_ready(500)):
                        break
                except Exception:
                    pass  # page navigating
            else:
                raise LoginRequired("Timed out waiting for a manual login.")
        else:
            if not (self.settings.email and self.settings.password):
                raise LoginRequired(
                    "Not logged in and SQ_EMAIL / SQ_PASSWORD are not set. Either add them to .env "
                    "or run `cbbsq login --headed` once to log in manually.")
            self._fill_login_form()
        self.open_scorecenter()
        if self.needs_login() or not self._scorecenter_ready(20_000):
            raise LoginRequired(
                "Login did not reach the ScoreCenter. If the site uses a CAPTCHA/SSO, run "
                "`cbbsq login --headed` and log in manually; otherwise check SQ_LOGIN_URL.")
        self.log("Logged in.")

    def _has_logged_in_marker(self) -> bool:
        try:
            return self.page.get_by_text(LOGGED_IN_TEXT).first.is_visible()
        except Exception:
            return False

    def _click_submit(self) -> bool:
        page = self.page
        for loc in (page.locator('button[type="submit"]'), page.get_by_role("button", name=SUBMIT_NAMES),
                    page.locator('input[type="submit"]')):
            try:
                if loc.first.is_visible():
                    loc.first.click()
                    return True
            except Exception:
                continue
        return False

    def _fill_login_form(self) -> None:
        page = self.page
        if not self._password_visible() and not page.locator(EMAIL_SELECTOR).first.is_visible():
            # Landing/marketing page: follow a "Log in" link if there is one.
            link = page.get_by_role("link", name=re.compile(r"log\s*in|sign\s*in", re.I))
            btn = page.get_by_role("button", name=re.compile(r"log\s*in|sign\s*in", re.I))
            for loc in (link, btn):
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    self._settle(3_000)
                    break
        email = page.locator(EMAIL_SELECTOR).first
        email.wait_for(state="visible", timeout=20_000)
        email.fill(self.settings.email)
        if not self._password_visible():  # two-step (email first, then password) forms
            self._click_submit() or email.press("Enter")
            page.locator(PASSWORD_SELECTOR).first.wait_for(state="visible", timeout=20_000)
        pw = page.locator(PASSWORD_SELECTOR).first
        pw.fill(self.settings.password)
        if not self._click_submit():
            pw.press("Enter")
        try:
            page.locator(PASSWORD_SELECTOR).first.wait_for(state="hidden", timeout=30_000)
        except Exception:
            raise LoginRequired("The login form is still showing after submitting - wrong credentials?")
        self._settle(3_000)

    # ------------------------------------------------------------ navigation
    def _settle(self, timeout_ms: int = 8_000) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    def _scorecenter_ready(self, timeout_ms: int) -> bool:
        try:
            self.page.wait_for_function(_WAIT_GAMES_JS, timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _wait_for_games(self, previous_signature: Optional[str] = None) -> None:
        if not self._scorecenter_ready(45_000):
            if self.needs_login():
                raise LoginRequired("Session expired - log in again.")
            raise ScraperError(f"ScoreCenter content never appeared at {self.page.url}")
        if previous_signature is not None:
            deadline = time.time() + 10
            while time.time() < deadline and self.page.evaluate(_SIGNATURE_JS) == previous_signature:
                self.page.wait_for_timeout(300)
        self._settle(10_000)
        # wait until the rendered card count stops changing
        last, stable = -1, 0
        for _ in range(30):
            n = self.page.evaluate("() => (document.body.innerText.match(/SQ Score/gi) || []).length")
            stable = stable + 1 if n == last else 0
            if stable >= 2:
                break
            last = n
            self.page.wait_for_timeout(400)

    def goto_date(self, d: date) -> None:
        template = self.settings.scorecenter_url
        mode = self.settings.date_mode
        if "{date" in template and mode in ("auto", "url"):
            self.page.goto(format_url(template, d), wait_until="domcontentloaded")
            if self.needs_login():
                raise LoginRequired("Session expired - log in again.")
            self._wait_for_games()
            return
        if mode == "url":
            raise ScraperError("SQ_DATE_MODE=url but SQ_SCORECENTER_URL has no {date} placeholder.")
        if not self._scorecenter_ready(1_000):
            self.open_scorecenter()
            if self.needs_login():
                raise LoginRequired("Session expired - log in again.")
            self._wait_for_games()
        self.set_date_ui(d)

    def _wait_for_date_data(self, d: date, mark: int, label: Optional[str]) -> None:
        """After picking a date the page updates in stages (old cards vanish, the new day's data
        is fetched, cards render, then the "Showing N games" line). Wait for the data request and
        the new count so a half-updated page is never read."""
        tokens = date_formats(d)
        deadline = time.time() + 20
        while time.time() < deadline and not any(t in u for u in self._fetched[mark:] for t in tokens):
            self.page.wait_for_timeout(250)
        if label:
            # done once the count line changes; if it doesn't (two dates with the same number of
            # games) accept the page after 8 s, but only once game cards are showing again -
            # big slates can take well over that to appear
            start = time.time()
            while time.time() < start + 60 and self.page.evaluate(_SHOWING_JS) == label:
                if time.time() > start + 8 and self.page.evaluate(_HAS_CARDS_JS):
                    break
                self.page.wait_for_timeout(250)

    def _read_date_control(self) -> Optional[dict]:
        return self.page.evaluate(_FIND_DATE_CONTROL_JS)

    @staticmethod
    def _matches(value: Optional[str], d: date) -> bool:
        if not value:
            return False
        value = value.strip()
        return value in (d.isoformat(), d.strftime("%m/%d/%Y"), f"{d.month}/{d.day}/{d.year}")

    def set_date_ui(self, d: date) -> None:
        page = self.page
        ctl = self._read_date_control()
        if ctl is None:
            raise ScraperError(
                "Could not find the ScoreCenter date control. Put a {date} placeholder in "
                "SQ_SCORECENTER_URL if the page URL carries the date (run `cbbsq probe` to check).")
        if self._matches(ctl["value"], d):
            return
        signature = page.evaluate(_SIGNATURE_JS)
        label, mark = page.evaluate(_SHOWING_JS), len(self._fetched)
        target = page.locator('[data-cbbsq-date="1"]').first
        if ctl["kind"] == "input":
            text = d.isoformat() if ctl["type"] == "date" else d.strftime("%m/%d/%Y")
            target.click()
            target.fill(text)
            target.press("Enter")
            page.wait_for_timeout(300)
            now = self._read_date_control()
            if not now or not self._matches(now["value"], d):
                target.click()
                target.press("ControlOrMeta+a")
                page.keyboard.type(text, delay=30)
                target.press("Enter")
            page.keyboard.press("Escape")
        else:
            # the popup can still be open from the last pick; clicking the button then would close it
            if not page.evaluate(_CALENDAR_OPEN_JS):
                target.click()
                page.wait_for_timeout(400)
            self._pick_calendar_day(d)
        self._wait_for_date_data(d, mark, label)
        self._wait_for_games(previous_signature=signature)
        now = self._read_date_control()
        if not now or not self._matches(now["value"], d):
            raise ScraperError(
                f"Tried to switch the ScoreCenter to {d:%m/%d/%Y} but it shows "
                f"{now and now['value']!r}. Run `cbbsq probe` and see README > Troubleshooting.")

    def _pick_calendar_day(self, d: date) -> None:
        page = self.page
        month_name = calendar.month_name[d.month]
        args = [d.isoformat(), str(d.year), str(d.month), str(d.day), month_name]
        for _ in range(60):
            state = page.evaluate(_CALENDAR_STATE_JS, args)
            if state.get("found"):
                page.locator('[data-cbbsq-day="1"]').first.click()
                page.wait_for_timeout(300)
                if page.evaluate(_CALENDAR_OPEN_JS):
                    page.keyboard.press("Escape")  # popup stayed open
                return
            caption = state.get("caption")
            direction = "prev"
            if caption:
                name, year = caption.rsplit(" ", 1)
                shown = int(year) * 12 + list(calendar.month_name).index(name.capitalize())
                direction = "next" if shown < d.year * 12 + d.month else "prev"
            if not page.evaluate(_CLICK_NAV_JS, direction):
                break
            page.wait_for_timeout(150)
        raise ScraperError(f"Could not find {d:%B %d, %Y} in the date picker.")

    # ------------------------------------------------------------ extraction
    def _scroll_through(self) -> None:
        page = self.page
        page.evaluate("window.scrollTo(0, 0)")
        for _ in range(400):
            done = page.evaluate(
                "() => { window.scrollBy(0, Math.floor(window.innerHeight * 0.9));"
                " return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4; }")
            page.wait_for_timeout(120)
            if done:
                break
        page.wait_for_timeout(500)

    def extract(self) -> dict:
        """Extract every card on the current page (handles lazy loading and virtualised lists)."""
        page = self.page
        self._scroll_through()
        data = page.evaluate(EXTRACT_JS)
        if data.get("showing") and data["total"] and data["showing"] < data["total"]:
            # a status filter (Live/Final/...) is active: switch back to "All Games"
            try:
                page.get_by_text("All Games", exact=True).first.click()
                self._wait_for_games()
                self._scroll_through()
                data = page.evaluate(EXTRACT_JS)
            except Exception:
                pass
        if data.get("showing") and len(data["cards"]) < data["showing"]:
            # virtualised list: only on-screen cards exist in the DOM, so sweep the page
            merged = {self._card_key(c): c for c in data["cards"]}
            page.evaluate("window.scrollTo(0, 0)")
            for _ in range(400):
                for c in page.evaluate(EXTRACT_JS)["cards"]:
                    merged[self._card_key(c)] = c
                done = page.evaluate(
                    "() => { window.scrollBy(0, Math.floor(window.innerHeight * 0.6));"
                    " return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4; }")
                page.wait_for_timeout(200)
                if done:
                    for c in page.evaluate(EXTRACT_JS)["cards"]:
                        merged[self._card_key(c)] = c
                    break
            data["cards"] = list(merged.values())
        return data

    @staticmethod
    def _card_key(card: dict) -> tuple:
        return ((card.get("away") or {}).get("name"), (card.get("home") or {}).get("name"))

    def scrape_date(self, d: date, *, save_html: bool = False) -> dict:
        self.drain_network()
        self.goto_date(d)
        data = self.extract()
        if data.get("showing") is not None and len(data["cards"]) != data["showing"]:
            self.page.wait_for_timeout(2_000)  # still rendering: give it one more look
            self._wait_for_games()
            data = self.extract()
        result = {
            "date": d.isoformat(),
            "url": self.page.url,
            "cards": data["cards"],
            "showing": data.get("showing"),
            "total": data.get("total"),
            "displayed_date": data.get("displayedDate"),
            "network": self.drain_network(),
        }
        if save_html:
            result["html"] = self.page.content()
        return result
