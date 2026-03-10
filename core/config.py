import os
import logging
from dataclasses import dataclass, field

# ── Constants ──────────────────────────────────────────
DEFAULT_DEPTH = 3
MAX_DEPTH = 10
DEFAULT_MODEL = "gpt-4-turbo"
SCREENSHOT_FORMAT = "png"
OUTPUT_DIR_NAME = "auto_screen_output"
REQUEST_TIMEOUT = 30
OPENAI_TIMEOUT = 60
CHROME_WINDOW_WIDTH = 1920
CHROME_WINDOW_HEIGHT = 1080


@dataclass
class AppConfig:
    url: str
    depth: int = DEFAULT_DEPTH
    output_dir: str = ""
    openai_api_key: str = ""
    model: str = DEFAULT_MODEL
    login: str = ""
    password: str = ""
    browser_engine: str = "playwright"  # "playwright" or "selenium"
    screenshot_mode: str = "viewport"  # "viewport" or "full_page"
    capture_mode: str = "smart"  # "smart" (diverse) or "exhaustive" (capture everything)
    viewport_width: int = 0  # custom viewport width (0 = use default 1920)
    viewport_height: int = 0  # custom viewport height (0 = use default 1080)
    headless: bool = True
    verbose: bool = False
    # Profile persistence (Browser Use Cloud pattern)
    profile_cookies_json: str = ""  # JSON list of cookies to restore
    profile_local_storage_json: str = ""  # JSON dict of localStorage to restore
    profile_session_storage_json: str = ""
    save_profile_callback: object = (
        None  # callable(cookies_json, local_storage_json, session_storage_json)
    )

    def __post_init__(self):
        if not self.openai_api_key:
            raise ValueError("OpenAI API key is required")
        if self.depth < 1 or self.depth > MAX_DEPTH:
            raise ValueError("Depth must be between 1 and {}".format(MAX_DEPTH))

    @property
    def is_exhaustive(self):
        return self.capture_mode == "exhaustive"

    @property
    def max_pages(self):
        if self.is_exhaustive:
            return min(self.depth * 15, 200)
        return min(self.depth * 5, 50)

    @property
    def max_plan_pages(self):
        if self.is_exhaustive:
            return min(self.depth * 12, 150)
        return min(self.depth * 4, 30)

    @property
    def max_pages_per_theme(self):
        if self.is_exhaustive:
            return 999
        return max(2, self.depth)

    @property
    def max_ui_clicks_per_page(self):
        if self.is_exhaustive:
            return min(self.depth * 3, 15)
        return min(max(self.depth - 1, 0), 3)

    @property
    def max_discover_pages(self):
        if self.is_exhaustive:
            return min(self.depth * 10, 100)
        return 0 if self.depth == 1 else min(self.depth * 3, 25)
