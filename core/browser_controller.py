import json
import os
import logging
import random
import re
import subprocess
import time
from urllib.parse import urlparse, urljoin

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from core.config import REQUEST_TIMEOUT, CHROME_WINDOW_WIDTH, CHROME_WINDOW_HEIGHT

logger = logging.getLogger("auto_screen.browser")

# Realistic User-Agent strings to rotate
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def _detect_chromium_version(chrome_bin=None):
    """Detect the major version of the installed Chrome/Chromium binary."""
    binary = chrome_bin or os.environ.get("CHROME_BIN", "")
    if not binary:
        return None
    try:
        out = subprocess.check_output(
            [binary, "--version"], stderr=subprocess.DEVNULL, timeout=5
        )
        match = re.search(r"(\d+)\.", out.decode())
        if match:
            ver = int(match.group(1))
            logger.info("Detected Chromium version_main: {}".format(ver))
            return ver
    except Exception as e:
        logger.warning("Could not detect Chromium version: {}".format(e))
    return None


class BrowserController:
    """Clean Selenium wrapper — shared by agent loop and MCP server.

    Supports Browser-Use-Cloud-inspired features:
    - Profile persistence: save/restore cookies & localStorage across sessions
    - CDP network monitoring: intercept XHR/fetch to discover dynamic URLs
    - Session lifecycle: structured start → operate → save → quit
    """

    def __init__(self, config):
        self.config = config
        self.driver = None
        self.base_domain = ""
        # Profile state (loaded externally before start)
        self._profile_cookies = None  # list[dict] — cookies to restore
        self._profile_local_storage = None  # dict — localStorage to restore
        self._profile_session_storage = None
        # CDP network monitoring
        self._cdp_discovered_urls = set()  # URLs seen via XHR/fetch
        self._cdp_listener_active = False

    # ── lifecycle ──────────────────────────────────────────

    def load_profile(
        self, cookies_json=None, local_storage_json=None, session_storage_json=None
    ):
        """Pre-load profile state to be restored after browser starts.

        Call BEFORE start(). Cookies/storage will be injected after
        navigating to the target domain so they are domain-scoped.

        Args:
            cookies_json: JSON string of cookie list, or None.
            local_storage_json: JSON string of localStorage dict, or None.
            session_storage_json: JSON string of sessionStorage dict, or None.
        """
        if cookies_json:
            try:
                self._profile_cookies = (
                    json.loads(cookies_json)
                    if isinstance(cookies_json, str)
                    else cookies_json
                )
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid cookies_json, skipping profile cookies")
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
        """Launch browser and navigate to starting URL.

        If a profile was loaded via load_profile(), cookies and localStorage
        are restored after the initial navigation so the site sees them on
        subsequent requests (mimics Browser Use Cloud's Profile concept).
        """
        self.base_domain = urlparse(url).netloc
        self.driver = self._create_driver()

        # Navigate first (sets domain context for cookies)
        result = self.navigate(url)

        # Restore profile state if available
        self._restore_profile_state()

        # If profile was restored, reload to apply cookies to the session
        if self._profile_cookies:
            try:
                self.driver.get(url)
                self._wait_for_load()
                self._dismiss_overlays()
                result = self.current_state()
            except Exception as e:
                logger.warning("Profile reload failed: {}".format(e))

        # Start CDP network monitoring
        self._start_cdp_network_monitor()

        return result

    def quit(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self._cdp_listener_active = False

    # ── Profile persistence (Browser Use Cloud: Profile concept) ──

    def save_profile_state(self):
        """Capture current cookies + localStorage + sessionStorage.

        Returns a dict with keys cookies_json, local_storage_json,
        session_storage_json — ready to persist in BrowserProfile model.
        """
        if not self.driver:
            return {}

        result = {}

        # Save cookies via CDP (includes httpOnly cookies that Selenium can't read)
        try:
            cdp_cookies = self.driver.execute_cdp_cmd("Network.getAllCookies", {})
            cookies = cdp_cookies.get("cookies", [])
            result["cookies_json"] = json.dumps(cookies, default=str)
            logger.info("Saved {} cookies for profile".format(len(cookies)))
        except Exception as e:
            logger.warning("Failed to save cookies: {}".format(e))
            # Fallback: Selenium cookies (misses httpOnly)
            try:
                cookies = self.driver.get_cookies()
                result["cookies_json"] = json.dumps(cookies, default=str)
            except Exception:
                pass

        # Save localStorage
        try:
            local_storage = self.driver.execute_script(
                """
                var data = {};
                for (var i = 0; i < localStorage.length; i++) {
                    var key = localStorage.key(i);
                    data[key] = localStorage.getItem(key);
                }
                return data;
            """
            )
            if local_storage:
                result["local_storage_json"] = json.dumps(local_storage, default=str)
                logger.info("Saved {} localStorage keys".format(len(local_storage)))
        except Exception as e:
            logger.warning("Failed to save localStorage: {}".format(e))

        # Save sessionStorage
        try:
            session_storage = self.driver.execute_script(
                """
                var data = {};
                for (var i = 0; i < sessionStorage.length; i++) {
                    var key = sessionStorage.key(i);
                    data[key] = sessionStorage.getItem(key);
                }
                return data;
            """
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
        if not self.driver:
            return

        # Restore cookies via CDP (preserves httpOnly, secure, sameSite flags)
        if self._profile_cookies:
            restored = 0
            for cookie in self._profile_cookies:
                try:
                    # CDP setCookie needs specific fields
                    cdp_cookie = {
                        "name": cookie.get("name", ""),
                        "value": cookie.get("value", ""),
                        "domain": cookie.get("domain", self.base_domain),
                        "path": cookie.get("path", "/"),
                    }
                    # Optionally set security fields
                    if cookie.get("secure"):
                        cdp_cookie["secure"] = True
                    if cookie.get("httpOnly"):
                        cdp_cookie["httpOnly"] = True
                    if cookie.get("sameSite"):
                        cdp_cookie["sameSite"] = cookie["sameSite"]
                    if cookie.get("expires") and cookie["expires"] > 0:
                        cdp_cookie["expires"] = cookie["expires"]

                    self.driver.execute_cdp_cmd("Network.setCookie", cdp_cookie)
                    restored += 1
                except Exception:
                    # Fallback: try Selenium add_cookie
                    try:
                        selenium_cookie = {
                            "name": cookie.get("name", ""),
                            "value": cookie.get("value", ""),
                            "domain": cookie.get("domain", ""),
                            "path": cookie.get("path", "/"),
                        }
                        if cookie.get("expiry"):
                            selenium_cookie["expiry"] = int(cookie["expiry"])
                        self.driver.add_cookie(selenium_cookie)
                        restored += 1
                    except Exception:
                        pass
            logger.info(
                "Restored {}/{} cookies from profile".format(
                    restored, len(self._profile_cookies)
                )
            )

        # Restore localStorage
        if self._profile_local_storage:
            try:
                self.driver.execute_script(
                    """
                    var data = arguments[0];
                    for (var key in data) {
                        try { localStorage.setItem(key, data[key]); } catch(e) {}
                    }
                """,
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
                self.driver.execute_script(
                    """
                    var data = arguments[0];
                    for (var key in data) {
                        try { sessionStorage.setItem(key, data[key]); } catch(e) {}
                    }
                """,
                    self._profile_session_storage,
                )
            except Exception:
                pass

    # ── CDP Network Monitoring (Browser Use Cloud: Browser concept) ──

    def _start_cdp_network_monitor(self):
        """Enable CDP Network domain to intercept XHR/fetch requests.

        Discovers dynamic URLs loaded by JavaScript that aren't in the DOM
        (API calls, lazy-loaded routes, AJAX navigation, etc.).
        """
        if not self.driver or self._cdp_listener_active:
            return

        try:
            self.driver.execute_cdp_cmd(
                "Network.enable",
                {
                    "maxTotalBufferSize": 10000000,
                    "maxResourceBufferSize": 5000000,
                },
            )
            self._cdp_listener_active = True
            logger.info("CDP Network monitoring enabled")
        except Exception as e:
            logger.warning("Failed to enable CDP Network monitoring: {}".format(e))

    def collect_cdp_discovered_urls(self):
        """Harvest URLs discovered via CDP network events.

        Polls the browser's performance log for navigation/XHR URLs
        and returns new internal URLs not yet seen.
        """
        if not self.driver:
            return []

        new_urls = []
        try:
            # Use performance entries to find XHR/fetch resource URLs
            resources = self.driver.execute_script(
                """
                var entries = performance.getEntriesByType('resource');
                var urls = [];
                for (var i = 0; i < entries.length; i++) {
                    var e = entries[i];
                    // Only include document/xhr/fetch navigations, skip images/css/fonts
                    if (e.initiatorType === 'xmlhttprequest' ||
                        e.initiatorType === 'fetch' ||
                        e.initiatorType === 'navigation' ||
                        e.initiatorType === 'other') {
                        urls.push(e.name);
                    }
                }
                return urls;
            """
            )

            for url in resources or []:
                if not url or url in self._cdp_discovered_urls:
                    continue
                # Only keep same-domain, page-like URLs (not API/media)
                if not self._is_same_domain(url):
                    continue
                if not self._is_valid_page_url(url):
                    continue
                # Skip API-like paths (JSON endpoints, etc.)
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
                    continue
                self._cdp_discovered_urls.add(url)
                new_urls.append(url)

        except Exception as e:
            logger.debug("CDP URL collection failed: {}".format(e))

        if new_urls:
            logger.info("CDP discovered {} new internal URLs".format(len(new_urls)))

        return new_urls

    # ── navigation ────────────────────────────────────────

    def navigate(self, url):
        """Go to URL, wait for load, dismiss overlays, return page state."""
        try:
            self.driver.get(url)
            self._wait_for_load()
            self._dismiss_overlays()
            return self.current_state()
        except Exception as e:
            logger.error("Navigation failed: {}".format(e))
            return {"url": url, "title": "Error", "error": str(e)}

    def _dismiss_overlays(self):
        """Dismiss cookie banners, GDPR modals, newsletter popups, etc."""
        try:
            self.driver.execute_script(
                """
                // Common cookie/consent/popup dismiss patterns
                var dismissSelectors = [
                    // Cookie consent
                    '[class*="cookie"] button', '[id*="cookie"] button',
                    '[class*="consent"] button', '[id*="consent"] button',
                    '[class*="gdpr"] button', '[id*="gdpr"] button',
                    '.cc-dismiss', '.cc-allow', '.cc-accept',
                    '#onetrust-accept-btn-handler',
                    '[data-testid="cookie-accept"]',
                    'button[aria-label*="accept"]', 'button[aria-label*="Accept"]',
                    'button[aria-label*="cookie"]', 'button[aria-label*="Cookie"]',
                    'button[aria-label*="agree"]', 'button[aria-label*="Agree"]',
                    // Generic close/dismiss
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
                                // Only click buttons that look like accept/close
                                if (txt.match(/accept|agree|allow|ok|got it|close|dismiss|understand|continue/i) ||
                                    btns[j].getAttribute('aria-label')) {
                                    btns[j].click();
                                    clicked++;
                                }
                            }
                        }
                    } catch(e) {}
                }
            """
            )
        except Exception:
            pass

    def click(self, selector):
        """Click element by CSS selector, return new page state."""
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, selector)
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
            time.sleep(0.3)
            el.click()
            time.sleep(1.5)
            return self.current_state()
        except Exception as e:
            logger.warning("Click failed on '{}': {}".format(selector, e))
            return {"error": str(e)}

    def try_login(self, login, password):
        """Detect login form and fill credentials.

        Handles:
        - Standard username+password on the same page
        - Multi-step flows (email first, then password on a new screen)
        - React/Vue forms with synthetic event dispatch
        - Submit via button click or Enter key fallback
        - Navigation to login page if not on one

        Returns True if login was attempted.
        """
        url_before = self.driver.current_url

        # Check if we are already logged in (heuristic)
        # Often sites redirect to /home or /dashboard if logged in, or hide login buttons
        # But here we assume if credentials are provided we should try to use them if possible.

        try:
            # 1. Check for login inputs on current page
            detection_script = """
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
                    
                    // Skip hidden or invisible inputs
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
                
                // Fallback: if password input found but no obvious login input, take the preceding text/email input
                if (!loginInput && passInput) {
                    var allInputs = Array.from(inputs);
                    var passIdx = allInputs.indexOf(passInput);
                    for (var j = passIdx - 1; j >= 0; j--) {
                        var t = (allInputs[j].type || '').toLowerCase();
                        if ((t === 'text' || t === 'email' || t === 'tel' || t === '') && allInputs[j].offsetParent !== null) {
                            loginInput = allInputs[j];
                            break;
                        }
                    }
                }
                
                // Multi-step: only login/email field visible (no password yet)
                if (loginInput && !passInput) {
                    return { hasForm: true, multiStep: true };
                }
                if (!loginInput || !passInput) return null;
                return { hasForm: true, multiStep: false };
            """

            result = self.driver.execute_script(detection_script)

            # 2. If not found, look for "Login" link and click it
            if not result or not result.get("hasForm"):
                logger.info(
                    "No login form detected. Looking for 'Login' / 'Sign In' link..."
                )
                clicked = self.driver.execute_script(
                    r"""
                    var candidates = document.querySelectorAll('a, button, [role="button"], span, div');
                    for (var i = 0; i < candidates.length; i++) {
                        var el = candidates[i];
                        if (el.offsetParent === null) continue; // Skip hidden
                        
                        var txt = (el.innerText || '').trim().toLowerCase();
                        var href = (el.href || '').toLowerCase();
                        var aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        
                        // Exact match preference or strong keyword match
                        if (
                            txt === 'login' || txt === 'log in' || txt === 'sign in' || txt === 'signin' ||
                            aria === 'login' || aria === 'sign in' ||
                            (el.tagName === 'A' && href.match(/\/login$|\/signin$|\/auth\//))
                        ) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                """
                )

                if clicked:
                    logger.info("Clicked login link. Waiting for page load...")
                    time.sleep(3)
                    self._wait_for_load()
                    # Re-run detection on new page
                    result = self.driver.execute_script(detection_script)

            if not result or not result.get("hasForm"):
                logger.warning(
                    "Could not find login form (even after navigation attempt)."
                )
                return False

            is_multi_step = result.get("multiStep", False)
            logger.info(
                "Login form detected (multi_step={}), filling credentials...".format(
                    is_multi_step
                )
            )

            # Fill the login/email field and optionally submit the first step
            self.driver.execute_script(
                """
                var inputs = document.querySelectorAll('input:not([type="hidden"])');
                var loginInput = null;
                var passInput = null;
                for (var i = 0; i < inputs.length; i++) {
                    var inp = inputs[i];
                    var type = (inp.type || '').toLowerCase();
                    var name = (inp.name || '').toLowerCase();
                    var placeholder = (inp.placeholder || '').toLowerCase();
                    var id = (inp.id || '').toLowerCase();
                    var autocomplete = (inp.autocomplete || '').toLowerCase();
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
                        if (t === 'text' || t === 'email' || t === 'tel' || t === '') {
                            loginInput = allInputs[j];
                            break;
                        }
                    }
                }
                function setVal(el, val) {
                    el.focus();
                    var nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    // React synthetic event
                    var tracker = el._valueTracker;
                    if (tracker) tracker.setValue('');
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                }
                if (loginInput) setVal(loginInput, arguments[0]);
                if (passInput) {
                    setVal(passInput, arguments[1]);
                }
                // Find and click submit
                var form = (passInput || loginInput).closest('form');
                var submitBtn = null;
                if (form) {
                    submitBtn = form.querySelector(
                        'button[type="submit"], input[type="submit"], button:not([type])'
                    );
                }
                if (!submitBtn) {
                    submitBtn = document.querySelector('button[type="submit"], input[type="submit"]');
                }
                if (!submitBtn) {
                    // Broader search: any button with login-related text
                    var btns = document.querySelectorAll('button, [role="button"], a.btn');
                    for (var b = 0; b < btns.length; b++) {
                        var bt = (btns[b].innerText || '').toLowerCase();
                        if (bt.match(/log.?in|sign.?in|submit|enter|continue|next/)) {
                            submitBtn = btns[b];
                            break;
                        }
                    }
                }
                if (submitBtn) {
                    submitBtn.click();
                } else if (form) {
                    form.submit();
                } else {
                    // Last resort: press Enter on the last filled field
                    var target = passInput || loginInput;
                    target.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    target.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    target.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                }
            """,
                login,
                password,
            )

            time.sleep(3)
            self._wait_for_load()

            # Handle multi-step: after submitting email, look for password field
            if is_multi_step:
                logger.info("Multi-step login: looking for password field...")
                for attempt in range(3):
                    time.sleep(2)
                    has_pass = self.driver.execute_script(
                        """
                        var p = document.querySelector('input[type="password"]');
                        if (p && p.getBoundingClientRect().width > 0) return true;
                        return false;
                    """
                    )
                    if has_pass:
                        self.driver.execute_script(
                            """
                            var passInput = document.querySelector('input[type="password"]');
                            function setVal(el, val) {
                                el.focus();
                                var nativeSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLInputElement.prototype, 'value'
                                ).set;
                                nativeSetter.call(el, val);
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                var tracker = el._valueTracker;
                                if (tracker) tracker.setValue('');
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                            }
                            setVal(passInput, arguments[0]);
                            var form = passInput.closest('form');
                            var submitBtn = null;
                            if (form) submitBtn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
                            if (!submitBtn) submitBtn = document.querySelector('button[type="submit"]');
                            if (!submitBtn) {
                                var btns = document.querySelectorAll('button, [role="button"]');
                                for (var b = 0; b < btns.length; b++) {
                                    var bt = (btns[b].innerText || '').toLowerCase();
                                    if (bt.match(/log.?in|sign.?in|submit|continue|next/)) {
                                        submitBtn = btns[b]; break;
                                    }
                                }
                            }
                            if (submitBtn) submitBtn.click();
                            else if (form) form.submit();
                            else passInput.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                        """,
                            password,
                        )
                        time.sleep(3)
                        self._wait_for_load()
                        break

            # Verify login: check if URL changed or password field disappeared
            url_after = self.driver.current_url
            still_has_password = self.driver.execute_script(
                "return !!document.querySelector('input[type=\"password\"]')"
            )
            if url_after != url_before or not still_has_password:
                logger.info("Login appears successful, now at: {}".format(url_after))
                return True
            else:
                logger.warning("Login may have failed (still on login page)")
                return False  # Don't report success if we're still on the login page

        except Exception as e:
            logger.warning("Login attempt failed: {}".format(e))
            return False

    def type_text(self, selector_text, text):
        """Find an input by label/placeholder/name and type into it.

        Supports React/Vue synthetic events, optional Enter-key submission.
        """
        try:
            result = self.driver.execute_script(
                """
                var query = arguments[0];
                var value = arguments[1];
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
                    if (ph.indexOf(q) !== -1 ||
                        name.indexOf(q) !== -1 ||
                        lbl.indexOf(q) !== -1 ||
                        ariaLabel.indexOf(q) !== -1) {
                        target = inp;
                        break;
                    }
                }
                if (!target) {
                    // Fallback: search by type=search or role=searchbox
                    target = document.querySelector('input[type="search"], [role="searchbox"], [role="combobox"]');
                }
                if (!target) return false;

                target.scrollIntoView({block: 'center'});
                target.focus();
                target.click();

                // Set value via native setter (works on React/Vue)
                if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') {
                    var nativeSetter = Object.getOwnPropertyDescriptor(
                        target.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype,
                        'value'
                    ).set;
                    nativeSetter.call(target, value);
                } else {
                    // contenteditable
                    target.innerText = value;
                }
                // React synthetic event support
                var tracker = target._valueTracker;
                if (tracker) tracker.setValue('');
                target.dispatchEvent(new Event('input', {bubbles: true}));
                target.dispatchEvent(new Event('change', {bubbles: true}));

                // Simulate Enter key to submit search/form
                setTimeout(function() {
                    target.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    target.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                    target.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                }, 300);
                return true;
            """,
                selector_text,
                text,
            )
            time.sleep(1.5)  # Wait for search results / form submission
            return bool(result)
        except Exception as e:
            logger.warning("Type text failed for '{}': {}".format(selector_text, e))
            return False

    def click_by_text(self, text):
        """Click element by its visible text.

        Multi-pass matching with JS dispatchEvent fallback for intercepted clicks.
        """
        try:
            safe_text = text.replace("'", "\\'").replace("\\n", " ")
            result = self.driver.execute_script(
                """
                var query = arguments[0].toLowerCase().trim();
                // Broad set of selectors for interactive elements, including non-semantic ones used in SPAs/SSRs.
                var selectors = 'button, [role="button"], a, summary, [role="tab"], [role="menuitem"], ' +
                    '[role="switch"], [role="checkbox"], [role="option"], [role="link"], ' +
                    '[onclick], details > summary, label, input[type="submit"], input[type="button"], ' +
                    '.btn, .button, .clickable, .link, .nav-link, .menu-item, .tab, .chip, .card-link, ' +
                    '[data-toggle], [data-bs-toggle], [data-action], [data-href], [data-link], ' +
                    'div[tabindex], span[tabindex], li[tabindex], ' +
                    '[aria-expanded], [aria-haspopup]';
                var els = document.querySelectorAll(selectors);
                // Also add elements with cursor:pointer
                var allPointer = document.querySelectorAll('div, span, li, img, svg, i, p');
                var combined = Array.from(els);
                for (var pi = 0; pi < allPointer.length && combined.length < 500; pi++) {
                    try {
                        if (window.getComputedStyle(allPointer[pi]).cursor === 'pointer') {
                            combined.push(allPointer[pi]);
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
                        // Fallback: dispatch mouse events (for elements that intercept .click())
                        try {
                            var rect = el.getBoundingClientRect();
                            var cx = rect.left + rect.width / 2;
                            var cy = rect.top + rect.height / 2;
                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: cx, clientY: cy}));
                            el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, clientX: cx, clientY: cy}));
                            el.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: cx, clientY: cy}));
                            return true;
                        } catch(e2) { return false; }
                    }
                }
                // Pass 1: exact match
                for (var i = 0; i < combined.length; i++) {
                    if (getText(combined[i]) === query) return tryClick(combined[i]);
                }
                // Pass 2: starts with
                for (var i = 0; i < combined.length; i++) {
                    var t = getText(combined[i]);
                    if (t.indexOf(query) === 0) return tryClick(combined[i]);
                }
                // Pass 3: contains query
                for (var i = 0; i < combined.length; i++) {
                    var t = getText(combined[i]);
                    if (t.length < 120 && t.indexOf(query) !== -1) return tryClick(combined[i]);
                }
                // Pass 4: query is substring of element text (fuzzy, e.g. 'About' matches 'About Us Page')
                for (var i = 0; i < combined.length; i++) {
                    var t = getText(combined[i]);
                    if (t.length > 2 && t.length < 80 && query.indexOf(t) !== -1) return tryClick(combined[i]);
                }
                return false;
            """,
                safe_text,
            )
            if result:
                time.sleep(1.5)
            return bool(result)
        except Exception as e:
            logger.warning("Click by text failed for '{}': {}".format(text, e))
            return False

    def go_back(self):
        """Navigate back."""
        try:
            self.driver.back()
            time.sleep(1)
            self._wait_for_load()
            return self.current_state()
        except Exception as e:
            return {"error": str(e)}

    def execute_script_action(self, script):
        """Execute a script provided by the AI agent."""
        try:
            self.driver.execute_script(script)
            return True
        except Exception as e:
            logger.warning("AI script execution failed for '{}': {}".format(script, e))
            return False

    def get_form_inputs(self):
        """Return visible form inputs on the page."""
        try:
            return (
                self.driver.execute_script(
                    """
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
            """
                )
                or []
            )
        except Exception:
            return []

    def scroll_to_bottom(self):
        """Scroll to bottom to trigger lazy-loaded content."""
        try:
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(1)
        except Exception:
            pass

    # ── reading page ──────────────────────────────────────

    def current_state(self):
        """Return current page state dict."""
        return {
            "url": self.driver.current_url,
            "title": self.driver.title or "Untitled",
        }

    def analyze_page(self):
        """Run comprehensive JS analysis inside the browser.

        Returns a structured dict with navigation_links, clickable_elements,
        form_inputs, page_sections, page_type, and text_content.
        The browser does the hard work — no raw HTML needed by the AI.
        """
        try:
            data = self.driver.execute_script(
                self._build_analysis_script(), self.base_domain
            )
            if not data:
                logger.warning("Page analysis JS returned null/undefined")
            return data or {}
        except Exception as e:
            logger.warning("Page analysis failed: {}".format(e))
            return {}

    @staticmethod
    def _build_analysis_script():
        """Build the JS analysis script as a plain string to avoid escaping issues."""
        # Using string concatenation to avoid Python string escaping
        # mangling regex literals and special characters
        return """
        try {
            var baseDomain = arguments[0];
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
                    if (u.protocol === 'mailto:' || u.protocol === 'tel:' ||
                        u.protocol === 'javascript:') continue;
                    var ext = u.pathname.split('.').pop().toLowerCase();
                    var skipExt = ['png','jpg','jpeg','gif','svg','webp','ico',
                                   'pdf','zip','mp4','mp3','css','js','json','woff','woff2'];
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
                    var isInternal = (u.hostname === baseDomain);
                    var rect = a.getBoundingClientRect();
                    var visible = rect.width > 0 && rect.height > 0;
                    var inNav = false;
                    try { inNav = !!a.closest('nav, header, [role="navigation"], .nav, .menu, .sidebar'); } catch(ce) {}
                    if (!visible && !inNav) continue;
                    result.navigation_links.push({
                        url: href,
                        text: text || u.pathname,
                        is_internal: isInternal,
                        in_nav: inNav
                    });
                } catch(e) {}
            }

            // 2. Clickable elements — multi-pass detection
            var seenTexts = {};
            // Helper: check if element is a reasonable clickable target
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
                // Get direct text first
                var text = (el.getAttribute('aria-label') || '').trim();
                if (text) return text.substring(0, 80);
                text = (el.value || '').trim();
                if (text) return text.substring(0, 80);
                text = (el.title || '').trim();
                if (text) return text.substring(0, 80);
                text = (el.innerText || '').trim();
                // Special case: Search icons/buttons with no text
                if (!text && (
                    el.classList.contains('search') || 
                    el.id.indexOf('search') !== -1 ||
                    el.querySelector('svg') || 
                    el.querySelector('i.fa')
                )) {
                    if (el.closest('form')) return "Search Submit";
                    if (el.className.indexOf('search') !== -1) return "Search Icon";
                    return "Icon Button"; 
                }
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
                result.clickable_elements.push({
                    text: text,
                    tag: el.tagName.toLowerCase(),
                    type: reason
                });
            }

            // Pass 1: Standard semantic clickable elements
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
                    (el.hasAttribute('onclick') ? 'onclick' :
                    (el.hasAttribute('tabindex') ? 'tabindex' : 'aria')));
                addClickable(el, reason);
            }

            // Pass 2: Anchor tags used as buttons (often missed by nav link analysis)
            var buttonAnchors = document.querySelectorAll('a[href="#"], a[href^="javascript:"], a:not([href])');
            for (var ba = 0; ba < buttonAnchors.length; ba++) {
                var el = buttonAnchors[ba];
                // Manual check, since isClickCandidate blocks 'a' tags, but these are not for navigation.
                var rect = el.getBoundingClientRect();
                if (rect.width < 10 || rect.height < 10 || rect.top > 5000) continue;
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                addClickable(el, 'button-anchor');
            }

            // Pass 3: Framework-specific data attributes (Vue, Angular, Alpine, HTMX, Bootstrap, etc.)
            var dataSelectors = [
                '[data-toggle]', '[data-bs-toggle]', '[data-action]',
                '[data-click]', '[data-href]', '[data-link]', '[data-url]',
                '[data-target]', '[data-bs-target]', '[data-dismiss]', '[data-bs-dismiss]',
                '[data-slide-to]', '[data-bs-slide-to]',
                '[ng-click]', '[v-on\\:click]',
                '[x-on\\:click]',
                '[data-testid*="button"]', '[data-testid*="btn"]', '[data-testid*="link"]',
                '[data-cy*="button"]', '[data-cy*="btn"]', '[data-cy*="link"]'
            ];
            for (var ds = 0; ds < dataSelectors.length; ds++) {
                try {
                    var dataEls = document.querySelectorAll(dataSelectors[ds]);
                    for (var di = 0; di < dataEls.length; di++) {
                        if (!isClickCandidate(dataEls[di])) continue;
                        addClickable(dataEls[di], 'data-attr');
                    }
                } catch(dse) {}
            }

            // Pass 4: Elements with cursor:pointer computed style (catches JS-bound handlers)
            // Scan common container elements, but also check deeper in the DOM
            var pointerCandidates = document.querySelectorAll(
                'div, span, li, td, th, label, img, svg, p, h1, h2, h3, h4, h5, h6, figure, section, article'
            );
            for (var cp = 0; cp < pointerCandidates.length; cp++) {
                if (result.clickable_elements.length >= 100) break;
                var cpEl = pointerCandidates[cp];
                if (!isClickCandidate(cpEl)) continue;
                try {
                    var style = window.getComputedStyle(cpEl);
                    if (style.cursor !== 'pointer') continue;
                    // Skip if a parent <a> or <button> already covers this element
                    if (cpEl.closest('a, button, [role="button"]')) continue;
                } catch(ce2) { continue; }
                addClickable(cpEl, 'cursor:pointer');
            }

            // Pass 5: Elements with inline event handler attributes (onmousedown, ontouchstart, etc.)
            var inlineHandlerEls = document.querySelectorAll(
                '[onmousedown], [onmouseup], [ontouchstart], [ontouchend], [onpointerdown]'
            );
            for (var ih = 0; ih < inlineHandlerEls.length; ih++) {
                if (result.clickable_elements.length >= 100) break;
                if (!isClickCandidate(inlineHandlerEls[ih])) continue;
                addClickable(inlineHandlerEls[ih], 'inline-handler');
            }

            // Pass 6: CSS class-based detection (common UI patterns)
            var classCandidates = document.querySelectorAll(
                '.btn, .button, .clickable, .link, .nav-link, .menu-item, ' +
                '.dropdown-toggle, .accordion-header, .accordion-button, ' +
                '.tab, .tab-item, .pill, .chip, .badge, .tag, ' +
                '.card, .card-link, .list-group-item-action, ' +
                '.carousel-control, .page-link, .breadcrumb-item'
            );
            for (var cc = 0; cc < classCandidates.length; cc++) {
                if (result.clickable_elements.length >= 100) break;
                var ccEl = classCandidates[cc];
                if (!isClickCandidate(ccEl)) continue;
                // Skip if already an <a> or <button> (handled in links/pass1)
                var ccTag = ccEl.tagName.toLowerCase();
                if (ccTag === 'a' || ccTag === 'button') continue;
                addClickable(ccEl, 'css-class');
            }

            // Pass 7: Detect elements with JS event listeners via getEventListeners (Chrome DevTools)
            // Also catches div/span/li with click handlers added by frameworks
            try {
                var jsClickCandidates = document.querySelectorAll(
                    'div, span, li, figure, article, section, td, th, img, i, path'
                );
                for (var jc = 0; jc < jsClickCandidates.length; jc++) {
                    if (result.clickable_elements.length >= 120) break;
                    var jcEl = jsClickCandidates[jc];
                    if (jcEl.tagName.toLowerCase() === 'a' || jcEl.tagName.toLowerCase() === 'button') continue;
                    // Check for jQuery event handlers
                    var hasJqEvent = false;
                    try {
                        if (typeof jQuery !== 'undefined') {
                            var evts = jQuery._data(jcEl, 'events');
                            if (evts && (evts.click || evts.mousedown || evts.touchstart)) hasJqEvent = true;
                        }
                    } catch(jqe) {}
                    // Check for __reactInternalInstance (React bound handlers)
                    var hasReactHandler = false;
                    var propNames = Object.getOwnPropertyNames(jcEl);
                    for (var rp = 0; rp < propNames.length; rp++) {
                        if (propNames[rp].indexOf('__reactInternalInstance') === 0 ||
                            propNames[rp].indexOf('__reactFiber') === 0 ||
                            propNames[rp].indexOf('__reactProps') === 0) {
                            // Check if it has an onClick in react props
                            try {
                                var rprops = jcEl[propNames[rp]];
                                if (rprops && (rprops.onClick || rprops.memoizedProps && rprops.memoizedProps.onClick)) {
                                    hasReactHandler = true;
                                }
                            } catch(rpe) {}
                            break;
                        }
                    }
                    // Check for Vue event handlers (@click stored on __vue__)
                    var hasVueHandler = !!(jcEl.__vue__ || jcEl.__vue_app__ || jcEl._vei);

                    if (hasJqEvent || hasReactHandler || hasVueHandler) {
                        if (!isClickCandidate(jcEl)) continue;
                        if (jcEl.closest('a, button, [role="button"]')) continue;
                        addClickable(jcEl, hasReactHandler ? 'react-onClick' : (hasVueHandler ? 'vue-click' : 'jquery-click'));
                    }
                }
            } catch(p7e) {}

            // Pass 8: Shadow DOM — scan open shadow roots for clickable elements
            try {
                var allEls = document.querySelectorAll('*');
                for (var sd = 0; sd < allEls.length && result.clickable_elements.length < 130; sd++) {
                    var sr = allEls[sd].shadowRoot;
                    if (!sr) continue;
                    var shadowClickables = sr.querySelectorAll(
                        'button, [role="button"], a[href], [onclick], [tabindex="0"]'
                    );
                    for (var sci = 0; sci < shadowClickables.length; sci++) {
                        var scEl = shadowClickables[sci];
                        var scRect = scEl.getBoundingClientRect();
                        if (scRect.width < 10 || scRect.height < 10) continue;
                        var scText = (scEl.getAttribute('aria-label') || scEl.innerText || scEl.title || '').trim().substring(0, 80);
                        if (!scText) continue;
                        var scKey = scText.toLowerCase().substring(0, 40);
                        if (seenTexts[scKey]) continue;
                        seenTexts[scKey] = true;
                        result.clickable_elements.push({
                            text: scText,
                            tag: scEl.tagName.toLowerCase(),
                            type: 'shadow-dom'
                        });
                    }
                }
            } catch(sde) {}

            // Pass 9: Same-origin iframes — scan for links and clickables inside
            try {
                var iframes = document.querySelectorAll('iframe');
                for (var fi = 0; fi < iframes.length; fi++) {
                    try {
                        var iframeDoc = iframes[fi].contentDocument || iframes[fi].contentWindow.document;
                        if (!iframeDoc) continue;
                        // Extract links from iframe
                        var iframeAnchors = iframeDoc.querySelectorAll('a[href]');
                        for (var ia = 0; ia < iframeAnchors.length && result.navigation_links.length < 80; ia++) {
                            var iHref = iframeAnchors[ia].href;
                            if (!iHref) continue;
                            try {
                                var iu = new URL(iHref);
                                if (iu.protocol === 'mailto:' || iu.protocol === 'tel:') continue;
                                var iNorm = iu.origin + iu.pathname.replace(/\\/+$/, '');
                                if (seenUrls[iNorm]) continue;
                                seenUrls[iNorm] = true;
                                result.navigation_links.push({
                                    url: iHref,
                                    text: (iframeAnchors[ia].innerText || iu.pathname).trim().substring(0, 80),
                                    is_internal: (iu.hostname === baseDomain),
                                    in_nav: false
                                });
                            } catch(iue) {}
                        }
                        // Extract clickables from iframe
                        var iframeBtns = iframeDoc.querySelectorAll('button, [role=\"button\"], [onclick]');
                        for (var ib = 0; ib < iframeBtns.length && result.clickable_elements.length < 140; ib++) {
                            var ibText = (iframeBtns[ib].innerText || iframeBtns[ib].getAttribute('aria-label') || '').trim().substring(0, 80);
                            if (!ibText) continue;
                            var ibKey = ibText.toLowerCase().substring(0, 40);
                            if (seenTexts[ibKey]) continue;
                            seenTexts[ibKey] = true;
                            result.clickable_elements.push({
                                text: ibText,
                                tag: iframeBtns[ib].tagName.toLowerCase(),
                                type: 'iframe'
                            });
                        }
                    } catch(ie) {} // Cross-origin iframes will throw — that's fine
                }
            } catch(ife) {}

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
                    type: itype,
                    name: inp.name || '',
                    placeholder: inp.placeholder || '',
                    label: ilabel
                });
            }
            result.has_login_form = hasPass;

            // 4. Page sections
            var sections = document.querySelectorAll(
                'nav, main, header, footer, section, article, aside, ' +
                '[role="navigation"], [role="main"], [role="banner"]'
            );
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

            // 5. Page type detection
            var metaTag = document.querySelector('meta[name="description"]');
            var metaDesc = metaTag ? (metaTag.content || '') : '';
            var h1 = document.querySelector('h1');
            var h1Text = h1 ? h1.innerText.trim() : '';

            if (hasPass) result.page_type = 'login/auth';
            else if (location.pathname === '/' || location.pathname === '') result.page_type = 'homepage';
            else if (document.querySelector('.product-detail, .pdp') ||
                     document.querySelector('[itemtype*="Product"]')) result.page_type = 'product';
            else if (document.querySelector('.cart, .basket, .shopping-cart')) result.page_type = 'cart';
            else if (document.querySelector('article, .blog-post, .post-content')) result.page_type = 'article/blog';
            else if (document.querySelector('.search-results')) result.page_type = 'search';
            else if (document.querySelectorAll('.product-card, .product-item, .card').length > 3) result.page_type = 'listing/catalog';

            // 6. Key text content
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
                navigation_links: [],
                clickable_elements: [],
                form_inputs: [],
                page_sections: [],
                page_type: 'error',
                has_login_form: false,
                text_content: 'JS analysis error: ' + e.message,
                js_error: e.message
            };
        }
        """

    def get_page_content(self, max_chars=15000):
        """Return cleaned HTML of current page for AI analysis."""
        import re

        html = self.driver.page_source
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
        html = re.sub(r"\s+", " ", html)
        if len(html) > max_chars:
            html = html[:max_chars] + "\n<!-- TRUNCATED -->"
        return html

    def get_links(self):
        """Return all internal links on current page. Uses JS for speed."""
        try:
            raw_links = self.driver.execute_script(
                """
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
            """
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
        """Return interactive elements that might reveal content."""
        try:
            results = self.driver.execute_script(
                """
                var selectors = [
                    "button:not([disabled])",
                    "[role='button']",
                    "[role='tab']",
                    "[aria-expanded='false']",
                    "details:not([open]) > summary",
                    "a[href='#']",
                    "a[href='javascript:void(0)']"
                ];
                var seen = new Set();
                var elements = [];
                for (var s = 0; s < selectors.length; s++) {
                    var found = document.querySelectorAll(selectors[s]);
                    for (var i = 0; i < found.length && elements.length < 30; i++) {
                        var el = found[i];
                        var text = (el.innerText || '').trim().substring(0, 80);
                        if (!text || seen.has(text)) continue;
                        var rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        seen.add(text);
                        elements.push({
                            selector: selectors[s],
                            text: text,
                            tag: el.tagName.toLowerCase()
                        });
                    }
                }
                return elements;
            """
            )
            return results or []
        except Exception as e:
            logger.warning("Failed to get clickable elements: {}".format(e))
            return []

    # ── helpers ────────────────────────────────────────────

    def _create_driver(self):
        # In Docker: use pre-installed Chrome/chromedriver from env vars
        chrome_bin = os.environ.get("CHROME_BIN")
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")

        # Detect actual Chromium version so uc patches the correct binary
        version_main = _detect_chromium_version(chrome_bin)

        options = uc.ChromeOptions()
        options.add_argument(
            "--window-size={},{}".format(CHROME_WINDOW_WIDTH, CHROME_WINDOW_HEIGHT)
        )
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-gpu")

        kwargs = {
            "options": options,
            "headless": self.config.headless,
        }
        if chrome_bin:
            kwargs["browser_executable_path"] = chrome_bin
        if chromedriver_path:
            kwargs["driver_executable_path"] = chromedriver_path
        if version_main:
            kwargs["version_main"] = version_main

        driver = uc.Chrome(**kwargs)
        driver.set_page_load_timeout(REQUEST_TIMEOUT)
        driver.implicitly_wait(5)

        # ── Anti-bot stealth layer ──────────────────────────────
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
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
                // Realistic chrome object
                window.chrome = {runtime: {}, loadTimes: function(){return {}}, csi: function(){return {}}};
                // Prevent CDP detection via Error stack
                var origGetOwnPropDesc = Object.getOwnPropertyDescriptor;
                Object.getOwnPropertyDescriptor = function(o, p) {
                    if (p === 'webdriver') return undefined;
                    return origGetOwnPropDesc(o, p);
                };
                // Hide automation extensions
                var origQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = function(params) {
                    if (params.name === 'notifications') {
                        return Promise.resolve({state: Notification.permission});
                    }
                    return origQuery(params);
                };
                // Fake WebGL renderer info
                var getparam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(p) {
                    if (p === 37445) return 'Intel Inc.';
                    if (p === 37446) return 'Intel Iris OpenGL Engine';
                    return getparam.call(this, p);
                };
            """
            },
        )

        # Randomised viewport jitter so fingerprint isn't always identical
        jitter = random.randint(-20, 20)
        try:
            driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {
                    "mobile": False,
                    "width": CHROME_WINDOW_WIDTH + jitter,
                    "height": CHROME_WINDOW_HEIGHT + jitter,
                    "deviceScaleFactor": 1,
                },
            )
        except Exception:
            pass

        return driver

    def _wait_for_load(self, timeout=15):
        """Wait for page readiness: readyState, DOM stability, content presence, SPA routing."""
        time.sleep(1.5)

        # Phase 1: wait for document.readyState === 'complete'
        for _ in range(timeout * 2):
            try:
                if (
                    self.driver.execute_script("return document.readyState")
                    == "complete"
                ):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        # Phase 2: wait for DOM to stabilise (handles SPAs / JS-rendered pages)
        self._wait_for_dom_stable()

        # Phase 3: wait for meaningful content (links or interactive elements)
        self._wait_for_content()

        # Phase 4: wait for any pending SPA route transitions (pushState / hash)
        try:
            self.driver.execute_script(
                """
                // Wait for any pending setTimeout/requestAnimationFrame transitions
                window.__autoscreen_nav_done = false;
                requestAnimationFrame(function() {
                    setTimeout(function() { window.__autoscreen_nav_done = true; }, 500);
                });
            """
            )
            deadline = time.time() + 3
            while time.time() < deadline:
                done = self.driver.execute_script(
                    "return window.__autoscreen_nav_done === true"
                )
                if done:
                    break
                time.sleep(0.3)
        except Exception:
            pass

    def _wait_for_dom_stable(self, settle_time=1.5, max_wait=8):
        """Poll DOM element count; return once it stops changing.

        settle_time – seconds the count must stay constant before we consider the DOM stable.
        max_wait    – hard upper bound in seconds.
        """
        try:
            last_count = self.driver.execute_script(
                "return document.querySelectorAll('*').length"
            )
        except Exception:
            time.sleep(2)
            return

        stable_since = time.time()
        deadline = time.time() + max_wait

        while time.time() < deadline:
            time.sleep(0.4)
            try:
                current_count = self.driver.execute_script(
                    "return document.querySelectorAll('*').length"
                )
            except Exception:
                break

            if current_count != last_count:
                last_count = current_count
                stable_since = time.time()  # reset the clock
            elif time.time() - stable_since >= settle_time:
                break  # DOM is stable

    def _wait_for_content(self, max_wait=10):
        """Wait until the page has at least some links or interactive content.

        Many SPAs report readyState='complete' and a stable DOM element count
        while the actual UI is still being hydrated / rendered by JS frameworks.
        This method polls for real content before returning.
        """
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                has_content = self.driver.execute_script(
                    """
                    // Check for any link
                    var links = document.querySelectorAll('a[href]');
                    if (links.length > 0) return true;
                    // Check for common interactive elements
                    var btns = document.querySelectorAll('button, [role="button"], input[type="submit"], ' +
                        '[role="tab"], [onclick], summary, .btn, .button, .clickable, .nav-link');
                    if (btns.length > 0) return true;
                    // Check for meaningful text content as a fallback
                    if (document.body && document.body.innerText.length > 50) return true;
                    // Check for a nav element
                    var nav = document.querySelector('nav, [role="navigation"]');
                    if (nav) return true;
                    return false;
                """
                )
                if has_content:
                    return
            except Exception:
                pass
            time.sleep(1)
        logger.warning("Timed out waiting for page content ({}s)".format(max_wait))

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
            return urlparse(url).netloc == self.base_domain
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
