import os
import re
import base64
import logging
from urllib.parse import urlparse

from core.config import SCREENSHOT_FORMAT

logger = logging.getLogger("auto_screen.screenshot")


class ScreenshotManager:
    def __init__(self, config):
        self.config = config
        self.output_dir = os.path.abspath(config.output_dir)
        self.screenshots_dir = os.path.join(self.output_dir, "screenshots")
        self._counter = 0

    def setup_output_dirs(self):
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.screenshots_dir, exist_ok=True)

    def _sanitize_filename(self, name):
        name = re.sub(r"[^\w\s-]", "", name.lower())
        name = re.sub(r"[\s]+", "_", name.strip())
        return name[:80] or "page"

    def _ensure_theme_dir(self, theme):
        safe_theme = self._sanitize_filename(theme)
        theme_dir = os.path.join(self.screenshots_dir, safe_theme)
        os.makedirs(theme_dir, exist_ok=True)
        return theme_dir

    def _generate_filename(self, url, title):
        self._counter += 1
        parsed = urlparse(url)
        path_part = self._sanitize_filename(
            parsed.path.strip("/").replace("/", "_") or "index"
        )
        title_part = self._sanitize_filename(title)[:30]
        return "{:03d}_{}_{}.{}".format(
            self._counter, path_part, title_part, SCREENSHOT_FORMAT
        )

    def capture_page(self, driver, url, title, theme, browser_engine="playwright",
                     screenshot_mode="viewport"):
        """Capture screenshot. Works with both Selenium (CDP) and Playwright.

        screenshot_mode: "viewport" (visible area only) or "full_page" (scroll entire page).
        """
        theme_dir = self._ensure_theme_dir(theme)
        filename = self._generate_filename(url, title)
        filepath = os.path.join(theme_dir, filename)

        full_page = screenshot_mode == "full_page"

        if browser_engine == "playwright":
            self._capture_playwright(driver, filepath, full_page=full_page)
        else:
            self._capture_selenium(driver, filepath, full_page=full_page)

        return filepath

    def _capture_playwright(self, page, filepath, full_page=False):
        """Screenshot via Playwright."""
        try:
            page.screenshot(
                path=filepath,
                full_page=full_page,
                timeout=15000,
            )
            logger.info(
                "  Screenshot saved (Playwright{}): {}".format(
                    ", full page" if full_page else "", os.path.basename(filepath)
                )
            )
        except Exception as e:
            logger.error("Playwright screenshot failed: {}".format(e))

    def _capture_selenium(self, driver, filepath, full_page=False):
        """Screenshot via Selenium CDP."""
        try:
            result = driver.execute_cdp_cmd(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": full_page,
                },
            )

            with open(filepath, "wb") as f:
                f.write(base64.b64decode(result["data"]))

            logger.info(
                "  Screenshot saved (Selenium{}): {}".format(
                    ", full page" if full_page else "", os.path.basename(filepath)
                )
            )

        except Exception as e:
            logger.warning("CDP screenshot failed, using fallback: {}".format(e))
            driver.save_screenshot(filepath)

    def get_themes_summary(self, captures):
        """Organize captures (list of dicts) by theme."""
        themes = {}
        for c in captures:
            theme = c.get("theme", "uncategorized")
            if theme not in themes:
                themes[theme] = []
            themes[theme].append(
                {
                    "url": c.get("url", ""),
                    "title": c.get("title", ""),
                    "screenshot_path": c.get("screenshot_path", ""),
                    "description": c.get("description", ""),
                }
            )
        return themes
