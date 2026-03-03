import logging
import random
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse

from core.config import AppConfig
from core.ai_analyzer import AIAnalyzer
from core.screenshot_manager import ScreenshotManager
from core.site_builder import SiteBuilder

logger = logging.getLogger("auto_screen.agent")

MAX_CONSECUTIVE_ERRORS = 3
MAX_AGENT_STEPS = 200
MAX_SAME_URL_STEPS = 6  # Allow more steps per URL for SPA/modal exploration
MAX_CLICKS_PER_PAGE = 6  # Allow clicking modals/tabs/drawers on the same page
MAX_PER_THEME = 3  # Don't screenshot more than 3 pages of the same theme
MAX_UI_INTERACTIONS_PER_PAGE = 5  # Max modal/tab clicks per page visit
AGENT_TIME_BUDGET_SECONDS = (
    840  # 14 minutes — leave 1 min buffer before Celery soft limit
)


class SiteAgent:
    """Dynamic AI agent — observes page state, decides actions in real time.

    Session lifecycle (Browser Use Cloud pattern):
    1. INIT: Launch browser, restore profile (cookies/localStorage)
    2. LOGIN: Attempt login if credentials provided and not already authenticated
    3. EXPLORE: AI-driven navigation loop (observe → decide → act → repeat)
    4. SAVE: Persist browser state to profile after successful login
    5. QUIT: Clean up browser resources
    """

    def __init__(self, config):
        self.config = config
        # Pick browser engine based on config
        if config.browser_engine == "selenium":
            from core.browser_controller import BrowserController

            self.browser = BrowserController(config)
        else:
            from core.playwright_controller import PlaywrightBrowserController

            self.browser = PlaywrightBrowserController(config)
        self.ai = AIAnalyzer(config)
        self.screenshot_mgr = ScreenshotManager(config)
        self.site_builder = SiteBuilder(config)
        self.captures = []
        self.captured_urls = set()
        self.visited_urls = set()
        self.screenshotted_urls = set()
        self.screenshotted_titles = (
            set()
        )  # Track page titles to avoid capturing same page via different URLs
        self.screenshotted_paths = []  # Track URL paths for similarity detection
        self.theme_counts = {}  # Track how many screenshots per theme
        self.discovered_urls = (
            {}
        )  # url -> {text, in_nav} — all internal links ever seen
        self.actions_history = []
        self.clicked_texts = set()
        self.clicked_actions = set()
        self.failed_nav_targets = set()  # URLs AI tried but couldn't reach
        self.ui_explored_urls = set()  # URLs where we already explored modals/tabs
        self.logged_in = False
        self._profile_saved = False
        self._last_screenshotted_url = ""  # Track for parent_url in tree structure
        self._root_url = config.url  # Remember the homepage for returning

    def run(self):
        """Dynamic agent loop: observe → decide → act → repeat.

        Session lifecycle:
        1. INIT → load profile state, launch browser
        2. LOGIN → attempt login if needed
        3. EXPLORE → AI decision loop with CDP URL discovery
        4. SAVE → persist profile state on successful login
        5. QUIT → clean up
        """
        self.screenshot_mgr.setup_output_dirs()
        url = self.config.url
        max_pages = self.config.max_pages

        logger.info("Starting dynamic AI agent for: {}".format(url))
        logger.info("Max {} screenshots, depth {}".format(max_pages, self.config.depth))

        try:
            # ── Phase 1: INIT — load profile and launch browser ──
            if (
                self.config.profile_cookies_json
                or self.config.profile_local_storage_json
                or self.config.profile_session_storage_json
            ):
                logger.info("Loading browser profile (cookies + localStorage)...")
                self.browser.load_profile(
                    cookies_json=self.config.profile_cookies_json,
                    local_storage_json=self.config.profile_local_storage_json,
                    session_storage_json=self.config.profile_session_storage_json,
                )

            state = self.browser.start(url)
            if "error" in state:
                logger.error("Cannot reach site: {}".format(state["error"]))
                return self._empty_result()

            self._record_action("navigate", url, "Loaded landing page")
            self.visited_urls.add(self.browser._normalize_url(url))

            # ── Phase 2: LOGIN — attempt login if credentials given ──
            if self.config.login and self.config.password:
                self._try_login()
                # Save profile state immediately after successful login
                if self.logged_in:
                    self._save_profile_state()

            # ── Phase 3: EXPLORE — BFS crawl ──────────────────
            bfs_queue = deque()  # URLs to visit (FIFO order)
            bfs_enqueued = set()  # normalized URLs already queued

            step = 0
            consecutive_errors = 0
            bfs_dry_steps = 0  # steps since last capture
            BFS_DRY_LIMIT = 8  # after this many steps with no capture, try recovery
            agent_start_time = time.time()

            # Mark landing page as enqueued
            landing_norm = self.browser._normalize_url(
                self.browser.current_state().get("url", url)
            )
            bfs_enqueued.add(landing_norm)
            bfs_enqueued.add(landing_norm.split("?")[0])

            while step < MAX_AGENT_STEPS and len(self.captures) < max_pages:
                step += 1

                # ── Time budget check ──
                elapsed = time.time() - agent_start_time
                time_remaining = AGENT_TIME_BUDGET_SECONDS - elapsed
                if time_remaining <= 30:
                    logger.warning(
                        "Time budget nearly exhausted ({:.0f}s elapsed), "
                        "finishing with {} captures".format(elapsed, len(self.captures))
                    )
                    break

                # ── OBSERVE: Build page state (discovers links via JS + CDP) ──
                page_state = self._build_page_state()
                current_url = page_state["url"]
                current_norm = self.browser._normalize_url(current_url)
                current_base = current_norm.split("?")[0]
                current_title = page_state.get("title", "").strip()

                # Turbo-stream endpoints are not real pages — skip
                is_turbo = "(turbo-stream" in str(page_state.get("nav_links", ""))

                if not is_turbo:
                    # ── SCREENSHOT: Auto-capture every new page ──
                    url_is_new = (
                        current_norm not in self.screenshotted_urls
                        and current_base not in self.screenshotted_urls
                    )

                    title_is_new = True
                    if current_title:
                        title_lower = current_title.lower()
                        for seen_key in self.screenshotted_titles:
                            if seen_key.startswith(title_lower + "::"):
                                title_is_new = False
                                break

                    current_path = urlparse(current_url).path.rstrip("/")
                    path_similar = current_path and self._is_path_similar_to_captured(
                        current_path
                    )

                    captured_this_step = False

                    if url_is_new and title_is_new and not path_similar:
                        # Brief SPA settle
                        time.sleep(0.3)
                        settled_url = self.browser.current_state().get(
                            "url", current_url
                        )
                        if self.browser._normalize_url(settled_url) != current_norm:
                            # URL changed during settle — re-evaluate
                            continue

                        # Heuristic description (no AI API call)
                        page_desc = self.ai._heuristic_describe(
                            current_url, current_title
                        )
                        if not page_desc:
                            page_desc = {
                                "description": current_title
                                or "Page at {}".format(urlparse(current_url).path),
                                "theme": "pages",
                            }

                        action = {
                            "action": "screenshot",
                            "theme": page_desc.get("theme", "pages"),
                            "description": page_desc.get("description", ""),
                            "reasoning": "BFS auto-screenshot",
                        }
                        pre_count = len(self.captures)
                        self._do_screenshot(action)

                        if len(self.captures) > pre_count:
                            captured_this_step = True
                            bfs_dry_steps = 0
                            logger.info(
                                "  BFS [{}/{}]: {}".format(
                                    len(self.captures),
                                    max_pages,
                                    current_title[:60] or current_url[:60],
                                )
                            )
                        else:
                            logger.info(
                                "  BFS skip (dedup/theme-cap): {}".format(
                                    current_url[:80]
                                )
                            )
                    elif not url_is_new:
                        logger.debug(
                            "  BFS skip (already seen): {}".format(current_url[:80])
                        )
                    elif not title_is_new:
                        logger.info(
                            "  BFS skip (title seen): {}".format(current_title[:60])
                        )
                        self.screenshotted_urls.add(current_norm)
                        self.screenshotted_urls.add(current_base)
                    elif path_similar:
                        logger.info(
                            "  BFS skip (similar path): {}".format(current_path[:60])
                        )
                        # Mark similar-path pages as seen so BFS skips them
                        self.screenshotted_urls.add(current_norm)
                        self.screenshotted_urls.add(current_base)
                        if current_path:
                            self.screenshotted_paths.append(current_path)

                    # Explore UI (modals, tabs, drawers) on every new page
                    # Even if screenshot was skipped (theme cap), the page
                    # may have unique modals worth capturing
                    if current_norm not in self.ui_explored_urls:
                        ui_caps = self._explore_ui_states()
                        self.ui_explored_urls.add(current_norm)
                        if ui_caps > 0:
                            bfs_dry_steps = 0
                            logger.info("  BFS: {} UI states captured".format(ui_caps))

                        # If a UI click navigated to a new page, process it
                        # in the next iteration instead of navigating away
                        post_ui_url = self.browser.current_state().get("url", "")
                        if self.browser._normalize_url(post_ui_url) != current_norm:
                            continue

                    if not captured_this_step:
                        bfs_dry_steps += 1
                else:
                    bfs_dry_steps += 1

                # ── ENQUEUE: Add discovered links to BFS queue ──
                self._enqueue_discovered_links(bfs_queue, bfs_enqueued)

                # ── DRY-RUN RECOVERY: too many steps with no captures ──
                if bfs_dry_steps >= BFS_DRY_LIMIT:
                    logger.info(
                        "  BFS: {} steps without capture, returning to root "
                        "for AI assistance".format(bfs_dry_steps)
                    )
                    bfs_dry_steps = 0
                    self._return_to_root()
                    time.sleep(0.5)
                    page_state = self._build_page_state()
                    self._enqueue_discovered_links(bfs_queue, bfs_enqueued)
                    action = self.ai.decide_next_action(
                        page_state=page_state,
                        history=self._get_history_summary(),
                        captures_count=len(self.captures),
                        max_captures=max_pages,
                        login=self.config.login,
                        password=self.config.password,
                        logged_in=self.logged_in,
                        clicks_on_current_page=0,
                        time_remaining_seconds=time_remaining,
                    )
                    if action:
                        action_type = action.get("action", "")
                        if action_type == "navigate":
                            target = action.get("url", "")
                            if target:
                                full = urljoin(
                                    self.browser.current_state().get("url", ""),
                                    target,
                                )
                                result = self.browser.navigate(full)
                                if "error" not in result:
                                    self.visited_urls.add(
                                        self.browser._normalize_url(full)
                                    )
                                    self._record_action(
                                        "navigate",
                                        full,
                                        "AI recovery after dry BFS",
                                    )
                        elif action_type == "click":
                            self._do_click(action)
                        elif action_type == "done":
                            min_caps = max(3, int(max_pages * 0.75))
                            if len(self.captures) >= min_caps:
                                logger.info(
                                    "AI says done with {} captures".format(
                                        len(self.captures)
                                    )
                                )
                                break
                    continue

                # ── NAVIGATE: Move to next BFS URL ──
                navigated = self._bfs_navigate_next(bfs_queue)

                if not navigated:
                    # Queue exhausted — recovery strategies

                    # 1. Return to root and scan for new links
                    self._return_to_root()
                    time.sleep(0.5)
                    self._build_page_state()
                    self._enqueue_discovered_links(bfs_queue, bfs_enqueued)

                    if bfs_queue:
                        navigated = self._bfs_navigate_next(bfs_queue)

                    if not navigated:
                        # 2. AI fallback — ask for a click or navigation
                        page_state = self._build_page_state()
                        action = self.ai.decide_next_action(
                            page_state=page_state,
                            history=self._get_history_summary(),
                            captures_count=len(self.captures),
                            max_captures=max_pages,
                            login=self.config.login,
                            password=self.config.password,
                            logged_in=self.logged_in,
                            clicks_on_current_page=0,
                            time_remaining_seconds=time_remaining,
                        )

                        ai_handled = False
                        if action:
                            action_type = action.get("action", "")
                            if action_type == "done":
                                min_caps = max(3, int(max_pages * 0.75))
                                if len(self.captures) >= min_caps:
                                    logger.info(
                                        "AI says done with {} captures".format(
                                            len(self.captures)
                                        )
                                    )
                                    break
                            elif action_type == "navigate":
                                target = action.get("url", "")
                                if target:
                                    full = urljoin(current_url, target)
                                    result = self.browser.navigate(full)
                                    if "error" not in result:
                                        self.visited_urls.add(
                                            self.browser._normalize_url(full)
                                        )
                                        self._record_action(
                                            "navigate",
                                            full,
                                            "AI fallback navigation",
                                        )
                                        ai_handled = True
                            elif action_type == "click":
                                self._do_click(action)
                                ai_handled = True

                        if not ai_handled:
                            # 3. Heuristic fallback
                            if not self._heuristic_explore():
                                logger.info(
                                    "Exploration exhausted: {} captures "
                                    "in {} steps".format(len(self.captures), step)
                                )
                                break

                    continue

                time.sleep(random.uniform(0.3, 0.5))

            # ── Build gallery ───────────────────────────────
            logger.info(
                "Building gallery with {} screenshots...".format(len(self.captures))
            )
            if not self.captures:
                logger.warning("No pages were captured!")
                return self._empty_result()

            themes = self.screenshot_mgr.get_themes_summary(self.captures)
            output_path = self.site_builder.build(self.captures, themes)

            logger.info(
                "Done! {} screenshots across {} themes in {} steps".format(
                    len(self.captures),
                    len(themes),
                    step,
                )
            )

            # ── Phase 4: SAVE — persist profile state ─────────
            if self.logged_in and not self._profile_saved:
                self._save_profile_state()

            return {
                "total_screenshots": len(self.captures),
                "total_themes": len(themes),
                "output_path": output_path,
            }

        finally:
            # ── Phase 5: QUIT — clean up browser ──────────
            self.browser.quit()

    # ── Build page state for AI ───────────────────────────

    def _build_page_state(self):
        """Gather structured page data via in-browser JS analysis."""
        state = self.browser.current_state()
        analysis = self.browser.analyze_page()

        # If JS analysis found nothing, the SPA may still be rendering — retry
        if (
            not analysis.get("navigation_links")
            and not analysis.get("clickable_elements")
            and not analysis.get("form_inputs")
        ):
            # Debug: log what the browser actually sees
            try:
                debug = self._eval_js(
                    """(() => {
                    return {
                        url: location.href,
                        title: document.title,
                        bodyText: (document.body && document.body.innerText || '').substring(0, 500),
                        elementCount: document.querySelectorAll('*').length,
                        linkCount: document.querySelectorAll('a[href]').length,
                        iframeCount: document.querySelectorAll('iframe').length
                    };
                    })()"""
                )
                logger.warning(
                    "  Page appears empty. Debug info: url={}, title={}, "
                    "elements={}, links={}, iframes={}, bodyText='{}'".format(
                        debug.get("url"),
                        debug.get("title"),
                        debug.get("elementCount"),
                        debug.get("linkCount"),
                        debug.get("iframeCount"),
                        debug.get("bodyText", "")[:200],
                    )
                )
            except Exception:
                pass

            # Detect turbo-stream responses — these are not real pages
            body_text = ""
            try:
                body_text = debug.get("bodyText", "") if debug else ""
            except Exception:
                pass
            if "<turbo-stream" in body_text or "<turbo-frame" in body_text:
                logger.info("  Turbo-stream/frame detected — not a real page, skipping")
                cur_state_url = self.browser.current_state().get("url", "")
                current_norm = self.browser._normalize_url(cur_state_url)
                self.screenshotted_urls.add(current_norm)
                self.screenshotted_urls.add(current_norm.split("?")[0])
                self.failed_nav_targets.add(cur_state_url)
                # Track path so similar sibling URLs are pre-filtered
                ts_path = urlparse(cur_state_url).path.rstrip("/")
                if ts_path:
                    self.screenshotted_paths.append(ts_path)
                # Return a minimal state so the agent moves on
                return {
                    "url": cur_state_url,
                    "title": "",
                    "nav_links": "(turbo-stream endpoint, not a page)",
                    "other_links": "",
                    "clickable": "",
                    "forms": "",
                }

            logger.info("  Page analysis empty, waiting for dynamic content...")
            for wait in (1, 2):
                time.sleep(wait)
                analysis = self.browser.analyze_page()
                if (
                    analysis.get("navigation_links")
                    or analysis.get("clickable_elements")
                    or analysis.get("form_inputs")
                ):
                    break
            else:
                # Last resort: try scrolling to trigger lazy rendering
                self.browser.scroll_to_bottom()
                time.sleep(1)
                self._eval_js("window.scrollTo(0, 0)")
                time.sleep(0.5)
                analysis = self.browser.analyze_page()

        # Format navigation links
        nav_links = analysis.get("navigation_links", [])
        internal_links = [l for l in nav_links if l.get("is_internal")]
        nav_section = [l for l in internal_links if l.get("in_nav")][:15]
        other_links = [l for l in internal_links if not l.get("in_nav")][:20]

        # Track all discovered internal URLs for frontier
        for l in internal_links:
            url_key = self.browser._normalize_url(l.get("url", ""))
            if url_key not in self.discovered_urls:
                self.discovered_urls[url_key] = {
                    "url": l.get("url", ""),
                    "text": l.get("text", "")[:50],
                    "in_nav": l.get("in_nav", False),
                }

        # Harvest CDP-discovered URLs (XHR/fetch dynamic routes)
        cdp_urls = self.browser.collect_cdp_discovered_urls()
        for cdp_url in cdp_urls:
            url_key = self.browser._normalize_url(cdp_url)
            if url_key not in self.discovered_urls:
                self.discovered_urls[url_key] = {
                    "url": cdp_url,
                    "text": "(dynamic)",
                    "in_nav": False,
                }

        # Current page links
        links_summary = ""
        if nav_section:
            links_summary += "NAV/MENU LINKS (on this page):\n"
            links_summary += "\n".join(
                "- [{}]({})".format(l.get("text", "")[:50], l.get("url", ""))
                for l in nav_section
            )
        if other_links:
            links_summary += "\nOTHER INTERNAL LINKS (on this page):\n"
            links_summary += "\n".join(
                "- [{}]({})".format(l.get("text", "")[:50], l.get("url", ""))
                for l in other_links
            )

        # Add unexplored URLs from frontier (key for backtracking)
        unexplored = []
        for url_key, info in self.discovered_urls.items():
            if (
                url_key not in self.visited_urls
                and url_key not in self.captured_urls
                and url_key not in self.screenshotted_urls
            ):
                target = info.get("url", "")
                if not self._is_likely_non_page_url(target):
                    unexplored.append(info)
        if unexplored:
            # Prioritize nav links
            unexplored.sort(key=lambda x: (not x.get("in_nav"), x.get("text", "")))
            links_summary += "\n\nUNEXPLORED URLS (discovered earlier, use 'back' then 'navigate'):\n"
            links_summary += "\n".join(
                "- [{}]({})".format(u.get("text", ""), u.get("url", ""))
                for u in unexplored[:15]
            )

        # Format clickable elements (from deep JS analysis)
        clickables = analysis.get("clickable_elements", [])
        clickables_summary = "\n".join(
            "- <{}> '{}' ({})".format(
                c.get("tag", "?"), c.get("text", "")[:60], c.get("type", "")
            )
            for c in clickables[:25]
        )

        # Format forms
        forms = analysis.get("form_inputs", [])
        forms_summary = "\n".join(
            "- {} name='{}' placeholder='{}' label='{}'".format(
                f.get("type", ""),
                f.get("name", ""),
                f.get("placeholder", ""),
                f.get("label", ""),
            )
            for f in forms[:15]
        )

        # Format captures with theme counts so AI knows what's overrepresented
        captures_summary = "\n".join(
            "- [{}] {} — {}".format(
                c.get("theme", ""), c.get("url", ""), c.get("description", "")[:60]
            )
            for c in self.captures[-25:]
        )
        # Add theme distribution summary
        if self.theme_counts:
            theme_dist = ", ".join(
                "{}={}".format(t, n)
                for t, n in sorted(self.theme_counts.items(), key=lambda x: -x[1])
            )
            captures_summary += "\n\nTHEME COUNTS: {}".format(theme_dist)
            overused = [t for t, n in self.theme_counts.items() if n >= MAX_PER_THEME]
            if overused:
                captures_summary += "\nFULL THEMES (do NOT add more): {}".format(
                    ", ".join(overused)
                )

        # Add failed navigation targets so AI doesn't retry them
        if self.failed_nav_targets:
            captures_summary += "\n\nFAILED URLS (DO NOT try these again — already attempted, navigation failed):\n"
            captures_summary += "\n".join(
                "- {}".format(u) for u in sorted(self.failed_nav_targets)[:20]
            )

        # Page sections
        sections = analysis.get("page_sections", [])
        sections_summary = "\n".join(
            "- <{}{}> {}".format(
                s.get("tag", ""),
                " role='{}'".format(s["role"]) if s.get("role") else "",
                s.get("heading", "(no heading)"),
            )
            for s in sections[:10]
        )

        return {
            "url": state["url"],
            "title": state.get("title", "Untitled"),
            "page_type": analysis.get("page_type", "unknown"),
            "has_login_form": analysis.get("has_login_form", False),
            "text_content": analysis.get("text_content", ""),
            "navigation_links": nav_links,
            "clickable_elements": clickables,
            "form_inputs": forms,
            "page_sections": sections,
            "links_summary": links_summary,
            "clickables_summary": clickables_summary,
            "forms_summary": forms_summary,
            "captures_summary": captures_summary,
            "sections_summary": sections_summary,
        }

    # ── UI state exploration (modals, tabs, drawers) ──────

    # Keywords in button/tab text that indicate UI state triggers worth screenshotting
    _UI_TRIGGER_KEYWORDS = (
        "create",
        "add",
        "new",
        "invite",
        "settings",
        "configure",
        "export",
        "import",
        "details",
        "info",
        "view",
        "preview",
        "open",
        "show",
        "menu",
        "more",
        "options",
        "actions",
        "manage",
        "compose",
        "schedule",
        "filter",
        # Tab-like keywords
        "tab",
        "overview",
        "general",
        "summary",
        "history",
        "activity",
        "comments",
        "notes",
        "files",
        "documents",
        "members",
        "permissions",
        "notifications",
        "integrations",
        "analytics",
        "reports",
        "logs",
        "audit",
        # Dropdown / menu triggers
        "dropdown",
        "select",
        "choose",
        "expand",
        "collapse",
        "toggle",
        # Common Russian UI text
        "\u0441\u043e\u0437\u0434\u0430\u0442\u044c",
        "\u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c",
        "\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f",
        "\u0435\u0449\u0451",
        "\u0435\u0449\u0435",
        "\u043c\u0435\u043d\u044e",
        "\u043e\u043f\u0446\u0438\u0438",
        "\u0444\u0438\u043b\u044c\u0442\u0440",
        "\u043f\u043e\u0434\u0440\u043e\u0431\u043d\u0435\u0435",
        "\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
        "\u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c",
        "\u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438",
    )

    # Keywords to SKIP — navigation, destructive actions, or data-modifying buttons
    _UI_TRIGGER_SKIP = (
        # Auth / navigation
        "sign out",
        "sign in",
        "log out",
        "log in",
        "logout",
        "login",
        "register",
        "signup",
        "sign up",
        "cancel",
        "close",
        "dismiss",
        "back",
        "home",
        "help",
        "support",
        "cookie",
        "privacy",
        "terms",
        # DESTRUCTIVE — never click on prod data
        "delete",
        "remove",
        "archive",
        "destroy",
        "purge",
        "erase",
        "deactivate",
        "disable",
        "suspend",
        "terminate",
        "revoke",
        "block",
        "ban",
        "reject",
        # DATA-MODIFYING — never save/submit/confirm on prod
        "save",
        "submit",
        "confirm",
        "apply",
        "update",
        "send",
        "approve",
        "publish",
        "deploy",
        "execute",
        "run",
        "transfer",
        "assign",
        "reassign",
        "merge",
        "yes",
        "proceed",
        "continue",
        "accept",
        "agree",
        # Editing
        "edit",
        "modify",
        "change",
        "rename",
        # Uploads that could affect data
        "upload",
        "attach",
    )

    def _take_dom_snapshot(self):
        """Take a lightweight DOM snapshot for before/after comparison.

        Returns a dict with element counts and content hashes that can detect
        visual changes (modals, tab switches, accordions, drawers) without
        relying on specific CSS class patterns.
        """
        try:
            return self._eval_js(
                """(() => {
                var body = document.body;
                if (!body) return {total: 0, visible: 0, text: ''};
                // Count total and visible elements
                var all = body.querySelectorAll('*');
                var visible = 0;
                for (var i = 0; i < all.length; i++) {
                    var r = all[i].getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) visible++;
                }
                // Get a content fingerprint from visible text in the main area
                var main = document.querySelector('main, [role="main"], .main-content, #content, #app, #root');
                var textSource = main || body;
                var textSnap = (textSource.innerText || '').substring(0, 2000);
                // Count overlay/fixed elements
                var overlays = 0;
                for (var j = 0; j < all.length; j++) {
                    var s = window.getComputedStyle(all[j]);
                    if ((s.position === 'fixed' || s.position === 'absolute') &&
                        parseInt(s.zIndex) > 99 &&
                        all[j].offsetWidth > 100 && all[j].offsetHeight > 100 &&
                        s.display !== 'none' && s.visibility !== 'hidden') {
                        overlays++;
                    }
                }
                return {
                    total: all.length,
                    visible: visible,
                    overlays: overlays,
                    text: textSnap
                };
            })()"""
            )
        except Exception:
            return None

    def _has_significant_dom_change(self, before, after):
        """Compare two DOM snapshots to detect significant visual changes.

        Catches: modals, tab content switches, accordion expansions,
        drawer slides, inline panel changes — anything that meaningfully
        alters what the user sees on the page.
        """
        if not before or not after:
            return False

        # New overlay appeared (modal, drawer, popup)
        if after.get("overlays", 0) > before.get("overlays", 0):
            return True

        # Significant element count change (content appeared/disappeared)
        total_before = before.get("total", 0)
        total_after = after.get("total", 0)
        if total_before > 0:
            change_ratio = abs(total_after - total_before) / max(total_before, 1)
            if change_ratio > 0.05 and abs(total_after - total_before) > 10:
                return True

        # Visible element count changed significantly
        vis_before = before.get("visible", 0)
        vis_after = after.get("visible", 0)
        if vis_before > 0:
            vis_change = abs(vis_after - vis_before) / max(vis_before, 1)
            if vis_change > 0.08 and abs(vis_after - vis_before) > 15:
                return True

        # Text content changed significantly (tab switch, accordion expand)
        text_before = before.get("text", "")
        text_after = after.get("text", "")
        if text_before and text_after:
            # Quick check: if first 200 chars differ, content changed
            if text_before[:200] != text_after[:200]:
                return True
            # Check if significant new text appeared
            if len(text_after) > len(text_before) + 100:
                return True

        return False

    def _explore_ui_states(self):
        """Click modal/tab/drawer triggers on the current page and screenshot each state.

        This runs AFTER the base page screenshot. It finds buttons/tabs that
        likely open modals, drawers, dropdowns, or switch tabs — clicks them,
        takes a screenshot of the new UI state, then dismisses (Escape/click away).

        Uses DOM snapshot comparison to detect ANY visual change after a click,
        not just modals — this catches tab panels, accordions, sliding drawers,
        inline expansions, and other SPA UI states that don't change the URL.

        Returns the number of UI state captures made.
        """
        captures_before = len(self.captures)
        max_pages = self.config.max_pages

        # Don't explore if we're near the capture limit
        if len(self.captures) >= max_pages - 2:
            return 0

        state = self.browser.current_state()
        current_url = state.get("url", "")
        page_title = state.get("title", "")

        # Get clickable elements on the page
        try:
            clickables = self.browser.get_clickable_elements()
        except Exception:
            clickables = []

        if not clickables:
            return 0

        # Filter for UI trigger candidates
        candidates = []
        for el in clickables:
            text = (el.get("text", "") or "").strip()
            if not text or len(text) > 60 or len(text) < 2:
                continue
            text_lower = text.lower()

            # Skip already-clicked elements
            click_key = "{}::{}".format(
                self.browser._normalize_url(current_url), text_lower
            )
            if click_key in self.clicked_actions:
                continue

            # Skip navigation/destructive keywords
            if any(skip in text_lower for skip in self._UI_TRIGGER_SKIP):
                continue

            # Identify element type
            tag = el.get("tag", "").lower()
            selector = el.get("selector", "")
            is_tab = "tab" in selector or "tab" in tag or 'role="tab"' in selector
            is_button = tag == "button" or "button" in selector
            is_link = tag == "a"
            has_keyword = any(kw in text_lower for kw in self._UI_TRIGGER_KEYWORDS)

            # Also look for aria-expanded hints (accordions, dropdowns)
            is_expandable = "expanded" in selector or "aria-haspopup" in selector

            # "..." or "⋮" or "⋯" or "…" menu triggers
            is_dots_menu = text in ("...", "\u22ee", "\u22ef", "\u2026")

            # Accept: tabs, expandables, dots menus,
            # buttons/links with trigger keyword,
            # AND any button at all (we'll use DOM diff to decide if it's worth capturing)
            if (
                is_tab
                or is_expandable
                or is_dots_menu
                or (is_button and has_keyword)
                or (is_link and has_keyword and len(text) < 30)
                or is_button  # Try ALL buttons — DOM diff will filter
            ):
                candidates.append(
                    {
                        "text": text,
                        "is_tab": is_tab,
                        "is_expandable": is_expandable or is_dots_menu,
                        "priority": (
                            0
                            if is_tab
                            else (
                                1
                                if is_expandable or is_dots_menu
                                else (2 if has_keyword else 3)
                            )
                        ),
                    }
                )

        if not candidates:
            return 0

        # Sort: tabs first, then expandables, then keyword buttons, then other buttons
        candidates.sort(key=lambda x: x["priority"])
        candidates = candidates[:MAX_UI_INTERACTIONS_PER_PAGE]

        logger.info(
            "  Exploring {} UI triggers: {}".format(
                len(candidates),
                ", ".join("'{}'".format(c["text"][:30]) for c in candidates),
            )
        )

        ui_captures = 0
        for candidate in candidates:
            if len(self.captures) >= max_pages:
                break

            click_text = candidate["text"]
            text_lower = click_text.lower()

            # Take DOM snapshot BEFORE the click
            dom_before = self._take_dom_snapshot()

            # Click the trigger
            clicked = self.browser.click_by_text(click_text)
            if not clicked:
                continue

            # Record the click
            click_key = "{}::{}".format(
                self.browser._normalize_url(current_url), text_lower
            )
            self.clicked_texts.add(click_text)
            self.clicked_actions.add(click_key)

            # Wait for content to render
            time.sleep(0.5)

            # Check if URL changed (this was a navigation, not a modal)
            new_url = self.browser.current_state().get("url", "")
            if self.browser._normalize_url(new_url) != self.browser._normalize_url(
                current_url
            ):
                # URL changed — this was actually a navigation link, handle normally
                logger.info(
                    "  UI click changed URL to {}, not a modal".format(new_url[:60])
                )
                self._record_action("click", click_text, "Navigation via UI click")
                break  # Stop UI exploration, main loop will handle the new page

            # Take DOM snapshot AFTER the click
            dom_after = self._take_dom_snapshot()

            # Detect if a modal/dialog/overlay appeared
            has_modal = self._detect_modal_overlay()

            # Detect ANY significant DOM change (tab switch, accordion, drawer, etc.)
            has_dom_change = self._has_significant_dom_change(dom_before, dom_after)

            should_capture = (
                has_modal
                or candidate["is_tab"]
                or candidate["is_expandable"]
                or has_dom_change
            )

            if should_capture:
                # Take screenshot of this UI state
                state_desc = ""
                if candidate["is_tab"]:
                    state_desc = "{} tab active".format(click_text)
                elif has_modal:
                    state_desc = "{} modal/dialog open".format(click_text)
                elif candidate["is_expandable"]:
                    state_desc = "{} expanded".format(click_text)
                elif has_dom_change:
                    state_desc = "{} UI state".format(click_text)
                else:
                    state_desc = "{} UI state".format(click_text)

                # Determine theme from base page + state
                heuristic = self.ai._heuristic_describe(current_url, page_title)
                base_theme = heuristic.get("theme", "pages") if heuristic else "pages"

                action = {
                    "action": "screenshot",
                    "theme": base_theme,
                    "description": "{} — {}".format(
                        page_title or current_url.split("/")[-1], state_desc
                    ),
                    "reasoning": "UI state capture: {}".format(state_desc),
                    "_is_ui_state": True,  # Flag for dedup bypass
                }
                pre = len(self.captures)
                self._do_screenshot(action)
                if len(self.captures) > pre:
                    ui_captures += 1
                    self._record_action("ui_state", click_text, state_desc)
                    logger.info(
                        "  UI capture: '{}' ({})".format(
                            click_text[:30],
                            "modal" if has_modal else "dom_change" if has_dom_change else "tab/expand",
                        )
                    )

                # Dismiss modal/overlay (press Escape, then wait)
                if has_modal:
                    try:
                        if self.config.browser_engine == "selenium":
                            from selenium.webdriver.common.keys import Keys

                            self.browser.driver.find_element(
                                "tag name", "body"
                            ).send_keys(Keys.ESCAPE)
                        else:
                            self.browser.page.keyboard.press("Escape")
                        time.sleep(0.3)
                    except Exception:
                        pass
            else:
                logger.debug(
                    "  UI click '{}': no visual change detected, skipping".format(
                        click_text[:30]
                    )
                )

        return ui_captures

    def _detect_modal_overlay(self):
        """Check if a modal, dialog, or overlay is currently visible on the page."""
        try:
            return (
                self._eval_js(
                    """(() => {
                // Check for dialog elements
                var dialogs = document.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"]');
                for (var i = 0; i < dialogs.length; i++) {
                    var r = dialogs[i].getBoundingClientRect();
                    if (r.width > 100 && r.height > 100) return true;
                }
                // Check for common modal class patterns (including data-state for Radix/Headless UI)
                var modals = document.querySelectorAll(
                    '[class*="modal"][class*="show"], [class*="modal"][class*="open"], ' +
                    '[class*="modal"][class*="active"], [class*="modal"][class*="visible"], ' +
                    '[class*="drawer"][class*="open"], [class*="drawer"][class*="active"], ' +
                    '[class*="overlay"][class*="show"], [class*="overlay"][class*="active"], ' +
                    '[class*="dialog"][class*="open"], [class*="popup"][class*="open"], ' +
                    '[class*="popup"][class*="show"], [class*="popup"][class*="visible"], ' +
                    '[class*="sidebar"][class*="open"], [class*="panel"][class*="open"], ' +
                    '[class*="sheet"][class*="open"], [class*="sheet"][class*="active"], ' +
                    '[class*="bottomsheet"], [class*="bottom-sheet"], ' +
                    '[data-state="open"], [data-open="true"], ' +
                    '[class*="dropdown"][class*="show"], [class*="dropdown"][class*="open"], ' +
                    '[class*="popover"][class*="show"], [class*="popover"][class*="open"], ' +
                    '[class*="tooltip"][class*="show"]'
                );
                for (var j = 0; j < modals.length; j++) {
                    var rm = modals[j].getBoundingClientRect();
                    if (rm.width > 100 && rm.height > 100) return true;
                }
                // Check for backdrop overlays (semi-transparent full-screen layers = modal behind them)
                var all = document.querySelectorAll('*');
                for (var k = 0; k < all.length; k++) {
                    var s = window.getComputedStyle(all[k]);
                    var zIdx = parseInt(s.zIndex);
                    if (isNaN(zIdx)) continue;
                    if ((s.position === 'fixed' || s.position === 'absolute') &&
                        zIdx > 99 &&
                        all[k].offsetWidth > 200 && all[k].offsetHeight > 200 &&
                        s.display !== 'none' && s.visibility !== 'hidden') {
                        // Full-screen with backdrop = modal is open
                        var bg = s.backgroundColor;
                        var isBackdrop = (bg && (bg.indexOf('rgba') !== -1 && bg.indexOf(', 0)') === -1));
                        if (all[k].offsetWidth >= window.innerWidth * 0.9 && isBackdrop) {
                            return true;
                        }
                        // Non-fullscreen floating panel = modal/popover
                        if (all[k].offsetWidth < window.innerWidth * 0.95 && zIdx > 999) {
                            return true;
                        }
                    }
                }
                return false;
            })()"""
                )
                or False
            )
        except Exception:
            return False

    # ── BFS queue management ─────────────────────────────

    def _enqueue_discovered_links(self, bfs_queue, bfs_enqueued):
        """Add newly discovered URLs to the BFS queue.

        Filters out non-page URLs, already-seen URLs, and failed targets.
        Prioritizes nav links (enqueued first) over content links.
        """
        nav_links = []
        other_links = []

        for url_key, info in list(self.discovered_urls.items()):
            if url_key in bfs_enqueued:
                continue
            target = info.get("url", "")
            if not target or not self.browser._is_same_domain(target):
                bfs_enqueued.add(url_key)
                continue
            if (
                url_key in self.screenshotted_urls
                or url_key.split("?")[0] in self.screenshotted_urls
            ):
                bfs_enqueued.add(url_key)
                continue
            if target in self.failed_nav_targets:
                bfs_enqueued.add(url_key)
                continue
            if self._is_likely_non_page_url(target):
                bfs_enqueued.add(url_key)
                continue

            bfs_enqueued.add(url_key)
            if info.get("in_nav"):
                nav_links.append(target)
            else:
                other_links.append(target)

        # Nav links first — they lead to main site sections
        for link in nav_links:
            bfs_queue.append(link)
        for link in other_links:
            bfs_queue.append(link)

    def _bfs_navigate_next(self, bfs_queue):
        """Dequeue and navigate to the next valid BFS URL.

        Applies late filters at dequeue time (theme cap, path similarity)
        so that pages captured in earlier iterations are respected.
        Returns True if successfully navigated to a new page.
        """
        full_themes = {
            t.lower().replace(" ", "-").replace("_", "-")
            for t, c in self.theme_counts.items()
            if c >= MAX_PER_THEME
        }

        skipped = 0
        while bfs_queue:
            next_url = bfs_queue.popleft()
            next_norm = self.browser._normalize_url(next_url)
            next_base = next_norm.split("?")[0]

            # Skip already screenshotted
            if (
                next_norm in self.screenshotted_urls
                or next_base in self.screenshotted_urls
            ):
                skipped += 1
                continue
            # Skip failed targets
            if next_url in self.failed_nav_targets:
                skipped += 1
                continue
            # Skip non-page URLs
            if self._is_likely_non_page_url(next_url):
                skipped += 1
                continue
            # Skip path-similar (same template, different ID)
            next_path = urlparse(next_url).path.rstrip("/")
            if next_path and self._is_path_similar_to_captured(next_path):
                self.screenshotted_urls.add(next_norm)
                self.screenshotted_urls.add(next_base)
                self.screenshotted_paths.append(next_path)
                skipped += 1
                continue
            # Skip URLs whose theme is already full
            if full_themes:
                heuristic = self.ai._heuristic_describe(next_url, "")
                if heuristic:
                    cand_theme = (
                        heuristic.get("theme", "")
                        .lower()
                        .replace(" ", "-")
                        .replace("_", "-")
                    )
                    if cand_theme in full_themes:
                        self.screenshotted_urls.add(next_norm)
                        self.screenshotted_urls.add(next_base)
                        if next_path:
                            self.screenshotted_paths.append(next_path)
                        skipped += 1
                        continue

            if skipped > 0:
                logger.info("  BFS queue: skipped {} URLs (filtered)".format(skipped))

            # Navigate to this URL
            logger.info("  BFS nav \u2192 {}".format(next_url[:80]))
            result = self.browser.navigate(next_url)
            if "error" in result:
                self.failed_nav_targets.add(next_url)
                continue

            self.visited_urls.add(next_norm)
            self._record_action("navigate", next_url, "BFS crawl")

            # Check for login form on new pages
            if self.config.login and self.config.password and not self.logged_in:
                self._try_login()

            return True

        return False

    # ── Heuristic fallback exploration (no AI needed) ─────

    def _heuristic_explore(self):
        """Navigate to the next unexplored frontier URL and screenshot it.

        This is a zero-AI fallback: when OpenAI quota is exhausted or the API
        is down, the agent can still crawl by following discovered links and
        using URL/title heuristics for page descriptions.

        Returns True if it successfully navigated to a new page.
        """
        # Refresh frontier from CDP-discovered URLs
        try:
            cdp_urls = self.browser.collect_cdp_discovered_urls()
            for cdp_url in cdp_urls:
                url_key = self.browser._normalize_url(cdp_url)
                if url_key not in self.discovered_urls:
                    self.discovered_urls[url_key] = {
                        "url": cdp_url,
                        "text": "(CDP discovered)",
                        "in_nav": False,
                    }
        except Exception:
            pass

        # Sort frontier: prefer nav links, then by text length (more descriptive = better)
        frontier = []
        for url_key, info in self.discovered_urls.items():
            if (
                url_key not in self.visited_urls
                and url_key not in self.captured_urls
                and url_key.split("?")[0] not in self.screenshotted_urls
            ):
                target = info.get("url", "")
                if (
                    target
                    and self.browser._is_same_domain(target)
                    and not self._is_likely_non_page_url(target)
                ):
                    frontier.append(info)

        # Prioritize: nav links first, then links with descriptive text
        frontier.sort(key=lambda x: (not x.get("in_nav"), -len(x.get("text", ""))))

        for info in frontier:
            target = info["url"]
            url_key = self.browser._normalize_url(target)

            logger.info("  Heuristic: navigating to {}".format(target[:80]))
            result = self.browser.navigate(target)
            if "error" in result:
                continue

            self.visited_urls.add(url_key)
            self._record_action("navigate", target, "Heuristic fallback")
            time.sleep(0.3)

            # Screenshot the page using heuristic description (no API call)
            state = self.browser.current_state()
            page_title = state.get("title", "").strip()

            # Check if this is actually a new page
            normalized = self.browser._normalize_url(state["url"])
            title_key_check = "{}::{}".format(page_title.lower(), "pages")
            if normalized in self.screenshotted_urls or (
                page_title and page_title in self.screenshotted_titles
            ):
                logger.info("  Heuristic: page already captured, skipping")
                continue

            # Use heuristic describe (no API needed)
            page_desc = self.ai._heuristic_describe(state["url"], page_title)
            if not page_desc:
                # Fallback: use link text as description
                link_text = info.get("text", "").strip()
                page_desc = {
                    "description": link_text
                    or page_title
                    or "Page at {}".format(urlparse(state["url"]).path),
                    "theme": "pages",
                    "page_title": page_title or link_text or "Page",
                }

            action = {
                "action": "screenshot",
                "theme": page_desc.get("theme", "pages"),
                "description": page_desc.get("description", ""),
                "reasoning": "Heuristic fallback: AI unavailable",
            }
            self._do_screenshot(action)
            return True

        logger.info("  Heuristic: no more frontier URLs to explore")
        return False

    # ── Fast-crawl frontier picker ───────────────────────

    def _pick_next_frontier_url(self):
        """Pick the best unexplored frontier URL for fast crawling.

        Returns a URL string or None if frontier is empty.
        Prioritizes: nav links > descriptive links > dynamic links.
        Skips non-page URLs and already-failed targets.
        """
        # Refresh CDP-discovered URLs
        try:
            cdp_urls = self.browser.collect_cdp_discovered_urls()
            for cdp_url in cdp_urls:
                url_key = self.browser._normalize_url(cdp_url)
                if url_key not in self.discovered_urls:
                    self.discovered_urls[url_key] = {
                        "url": cdp_url,
                        "text": "(CDP discovered)",
                        "in_nav": False,
                    }
        except Exception:
            pass

        # Build set of themes that are already full (at MAX_PER_THEME)
        full_themes = set()
        for t, count in self.theme_counts.items():
            if count >= MAX_PER_THEME:
                full_themes.add(t.lower().replace(" ", "-").replace("_", "-"))

        candidates = []
        for url_key, info in self.discovered_urls.items():
            target = info.get("url", "")
            if not target or not self.browser._is_same_domain(target):
                continue
            if url_key in self.visited_urls:
                continue
            if url_key in self.captured_urls:
                continue
            if url_key in self.screenshotted_urls:
                continue
            if url_key.split("?")[0] in self.screenshotted_urls:
                continue
            if target in self.failed_nav_targets:
                continue
            if self._is_likely_non_page_url(target):
                continue

            # Pre-filter: skip URLs whose heuristic theme is already full
            if full_themes:
                heuristic = self.ai._heuristic_describe(target, "")
                if heuristic:
                    cand_theme = (
                        heuristic.get("theme", "")
                        .lower()
                        .replace(" ", "-")
                        .replace("_", "-")
                    )
                    if cand_theme in full_themes:
                        # Mark as seen so we never revisit
                        self.screenshotted_urls.add(url_key)
                        self.screenshotted_urls.add(url_key.split("?")[0])
                        cand_p = urlparse(target).path.rstrip("/")
                        if cand_p:
                            self.screenshotted_paths.append(cand_p)
                        continue

            # Also skip if path is similar to already-captured pages
            cand_path = urlparse(target).path.rstrip("/")
            if cand_path and self._is_path_similar_to_captured(cand_path):
                self.screenshotted_urls.add(url_key)
                self.screenshotted_urls.add(url_key.split("?")[0])
                self.screenshotted_paths.append(cand_path)
                continue

            candidates.append(info)

        if not candidates:
            return None

        # Prioritize: nav links first, then by descriptive text length
        candidates.sort(key=lambda x: (not x.get("in_nav"), -len(x.get("text", ""))))
        return candidates[0]["url"]

    # ── URL path similarity detection ─────────────────────

    def _is_path_similar_to_captured(self, current_path):
        """Check if the current URL path is structurally similar to already-captured pages.

        Detects patterns like:
        - /category/sub1/ vs /category/sub2/ (sibling subcategories)
        - /category/sub1/sub-sub1/ vs /category/sub1/sub-sub2/ (same depth)
        - /forms/277 vs /forms/270 (same resource, different ID — 1 capture enough)

        Returns True if a sibling or cousin path was already captured,
        meaning this is likely the same page template.
        """
        if not self.screenshotted_paths:
            return False

        current_parts = [p for p in current_path.split("/") if p]
        if len(current_parts) < 2:
            # Top-level paths (e.g., /electronics/) are always considered unique
            return False

        # Get the parent path (e.g., /category/sub/ → /category/)
        current_parent = "/".join(current_parts[:-1])
        # Is the last segment a numeric ID? (e.g., /forms/277, /people/467)
        last_is_numeric = current_parts[-1].isdigit() or bool(
            re.match(r"^\d+(-|$)", current_parts[-1])
        )

        # Count how many captured pages share the same parent path
        sibling_count = 0
        for captured_path in self.screenshotted_paths:
            captured_parts = [p for p in captured_path.split("/") if p]
            if len(captured_parts) < 2:
                continue
            captured_parent = "/".join(captured_parts[:-1])
            if current_parent == captured_parent:
                sibling_count += 1

        # For /resource/ID patterns, 1 capture is enough (all same template)
        # For named sub-pages, allow 2 before considering redundant
        max_siblings = 1 if last_is_numeric else 2
        if sibling_count >= max_siblings:
            logger.debug(
                "  Path similarity: {} siblings under /{}/".format(
                    sibling_count, current_parent
                )
            )
            return True

        # Also check if we have too many pages at the same depth in the same tree
        # e.g., /a/b/c/ and /a/b/d/ and /a/b/e/ — all listing pages
        if len(current_parts) >= 3:
            grandparent = "/".join(current_parts[:-2])
            deep_count = 0
            for captured_path in self.screenshotted_paths:
                captured_parts = [p for p in captured_path.split("/") if p]
                if len(captured_parts) >= 3:
                    captured_gp = "/".join(captured_parts[:-2])
                    if grandparent == captured_gp:
                        deep_count += 1
            if deep_count >= 2:
                logger.debug(
                    "  Path similarity: {} deep pages under /{}/ tree".format(
                        deep_count, grandparent
                    )
                )
                return True

        return False

    # ── Non-page URL filter ───────────────────────────────

    # URL patterns that are usually AJAX/modal endpoints, not standalone pages
    _NON_PAGE_URL_PATTERNS = (
        "/filter_",
        "/sign_in",
        "/sign_out",
        "/sign_up",
        "/password/",
        "/password_reset",
        "/export",
        "/download",
        "/api/",
        "/_api/",
        ".json",
        ".xml",
        ".csv",
        "/toggle",
        "/sort",
        "/reorder",
        "/destroy",
        "/archive",
        "/unarchive",
        "/inline_edit",
    )

    def _is_likely_non_page_url(self, url):
        """Check if a URL is likely not a standalone page (modal, API, AJAX, etc.).

        Filters out:
        - Rails-style creation/edit endpoints (/something/new, /123/edit)
        - Filter/AJAX endpoints (/filter_nudgeable, etc.)
        - Auth endpoints (/sign_in, /sign_out)
        - Very deep paths with multiple numeric IDs (likely modal details)
        - API/data endpoints (.json, .xml, /api/)
        - Toggle/sort/destroy action endpoints
        """
        path = urlparse(url).path.lower().rstrip("/")

        # URLs ending in /new, /edit, /preview are typically Rails modal/turbo-stream endpoints
        last_segment = path.rsplit("/", 1)[-1] if "/" in path else path
        if last_segment in ("new", "edit", "preview"):
            return True

        # Pattern: /resource/ID/action — numeric ID followed by action word
        _ID_ACTION_WORDS = (
            "preview",
            "edit",
            "toggle",
            "destroy",
            "archive",
            "unarchive",
            "duplicate",
            "clone",
            "restore",
            "approve",
            "reject",
            "remind",
            "cancel",
            "confirm",
            "complete",
            "reopen",
            "close",
            "merge",
            "move",
            "assign",
            "unassign",
            "pin",
            "unpin",
            "lock",
            "unlock",
            "publish",
            "unpublish",
            "activate",
            "deactivate",
            "enable",
            "disable",
            "dismiss",
            "snooze",
        )
        if len(path.split("/")) >= 3:
            parts = path.strip("/").split("/")
            for i in range(len(parts) - 1):
                if parts[i].isdigit() and parts[i + 1] in _ID_ACTION_WORDS:
                    return True

        # Last segment is a known action word (even without numeric prefix)
        if last_segment in (
            "approve",
            "reject",
            "remind",
            "cancel",
            "confirm",
            "complete",
            "reopen",
            "close",
            "merge",
            "assign",
        ):
            return True

        # Check known non-page patterns
        for pattern in self._NON_PAGE_URL_PATTERNS:
            if pattern in path:
                return True

        # Very deep paths with 2+ numeric segments are usually modal/detail AJAX
        segments = [s for s in path.split("/") if s]
        numeric_segments = sum(1 for s in segments if s.isdigit())
        if numeric_segments >= 2 and len(segments) >= 5:
            return True

        # Deep settings / admin sub-pages (4+ segments under settings) are repetitive
        if "settings" in segments and len(segments) >= 5:
            return True

        return False

    # ── Execute action from AI ────────────────────────────

    def _execute_action(self, action):
        """Execute the action decided by AI. Returns True on success."""
        action_type = action.get("action", "done")

        if action_type == "screenshot":
            return self._do_screenshot(action)
        elif action_type == "navigate":
            return self._do_navigate(action)
        elif action_type == "click":
            return self._do_click(action)
        elif action_type == "type":
            return self._do_type(action)
        elif action_type == "login":
            return self._do_login()
        elif action_type == "scroll":
            return self._do_scroll()
        elif action_type == "back":
            return self._do_back()
        elif action_type == "execute_script":
            return self._do_execute_script(action)
        elif action_type == "done":
            return True
        else:
            logger.warning("Unknown action type: {}".format(action_type))
            return False

    # Error page indicators — skip screenshots of these
    _ERROR_PAGE_INDICATORS = (
        "page not found",
        "страница не найдена",
        "ничего не найдено",
        "internal server error",
        "bad gateway",
        "service unavailable",
    )
    # Only match these codes when they ARE the full title (not part of a brand name)
    _ERROR_TITLE_PATTERNS = ("404", "403", "500", "502", "503")

    def _do_screenshot(self, action):
        """Capture screenshot of current page."""
        state = self.browser.current_state()
        url = state["url"]
        normalized = self.browser._normalize_url(url)
        page_title = state.get("title", "").strip()

        theme = action.get("theme", "pages")
        description = action.get("description", "")
        is_ui_state = action.get("_is_ui_state", False)

        # Skip error pages (404, 500, etc.)
        title_lower = page_title.lower()
        desc_lower = description.lower()
        # Check multi-word indicators in title or description
        is_error = False
        for indicator in self._ERROR_PAGE_INDICATORS:
            if indicator in title_lower or indicator in desc_lower:
                is_error = True
                break
        # Check error codes — must be the ENTIRE title (not a brand like "OLX.uz")
        if not is_error:
            for code in self._ERROR_TITLE_PATTERNS:
                if title_lower.strip() == code:
                    is_error = True
                    break
        if is_error:
            self._record_action(
                "screenshot_skip", url, "Skipped error page: {}".format(page_title[:40])
            )
            logger.info("  Skipping error page: {}".format(page_title[:60]))
            # Mark as seen so we don't retry this URL
            self.screenshotted_urls.add(normalized)
            self.screenshotted_urls.add(normalized.split("?")[0])
            return True

        # Dedup by page title + theme (catches same page via different URLs/params)
        # BUT be careful: some sites have the same title for every page ("My App")
        # We only block if the title seems "specific" enough (long) or if it's very generic
        title_key = (
            "{}::{}".format(page_title.lower(), theme.lower()) if page_title else None
        )

        should_block_on_title = False
        if title_key and title_key in self.screenshotted_titles and not is_ui_state:
            # If title is short/generic ("Home", "Dashboard", "My Site"), allow duplicates if URL differs
            is_generic = len(page_title) < 15 or page_title.lower() in (
                "home",
                "homepage",
                "dashboard",
                "index",
                "welcome",
                "login",
            )
            if not is_generic:
                should_block_on_title = True

        if should_block_on_title:
            self._record_action(
                "screenshot_skip",
                url,
                "Same page title already captured: {}".format(page_title[:40]),
            )
            # Mark as seen so we don't retry this URL
            self.screenshotted_urls.add(normalized)
            self.screenshotted_urls.add(normalized.split("?")[0])
            return True

        # Theme-based cap: don't capture more than N pages of the same theme
        # (e.g., 2 "category listing" pages is enough to show the template)
        theme_normalized = theme.lower().replace(" ", "-").replace("_", "-")
        # Count similar themes (normalize to catch "category listing" vs "category_listing")
        current_theme_count = 0
        for t, count in self.theme_counts.items():
            t_normalized = t.lower().replace(" ", "-").replace("_", "-")
            if t_normalized == theme_normalized:
                current_theme_count += count
        if current_theme_count >= MAX_PER_THEME:
            # Still allow if this is a UI-state screenshot (modal, etc.)
            if is_ui_state:
                pass  # Always allow UI state captures
            elif not any(
                word in desc_lower
                for word in (
                    "modal",
                    "menu",
                    "dropdown",
                    "tab",
                    "expanded",
                    "open",
                    "popup",
                    "dialog",
                    "overlay",
                    "drawer",
                )
            ):
                self._record_action(
                    "screenshot_skip",
                    url,
                    "Theme '{}' already has {} captures (max {})".format(
                        theme, current_theme_count, MAX_PER_THEME
                    ),
                )
                logger.info(
                    "  Skipping: theme '{}' already has {} captures".format(
                        theme, current_theme_count
                    )
                )
                # Mark as seen so we don't retry this URL
                self.screenshotted_urls.add(normalized)
                self.screenshotted_urls.add(normalized.split("?")[0])
                return True

        # Allow re-screenshot if description differs (e.g. modal open vs closed)
        capture_key = "{}::{}".format(normalized, theme)
        # Also check with stripped query params
        base_key = "{}::{}".format(normalized.split("?")[0], theme)
        if capture_key in self.captured_urls or base_key in self.captured_urls:
            # Always allow UI state captures (same URL, different state)
            if is_ui_state:
                pass  # Bypass URL+theme dedup for UI states
            # Still allow if the description mentions a UI state change
            elif not any(
                word in desc_lower
                for word in (
                    "modal",
                    "menu",
                    "dropdown",
                    "tab",
                    "expanded",
                    "open",
                    "popup",
                    "dialog",
                    "overlay",
                    "drawer",
                    "accordion",
                    "hover",
                    "active",
                )
            ):
                self._record_action(
                    "screenshot_skip", url, "Already captured this URL+theme"
                )
                return True

        screenshot_path = self.screenshot_mgr.capture_page(
            driver=self.browser.driver,
            url=url,
            title=state.get("title", "Untitled"),
            theme=theme,
            browser_engine=self.config.browser_engine,
        )

        # Track parent for tree structure — the last page we screenshotted is the parent
        parent_url = self._last_screenshotted_url

        capture = {
            "url": url,
            "title": state.get("title", "Untitled"),
            "theme": theme,
            "description": description,
            "screenshot_path": screenshot_path,
            "parent_url": parent_url,
        }
        self.captures.append(capture)
        self.captured_urls.add(capture_key)
        self.captured_urls.add(base_key)  # Also store base (no query params) key
        self.screenshotted_urls.add(normalized)
        self.screenshotted_urls.add(normalized.split("?")[0])  # Also store base URL
        if title_key:
            self.screenshotted_titles.add(title_key)
        # Track URL path for similarity detection
        url_path = urlparse(url).path.rstrip("/")
        self.screenshotted_paths.append(url_path)
        # Track theme counts
        self.theme_counts[theme] = self.theme_counts.get(theme, 0) + 1
        self._last_screenshotted_url = url  # Remember for next screenshot's parent
        self._record_action("screenshot", url, description)

        logger.info(
            "  Captured [{}/{}]: {} — {}".format(
                len(self.captures),
                self.config.max_pages,
                state.get("title", ""),
                description[:60],
            )
        )
        return True

    def _do_navigate(self, action):
        """Navigate to a URL."""
        target_url = action.get("url", "")
        if not target_url:
            return False

        # Block URLs that already failed (AI keeps retrying the same ones)
        if target_url in self.failed_nav_targets:
            self._record_action(
                "navigate_skip", target_url, "Already failed to navigate here"
            )
            logger.info(
                "  Blocking re-navigation to failed URL: {}".format(target_url[:60])
            )
            return True

        full_url = urljoin(self.browser.current_state()["url"], target_url)
        normalized = self.browser._normalize_url(full_url)

        if not self.browser._is_same_domain(full_url):
            self._record_action("navigate_skip", full_url, "External domain blocked")
            return True

        if not self.browser._is_valid_page_url(full_url):
            self._record_action(
                "navigate_skip", full_url, "Invalid/non-page URL blocked"
            )
            return True

        # Skip URLs that differ only by query params from already-captured pages
        # but be careful — some sites use ?category=abc vs ?category=xyz
        # We only block if the query params are purely functional (sort, page, filter, limit, order)
        base_path = normalized.split("?")[0]
        query_part = normalized.split("?")[1] if "?" in normalized else ""

        # Block specific params known to be duplicates/functional
        functional_params = (
            "sort",
            "order",
            "direction",
            "page",
            "p",
            "limit",
            "per_page",
            "filter",
            "view",
            "mode",
        )
        is_functional_variant = False
        if query_part:
            for param in functional_params:
                if param + "=" in query_part.lower():
                    is_functional_variant = True
                    break

        if is_functional_variant:
            for captured_key in self.captured_urls:
                if captured_key.split("::")[0].split("?")[0] == base_path:
                    self._record_action(
                        "navigate_skip",
                        full_url,
                        "Same page functional variant blocked",
                    )
                    return True

        result = self.browser.navigate(full_url)
        if "error" in result:
            self._record_action(
                "navigate_failed",
                target_url,
                "Error: {}".format(result.get("error", "")[:80]),
            )
            return False

        self.visited_urls.add(normalized)
        self._record_action("navigate", full_url, action.get("description", ""))

        # Actively check for login form on every new page
        if self.config.login and self.config.password and not self.logged_in:
            self._try_login()

        return True

    # Patterns that indicate wasteful clicks (filters, sorts, pagination)
    _WASTE_CLICK_PATTERNS = (
        "sort",
        "filter",
        "order",
        "page ",
        "next page",
        "prev page",
        "previous",
        "показать ещё",
        "показать еще",
        "загрузить ещё",
        "load more",
        "show more",
        "по дате",
        "по цене",
        "cheapest",
        "newest",
        "oldest",
        "popular",
        "price",
        "region",
        "город",
        "location",
        "область",
        "район",
        "currency",
        "валюта",
    )

    # DANGEROUS patterns — NEVER click these (could modify/destroy prod data)
    _DANGEROUS_CLICK_PATTERNS = (
        # Destructive
        "delete",
        "remove",
        "archive",
        "destroy",
        "purge",
        "erase",
        "deactivate",
        "disable",
        "suspend",
        "terminate",
        "revoke",
        "block",
        "ban",
        # Data-modifying confirms
        "save",
        "submit",
        "confirm",
        "apply",
        "update",
        "approve",
        "reject",
        "publish",
        "deploy",
        "transfer",
        "assign",
        "reassign",
        "merge",
        "yes, delete",
        "yes, remove",
        "yes, archive",
        "yes",
        "proceed",
        # Sending
        "send",
        "send message",
        "send email",
        "send invite",
        # Editing saves
        "save changes",
        "save & close",
        "update profile",
        "сохранить",
        "удалить",
        "отправить",
        "подтвердить",
    )

    def _do_click(self, action):
        """Click an element by its visible text."""
        click_text = action.get("click_text", "")
        if not click_text:
            return False

        # Block wasteful clicks at agent level (backup for AI prompt)
        text_lower = click_text.strip().lower()
        for pattern in self._WASTE_CLICK_PATTERNS:
            if pattern in text_lower:
                self._record_action(
                    "click_skip",
                    click_text,
                    "Blocked: wasteful UI control ({})".format(pattern),
                )
                return True

        # Block dangerous/destructive clicks (protect prod data)
        for pattern in self._DANGEROUS_CLICK_PATTERNS:
            if pattern in text_lower or text_lower == pattern:
                self._record_action(
                    "click_skip",
                    click_text,
                    "BLOCKED: dangerous action ({})".format(pattern),
                )
                logger.warning(
                    "  SAFETY: blocked dangerous click '{}'".format(click_text[:40])
                )
                return True

        current_url = self.browser.current_state().get("url", "")
        click_key = "{}::{}".format(
            self.browser._normalize_url(current_url),
            text_lower,
        )

        if click_key in self.clicked_actions:
            self._record_action("click_skip", click_text, "Already clicked")
            return True

        clicked = self.browser.click_by_text(click_text)
        if clicked:
            self.clicked_texts.add(click_text)
            self.clicked_actions.add(click_key)
            self._record_action("click", click_text, action.get("description", ""))

            # After click, check if we landed on a login page
            if self.config.login and self.config.password and not self.logged_in:
                self._try_login()

            return True
        else:
            self._record_action("click_failed", click_text, "Element not found")
            return False

    def _do_type(self, action):
        """Type text into a form field."""
        field_name = action.get("field_name", "")
        text = action.get("text", "")
        if not field_name or not text:
            return False

        typed = self.browser.type_text(field_name, text)
        if typed:
            self._record_action("type", field_name, "Typed '{}'".format(text[:30]))
            return True
        else:
            self._record_action("type_failed", field_name, "Field not found")
            return False

    def _do_login(self):
        """Fill and submit login form."""
        if self.logged_in:
            self._record_action("login_skip", "", "Already logged in")
            return True

        self._try_login()
        return True

    def _do_scroll(self):
        """Scroll down the page."""
        self.browser.scroll_to_bottom()
        self._record_action("scroll", "", "Scrolled down")
        time.sleep(0.5)
        return True

    def _do_back(self):
        """Go back to previous page."""
        result = self.browser.go_back()
        if "error" in result:
            # go_back failed — return to homepage as fallback
            self._return_to_root()
            return True
        self._record_action("back", "", "Went back")
        return True

    def _return_to_root(self):
        """Navigate back to the homepage so the agent can access all top-level sections."""
        current = self.browser.current_state().get("url", "")
        root_normalized = self.browser._normalize_url(self._root_url)
        current_normalized = self.browser._normalize_url(current)
        if current_normalized == root_normalized:
            return  # Already at root
        logger.info("  Returning to homepage: {}".format(self._root_url))
        result = self.browser.navigate(self._root_url)
        if "error" not in result:
            self._record_action("navigate", self._root_url, "Returned to homepage")
        else:
            logger.warning(
                "  Failed to return to homepage: {}".format(result.get("error", ""))
            )

    def _do_execute_script(self, action):
        """Execute arbitrary JavaScript."""
        script = action.get("script", "")
        if not script:
            return False

        executed = self.browser.execute_script_action(script)
        if executed:
            self._record_action(
                "execute_script", script[:80], action.get("description", "")
            )
            time.sleep(1)
            return True
        else:
            self._record_action(
                "execute_script_failed", script[:80], "Script execution failed"
            )
            return False

    # ── Login helper ──────────────────────────────────────

    def _try_login(self):
        if self.logged_in:
            return
        success = self.browser.try_login(self.config.login, self.config.password)
        if success:
            self.logged_in = True
            self._record_action("login", self.config.login, "Login successful")
            logger.info("Login successful")
            # Save profile state immediately after login
            self._save_profile_state()

    def _save_profile_state(self):
        """Save browser state (cookies, localStorage) via profile callback.

        Implements Browser Use Cloud's Profile persistence — login state
        survives across jobs so users don't need to re-authenticate.
        """
        if self._profile_saved:
            return
        callback = self.config.save_profile_callback
        if not callback:
            return
        try:
            state = self.browser.save_profile_state()
            if state:
                callback(
                    state.get("cookies_json", ""),
                    state.get("local_storage_json", ""),
                    state.get("session_storage_json", ""),
                )
                self._profile_saved = True
                logger.info("Browser profile state saved successfully")
        except Exception as e:
            logger.warning("Failed to save profile state: {}".format(e))

    # ── History tracking ──────────────────────────────────

    def _record_action(self, action_type, target, detail=""):
        self.actions_history.append(
            {
                "action": action_type,
                "target": target,
                "detail": detail[:100],
            }
        )

    def _get_history_summary(self):
        recent = self.actions_history[-30:]
        lines = []
        for h in recent:
            lines.append("  {} {} — {}".format(h["action"], h["target"], h["detail"]))
        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────

    def _eval_js(self, script, *args):
        """Execute JavaScript in the browser — works for both Selenium and Playwright."""
        if self.config.browser_engine == "selenium":
            return self.browser.driver.execute_script(script, *args)
        else:
            # Playwright uses page.evaluate(); wrap non-function JS as IIFE
            if args:
                return self.browser.page.evaluate(
                    script, args[0] if len(args) == 1 else list(args)
                )
            return self.browser.page.evaluate(script)

    def _empty_result(self):
        return {
            "total_screenshots": 0,
            "total_themes": 0,
            "output_path": "",
        }
