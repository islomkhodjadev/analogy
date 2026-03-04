"""
Bridge between Celery task and the core SiteAgent.
Constructs AppConfig from Job, runs the agent, persists Screenshot rows.
Loads/saves BrowserProfile for session persistence (Browser Use Cloud pattern).
"""

import os
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models.screenshot import Screenshot
from app.models.profile import BrowserProfile
from core.config import AppConfig
from core.agent import SiteAgent

logger = logging.getLogger("worker.engine")


def run_agent_for_job(db: Session, job, openai_api_key: str) -> dict:
    """
    Build AppConfig from Job, run SiteAgent, persist captures as Screenshot rows.
    If job has a profile_id, loads cookies/localStorage from the profile before
    starting and saves updated state after the crawl completes.
    """
    output_dir = os.path.join(
        settings.screenshots_root,
        str(job.user_id),
        str(job.id),
    )

    # Load profile state if available
    profile_cookies = ""
    profile_local_storage = ""
    profile_session_storage = ""
    login = job.target_login or ""
    password = job.target_password or ""
    profile = None

    if job.profile_id:
        profile = (
            db.query(BrowserProfile)
            .filter(
                BrowserProfile.id == job.profile_id,
                BrowserProfile.is_active == True,
            )
            .first()
        )
        if profile:
            profile_cookies = profile.cookies_json or ""
            profile_local_storage = profile.local_storage_json or ""
            profile_session_storage = profile.session_storage_json or ""
            # Use profile credentials as fallback if job doesn't specify them
            if not login and profile.login_email:
                login = profile.login_email
            if not password and profile.login_password:
                password = profile.login_password
            logger.info(
                "Loaded browser profile '{}' for domain {}".format(
                    profile.name or profile.id, profile.domain
                )
            )

    # Build save callback to persist profile state after crawl
    def save_profile_callback(cookies_json, local_storage_json, session_storage_json):
        """Called by SiteAgent after login to persist browser state."""
        if not profile:
            # Auto-create a profile if login was successful and no profile existed
            if not job.profile_id and login:
                from urllib.parse import urlparse

                domain = urlparse(str(job.url)).netloc
                new_profile = BrowserProfile(
                    user_id=job.user_id,
                    domain=domain,
                    name="Auto: {}".format(domain),
                    cookies_json=cookies_json,
                    local_storage_json=local_storage_json,
                    session_storage_json=session_storage_json,
                    login_email=login,
                    login_password=password,
                    last_used_at=datetime.utcnow(),
                )
                db.add(new_profile)
                job.profile_id = new_profile.id
                db.commit()
                logger.info("Auto-created browser profile for {}".format(domain))
            return

        # Update existing profile
        profile.cookies_json = cookies_json
        profile.local_storage_json = local_storage_json
        profile.session_storage_json = session_storage_json
        profile.last_used_at = datetime.utcnow()
        db.commit()
        logger.info("Updated browser profile '{}'".format(profile.name or profile.id))

    config = AppConfig(
        url=str(job.url),
        depth=job.depth,
        output_dir=output_dir,
        openai_api_key=openai_api_key,
        model=job.model or "gpt-4-turbo",
        login=login,
        password=password,
        browser_engine=getattr(job, "browser_engine", None) or "playwright",
        screenshot_mode=getattr(job, "screenshot_mode", None) or "viewport",
        viewport_width=getattr(job, "viewport_width", None) or 0,
        viewport_height=getattr(job, "viewport_height", None) or 0,
        headless=os.environ.get("HEADLESS", "true").lower() != "false",
        verbose=False,
        profile_cookies_json=profile_cookies,
        profile_local_storage_json=profile_local_storage,
        profile_session_storage_json=profile_session_storage,
        save_profile_callback=save_profile_callback,
    )

    agent = SiteAgent(config)
    result = agent.run()

    # Persist each capture to DB
    for i, capture in enumerate(agent.captures):
        abs_path = capture["screenshot_path"]
        rel_path = os.path.relpath(abs_path, settings.static_root)
        file_size = os.path.getsize(abs_path) if os.path.exists(abs_path) else None

        screenshot = Screenshot(
            job_id=job.id,
            url=capture["url"],
            title=capture.get("title", ""),
            description=capture.get("description", ""),
            theme=capture.get("theme", "uncategorized"),
            file_path=rel_path,
            file_size_bytes=file_size,
            parent_url=capture.get("parent_url", ""),
            order_index=i,
        )
        db.add(screenshot)

    db.commit()

    return {
        "total_screenshots": result.get("total_screenshots", 0),
        "total_themes": result.get("total_themes", 0),
    }
