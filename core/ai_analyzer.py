import json
import re
import logging
import time
from urllib.parse import urlparse

from openai import OpenAI

from core.config import AppConfig, OPENAI_TIMEOUT

logger = logging.getLogger("auto_screen.ai")


class AIAnalyzer:
    """AI brain — plans which pages to capture and describes them."""

    def __init__(self, config):
        self.config = config
        self.client = OpenAI(
            api_key=config.openai_api_key,
            timeout=OPENAI_TIMEOUT,
            max_retries=3,
        )
        self.model = config.model

    # ── Plan: analyze landing page, list all pages to visit ──

    def plan_site_capture(self, url, html, links, max_pages=15):
        """Analyze landing page and return a flat list of pages to capture."""
        links_summary = "\n".join(
            "- [{}]({})".format(l.get("text", ""), l.get("url", ""))
            for l in links[:80]
            if l.get("is_internal")
        )

        prompt = """You are analyzing a website to plan a thorough screenshot capture.

URL: {url}

Page HTML (truncated):
---
{html}
---

Internal links found:
{links}

Create a capture plan covering the website comprehensively.
The goal is to screenshot every meaningful page and UI state a user would encounter.

Respond with ONLY a JSON object:

{{
  "site_name": "Name of the website",
  "site_description": "What this website is about",
  "landing_description": "Description of the homepage",
  "pages": [
    {{
      "url": "/full-or-relative-path",
      "description": "What this page shows",
      "theme": "section category (e.g. products, about, blog, legal, support)"
    }}
  ]
}}

RULES:
- MAX {max_pages} pages total. Pick the most important and diverse ones.
- Include MULTIPLE examples of key page types to show variety:
  - 2-3 different category pages to show different layouts/content
  - 2-3 different product/detail pages to show variety
  - 2-3 blog posts if they exist (different topics/formats)
- Include ALL structurally different pages: homepage sections, about, contact, \
blog listing, blog posts, products, categories, legal, pricing, FAQ, settings, \
dashboard, profile, search results, etc.
- Do NOT include the homepage (it's captured separately)
- Do NOT include anchor links (#) or javascript: links
- Do NOT include file links (images, PDFs, etc)
- Only include URLs you actually see in the links list — do NOT invent URLs
- Aim to fill the {max_pages} page budget — more pages = better coverage""".format(
            url=url, html=html[:12000], links=links_summary, max_pages=max_pages
        )

        response = self._call_openai(
            prompt,
            system=(
                "You plan thorough website screenshot captures. "
                "MAX {} pages. Include multiple examples of important page types. "
                "Cover every section of the site. "
                "Respond with valid JSON only.".format(max_pages)
            ),
        )
        return self._parse_json_response(response)

    # ── Find new pages from a visited page ───────────────────

    def find_new_pages(
        self, current_url, html, links, already_captured, planned_urls, history=""
    ):
        """Check a page for structurally NEW page types not yet in the plan."""
        links_summary = "\n".join(
            "- [{}]({})".format(l.get("text", ""), l.get("url", ""))
            for l in links[:40]
            if l.get("is_internal")
        )

        already = "\n".join("- {}".format(u) for u in already_captured[:20])
        planned = "\n".join("- {}".format(u) for u in planned_urls[:20])

        prompt = """Check this page for links to pages we haven't captured yet.

Current page: {url}

Links on this page:
{links}

Already captured or planned:
{already}
{planned}

HISTORY:
{history}

Return up to 5 new pages worth capturing. Good candidates:
- Pages with different content or layout than what we already have
- Different sections of the site (about, contact, FAQ, legal, pricing, blog, etc.)
- Additional product/category pages with visually different content
- Any page that shows a different UI pattern or layout

Do NOT return:
- URLs we already captured or planned (check the lists above)
- Anchor links (#) or javascript: links
- File downloads (PDFs, images)

Respond with ONLY JSON:
{{
  "new_pages": [
    {{"url": "/path", "description": "What this page shows", "theme": "category"}}
  ]
}}

If nothing new, return: {{"new_pages": []}}""".format(
            url=current_url,
            links=links_summary or "(no links)",
            already=already or "(none)",
            planned=planned or "(none)",
            history=history or "(none)",
        )

        response = self._call_openai(
            prompt,
            system=(
                "You find new pages worth capturing for comprehensive site coverage. "
                "Up to 5 results. Respond with valid JSON only."
            ),
        )
        result = self._parse_json_response(response)
        return result.get("new_pages", [])[:3]

    # ── Find clickable UI elements worth screenshotting ──────

    def find_clickable_ui(
        self, current_url, html, clickables, history="", captures_so_far=None
    ):
        """Given a page's interactive elements, pick which ones to click and screenshot."""
        clickables_summary = "\n".join(
            "- {} '{}' (selector: {})".format(
                c.get("tag", ""), c.get("text", ""), c.get("selector", "")
            )
            for c in clickables[:25]
        )

        captures_summary = ""
        if captures_so_far:
            captures_summary = "\n".join(
                "- [{}] {} — {}".format(
                    c.get("theme", ""), c.get("url", ""), c.get("description", "")
                )
                for c in captures_so_far[-20:]
            )

        prompt = """You are inspecting a page for interactive elements that reveal NEW UI states worth screenshotting.

Current page: {url}

Interactive elements on this page:
{clickables}

ALREADY CAPTURED ({count} screenshots):
{captures}

RECENT HISTORY:
{history}

Pick which elements to click. Good candidates:
- Login/signup buttons (opens modal or form)
- Navigation menus, hamburger menus (opens dropdown)
- Tabs (shows different content)
- "Show more", "Read more", FAQ accordions
- Shopping cart, search, filters
- Language/currency switchers
- Any button that opens a modal, drawer, or overlay

Do NOT pick:
- Links to other pages (those are handled by navigation)
- Elements we already clicked (check history)
- Multiple similar items (e.g. don't click every FAQ, just one)
- Submit buttons, "close" buttons, social media links

Respond with ONLY a JSON object:

{{
  "clicks": [
    {{
      "click_text": "Exact visible text of the button/element to click",
      "description": "What UI state this reveals (e.g. 'Login modal', 'Mobile menu open')"
    }}
  ]
}}

If nothing worth clicking, return: {{"clicks": []}}""".format(
            url=current_url,
            clickables=clickables_summary or "(none)",
            count=len(captures_so_far or []),
            captures=captures_summary or "(none)",
            history=history or "(none)",
        )

        response = self._call_openai(
            prompt,
            system=(
                "You identify interactive UI elements worth screenshotting. "
                "Only pick elements that reveal new visual states (modals, menus, tabs). "
                "Check history to avoid repeating clicks. Respond with valid JSON only."
            ),
        )
        result = self._parse_json_response(response)
        return result.get("clicks", [])

    # ── Review coverage ──────────────────────────────────────

    def review_coverage(self, captures, site_plan):
        """Compare captures against plan, find gaps."""
        captures_summary = "\n".join(
            "- [{}] {} — {}".format(
                c.get("theme", ""), c.get("url", ""), c.get("description", "")
            )
            for c in captures
        )

        prompt = """Review captured screenshots against the site plan.

PLAN:
{plan}

CAPTURED ({count}):
{captures}

Find any MISSING pages (URLs not visited).

Respond with ONLY JSON:
{{
  "coverage_complete": true/false,
  "missing": [
    {{
      "url": "/path",
      "theme": "section name",
      "description": "what's missing"
    }}
  ],
  "summary": "Brief assessment"
}}""".format(
            plan=json.dumps(site_plan, indent=2),
            count=len(captures),
            captures=captures_summary,
        )

        response = self._call_openai(
            prompt,
            system=(
                "You are reviewing website screenshot coverage. "
                "Find missing pages. Respond with valid JSON only."
            ),
        )
        return self._parse_json_response(response)

    # ── Describe a page ──────────────────────────────────────

    # Heuristic theme detection from URL path — avoids API call
    _THEME_HEURISTICS = [
        # Auth
        (("login", "signin", "sign-in", "auth"), "login"),
        (("register", "signup", "sign-up"), "registration"),
        # E-commerce
        (("cart", "basket", "shopping-cart"), "cart"),
        (("checkout", "payment", "pay"), "checkout"),
        # Enterprise / SaaS (BEFORE generic keywords to win priority)
        (("calendar", "calendars", "schedule"), "calendar"),
        (("document", "documents"), "documents"),
        (("workflow", "workflows", "automation"), "workflow"),
        (("report", "reports", "analytics"), "reports"),
        (("inbox", "notification", "notifications", "messages"), "inbox"),
        (("asset", "assets", "inventory"), "assets"),
        (("task", "tasks", "todo", "todos"), "tasks"),
        (("people", "employee", "employees", "staff", "members"), "people"),
        (("leave", "vacation", "time-off", "timeoff"), "leave management"),
        (("poll", "polls", "survey", "surveys"), "survey"),
        (("announcement", "announcements"), "announcements"),
        (("forms", "requests"), "forms"),
        (("billing", "credit-card", "credit_card"), "billing"),
        # Generic pages
        (("account", "myaccount", "profile", "dashboard", "dashboards"), "account"),
        (("settings", "preferences", "user-settings"), "settings"),
        (("contact", "contacts", "support", "help"), "support"),
        (("about", "about-us"), "about"),  # Removed "company"/"team" — too generic
        (("blog", "news", "articles"), "blog"),  # Removed "post" — too generic
        (("faq", "help-center", "knowledge"), "faq"),
        (("privacy", "terms", "legal", "policy", "cookies"), "legal"),
        (("search", "find"), "search"),  # Removed "results" — too generic
        (("pricing", "plans", "subscription"), "pricing"),
        (("category", "categories", "catalog", "catalogue"), "category listing"),
        (
            ("product", "item", "detail", "listing", "obyavlenie", "d/"),
            "product listing",
        ),
        (("adding", "create", "post-ad"), "listing creation"),  # Removed "new"
        (
            (
                "nedvizhimost",
                "transport",
                "elektronika",
                "uslugi",
                "rabota",
                "real-estate",
                "vehicles",
                "electronics",
                "services",
                "jobs",
            ),
            "category listing",
        ),
    ]

    def _heuristic_describe(self, url, title):
        """Try to describe a page from URL and title alone, without API call.

        Uses SEGMENT-based matching for URL paths (not substring) to avoid
        false positives like 'company' in '/forms/company' matching 'about'.
        """
        path = urlparse(url).path.lower().rstrip("/")
        title_lower = (title or "").lower()
        # Split path into segments for precise matching
        path_segments = [s for s in path.split("/") if s]

        # Check if root/homepage
        if path in ("", "/", "/index", "/index.html"):
            return {
                "description": "Homepage of the website showing main navigation and featured content.",
                "theme": "homepage",
                "page_title": title or "Homepage",
            }

        for keywords, theme in self._THEME_HEURISTICS:
            for kw in keywords:
                # Segment match: check if any path segment STARTS with the keyword
                # This catches plurals (calendar→calendars) but avoids false
                # substring matches (company in /forms/company → NOT about)
                segment_match = any(
                    seg == kw
                    or seg.startswith(kw + "s")
                    or seg.startswith(kw + "_")
                    or seg.startswith(kw + "-")
                    for seg in path_segments
                )
                # Title match: use word-boundary matching to prevent
                # 'people' from matching 'PeopleForce' in every page title.
                # re.search with \b ensures kw matches a whole word.
                title_match = bool(
                    re.search(r"\b" + re.escape(kw) + r"\b", title_lower)
                )
                if segment_match or title_match:
                    clean_title = title if title else theme.replace("-", " ").title()
                    return {
                        "description": "{} page.".format(
                            theme.replace("-", " ").title()
                        ),
                        "theme": theme,
                        "page_title": clean_title,
                    }

        return None  # Unknown — needs API call

    def describe_page(self, url, title, html):
        """Generate a description for a screenshot.

        First tries heuristic detection from URL/title to save API calls.
        Falls back to OpenAI only for ambiguous pages.
        """
        # Try heuristic first — free, instant, no tokens
        heuristic = self._heuristic_describe(url, title)
        if heuristic:
            return heuristic

        prompt = """Describe this web page for a visual screenshot gallery.

URL: {url}
Title: {title}

Page HTML (truncated):
---
{html}
---

Respond with ONLY a JSON object:
{{
  "description": "2-3 sentence description of what this page shows (e.g. 'Product listing for shoes', 'Login form', 'About Us page')",
  "theme": "section category (e.g. homepage, products, about, blog, pricing)",
  "page_title": "Clean human-readable page title"
}}""".format(
            url=url, title=title, html=html[:3000]
        )

        response = self._call_openai(
            prompt,
            system=(
                "You are describing web pages for a visual gallery. "
                "Be concise but informative. Respond with valid JSON only."
            ),
        )

        parsed = self._parse_json_response(response)
        if parsed.get("action") == "quota_exceeded":
            # Fallback for description if AI is dead
            return {
                "description": "Captured page (AI quota exceeded)",
                "theme": "pages",
                "page_title": title or "Page",
            }
        return parsed

    # ── Dynamic agent: decide next action ───────────────────

    def decide_next_action(
        self,
        page_state,
        history,
        captures_count,
        max_captures,
        login="",
        password="",
        logged_in=False,
        clicks_on_current_page=0,
        time_remaining_seconds=0,
    ):
        """Given structured page state from browser JS, decide next action."""

        url = page_state.get("url", "")
        title = page_state.get("title", "Untitled")
        page_type = page_state.get("page_type", "unknown")
        has_login_form = page_state.get("has_login_form", False)
        text_content = page_state.get("text_content", "")

        # Format lists for LLM context
        nav_links = page_state.get("navigation_links", [])
        links = "\n".join(
            "- [{}]({})".format(link.get("text", "")[:50], link.get("url", ""))
            for link in nav_links[:40]
        )
        if not links:
            links = page_state.get("links_summary", "")

        click_els = page_state.get("clickable_elements", [])
        clickables = "\n".join(
            "- <{}> '{}' ({})".format(
                btn.get("tag", "?"), btn.get("text", "")[:50], btn.get("type", "")
            )
            for btn in click_els[:30]
        )
        if not clickables:
            clickables = page_state.get("clickables_summary", "")

        form_els = page_state.get("form_inputs", [])
        forms = "\n".join(
            "- {} name='{}' placeholder='{}'".format(
                inp.get("type", ""), inp.get("name", ""), inp.get("placeholder", "")
            )
            for inp in form_els
        )
        if not forms:
            forms = page_state.get("forms_summary", "")

        captures_info = "{} captured out of {} allowed".format(
            captures_count, max_captures
        )

        # Add captured themes breakdown so AI knows what diversity it has
        captures_summary = page_state.get("captures_summary", "")

        login_instruction = ""
        if has_login_form and not logged_in and login:
            login_instruction = (
                "\n!! LOGIN REQUIRED: This page has a login form. "
                "Credentials available for '{}'. Use 'login' action IMMEDIATELY. "
                "Do NOT skip login — we need authenticated pages.\n".format(login)
            )

        time_warning = ""
        if time_remaining_seconds > 0:
            minutes_left = int(time_remaining_seconds / 60)
            if minutes_left <= 2:
                time_warning = (
                    "\n!! CRITICAL: Only ~{} minutes remaining! "
                    "Return 'done' NOW to save progress.\n".format(minutes_left)
                )
            elif minutes_left <= 4:
                time_warning = (
                    "\n!! HURRY: Only ~{} minutes left. Stop clicking UI controls. "
                    "Only navigate to NEW uncaptured pages.\n".format(minutes_left)
                )

        click_budget_warning = ""
        if clicks_on_current_page >= 3:
            click_budget_warning = (
                "\n!! You have already clicked {} elements on THIS page. "
                "STOP clicking and NAVIGATE to a different page instead. "
                "Do not click filters, sorts, or dropdowns.\n".format(
                    clicks_on_current_page
                )
            )

        prompt = """You are a human-like web browsing agent. Explore the website methodically.

CURRENT PAGE: {url}
Title: {title}
Page type: {page_type}
{login_instruction}{time_warning}{click_budget_warning}
PAGE CONTENT:
{text_content}

LINKS ON THIS PAGE:
{links}

INTERACTIVE ELEMENTS:
{clickables}

FORMS:
{forms}

PROGRESS: {captures_info}

ALREADY CAPTURED (do NOT capture similar pages):
{captures_summary}

RECENT HISTORY (last actions):
{history}

=== RULES (follow strictly) ===

1. PRIORITY ORDER:
   a) If login form visible and not logged in → use "login" action
   b) If current view shows a NEW UI state (e.g. open modal, dropdown) → use "screenshot"
   c) NAVIGATE to a page of a DIFFERENT TYPE than what you've already captured
   d) Click interactive elements that reveal NEW UI states (hamburger menus, tabs,
      dropdowns, modals, accordions)
   e) If nothing new remains → "done"

2. PAGE TYPE DIVERSITY (MOST IMPORTANT RULE):
   - The goal is to capture every DIFFERENT page template/type on the site.
   - A UI/UX designer needs to see: homepage, category listing, product/detail page,
     about page, contact page, login/register, blog listing, blog post, FAQ, legal,
     pricing, cart, checkout, profile, settings, search results, error page.
   - Each page TYPE needs only 1-2 examples. NEVER capture 3+ pages of the same type.
   - Category pages all look the SAME — one category screenshot is enough.
   - Product detail pages all look the SAME — one product screenshot is enough.
   - Check THEME COUNTS in the captures summary. If a theme has 2+, STOP adding to it.
   - WRONG: Electronics → Phones → Phone Detail → Laptops → Laptop Detail (5 similar pages)
   - CORRECT: Electronics (1 category) → Phone Detail (1 detail) → About → Contact → Blog

3. INTERACTION RULES:
   - For 'click', use the EXACT text shown in the 'INTERACTIVE ELEMENTS' list.
   - For 'navigate', use the EXACT URL shown in the 'LINKS' list.
   - Do NOT guess element IDs or classes. Only use what is provided.
   - Do NOT click sort/order controls, pagination, or "show more".
   - Do NOT click links that just add query parameters (?sort=price).
   - SAFETY: NEVER click "Delete", "Remove", "Save", "Submit", "Confirm",
     "Approve", "Reject", "Send", "Publish", "Archive", "Deactivate",
     "Transfer", "Assign", or "Update" buttons. These modify production data.
   - You may OPEN a modal (e.g. click "Edit" or "Create") to screenshot it,
     but NEVER click the confirm/save/submit button INSIDE that modal.

4. WHAT TO EXPLORE (in this priority order):
   a) Homepage (if not captured)
   b) One category/listing page
   c) One product/detail/item page
   d) About / Company page
   e) Contact / Support page
   f) Blog listing + one blog post
   g) Login / Register form
   h) FAQ / Help center
   i) Legal / Privacy / Terms
   j) Pricing / Plans page
   k) Search results page
   l) Cart / Checkout (if accessible)
   m) Profile / Settings / Dashboard
   n) Interactive UI states: open modal, hamburger menu, tabs, dropdowns
   - After clicking a tab or opening a modal, ALWAYS follow with 'screenshot'

5. NAVIGATION STRATEGY:
   - Look at THEME COUNTS. Go to the page type you have ZERO captures of.
   - After capturing any category or listing, immediately leave to a DIFFERENT section.
   - Use the footer links — they often contain About, Contact, Legal, FAQ.
   - Use the header nav — it has the main sections.
   - NEVER drill deeper into subcategories. One level is enough.

6. EFFICIENCY:
   - You have a BUDGET of {max_captures} screenshots. Fill it with DIVERSE pages.
   - Navigate to the MOST different section from what you've already captured.
   - If you see FULL THEMES in the captures summary, those are done. Move on.

7. WHEN TO SAY "done":
   - ONLY say "done" if you've captured at least 75% of the max ({min_done} screenshots)
     OR there are truly no more unexplored page TYPES left
   - If you have fewer than {min_done} captures, keep exploring — find pages of
     DIFFERENT types, not more of the same type
   - Explore: account pages, settings, help/FAQ, legal pages, contact, about, blog

Respond with ONLY JSON:
{{
  "action": "navigate|click|type|login|screenshot|scroll|back|done",
  "url": "exact URL from list (for navigate)",
  "click_text": "exact text from list (for click)",
  "field_name": "name/placeholder from list (for type)",
  "text": "text to type (for type)",
  "theme": "category (for screenshot)",
  "description": "what and why",
  "reasoning": "brief explanation"
}}""".format(
            url=url,
            title=title,
            page_type=page_type,
            login_instruction=login_instruction,
            time_warning=time_warning,
            click_budget_warning=click_budget_warning,
            text_content=text_content[:2000] or "(empty)",
            links=links or "(no links found)",
            clickables=clickables or "(none detected)",
            forms=forms or "(none)",
            captures_info=captures_info,
            captures_summary=captures_summary or "(none yet)",
            history=history or "(none)",
            max_captures=max_captures,
            min_done=max(3, int(max_captures * 0.75)),
        )

        response = self._call_openai(
            prompt,
            system=(
                "You are a smart web browsing agent that explores websites like a REAL HUMAN USER. "
                "Your PRIMARY GOAL is to capture every DIFFERENT page type/template on the site. "
                "You NEVER capture multiple pages of the same type (e.g., multiple category listings). "
                "One category page, one product page, one blog post — then MOVE ON to about, contact, FAQ, etc. "
                "You prioritize structural diversity: homepage, listing, detail, about, contact, blog, legal, profile. "
                "If credentials are available and a login form is visible, you ALWAYS log in first. "
                "Respond with valid JSON only."
            ),
        )
        return self._parse_json_response(response)

    # ── internals ────────────────────────────────────────────

    def _call_openai(self, prompt, system="Respond with valid JSON only."):
        try:
            # Add a small static delay to be a good citizen and reduce rate limit pressure
            time.sleep(0.5)
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_completion_tokens=4096,
                response_format={"type": "json_object"},
            )
            return completion.choices[0].message.content
        except Exception as e:
            # Check for quota/billing errors to fail fast
            err_str = str(e).lower()
            if "insufficient_quota" in err_str or "billing" in err_str:
                logger.error(
                    "OpenAI QUOTA EXCEEDED (429). Triggering immediate heuristic fallback."
                )
                return "QUOTA_EXCEEDED"

            logger.error("OpenAI API error after retries: {}".format(e))
            return "{}"

    def _parse_json_response(self, response):
        if response == "QUOTA_EXCEEDED":
            # Signal immediate fallback to agent with a special action
            return {
                "action": "quota_exceeded",
                "reasoning": "OpenAI API quota exhausted",
            }

        try:
            response = response.strip()
            if response.startswith("```"):
                response = re.sub(r"^```(?:json)?\s*", "", response)
                response = re.sub(r"\s*```$", "", response)
            return json.loads(response)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse AI response: {}".format(e))
            return {}
