import os
import logging
from datetime import datetime

logger = logging.getLogger("auto_screen.site_builder")


class SiteBuilder:
    def __init__(self, config):
        self.config = config
        self.output_dir = os.path.abspath(config.output_dir)

    def build(self, captures, themes):
        """Build HTML gallery from captures and themes."""
        index_path = os.path.join(self.output_dir, "index.html")
        html = self._generate_html(captures, themes)

        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info("Index built: {}".format(index_path))
        return index_path

    def _generate_html(self, captures, themes):
        site_name = self.config.url
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = len(captures)

        theme_tabs = ""
        theme_sections = ""

        for i, (theme, theme_pages) in enumerate(sorted(themes.items())):
            active = " active" if i == 0 else ""
            theme_tabs += (
                '<button class="tab-btn{active}" '
                "onclick=\"showTheme('{theme}', this)\">"
                '{label} ({count})</button>\n'
            ).format(
                active=active,
                theme=theme,
                label=theme.replace("-", " ").title(),
                count=len(theme_pages),
            )

            display = "grid" if i == 0 else "none"
            cards = ""
            for page in theme_pages:
                rel_path = os.path.relpath(page["screenshot_path"], self.output_dir)
                safe_title = page.get("title", "").replace('"', '&quot;').replace('<', '&lt;')
                safe_url = page.get("url", "").replace('"', '&quot;')
                description = page.get("description", "")
                safe_desc = description.replace('"', '&quot;').replace('<', '&lt;')

                desc_html = ""
                if description:
                    desc_html = '<p class="description">{}</p>'.format(safe_desc)

                cards += """
                <div class="card">
                    <a href="{rel}" target="_blank">
                        <img src="{rel}" alt="{title}" loading="lazy" />
                    </a>
                    <div class="card-info">
                        <h3>{title}</h3>
                        {desc}
                        <a href="{url}" class="url" target="_blank">{url}</a>
                    </div>
                </div>""".format(
                    rel=rel_path,
                    title=safe_title,
                    url=safe_url,
                    desc=desc_html,
                )

            theme_sections += """
            <div class="theme-section" id="theme-{theme}" style="display:{display}">
                {cards}
            </div>""".format(theme=theme, display=display, cards=cards)

        return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>auto_screen - {site_name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; }}
        header {{ background: #1a1a2e; padding: 24px 32px; border-bottom: 2px solid #16213e; }}
        header h1 {{ font-size: 1.8em; color: #e94560; }}
        header p {{ color: #888; margin-top: 4px; }}
        .stats {{ display: flex; gap: 24px; margin-top: 12px; }}
        .stat {{ background: #16213e; padding: 8px 16px; border-radius: 6px; font-size: 0.9em; }}
        .tabs {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 16px 32px; background: #1a1a2e; }}
        .tab-btn {{ padding: 8px 20px; border: 1px solid #333; background: #0f0f0f; color: #ccc; border-radius: 20px; cursor: pointer; font-size: 0.9em; }}
        .tab-btn:hover {{ background: #16213e; color: #fff; }}
        .tab-btn.active {{ background: #e94560; color: #fff; border-color: #e94560; }}
        .theme-section {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr)); gap: 20px; padding: 24px 32px; }}
        .card {{ background: #1a1a2e; border-radius: 10px; overflow: hidden; border: 1px solid #222; }}
        .card:hover {{ transform: translateY(-4px); border-color: #e94560; }}
        .card img {{ width: 100%; height: 300px; object-fit: cover; object-position: top; border-bottom: 1px solid #222; }}
        .card-info {{ padding: 12px 16px; }}
        .card-info h3 {{ font-size: 1em; margin-bottom: 4px; }}
        .card-info .description {{ font-size: 0.85em; color: #aaa; margin-bottom: 8px; line-height: 1.4; }}
        .card-info .url {{ font-size: 0.8em; color: #4a9eff; word-break: break-all; text-decoration: none; }}
    </style>
</head>
<body>
    <header>
        <h1>auto_screen</h1>
        <p>Visual offline copy of <strong>{site_name}</strong></p>
        <div class="stats">
            <div class="stat">Pages: {total}</div>
            <div class="stat">Sections: {themes_count}</div>
            <div class="stat">Generated: {timestamp}</div>
        </div>
    </header>
    <nav class="tabs">{theme_tabs}</nav>
    <main>{theme_sections}</main>
    <script>
        function showTheme(theme, btn) {{
            document.querySelectorAll('.theme-section').forEach(s => s.style.display = 'none');
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('theme-' + theme).style.display = 'grid';
            btn.classList.add('active');
        }}
    </script>
</body>
</html>""".format(
            site_name=site_name,
            total=total,
            themes_count=len(themes),
            timestamp=timestamp,
            theme_tabs=theme_tabs,
            theme_sections=theme_sections,
        )
