import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models.screenshot import Screenshot

logger = logging.getLogger("services.miro")

MIRO_BASE_URL = "https://api.miro.com/v2"

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Theme → color mapping for visual distinction
THEME_COLORS = {
    "homepage": "#4262ff",
    "products": "#ff6b35",
    "blog": "#7b68ee",
    "about": "#20b2aa",
    "contact": "#ff69b4",
    "legal": "#808080",
    "pricing": "#ffd700",
    "support": "#32cd32",
    "faq": "#32cd32",
    "login": "#dc143c",
    "auth": "#dc143c",
    "settings": "#8b4513",
    "dashboard": "#4169e1",
    "profile": "#9370db",
    "search": "#ff8c00",
    "categories": "#2e8b57",
}


def _safe_hex(color: str | None, default: str = "#000000") -> str:
    """Ensure color is a valid 6-digit CSS hex like #aabbcc."""
    if not color:
        return default
    color = color.strip()
    # expand 3-digit hex → 6-digit
    if re.match(r"^#[0-9a-fA-F]{3}$", color):
        color = "#" + color[1] * 2 + color[2] * 2 + color[3] * 2
    if _HEX_RE.match(color):
        return color
    return default


def _normalize_url(url):
    """Normalize URL for matching parent_url to screenshot.url."""
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return "{}://{}{}".format(parsed.scheme or "https", parsed.netloc, path).lower()


# Layout constants
IMAGE_WIDTH = 800
COLUMN_GAP = 1000
ROW_GAP = 1500
TITLE_OFFSET_Y = -80
DESC_OFFSET_Y = 80
THEME_LABEL_OFFSET_Y = -160


class MiroExportError(Exception):
    pass


class MiroExporter:
    def __init__(self, access_token: str):
        self._client = httpx.Client(
            base_url=MIRO_BASE_URL,
            headers={"Authorization": "Bearer {}".format(access_token)},
            timeout=60.0,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code == 401:
            raise MiroExportError("Miro access token is invalid or expired")
        if resp.status_code == 429:
            raise MiroExportError("Miro rate limit exceeded. Try again later.")
        if resp.status_code >= 400:
            raise MiroExportError(
                "Miro API error {}: {}".format(resp.status_code, resp.text[:500])
            )
        return resp.json()

    def create_board(self, name: str, description: str = "") -> tuple[str, str]:
        data = self._request(
            "POST",
            "/boards",
            json={
                "name": name,
                "description": description,
            },
        )
        return data["id"], data["viewLink"]

    def get_board(self, board_id: str) -> tuple[str, str]:
        data = self._request("GET", "/boards/{}".format(board_id))
        return data["id"], data["viewLink"]

    def upload_image(
        self,
        board_id: str,
        file_path: str,
        x: float,
        y: float,
        width: int = IMAGE_WIDTH,
    ) -> tuple[str, float] | None:
        """Upload image. Returns (widget_id, rendered_height) or None."""
        path = Path(file_path)
        if not path.exists():
            logger.warning("Screenshot file not found, skipping: {}".format(file_path))
            return None

        position_data = json.dumps(
            {
                "position": {"x": x, "y": y},
                "geometry": {"width": width},
            }
        )

        with open(path, "rb") as f:
            data = self._request(
                "POST",
                "/boards/{}/images".format(board_id),
                data={"data": position_data},
                files={"resource": (path.name, f, "image/png")},
            )

        height = width  # fallback
        geo = data.get("geometry", {})
        if geo.get("height"):
            height = geo["height"]

        return data["id"], height

    def create_text(
        self,
        board_id: str,
        content: str,
        x: float,
        y: float,
        font_size: int = 14,
        bold: bool = False,
        width: int = 0,
    ) -> str:
        formatted = "<b>{}</b>".format(content) if bold else content
        body = {
            "data": {"content": formatted},
            "position": {"x": x, "y": y},
            "style": {"fontSize": str(font_size), "textAlign": "center"},
        }
        if width > 0:
            body["geometry"] = {"width": width}
        data = self._request(
            "POST",
            "/boards/{}/texts".format(board_id),
            json=body,
        )
        return data["id"]

    def create_sticky_note(
        self,
        board_id: str,
        content: str,
        x: float,
        y: float,
        color: str = "yellow",
        shape: str = "square",
        width: int = 200,
    ) -> str:
        body = {
            "data": {"content": content, "shape": shape},
            "position": {"x": x, "y": y},
            "style": {"fillColor": color, "textAlign": "center"},
        }
        if width > 0:
            body["geometry"] = {"width": width}
        data = self._request(
            "POST",
            "/boards/{}/sticky_notes".format(board_id),
            json=body,
        )
        return data["id"]

    def create_shape(
        self,
        board_id: str,
        content: str,
        shape_type: str,
        x: float,
        y: float,
        width: int = 200,
        height: int = 100,
        fill_color: str = "#ffffff",
        border_color: str = "#000000",
        font_size: int = 14,
        text_color: str = "#000000",
    ) -> str:
        body = {
            "data": {"content": content, "shape": shape_type},
            "position": {"x": x, "y": y},
            "geometry": {"width": width, "height": height},
            "style": {
                "fillColor": _safe_hex(fill_color, "#ffffff"),
                "borderColor": _safe_hex(border_color, "#000000"),
                "fontSize": str(font_size),
                "color": _safe_hex(text_color, "#000000"),
            },
        }
        data = self._request(
            "POST",
            "/boards/{}/shapes".format(board_id),
            json=body,
        )
        return data["id"]

    def create_frame(
        self,
        board_id: str,
        title: str,
        x: float,
        y: float,
        width: int = 2000,
        height: int = 1500,
    ) -> str:
        body = {
            "data": {"title": title, "type": "freeform"},
            "position": {"x": x, "y": y},
            "geometry": {"width": width, "height": height},
        }
        data = self._request(
            "POST",
            "/boards/{}/frames".format(board_id),
            json=body,
        )
        return data["id"]

    def create_connector(
        self,
        board_id: str,
        start_id: str,
        end_id: str,
        shape: str = "elbowed",
        color: str = "#4262ff",
        label: str | None = None,
        start_snap: str | None = None,
        end_snap: str | None = None,
    ) -> str:
        start_item = {"id": start_id}
        end_item = {"id": end_id}
        if start_snap:
            start_item["snapTo"] = start_snap
        if end_snap:
            end_item["snapTo"] = end_snap

        body = {
            "startItem": start_item,
            "endItem": end_item,
            "shape": shape,
            "style": {
                "strokeColor": _safe_hex(color, "#4262ff"),
                "strokeWidth": "2",
                "endStrokeCap": "stealth",
            },
        }
        if label:
            body["captions"] = [{"content": label, "position": "50%"}]

        data = self._request(
            "POST",
            "/boards/{}/connectors".format(board_id),
            json=body,
        )
        return data["id"]

    # ── Simple grid layout (theme rows) ──

    def export_job(
        self,
        board_name: str,
        screenshots: list[Screenshot],
        existing_board_id: str | None = None,
    ) -> tuple[str, str]:
        """Export screenshots to Miro in a clean grid.

        Layout:
        - Each theme = one horizontal row
        - Screenshots go left-to-right in capture order
        - A single text label sits directly above each image showing
          the page title and URL path
        - No arrows, no sticky notes, no shapes
        """
        if existing_board_id:
            board_id, board_url = self.get_board(existing_board_id)
        else:
            board_id, board_url = self.create_board(
                name=board_name,
                description="Site map: {}".format(board_name),
            )

        if not screenshots:
            return board_id, board_url

        sorted_shots = sorted(screenshots, key=lambda s: s.order_index)

        # Group by theme, preserving capture order within each theme
        from collections import OrderedDict

        theme_groups = OrderedDict()
        for shot in sorted_shots:
            theme = shot.theme or "pages"
            if theme not in theme_groups:
                theme_groups[theme] = []
            theme_groups[theme].append(shot)

        # Layout constants
        IMG_W = 800  # image widget width
        H_GAP = 250  # horizontal space between images
        V_GAP = 400  # vertical space between theme rows
        LABEL_GAP = 60  # gap between label bottom and image top
        COL_W = IMG_W + H_GAP  # one column width
        MAX_COLS = 5  # wrap after this many columns

        cursor_y = 0  # y of the top edge of the current theme row

        for theme, shots in theme_groups.items():
            # ── Theme header — plain bold text, left-aligned ──
            self.create_text(
                board_id,
                content=theme.upper(),
                x=IMG_W / 2,
                y=cursor_y,
                font_size=24,
                bold=True,
                width=IMG_W,
            )

            # Leave space after theme header
            row_start_y = cursor_y + 80

            max_img_h = 0  # track tallest image in current sub-row
            sub_row = 0  # which wrapped row we're on

            for idx, shot in enumerate(shots):
                col = idx % MAX_COLS
                if idx > 0 and col == 0:
                    # Wrap to next sub-row: advance by tallest image + gaps
                    sub_row += 1
                    row_start_y += max_img_h + LABEL_GAP + 80 + V_GAP
                    max_img_h = 0

                x_center = col * COL_W + IMG_W / 2

                # ── Upload image ──
                abs_path = str(Path(settings.static_root) / shot.file_path)
                # Place image; y will be adjusted once we know image height
                # Use a placeholder y, then compute label position from actual height
                img_y = row_start_y + LABEL_GAP + 400  # rough center
                result = self.upload_image(
                    board_id, abs_path, x=x_center, y=img_y, width=IMG_W
                )
                if result is None:
                    continue

                image_id, actual_h = result
                max_img_h = max(max_img_h, actual_h)

                # Compute where the image top edge actually is
                img_top = img_y - actual_h / 2

                # ── Label above image ──
                # Shows: page title + URL path so user knows what page it is
                title = shot.title or "Untitled"
                if len(title) > 80:
                    title = title[:77] + "..."

                path = urlparse(shot.url or "").path or "/"
                if len(path) > 70:
                    path = path[:67] + "..."

                label = "{}\n{}".format(title, path)

                self.create_text(
                    board_id,
                    content=label,
                    x=x_center,
                    y=img_top - LABEL_GAP,
                    font_size=14,
                    bold=False,
                    width=IMG_W,
                )

            # Advance cursor past all sub-rows of this theme
            cursor_y = row_start_y + max_img_h + LABEL_GAP + 80 + V_GAP

        return board_id, board_url

    # ── AI-driven export from plan ──

    def export_from_plan(
        self,
        plan,
        screenshots: list[Screenshot],
        existing_board_id: str | None = None,
    ) -> tuple[str, str]:
        """Render an AI-generated board plan onto Miro."""
        if existing_board_id:
            board_id, board_url = self.get_board(existing_board_id)
        else:
            board_id, board_url = self.create_board(
                name=plan.board_title,
                description=plan.board_description,
            )

        sorted_screenshots = sorted(screenshots, key=lambda s: s.order_index)
        id_map: dict[str, str] = {}

        # Phase 1: create all elements
        for el in plan.elements:
            miro_id = None
            try:
                if el.type == "frame":
                    miro_id = self.create_frame(
                        board_id,
                        title=el.title or "",
                        x=el.x,
                        y=el.y,
                        width=int(el.width or 2000),
                        height=int(el.height or 1500),
                    )

                elif el.type == "screenshot":
                    idx = el.screenshot_ref
                    screenshot = sorted_screenshots[idx]
                    abs_path = str(Path(settings.static_root) / screenshot.file_path)
                    result = self.upload_image(
                        board_id,
                        abs_path,
                        x=el.x,
                        y=el.y,
                        width=int(el.width or 800),
                    )
                    if result:
                        miro_id = result[0]

                elif el.type == "text":
                    miro_id = self.create_text(
                        board_id,
                        content=el.content or "",
                        x=el.x,
                        y=el.y,
                        font_size=el.font_size or 14,
                        bold=el.bold,
                        width=int(el.width or 0),
                    )

                elif el.type == "sticky_note":
                    miro_id = self.create_sticky_note(
                        board_id,
                        content=el.content or "",
                        x=el.x,
                        y=el.y,
                        color=el.color or "yellow",
                        shape=el.shape or "square",
                        width=int(el.width or 200),
                    )

                elif el.type == "shape":
                    miro_id = self.create_shape(
                        board_id,
                        content=el.content or "",
                        shape_type=el.shape_type or "round_rectangle",
                        x=el.x,
                        y=el.y,
                        width=int(el.width or 200),
                        height=int(el.height or 100),
                        fill_color=el.fill_color or "#ffffff",
                        border_color=el.border_color or "#000000",
                        font_size=el.font_size or 14,
                        text_color=el.text_color or "#000000",
                    )

            except Exception as exc:
                logger.warning("Failed to create element {}: {}".format(el.id, exc))
                continue

            if miro_id:
                id_map[el.id] = miro_id

        # Build set of frame element IDs — Miro forbids connectors to/from frames
        frame_ids = {el.id for el in plan.elements if el.type == "frame"}

        # Phase 2: create connectors
        for conn in plan.connectors:
            if conn.from_id in frame_ids or conn.to_id in frame_ids:
                logger.info(
                    "Skipping connector {}->{}: frames cannot have connectors".format(
                        conn.from_id, conn.to_id
                    )
                )
                continue
            start = id_map.get(conn.from_id)
            end = id_map.get(conn.to_id)
            if start and end:
                try:
                    self.create_connector(
                        board_id,
                        start,
                        end,
                        shape=conn.style,
                        color=conn.color,
                        label=conn.label,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to create connector {}->{}:  {}".format(
                            conn.from_id, conn.to_id, exc
                        )
                    )

        return board_id, board_url

    def close(self):
        self._client.close()
