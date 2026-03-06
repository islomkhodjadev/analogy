"""
Playwright-based browser controller — drop-in replacement for BrowserController.

Provides the exact same public API so SiteAgent can switch between Selenium
and Playwright via config.browser_engine.

Key advantages over Selenium/undetected-chromedriver:
- Built-in stealth (no webdriver flag leaks)
- Native async network interception (replaces CDP hacks)
- Reliable auto-wait for navigation & elements
- Full-page screenshots without CDP workarounds
- Better handling of SPAs, Shadow DOM, iframes
"""

import json
import logging
import os
import random
import re
import time
import base64
from urllib.parse import urlparse, urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from core.config import REQUEST_TIMEOUT, CHROME_WINDOW_WIDTH, CHROME_WINDOW_HEIGHT

logger = logging.getLogger("auto_screen.playwright")

# Realistic User-Agent strings
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


class PlaywrightBrowserController:
    """Playwright wrapper with the same public API as BrowserController (Selenium).

    Public methods (used by SiteAgent):
        load_profile, start, quit, save_profile_state,
        navigate, go_back, click, click_by_text, type_text,
        try_login, current_state, analyze_page, get_page_content,
        get_links, get_clickable_elements, get_form_inputs,
        scroll_to_bottom, execute_script_action,
        collect_cdp_discovered_urls,
        _normalize_url, _is_same_domain, _is_valid_page_url

    Attributes used by SiteAgent:
        driver  → replaced by `page` but we also expose `driver` as alias
    """

    def __init__(self, config):
        self.config = config
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self.driver = None  # Alias — agent uses self.browser.driver
        self.base_domain = ""

        # Profile state
        self._profile_cookies = None
        self._profile_local_storage = None
        self._profile_session_storage = None

        # Network interception — tracks discovered URLs
        self._discovered_urls = set()
        self._network_listener_active = False

    # ── lifecycle ──────────────────────────────────────────

    def load_profile(
        self, cookies_json=None, local_storage_json=None, session_storage_json=None
    ):
        """Pre-load profile state to be restored after browser starts."""
        if cookies_json:
            try:
                self._profile_cookies = (
                    json.loads(cookies_json)
                    if isinstance(cookies_json, str)
                    else cookies_json
                )
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid cookies_json, skipping")
                self._profile_cookies = None

        if local_storage_json:
            try:
                self._profile_local_storage = (
                    json.loads(local_storage_json)
                    if isinstance(local_storage_json, str)
                    else local_storage_json
                )
            except (json.JSONDecodeError, TypeError):
                self._profile_local_storage = None

        if session_storage_json:
            try:
                self._profile_session_storage = (
                    json.loads(session_storage_json)
                    if isinstance(session_storage_json, str)
                    else session_storage_json
                )
            except (json.JSONDecodeError, TypeError):
                self._profile_session_storage = None

    def start(self, url):
        """Launch Playwright browser and navigate to the starting URL."""
        self.base_domain = urlparse(url).netloc

        self._playwright = sync_playwright().start()

        # Use custom viewport if configured, otherwise Full HD 1920×1080
        viewport_w = self.config.viewport_width or CHROME_WINDOW_WIDTH
        viewport_h = self.config.viewport_height or CHROME_WINDOW_HEIGHT

        user_agent = random.choice(_USER_AGENTS)

        self._browser = self._playwright.chromium.launch(
            headless=self.config.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        self._context = self._browser.new_context(
            viewport={"width": viewport_w, "height": viewport_h},
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            device_scale_factor=1,
            java_script_enabled=True,
            ignore_https_errors=True,
            # Stealth: pretend we're not automated
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        # Anti-bot stealth scripts — injected before every page
        self._context.add_init_script(
            """
            // Hide webdriver flag
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Realistic plugins array
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    var p = [1,2,3,4,5];
                    p.namedItem = function(n){return null};
                    p.refresh = function(){};
                    return p;
                }
            });
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 4});
            Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
            Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
            // Chrome object
            window.chrome = {runtime: {}, loadTimes: function(){return {}}, csi: function(){return {}}};
            // Permissions
            var origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = function(params) {
                if (params.name === 'notifications') {
                    return Promise.resolve({state: Notification.permission});
                }
                return origQuery(params);
            };
            // WebGL
            var getparam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {
                if (p === 37445) return 'Intel Inc.';
                if (p === 37446) return 'Intel Iris OpenGL Engine';
                return getparam.call(this, p);
            };
        """
        )

        # Universal navigation detection — intercepts all SPA/JS navigation APIs
        self._context.add_init_script(
            """
            (function () {
                window.__NAV_EVENTS__ = [];

                function _navPush(type, url) {
                    try {
                        var resolved = url ? new URL(url, location.href).href : location.href;
                        window.__NAV_EVENTS__.push({type: type, url: resolved, ts: Date.now()});
                    } catch(e) {
                        window.__NAV_EVENTS__.push({type: type, url: String(url), ts: Date.now()});
                    }
                }

                // ── location navigation ──
                var origAssign = window.location.assign;
                window.location.assign = function(url) {
                    _navPush('location.assign', url);
                    return origAssign.apply(this, arguments);
                };
                var origReplace = window.location.replace;
                window.location.replace = function(url) {
                    _navPush('location.replace', url);
                    return origReplace.apply(this, arguments);
                };
                var origOpen = window.open;
                window.open = function(url) {
                    _navPush('window.open', url);
                    return origOpen.apply(this, arguments);
                };

                // ── history API (React Router, Next.js, Vue Router, etc.) ──
                var origPushState = history.pushState;
                history.pushState = function(state, title, url) {
                    _navPush('history.pushState', url);
                    return origPushState.apply(this, arguments);
                };
                var origReplaceState = history.replaceState;
                history.replaceState = function(state, title, url) {
                    _navPush('history.replaceState', url);
                    return origReplaceState.apply(this, arguments);
                };

                // ── browser events ──
                window.addEventListener('popstate', function() {
                    _navPush('popstate', location.href);
                });
                window.addEventListener('hashchange', function() {
                    _navPush('hashchange', location.href);
                });

                // ── anchor click detection ──
                document.addEventListener('click', function(e) {
                    var el = e.target;
                    while (el && el.tagName !== 'A') { el = el.parentElement; }
                    if (el && el.href) { _navPush('anchor_click', el.href); }
                }, true);

                // ── form submit detection ──
                document.addEventListener('submit', function(e) {
                    var form = e.target;
                    if (form && form.action) { _navPush('form_submit', form.action); }
                }, true);

                // ── programmatic anchor click ──
                var origAClick = HTMLAnchorElement.prototype.click;
                HTMLAnchorElement.prototype.click = function() {
                    _navPush('anchor.programmatic_click', this.href);
                    return origAClick.apply(this, arguments);
                };
            })();
        """
        )

        self.page = self._context.new_page()
        self.page.set_default_timeout(REQUEST_TIMEOUT * 1000)
        self.page.set_default_navigation_timeout(REQUEST_TIMEOUT * 1000)
        self.driver = self.page  # Alias for agent compatibility

        # Enable network interception for URL discovery
        self._start_network_monitor()

        # Navigate first
        result = self.navigate(url)

        # Restore profile (cookies + storage)
        self._restore_profile_state()

        # If profile was restored, reload to apply cookies
        if self._profile_cookies:
            try:
                self.page.goto(
                    url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000
                )
                self._wait_for_load()
                self._dismiss_overlays()
                result = self.current_state()
            except Exception as e:
                logger.warning("Profile reload failed: {}".format(e))

        return result

    def quit(self):
        """Clean up browser resources."""
        try:
            if self.page:
                self.page.close()
        except Exception:
            pass
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self.page = None
        self.driver = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._network_listener_active = False

    def resize_viewport(self, width, height):
        """Resize the browser viewport to the given dimensions."""
        if self.page:
            self.page.set_viewport_size({"width": width, "height": height})
            logger.info("Viewport resized to {}x{}".format(width, height))

    # ── Profile persistence ───────────────────────────────

    def save_profile_state(self):
        """Capture current cookies + localStorage + sessionStorage."""
        if not self.page:
            return {}

        result = {}

        # Cookies via context
        try:
            cookies = self._context.cookies()
            result["cookies_json"] = json.dumps(cookies, default=str)
            logger.info("Saved {} cookies for profile".format(len(cookies)))
        except Exception as e:
            logger.warning("Failed to save cookies: {}".format(e))

        # localStorage
        try:
            local_storage = self.page.evaluate(
                """() => {
                var data = {};
                for (var i = 0; i < localStorage.length; i++) {
                    var key = localStorage.key(i);
                    data[key] = localStorage.getItem(key);
                }
                return data;
            }"""
            )
            if local_storage:
                result["local_storage_json"] = json.dumps(local_storage, default=str)
                logger.info("Saved {} localStorage keys".format(len(local_storage)))
        except Exception as e:
            logger.warning("Failed to save localStorage: {}".format(e))

        # sessionStorage
        try:
            session_storage = self.page.evaluate(
                """() => {
                var data = {};
                for (var i = 0; i < sessionStorage.length; i++) {
                    var key = sessionStorage.key(i);
                    data[key] = sessionStorage.getItem(key);
                }
                return data;
            }"""
            )
            if session_storage:
                result["session_storage_json"] = json.dumps(
                    session_storage, default=str
                )
        except Exception:
            pass

        return result

    def _restore_profile_state(self):
        """Inject saved cookies and localStorage into the current session."""
        if not self.page:
            return

        # Restore cookies
        if self._profile_cookies:
            restored = 0
            playwright_cookies = []
            for cookie in self._profile_cookies:
                try:
                    pw_cookie = {
                        "name": cookie.get("name", ""),
                        "value": cookie.get("value", ""),
                        "domain": cookie.get("domain", self.base_domain),
                        "path": cookie.get("path", "/"),
                    }
                    if cookie.get("secure"):
                        pw_cookie["secure"] = True
                    if cookie.get("httpOnly"):
                        pw_cookie["httpOnly"] = True
                    if cookie.get("sameSite"):
                        samesite = cookie["sameSite"]
                        if samesite in ("Strict", "Lax", "None"):
                            pw_cookie["sameSite"] = samesite
                    if cookie.get("expires") and cookie["expires"] > 0:
                        pw_cookie["expires"] = cookie["expires"]
                    playwright_cookies.append(pw_cookie)
                    restored += 1
                except Exception:
                    pass

            if playwright_cookies:
                try:
                    self._context.add_cookies(playwright_cookies)
                except Exception as e:
                    logger.warning("Failed to restore cookies: {}".format(e))
            logger.info(
                "Restored {}/{} cookies from profile".format(
                    restored, len(self._profile_cookies)
                )
            )

        # Restore localStorage
        if self._profile_local_storage:
            try:
                self.page.evaluate(
                    """(data) => {
                    for (var key in data) {
                        try { localStorage.setItem(key, data[key]); } catch(e) {}
                    }
                }""",
                    self._profile_local_storage,
                )
                logger.info(
                    "Restored {} localStorage keys".format(
                        len(self._profile_local_storage)
                    )
                )
            except Exception as e:
                logger.warning("Failed to restore localStorage: {}".format(e))

        # Restore sessionStorage
        if self._profile_session_storage:
            try:
                self.page.evaluate(
                    """(data) => {
                    for (var key in data) {
                        try { sessionStorage.setItem(key, data[key]); } catch(e) {}
                    }
                }""",
                    self._profile_session_storage,
                )
            except Exception:
                pass

    # ── Network monitoring (replaces CDP) ─────────────────

    def _start_network_monitor(self):
        """Intercept network requests to discover dynamic URLs."""
        if self._network_listener_active:
            return

        def on_response(response):
            try:
                url = response.url
                if not url or url in self._discovered_urls:
                    return
                resource_type = response.request.resource_type
                if resource_type not in ("document", "xhr", "fetch", "other"):
                    return
                if not self._is_same_domain(url):
                    return
                if not self._is_valid_page_url(url):
                    return
                # Skip API-like paths
                parsed = urlparse(url)
                path_lower = parsed.path.lower()
                if any(
                    seg in path_lower
                    for seg in [
                        "/api/",
                        "/graphql",
                        "/auth/",
                        "/_next/data",
                        "/wp-json/",
                        "/rest/",
                        ".json",
                    ]
                ):
                    return
                self._discovered_urls.add(url)
            except Exception:
                pass

        try:
            self.page.on("response", on_response)
            self._network_listener_active = True
            logger.info("Playwright network monitoring enabled")
        except Exception as e:
            logger.warning("Failed to enable network monitoring: {}".format(e))

    def collect_cdp_discovered_urls(self):
        """Return newly discovered URLs from network monitoring."""
        new_urls = list(self._discovered_urls)
        if new_urls:
            logger.info("Network discovered {} internal URLs".format(len(new_urls)))
        return new_urls

    # ── SPA navigation event detection ─────────────────────

    def _collect_nav_events(self):
        """Read and flush JS-intercepted navigation events.

        Returns list of {type, url} dicts from the injected navigation
        detection script. Also feeds valid same-domain URLs into
        _discovered_urls for the crawler to pick up.
        """
        try:
            events = self.page.evaluate(
                """() => {
                    var evts = window.__NAV_EVENTS__ || [];
                    window.__NAV_EVENTS__ = [];
                    return evts;
                }"""
            )
            if events:
                for evt in events:
                    url = evt.get("url", "")
                    if url and self._is_same_domain(url) and self._is_valid_page_url(url):
                        self._discovered_urls.add(url)
                logger.debug("Collected {} nav events: {}".format(
                    len(events),
                    ", ".join("{} -> {}".format(e.get("type", "?"), e.get("url", "?")[:60]) for e in events[:5])
                ))
            return events or []
        except Exception:
            return []

    def _has_spa_navigation(self):
        """Check if any SPA navigation events occurred since last flush."""
        try:
            count = self.page.evaluate(
                "() => (window.__NAV_EVENTS__ || []).length"
            )
            return count > 0
        except Exception:
            return False

    # ── navigation ────────────────────────────────────────

    def navigate(self, url):
        """Go to URL, wait for load, dismiss overlays, return page state."""
        try:
            # Use "commit" to proceed as soon as first response is received,
            # then wait for content ourselves — this handles SPAs much better
            # than "domcontentloaded" which fires too early for dynamic content
            try:
                self.page.goto(
                    url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000
                )
            except PlaywrightTimeout:
                logger.warning("Navigation timed out (domcontentloaded) for: {}".format(url[:100]))
                # Page may have partially loaded — continue anyway

            self._wait_for_load()
            self._dismiss_overlays()
            return self.current_state()
        except PlaywrightTimeout:
            logger.warning("Navigation timed out for: {}".format(url[:100]))
            # Page may have partially loaded — still return state
            try:
                return self.current_state()
            except Exception:
                return {"url": url, "title": "Timeout", "error": "Navigation timed out"}
        except Exception as e:
            logger.error("Navigation failed: {}".format(e))
            return {"url": url, "title": "Error", "error": str(e)}

    def _dismiss_overlays(self):
        """Dismiss cookie banners, GDPR modals, newsletter popups."""
        try:
            self.page.evaluate(
                """() => {
                var dismissSelectors = [
                    '[class*="cookie"] button', '[id*="cookie"] button',
                    '[class*="consent"] button', '[id*="consent"] button',
                    '[class*="gdpr"] button', '[id*="gdpr"] button',
                    '.cc-dismiss', '.cc-allow', '.cc-accept',
                    '#onetrust-accept-btn-handler',
                    '[data-testid="cookie-accept"]',
                    'button[aria-label*="accept"]', 'button[aria-label*="Accept"]',
                    'button[aria-label*="cookie"]', 'button[aria-label*="Cookie"]',
                    'button[aria-label*="agree"]', 'button[aria-label*="Agree"]',
                    '[class*="popup"] [class*="close"]',
                    '[class*="modal"] [class*="close"]',
                    '[class*="overlay"] [class*="close"]',
                    '[class*="banner"] [class*="close"]',
                    '[aria-label="Close"]', '[aria-label="close"]',
                    '[data-dismiss="modal"]', '[data-bs-dismiss="modal"]',
                ];
                var clicked = 0;
                for (var i = 0; i < dismissSelectors.length && clicked < 3; i++) {
                    try {
                        var btns = document.querySelectorAll(dismissSelectors[i]);
                        for (var j = 0; j < btns.length && clicked < 3; j++) {
                            var r = btns[j].getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                var txt = (btns[j].innerText || '').toLowerCase();
                                if (txt.match(/accept|agree|allow|ok|got it|close|dismiss|understand|continue/i) ||
                                    btns[j].getAttribute('aria-label')) {
                                    btns[j].click();
                                    clicked++;
                                }
                            }
                        }
                    } catch(e) {}
                }
            }"""
            )
        except Exception:
            pass

    def click(self, selector):
        """Click element by CSS selector."""
        try:
            url_before = self.page.url
            self.page.click(selector, timeout=5000)
            self._wait_after_click(url_before)
            return self.current_state()
        except Exception as e:
            logger.warning("Click failed on '{}': {}".format(selector, e))
            return {"error": str(e)}

    def try_login(self, login, password):
        """Detect login form and fill credentials.

        Handles:
        - Standard username+password forms
        - Multi-step flows (email → password)
        - SPA forms (React/Vue synthetic events)
        - Finding and clicking 'Login' link if form not on current page
        """
        url_before = self.page.url

        try:
            # 1. Detect login form on current page
            detection_result = self.page.evaluate(
                """() => {
                var loginInput = null;
                var passInput = null;
                var inputs = document.querySelectorAll('input:not([type="hidden"])');
                for (var i = 0; i < inputs.length; i++) {
                    var inp = inputs[i];
                    var type = (inp.type || '').toLowerCase();
                    var name = (inp.name || '').toLowerCase();
                    var placeholder = (inp.placeholder || '').toLowerCase();
                    var id = (inp.id || '').toLowerCase();
                    var autocomplete = (inp.autocomplete || '').toLowerCase();
                    if (inp.offsetParent === null) continue;
                    if (type === 'password') {
                        passInput = inp;
                    } else if (
                        type === 'email' || type === 'tel' ||
                        name.match(/login|email|user|phone|username/) ||
                        id.match(/login|email|user|phone|username/) ||
                        placeholder.match(/login|email|user|phone/) ||
                        autocomplete.match(/email|username/)
                    ) {
                        loginInput = inp;
                    }
                }
                if (!loginInput && passInput) {
                    var allInputs = Array.from(inputs);
                    var passIdx = allInputs.indexOf(passInput);
                    for (var j = passIdx - 1; j >= 0; j--) {
                        var t = (allInputs[j].type || '').toLowerCase();
                        if ((t === 'text' || t === 'email' || t === 'tel' || t === '') &&
                            allInputs[j].offsetParent !== null) {
                            loginInput = allInputs[j];
                            break;
                        }
                    }
                }
                if (loginInput && !passInput) {
                    return { hasForm: true, multiStep: true };
                }
                if (!loginInput || !passInput) return null;
                return { hasForm: true, multiStep: false };
            }"""
            )

            # 2. If no form, look for login link/button and navigate
            if not detection_result or not detection_result.get("hasForm"):
                logger.info(
                    "No login form found. Looking for 'Login'/'Sign In' link..."
                )
                clicked = self.page.evaluate(
                    r"""() => {
                    var candidates = document.querySelectorAll('a, button, [role="button"], span, div');
                    for (var i = 0; i < candidates.length; i++) {
                        var el = candidates[i];
                        if (el.offsetParent === null) continue;
                        var txt = (el.innerText || '').trim().toLowerCase();
                        var href = (el.href || '').toLowerCase();
                        var aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        if (
                            txt === 'login' || txt === 'log in' || txt === 'sign in' ||
                            txt === 'signin' || txt === 'войти' || txt === 'кириш' ||
                            aria === 'login' || aria === 'sign in' ||
                            (el.tagName === 'A' && href.match(/\/login$|\/signin$|\/auth\//))
                        ) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }"""
                )

                if clicked:
                    logger.info("Clicked login link, waiting for page load...")
                    try:
                        self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    time.sleep(2)
                    self._wait_for_load()
                    # Re-detect
                    detection_result = self.page.evaluate(
                        """() => {
                        var loginInput = null;
                        var passInput = null;
                        var inputs = document.querySelectorAll('input:not([type="hidden"])');
                        for (var i = 0; i < inputs.length; i++) {
                            var inp = inputs[i];
                            var type = (inp.type || '').toLowerCase();
                            var name = (inp.name || '').toLowerCase();
                            var placeholder = (inp.placeholder || '').toLowerCase();
                            var id = (inp.id || '').toLowerCase();
                            var autocomplete = (inp.autocomplete || '').toLowerCase();
                            if (inp.offsetParent === null) continue;
                            if (type === 'password') passInput = inp;
                            else if (
                                type === 'email' || type === 'tel' ||
                                name.match(/login|email|user|phone|username/) ||
                                id.match(/login|email|user|phone|username/) ||
                                placeholder.match(/login|email|user|phone/) ||
                                autocomplete.match(/email|username/)
                            ) loginInput = inp;
                        }
                        if (!loginInput && passInput) {
                            var allInputs = Array.from(inputs);
                            var passIdx = allInputs.indexOf(passInput);
                            for (var j = passIdx - 1; j >= 0; j--) {
                                var t = (allInputs[j].type || '').toLowerCase();
                                if ((t === 'text' || t === 'email' || t === 'tel' || t === '') &&
                                    allInputs[j].offsetParent !== null) {
                                    loginInput = allInputs[j]; break;
                                }
                            }
                        }
                        if (loginInput && !passInput) return { hasForm: true, multiStep: true };
                        if (!loginInput || !passInput) return null;
                        return { hasForm: true, multiStep: false };
                    }"""
                    )

            if not detection_result or not detection_result.get("hasForm"):
                logger.warning("Could not find login form.")
                return False

            is_multi_step = detection_result.get("multiStep", False)
            logger.info(
                "Login form found (multi_step={}), filling...".format(is_multi_step)
            )

            # 3. Fill credentials using Playwright's native input methods
            # This is more reliable than JS injection for React/Vue apps
            if is_multi_step:
                # Fill only the login/email field first
                login_filled = self._fill_login_field(login)
                if not login_filled:
                    logger.warning("Could not fill login field")
                    return False

                # Submit the first step
                self._submit_form()
                time.sleep(3)
                self._wait_for_load()

                # Look for password field
                for attempt in range(3):
                    time.sleep(2)
                    has_pass = self.page.evaluate(
                        """() => {
                        var p = document.querySelector('input[type="password"]');
                        return p && p.getBoundingClientRect().width > 0;
                    }"""
                    )
                    if has_pass:
                        self._fill_password_field(password)
                        self._submit_form()
                        time.sleep(3)
                        self._wait_for_load()
                        break
            else:
                # Standard: fill both fields
                self._fill_login_field(login)
                time.sleep(0.3)
                self._fill_password_field(password)
                time.sleep(0.3)
                self._submit_form()
                time.sleep(3)
                self._wait_for_load()

            # 4. Verify login
            url_after = self.page.url
            still_has_password = self.page.evaluate(
                "() => !!document.querySelector('input[type=\"password\"]')"
            )
            if url_after != url_before or not still_has_password:
                logger.info("Login successful, now at: {}".format(url_after))
                return True
            else:
                logger.warning("Login may have failed (still on login page)")
                return False

        except Exception as e:
            logger.warning("Login attempt failed: {}".format(e))
            return False

    def _fill_login_field(self, value):
        """Find and fill the login/email/username field using Playwright's native typing."""
        selectors = [
            'input[type="email"]:visible',
            'input[name*="email" i]:visible',
            'input[name*="login" i]:visible',
            'input[name*="user" i]:visible',
            'input[name*="phone" i]:visible',
            'input[id*="email" i]:visible',
            'input[id*="login" i]:visible',
            'input[id*="user" i]:visible',
            'input[autocomplete="email"]:visible',
            'input[autocomplete="username"]:visible',
            'input[placeholder*="email" i]:visible',
            'input[placeholder*="login" i]:visible',
            'input[placeholder*="user" i]:visible',
            'input[placeholder*="phone" i]:visible',
        ]
        for sel in selectors:
            try:
                locator = self.page.locator(sel).first
                if locator.is_visible(timeout=500):
                    locator.click(timeout=2000)
                    locator.fill(value, timeout=2000)
                    return True
            except Exception:
                continue

        # Fallback: find the input right before the password field
        try:
            filled = self.page.evaluate(
                """(val) => {
                var passInput = document.querySelector('input[type="password"]');
                if (!passInput) return false;
                var inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
                var passIdx = inputs.indexOf(passInput);
                for (var j = passIdx - 1; j >= 0; j--) {
                    var t = (inputs[j].type || '').toLowerCase();
                    if ((t === 'text' || t === 'email' || t === 'tel' || t === '') &&
                        inputs[j].offsetParent !== null) {
                        inputs[j].focus();
                        var nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeSetter.call(inputs[j], val);
                        inputs[j].dispatchEvent(new Event('input', {bubbles: true}));
                        inputs[j].dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }""",
                value,
            )
            return bool(filled)
        except Exception:
            return False

    def _fill_password_field(self, value):
        """Find and fill the password field."""
        try:
            locator = self.page.locator('input[type="password"]:visible').first
            if locator.is_visible(timeout=1000):
                locator.click(timeout=2000)
                locator.fill(value, timeout=2000)
                return True
        except Exception:
            pass

        # JS fallback for React/Vue
        try:
            return self.page.evaluate(
                """(val) => {
                var p = document.querySelector('input[type="password"]');
                if (!p) return false;
                p.focus();
                var nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(p, val);
                p.dispatchEvent(new Event('input', {bubbles: true}));
                p.dispatchEvent(new Event('change', {bubbles: true}));
                var tracker = p._valueTracker;
                if (tracker) tracker.setValue('');
                p.dispatchEvent(new Event('input', {bubbles: true}));
                return true;
            }""",
                value,
            )
        except Exception:
            return False

    def _submit_form(self):
        """Find and click the submit button, or press Enter."""
        try:
            submitted = self.page.evaluate(
                r"""() => {
                // Look for visible password or login input to find form context
                var passInput = document.querySelector('input[type="password"]');
                var loginInput = document.querySelector('input[type="email"], input[name*="login"], input[name*="email"], input[name*="user"]');
                var contextEl = passInput || loginInput;
                if (!contextEl) return false;

                var form = contextEl.closest('form');
                var submitBtn = null;
                if (form) {
                    submitBtn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
                }
                if (!submitBtn) {
                    submitBtn = document.querySelector('button[type="submit"], input[type="submit"]');
                }
                if (!submitBtn) {
                    var btns = document.querySelectorAll('button, [role="button"], a.btn');
                    for (var b = 0; b < btns.length; b++) {
                        var bt = (btns[b].innerText || '').toLowerCase();
                        if (bt.match(/log.?in|sign.?in|submit|enter|continue|next|войти|кириш/)) {
                            submitBtn = btns[b]; break;
                        }
                    }
                }
                if (submitBtn) {
                    submitBtn.click();
                    return true;
                } else if (form) {
                    form.submit();
                    return true;
                } else {
                    // Press Enter
                    var target = passInput || loginInput;
                    target.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    return true;
                }
            }"""
            )
            return submitted
        except Exception as e:
            logger.warning("Form submit failed: {}".format(e))
            return False

    def type_text(self, selector_text, text):
        """Find an input by label/placeholder/name and type into it."""
        try:
            result = self.page.evaluate(
                """(args) => {
                var query = args[0];
                var value = args[1];
                var inputs = document.querySelectorAll('input, textarea, [contenteditable="true"]');
                var target = null;
                for (var i = 0; i < inputs.length; i++) {
                    var inp = inputs[i];
                    var ph = (inp.placeholder || '').toLowerCase();
                    var name = (inp.name || '').toLowerCase();
                    var ariaLabel = (inp.getAttribute('aria-label') || '').toLowerCase();
                    var lbl = '';
                    if (inp.id) {
                        var label = document.querySelector('label[for="' + inp.id + '"]');
                        if (label) lbl = (label.innerText || '').toLowerCase();
                    }
                    var q = query.toLowerCase();
                    if (ph.indexOf(q) !== -1 || name.indexOf(q) !== -1 ||
                        lbl.indexOf(q) !== -1 || ariaLabel.indexOf(q) !== -1) {
                        target = inp;
                        break;
                    }
                }
                if (!target) {
                    target = document.querySelector('input[type="search"], [role="searchbox"], [role="combobox"]');
                }
                if (!target) return false;
                target.scrollIntoView({block: 'center'});
                target.focus();
                target.click();
                if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') {
                    var nativeSetter = Object.getOwnPropertyDescriptor(
                        target.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype,
                        'value'
                    ).set;
                    nativeSetter.call(target, value);
                } else {
                    target.innerText = value;
                }
                var tracker = target._valueTracker;
                if (tracker) tracker.setValue('');
                target.dispatchEvent(new Event('input', {bubbles: true}));
                target.dispatchEvent(new Event('change', {bubbles: true}));
                setTimeout(function() {
                    target.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    target.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    target.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                }, 300);
                return true;
            }""",
                [selector_text, text],
            )
            time.sleep(1.5)
            return bool(result)
        except Exception as e:
            logger.warning("Type text failed for '{}': {}".format(selector_text, e))
            return False

    def click_by_text(self, text):
        """Click element by its visible text — multi-pass matching."""
        url_before = self.page.url
        try:
            # Try exact text match first via Playwright locator
            for strategy in [
                lambda: self.page.get_by_role("link", name=text, exact=True),
                lambda: self.page.get_by_role("button", name=text, exact=True),
                lambda: self.page.get_by_role("tab", name=text, exact=True),
                lambda: self.page.get_by_role("menuitem", name=text, exact=True),
                lambda: self.page.get_by_text(text, exact=True),
            ]:
                try:
                    loc = strategy()
                    if loc.count() > 0 and loc.first.is_visible(timeout=500):
                        loc.first.click(timeout=3000)
                        # Wait for potential navigation or SPA transition
                        self._wait_after_click(url_before)
                        return True
                except Exception:
                    continue

            # Fallback: JS-based click (handles cursor:pointer, data-attrs, etc.)
            safe_text = text.replace("'", "\\'").replace("\n", " ")
            result = self.page.evaluate(
                """(query) => {
                query = query.toLowerCase().trim();
                var selectors = 'button, [role="button"], a, summary, [role="tab"], [role="menuitem"], ' +
                    '[role="switch"], [role="checkbox"], [role="option"], [role="link"], ' +
                    '[onclick], [aria-expanded], [aria-haspopup], [tabindex="0"], ' +
                    '.btn, .button, .clickable, .link, .nav-link, .menu-item, .tab';
                var els = Array.from(document.querySelectorAll(selectors));
                // Also add cursor:pointer elements
                var allDiv = document.querySelectorAll('div, span, li, img, svg, i, p');
                for (var pi = 0; pi < allDiv.length && els.length < 500; pi++) {
                    try {
                        if (window.getComputedStyle(allDiv[pi]).cursor === 'pointer') {
                            els.push(allDiv[pi]);
                        }
                    } catch(e) {}
                }
                function getText(el) {
                    return (el.getAttribute('aria-label') || el.innerText || el.value || el.title || '').trim().toLowerCase();
                }
                function tryClick(el) {
                    try {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return true;
                    } catch(e) {
                        try {
                            var rect = el.getBoundingClientRect();
                            el.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: rect.left + rect.width/2, clientY: rect.top + rect.height/2}));
                            return true;
                        } catch(e2) { return false; }
                    }
                }
                // Pass 1: exact match
                for (var i = 0; i < els.length; i++) {
                    if (getText(els[i]) === query) return tryClick(els[i]);
                }
                // Pass 2: starts with
                for (var i = 0; i < els.length; i++) {
                    var t = getText(els[i]);
                    if (t.indexOf(query) === 0) return tryClick(els[i]);
                }
                // Pass 3: contains
                for (var i = 0; i < els.length; i++) {
                    var t = getText(els[i]);
                    if (t.length < 120 && t.indexOf(query) !== -1) return tryClick(els[i]);
                }
                return false;
            }""",
                safe_text,
            )
            if result:
                # JS click doesn't trigger Playwright navigation tracking,
                # so we must explicitly wait for any navigation to complete
                self._wait_after_click(url_before)
            return bool(result)
        except Exception as e:
            logger.warning("Click by text failed for '{}': {}".format(text, e))
            return False

    def _wait_after_click(self, url_before):
        """Wait for navigation or SPA transition after a click.

        If the URL changed, wait for the new page to load.
        Also checks JS-intercepted navigation events to catch SPA
        transitions (pushState/replaceState) that may not yet be
        reflected in page.url.
        """
        try:
            # Brief wait for navigation to start
            time.sleep(0.3)

            url_after = self.page.url
            spa_navigated = self._has_spa_navigation()

            if url_after != url_before:
                # Full navigation happened — wait for new page to fully load
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                self._wait_for_dom_stable(settle_time=0.8, max_wait=4)
                # Flush nav events and feed URLs into discovered set
                self._collect_nav_events()
            elif spa_navigated:
                # SPA navigation detected (pushState/replaceState/hashchange)
                # but page.url may not have updated yet
                nav_events = self._collect_nav_events()
                spa_types = [e.get("type", "") for e in nav_events]
                logger.info("SPA navigation detected: {}".format(", ".join(spa_types[:5])))
                self._wait_for_dom_stable(settle_time=0.8, max_wait=3)
            else:
                # No navigation — could be modal or DOM update, brief wait
                self._wait_for_dom_stable(settle_time=0.5, max_wait=2)
        except Exception:
            time.sleep(0.5)

    def go_back(self):
        """Navigate back."""
        try:
            self.page.go_back(wait_until="domcontentloaded", timeout=10000)
            self._wait_for_load()
            return self.current_state()
        except Exception as e:
            return {"error": str(e)}

    def execute_script_action(self, script):
        """Execute a script provided by the AI agent."""
        try:
            self.page.evaluate(script)
            return True
        except Exception as e:
            logger.warning("Script execution failed: {}".format(e))
            return False

    def get_form_inputs(self):
        """Return visible form inputs on the page."""
        try:
            return (
                self.page.evaluate(
                    """() => {
                var inputs = document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="submit"]), textarea, select'
                );
                var result = [];
                for (var i = 0; i < inputs.length && result.length < 20; i++) {
                    var inp = inputs[i];
                    var rect = inp.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var label = '';
                    if (inp.id) {
                        var lbl = document.querySelector('label[for="' + inp.id + '"]');
                        if (lbl) label = (lbl.innerText || '').trim();
                    }
                    result.push({
                        type: inp.type || inp.tagName.toLowerCase(),
                        name: inp.name || '',
                        placeholder: inp.placeholder || '',
                        label: label,
                        value: inp.value || ''
                    });
                }
                return result;
            }"""
                )
                or []
            )
        except Exception:
            return []

    def scroll_to_bottom(self):
        """Scroll to bottom to trigger lazy-loaded content."""
        try:
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
        except Exception:
            pass

    # ── reading page ──────────────────────────────────────

    def current_state(self):
        """Return current page state dict."""
        try:
            return {
                "url": self.page.url,
                "title": self.page.title() or "Untitled",
            }
        except Exception:
            return {"url": "", "title": "Error"}

    def analyze_page(self):
        """Run the same comprehensive JS analysis as the Selenium controller."""
        try:
            data = self.page.evaluate(
                self._build_analysis_script_fn(),
                self.base_domain,
            )
            if not data:
                logger.warning("Page analysis returned null")
            return data or {}
        except Exception as e:
            logger.warning("Page analysis failed: {}".format(e))
            return {}

    @staticmethod
    def _build_analysis_script_fn():
        """Return JS function body for page analysis.

        This is the same analysis logic as BrowserController._build_analysis_script
        but wrapped as a function that takes baseDomain as argument (for page.evaluate).
        """
        return """(baseDomain) => {
        try {
            var result = {
                navigation_links: [],
                clickable_elements: [],
                form_inputs: [],
                page_sections: [],
                page_type: 'unknown',
                has_login_form: false,
                text_content: ''
            };
            var seenUrls = {};
            var seenTexts = {};

            // 1. Navigation links
            var anchors = document.querySelectorAll('a[href]');
            for (var i = 0; i < anchors.length && result.navigation_links.length < 60; i++) {
                var a = anchors[i];
                var href = a.href;
                if (!href) continue;
                try {
                    var u = new URL(href);
                    if (u.protocol === 'mailto:' || u.protocol === 'tel:' || u.protocol === 'javascript:') continue;
                    var ext = u.pathname.split('.').pop().toLowerCase();
                    var skipExt = ['png','jpg','jpeg','gif','svg','webp','ico','pdf','zip','mp4','mp3','css','js','json','woff','woff2'];
                    if (skipExt.indexOf(ext) !== -1) continue;
                    if (u.origin + u.pathname === location.origin + location.pathname && u.hash) continue;
                    var path = u.pathname;
                    if (path.length > 1 && path.charAt(path.length - 1) === '/') {
                        path = path.substring(0, path.length - 1);
                    }
                    var normalized = u.origin + path;
                    if (seenUrls[normalized]) continue;
                    seenUrls[normalized] = true;
                    var text = (a.innerText || a.title || a.getAttribute('aria-label') || '').trim().substring(0, 80);
                    var isInternal = (u.hostname === baseDomain || u.hostname.endsWith('.' + baseDomain) || baseDomain.endsWith('.' + u.hostname));
                    var rect = a.getBoundingClientRect();
                    var visible = rect.width > 0 && rect.height > 0;
                    var inNav = false;
                    try { inNav = !!a.closest('nav, header, [role="navigation"], .nav, .menu, .sidebar'); } catch(ce) {}
                    if (!visible && !inNav) continue;
                    result.navigation_links.push({
                        url: href, text: text || u.pathname,
                        is_internal: isInternal, in_nav: inNav
                    });
                } catch(e) {}
            }

            // 2. Clickable elements
            function isClickCandidate(el) {
                var tag = el.tagName.toLowerCase();
                if (tag === 'a' || tag === 'script' || tag === 'style' || tag === 'html' || tag === 'body') return false;
                var rect = el.getBoundingClientRect();
                if (rect.width < 10 || rect.height < 10) return false;
                if (rect.top > 5000) return false;
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                var style = window.getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none') return false;
                return true;
            }
            function getClickText(el) {
                var text = (el.getAttribute('aria-label') || '').trim();
                if (text) return text.substring(0, 80);
                text = (el.value || '').trim();
                if (text) return text.substring(0, 80);
                text = (el.title || '').trim();
                if (text) return text.substring(0, 80);
                text = (el.innerText || '').trim();
                if (text.length > 200) {
                    var firstLine = text.split(String.fromCharCode(10))[0].trim();
                    if (firstLine.length > 0 && firstLine.length < 100) return firstLine.substring(0, 80);
                    return '';
                }
                return text.substring(0, 80);
            }
            function addClickable(el, reason) {
                if (result.clickable_elements.length >= 100) return;
                var text = getClickText(el);
                if (!text) return;
                var textKey = text.toLowerCase().substring(0, 40);
                if (seenTexts[textKey]) return;
                seenTexts[textKey] = true;
                result.clickable_elements.push({text: text, tag: el.tagName.toLowerCase(), type: reason});
            }

            var clickSelectors = 'button, [role="button"], [role="tab"], [role="menuitem"], ' +
                '[role="switch"], [role="checkbox"], [role="option"], [role="link"], summary, ' +
                '[onclick], [aria-expanded], [aria-haspopup], [tabindex="0"]';
            var clickEls = document.querySelectorAll(clickSelectors);
            for (var j = 0; j < clickEls.length; j++) {
                var el = clickEls[j];
                if (!isClickCandidate(el)) continue;
                var tag = el.tagName.toLowerCase();
                var reason = tag === 'button' || tag === 'summary' ? tag :
                    (el.getAttribute('role') ? 'role:' + el.getAttribute('role') :
                    (el.hasAttribute('onclick') ? 'onclick' : 'aria'));
                addClickable(el, reason);
            }

            // Pass 2: Button-like anchors (no real href)
            var btnAnchors = document.querySelectorAll('a[href="#"], a[href^="javascript:"], a:not([href])');
            for (var ba = 0; ba < btnAnchors.length; ba++) {
                if (result.clickable_elements.length >= 140) break;
                var baEl = btnAnchors[ba];
                if (!isClickCandidate(baEl)) continue;
                addClickable(baEl, 'anchor-button');
            }

            // Pass 3: Framework data attributes (React, Vue, Angular, Bootstrap, etc.)
            var frameworkSelectors = '[data-toggle], [data-bs-toggle], [data-action], ' +
                '[ng-click], [v-on\\:click], [\\@click], [x-on\\:click], ' +
                '[wire\\:click], [data-click], [data-onclick]';
            try {
                var frameworkEls = document.querySelectorAll(frameworkSelectors);
                for (var fw = 0; fw < frameworkEls.length; fw++) {
                    if (result.clickable_elements.length >= 140) break;
                    var fwEl = frameworkEls[fw];
                    if (!isClickCandidate(fwEl)) continue;
                    var fwReason = fwEl.hasAttribute('data-toggle') || fwEl.hasAttribute('data-bs-toggle')
                        ? 'data-toggle' : 'framework-binding';
                    addClickable(fwEl, fwReason);
                }
            } catch(fwe) {}

            // Pass 4: Inline event handlers beyond onclick
            var eventSelectors = '[onmousedown], [onmouseup], [ontouchstart], [ontouchend], [onpointerdown]';
            try {
                var eventEls = document.querySelectorAll(eventSelectors);
                for (var ev = 0; ev < eventEls.length; ev++) {
                    if (result.clickable_elements.length >= 140) break;
                    var evEl = eventEls[ev];
                    if (!isClickCandidate(evEl)) continue;
                    addClickable(evEl, 'event-handler');
                }
            } catch(eve) {}

            // Pass 5: CSS class patterns for common button-like elements
            var classSelectors = '.btn, .button, .dropdown-toggle, .accordion-header, ' +
                '.accordion-button, .tab, .tab-item, .nav-link, .card, .card-header, ' +
                '.list-group-item, .page-link, .chip, .tag, .badge[role], ' +
                '.clickable, .selectable, .action-item, .trigger';
            try {
                var classEls = document.querySelectorAll(classSelectors);
                for (var cl = 0; cl < classEls.length; cl++) {
                    if (result.clickable_elements.length >= 140) break;
                    var clEl = classEls[cl];
                    if (!isClickCandidate(clEl)) continue;
                    addClickable(clEl, 'css-class');
                }
            } catch(cle) {}

            // Pass 6: Framework JS handler detection (React, Vue)
            try {
                var allInteractive = document.querySelectorAll('div, span, li, td, label, section, article');
                for (var ri = 0; ri < allInteractive.length; ri++) {
                    if (result.clickable_elements.length >= 140) break;
                    var riEl = allInteractive[ri];
                    if (!isClickCandidate(riEl)) continue;
                    var hasHandler = false;
                    // React fiber detection
                    var keys = Object.keys(riEl);
                    for (var rk = 0; rk < keys.length; rk++) {
                        if (keys[rk].indexOf('__reactFiber') === 0 || keys[rk].indexOf('__reactProps') === 0) {
                            try {
                                if (riEl[keys[rk]] && riEl[keys[rk]].onClick) {
                                    hasHandler = true;
                                    break;
                                }
                            } catch(re) {}
                        }
                    }
                    // Vue event detection
                    if (!hasHandler && (riEl.__vue__ || riEl._vei || riEl.__vueParentComponent)) {
                        try {
                            if (riEl._vei && riEl._vei.onClick) hasHandler = true;
                            else if (riEl.__vue__ && riEl.__vue__.$listeners && riEl.__vue__.$listeners.click) hasHandler = true;
                        } catch(ve) {}
                    }
                    if (hasHandler) {
                        addClickable(riEl, 'js-handler');
                    }
                }
            } catch(jse) {}

            // Pass 7: cursor:pointer pass (catch-all)
            var pointerCandidates = document.querySelectorAll('div, span, li, td, th, label, img, svg, p, h1, h2, h3, h4, h5, h6');
            for (var cp = 0; cp < pointerCandidates.length; cp++) {
                if (result.clickable_elements.length >= 140) break;
                var cpEl = pointerCandidates[cp];
                if (!isClickCandidate(cpEl)) continue;
                try {
                    if (window.getComputedStyle(cpEl).cursor !== 'pointer') continue;
                    if (cpEl.closest('a, button, [role="button"]')) continue;
                } catch(ce2) { continue; }
                addClickable(cpEl, 'cursor:pointer');
            }

            // 3. Form inputs
            var inputs = document.querySelectorAll('input:not([type="hidden"]), textarea, select');
            var hasPass = false;
            for (var k = 0; k < inputs.length && result.form_inputs.length < 15; k++) {
                var inp = inputs[k];
                var inpRect = inp.getBoundingClientRect();
                if (inpRect.width === 0 || inpRect.height === 0) continue;
                var itype = (inp.type || inp.tagName).toLowerCase();
                if (itype === 'password') hasPass = true;
                var ilabel = '';
                if (inp.id) {
                    var lbl = document.querySelector('label[for="' + inp.id + '"]');
                    if (lbl) ilabel = (lbl.innerText || '').trim();
                }
                result.form_inputs.push({
                    type: itype, name: inp.name || '',
                    placeholder: inp.placeholder || '', label: ilabel
                });
            }
            result.has_login_form = hasPass;

            // 4. Page sections
            var sections = document.querySelectorAll('nav, main, header, footer, section, article, aside, [role="navigation"], [role="main"], [role="banner"]');
            for (var s = 0; s < sections.length && result.page_sections.length < 10; s++) {
                var sec = sections[s];
                var heading = sec.querySelector('h1, h2, h3');
                result.page_sections.push({
                    tag: sec.tagName.toLowerCase(),
                    role: sec.getAttribute('role') || '',
                    heading: heading ? heading.innerText.trim().substring(0, 80) : '',
                    id: sec.id || ''
                });
            }

            // 5. Page type
            var metaTag = document.querySelector('meta[name="description"]');
            var metaDesc = metaTag ? (metaTag.content || '') : '';
            var h1 = document.querySelector('h1');
            var h1Text = h1 ? h1.innerText.trim() : '';
            if (hasPass) result.page_type = 'login/auth';
            else if (location.pathname === '/' || location.pathname === '') result.page_type = 'homepage';
            else if (document.querySelector('.product-detail, .pdp, [itemtype*="Product"]')) result.page_type = 'product';
            else if (document.querySelector('.cart, .basket, .shopping-cart')) result.page_type = 'cart';
            else if (document.querySelector('article, .blog-post, .post-content')) result.page_type = 'article/blog';
            else if (document.querySelector('.search-results')) result.page_type = 'search';
            else if (document.querySelectorAll('.product-card, .product-item, .card').length > 3) result.page_type = 'listing/catalog';

            // 6. Text content
            var headings = document.querySelectorAll('h1, h2, h3');
            var headingTexts = [];
            for (var hi = 0; hi < headings.length && hi < 10; hi++) {
                var ht = headings[hi].innerText.trim().substring(0, 80);
                if (ht) headingTexts.push(headings[hi].tagName.toLowerCase() + ': ' + ht);
            }
            result.text_content = 'Title: ' + document.title +
                String.fromCharCode(10) + 'H1: ' + h1Text +
                String.fromCharCode(10) + 'Meta: ' + metaDesc.substring(0, 150) +
                String.fromCharCode(10) + 'Headings:' +
                String.fromCharCode(10) + headingTexts.join(String.fromCharCode(10));

            return result;
        } catch(e) {
            return {
                navigation_links: [], clickable_elements: [], form_inputs: [],
                page_sections: [], page_type: 'error', has_login_form: false,
                text_content: 'JS analysis error: ' + e.message, js_error: e.message
            };
        }
        }"""

    def get_page_content(self, max_chars=15000):
        """Return cleaned HTML for AI analysis."""
        try:
            html = self.page.content()
        except Exception:
            return ""
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
        html = re.sub(r"\s+", " ", html)
        if len(html) > max_chars:
            html = html[:max_chars] + "\n<!-- TRUNCATED -->"
        return html

    def get_links(self):
        """Return all internal links on the page."""
        try:
            raw_links = self.page.evaluate(
                """() => {
                var links = document.querySelectorAll('a[href]');
                var result = [];
                var seen = new Set();
                for (var i = 0; i < links.length; i++) {
                    var href = links[i].href;
                    if (!href || seen.has(href)) continue;
                    seen.add(href);
                    result.push({
                        url: href,
                        text: (links[i].innerText || '').trim().substring(0, 100)
                    });
                }
                return result;
            }"""
            )
        except Exception as e:
            logger.warning("Failed to extract links: {}".format(e))
            return []

        links = []
        seen = set()
        for item in raw_links or []:
            url = item.get("url", "")
            if not url:
                continue
            normalized = self._normalize_url(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            if not self._is_valid_page_url(url):
                continue
            links.append(
                {
                    "url": url,
                    "text": item.get("text", ""),
                    "is_internal": self._is_same_domain(url),
                }
            )
        return links

    def get_clickable_elements(self):
        """Return interactive elements that might reveal content.

        Uses multiple detection passes to catch:
        - Standard semantic elements (button, role=button, tabs, etc.)
        - Framework-bound elements (data-toggle, @click, ng-click, etc.)
        - Elements with JS event handlers (React onClick, Vue _vei, etc.)
        - CSS class patterns (.btn, .dropdown-toggle, .accordion-header, etc.)
        - cursor:pointer elements (catch-all)
        """
        try:
            return (
                self.page.evaluate(
                    """() => {
                var seen = new Set();
                var elements = [];
                var MAX = 80;

                function addEl(el, selector) {
                    if (elements.length >= MAX) return;
                    var text = (el.getAttribute('aria-label') || el.innerText || '').trim().substring(0, 80);
                    if (!text || seen.has(text.toLowerCase())) return;
                    var rect = el.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) return;
                    if (rect.top > 5000) return;
                    var style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return;
                    seen.add(text.toLowerCase());
                    elements.push({selector: selector, text: text, tag: el.tagName.toLowerCase()});
                }

                // Pass 1: Semantic clickables
                var selectors = [
                    "button:not([disabled])", "[role='button']", "[role='tab']",
                    "[role='menuitem']", "[role='switch']", "[role='link']",
                    "[aria-expanded]", "[aria-haspopup]",
                    "details > summary",
                    "a[href='#']", "a[href='javascript:void(0)']",
                    "[tabindex='0']"
                ];
                for (var s = 0; s < selectors.length; s++) {
                    var found = document.querySelectorAll(selectors[s]);
                    for (var i = 0; i < found.length; i++) {
                        addEl(found[i], selectors[s]);
                    }
                }

                // Pass 2: Framework data attributes
                try {
                    var fwSel = '[data-toggle], [data-bs-toggle], [data-action], ' +
                        '[ng-click], [v-on\\\\:click], [\\\\@click], [x-on\\\\:click], ' +
                        '[wire\\\\:click], [data-click]';
                    var fwEls = document.querySelectorAll(fwSel);
                    for (var fw = 0; fw < fwEls.length; fw++) addEl(fwEls[fw], 'framework');
                } catch(e) {}

                // Pass 3: CSS class patterns
                try {
                    var clsSel = '.btn, .button, .dropdown-toggle, .accordion-header, ' +
                        '.accordion-button, .tab, .tab-item, .nav-link, .card-header, ' +
                        '.list-group-item, .chip, .tag, .clickable, .selectable, .action-item';
                    var clsEls = document.querySelectorAll(clsSel);
                    for (var cl = 0; cl < clsEls.length; cl++) addEl(clsEls[cl], 'css-class');
                } catch(e) {}

                // Pass 4: React/Vue JS handler detection
                try {
                    var interactiveEls = document.querySelectorAll('div, span, li, td, label');
                    for (var ri = 0; ri < interactiveEls.length && elements.length < MAX; ri++) {
                        var riEl = interactiveEls[ri];
                        var rect = riEl.getBoundingClientRect();
                        if (rect.width < 10 || rect.height < 10 || rect.top > 5000) continue;
                        var hasHandler = false;
                        var keys = Object.keys(riEl);
                        for (var rk = 0; rk < keys.length; rk++) {
                            if (keys[rk].indexOf('__reactProps') === 0) {
                                try { if (riEl[keys[rk]].onClick) { hasHandler = true; break; } } catch(e) {}
                            }
                        }
                        if (!hasHandler && riEl._vei) {
                            try { if (riEl._vei.onClick) hasHandler = true; } catch(e) {}
                        }
                        if (hasHandler) addEl(riEl, 'js-handler');
                    }
                } catch(e) {}

                // Pass 5: cursor:pointer (catch-all)
                var pointerEls = document.querySelectorAll('div, span, li, label, td, section');
                for (var p = 0; p < pointerEls.length && elements.length < MAX; p++) {
                    var pel = pointerEls[p];
                    try {
                        if (window.getComputedStyle(pel).cursor !== 'pointer') continue;
                        if (pel.closest('a, button, [role="button"]')) continue;
                        addEl(pel, 'cursor:pointer');
                    } catch(e) {}
                }
                return elements;
            }"""
                )
                or []
            )
        except Exception as e:
            logger.warning("Failed to get clickable elements: {}".format(e))
            return []

    # ── Wait helpers ──────────────────────────────────────

    def _wait_for_load(self, timeout=15):
        """Wait for page readiness: load state, DOM stability, content presence."""
        # Playwright's built-in load state wait
        try:
            self.page.wait_for_load_state("load", timeout=timeout * 1000)
        except Exception:
            pass

        time.sleep(0.3)

        # Wait for network to be idle (no pending requests for 300ms)
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        # DOM stability check (reduced settle time)
        self._wait_for_dom_stable(settle_time=0.8, max_wait=5)

        # Content presence check (reduced timeout)
        self._wait_for_content(max_wait=5)

    def _wait_for_dom_stable(self, settle_time=0.8, max_wait=5):
        """Poll DOM element count until it stops changing."""
        try:
            last_count = self.page.evaluate(
                "() => document.querySelectorAll('*').length"
            )
        except Exception:
            time.sleep(1)
            return

        stable_since = time.time()
        deadline = time.time() + max_wait

        while time.time() < deadline:
            time.sleep(0.3)
            try:
                current_count = self.page.evaluate(
                    "() => document.querySelectorAll('*').length"
                )
            except Exception:
                break
            if current_count != last_count:
                last_count = current_count
                stable_since = time.time()
            elif time.time() - stable_since >= settle_time:
                break

    def _wait_for_content(self, max_wait=5):
        """Wait until the page has at least some interactive content."""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                has_content = self.page.evaluate(
                    """() => {
                    var links = document.querySelectorAll('a[href]');
                    if (links.length > 0) return true;
                    var btns = document.querySelectorAll('button, [role="button"], input[type="submit"], [role="tab"], [onclick], summary, .btn');
                    if (btns.length > 0) return true;
                    if (document.body && document.body.innerText.length > 50) return true;
                    var nav = document.querySelector('nav, [role="navigation"]');
                    if (nav) return true;
                    return false;
                }"""
                )
                if has_content:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        logger.warning("Timed out waiting for page content ({}s)".format(max_wait))

    # ── URL helpers ───────────────────────────────────────

    def _normalize_url(self, url):
        parsed = urlparse(url)
        normalized = "{}://{}{}".format(
            parsed.scheme, parsed.netloc, parsed.path.rstrip("/")
        )
        if parsed.query:
            normalized += "?{}".format(parsed.query)
        return normalized

    def _is_same_domain(self, url):
        try:
            hostname = urlparse(url).netloc
            if hostname == self.base_domain:
                return True
            # Handle subdomains: app.example.com is same domain as example.com
            if hostname.endswith('.' + self.base_domain):
                return True
            if self.base_domain.endswith('.' + hostname):
                return True
            return False
        except Exception:
            return False

    def _is_valid_page_url(self, url):
        parsed = urlparse(url)
        if parsed.scheme in ("mailto", "tel", "javascript", ""):
            return False
        if not parsed.netloc:
            return False
        skip_ext = (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
            ".ico",
            ".pdf",
            ".zip",
            ".tar",
            ".gz",
            ".mp4",
            ".mp3",
            ".avi",
            ".css",
            ".js",
            ".json",
            ".xml",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
            ".map",
        )
        path_lower = parsed.path.lower()
        for ext in skip_ext:
            if path_lower.endswith(ext):
                return False
        return True
