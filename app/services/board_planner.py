import json
import logging
import re
import time

from openai import OpenAI

from app.models.screenshot import Screenshot
from app.schemas.board_plan import BoardPlan

logger = logging.getLogger("services.board_planner")

BOARD_PLANNER_TIMEOUT = 120

SYSTEM_PROMPT = """\
You are a Miro board layout architect. You receive a set of website screenshots \
with metadata and a user instruction describing what kind of board to create.

You produce a JSON board plan that will be rendered onto a Miro board.
Your output MUST be valid JSON matching the schema exactly.

COORDINATE SYSTEM & LAYOUT RULES:
- (0, 0) is the top-left anchor of your layout.
- X increases to the right, Y increases downward.
- Units are Miro points (approx pixels).
- **CRITICAL**: Your coordinates will be algorithmically corrected, so focus on \
STRUCTURE (grouping, connectors) rather than pixel-perfect positioning.

GROUPING STRATEGY (MOST IMPORTANT):
- Group screenshots by their "theme" into FRAMES.
- Each frame should contain only screenshots of the same theme.
- Name each frame after the theme (e.g., "Homepage", "Product Pages", "Account").
- Place screenshots inside their frame at rough positions — the layout engine \
will fix exact coordinates.

ELEMENT TYPES you can create:
1. "frame" — grouping container with a title. Needs width + height.
2. "screenshot" — places a screenshot image. Reference by screenshot_ref (0-based index).
   - Screenshots are typically 800px wide. Place them inside frames.
3. "text" — text label. Supports bold, font_size, width.
4. "sticky_note" — colored sticky note for annotations, ideas, action items.
   Colors: "gray", "light_yellow", "yellow", "orange", "light_green", "green", \
"dark_green", "cyan", "light_pink", "pink", "violet", "red", "light_blue", "blue", \
"dark_blue", "black".
   Shape: "square" or "rectangle".
5. "shape" — geometric shape with text content for nodes, headers, decisions.
   shape_type: "rectangle", "round_rectangle", "circle", "triangle", "rhombus", \
"parallelogram", "trapezoid", "pentagon", "hexagon", "octagon", "star", \
"wedge_round_rectangle_callout", "flow_chart_predefined_process", \
"flow_chart_decision", "flow_chart_document", "flow_chart_terminator".

CONNECTOR rules:
- Connect elements using their "id" field via the "connectors" array.
- style: "straight", "elbowed", or "curved".
- Connectors can have an optional label.
- Do NOT create connectors to/from "frame" elements.
- Connect screenshots that have a Parent→Child navigation relationship.

COLOR RULES:
- All hex colors MUST be valid 6-digit CSS hex: "#rrggbb" (e.g. "#4262ff", "#ffffff").
- Do NOT use 3-digit hex (#fff), named colors (white, red), or rgba().
- For sticky_note "color" field, use ONLY the predefined names listed above (yellow, \
light_blue, etc.) — NOT hex values.
- For shape fill_color, border_color, text_color — use 6-digit hex only.
- For connector color — use 6-digit hex only.

NAVIGATION TREE:
- Each screenshot has a "Parent" field showing which page the user navigated FROM.
- "(root)" means it's a top-level page (homepage or entry point).
- Use the Parent→Child relationships to create connectors between screenshots.

IMPORTANT RULES:
- Every element MUST have a unique "id" string.
- screenshot_ref is a 0-based index.
- Create ONE frame per theme. Put ALL screenshots of that theme inside it.
- For EVERY screenshot, add a TEXT label nearby with the page description.
- Include ALL screenshots in the board unless the user explicitly asks for a subset.
- Add sticky notes with UX observations, business logic notes, or action items \
OUTSIDE the frames (they will be placed in a notes section).
- Connect screenshots following the Parent→Child navigation flow with arrows.

Respond with ONLY a valid JSON object. No markdown fences, no explanation."""

USER_PROMPT_TEMPLATE = """\
USER INSTRUCTION: {prompt}

WEBSITE: {site_url}

SCREENSHOTS ({count} total, sorted by navigation order):
{screenshots_block}

Create a Miro board plan as JSON:
{{
  "board_title": "string — concise board title",
  "board_description": "string — one sentence describing the board",
  "elements": [
    {{
      "id": "el_1",
      "type": "frame | screenshot | text | sticky_note | shape",
      "x": 0,
      "y": 0,
      "width": 800,
      "height": 600,

      "title": "for frame only",
      "color": "#hex or sticky color name",

      "screenshot_ref": 0,
      "label": "for screenshot only",

      "content": "for text / sticky_note / shape",
      "font_size": 14,
      "bold": false,

      "shape": "square or rectangle (sticky_note)",
      "shape_type": "round_rectangle (shape)",
      "fill_color": "#hex",
      "border_color": "#hex",
      "text_color": "#hex"
    }}
  ],
  "connectors": [
    {{
      "from_id": "el_1",
      "to_id": "el_2",
      "label": "optional arrow label",
      "style": "straight | elbowed | curved",
      "color": "#hex"
    }}
  ]
}}"""


class BoardPlannerError(Exception):
    pass


class BoardPlanner:
    def __init__(self, openai_api_key: str, model: str = "gpt-4-turbo"):
        self.client = OpenAI(
            api_key=openai_api_key,
            timeout=BOARD_PLANNER_TIMEOUT,
        )
        self.model = model

    def generate_plan(
        self,
        prompt: str,
        screenshots: list[Screenshot],
        site_url: str,
    ) -> BoardPlan:
        screenshots_block = self._format_screenshots(screenshots)

        user_prompt = USER_PROMPT_TEMPLATE.format(
            prompt=prompt,
            site_url=site_url,
            count=len(screenshots),
            screenshots_block=screenshots_block,
        )

        raw = self._call_openai(user_prompt)
        parsed = self._parse_json(raw)
        plan = BoardPlan(**parsed)
        plan = self._validate_plan(plan, len(screenshots))

        # --- Force Re-Layout to prevent Overlaps ---
        # The AI is bad at geometry. We trust its structure (connectors) but not its coordinates.
        plan = self._enforce_layout(plan, prompt)

        logger.info(
            "Board plan generated: {} elements, {} connectors".format(
                len(plan.elements),
                len(plan.connectors),
            )
        )
        return plan

    def _enforce_layout(self, plan: BoardPlan, user_prompt: str) -> BoardPlan:
        """
        Re-calculates X,Y coordinates to prevent overlaps.
        Groups screenshots into theme-based frames and lays them out in a grid.
        """
        import math
        from collections import defaultdict

        logger.info("Enforcing algorithmic layout to prevent overlaps...")

        element_map = {el.id: el for el in plan.elements}

        # Separate element types
        frames = [el for el in plan.elements if el.type == "frame"]
        screenshots = [el for el in plan.elements if el.type == "screenshot"]
        texts = [el for el in plan.elements if el.type == "text"]
        stickies = [el for el in plan.elements if el.type == "sticky_note"]
        shapes = [el for el in plan.elements if el.type == "shape"]

        # --- Constants ---
        SCREENSHOT_W = 800
        SCREENSHOT_H = 1200
        INNER_GAP_X = 200  # gap between screenshots inside a frame
        INNER_GAP_Y = 400  # vertical gap for labels above/below screenshots
        FRAME_PADDING = 150  # padding inside frame edges
        FRAME_GAP = 400  # gap between frames
        COLS_PER_FRAME = 3  # max screenshots per row inside a frame
        STICKY_WIDTH = 250
        STICKY_HEIGHT = 200

        # --- Step 1: Group screenshots by their parent frame ---
        # The AI typically puts screenshots inside frames. Detect by proximity or
        # by matching frame titles to screenshot themes.
        frame_to_screenshots = defaultdict(list)  # frame_id -> [screenshot elements]
        unframed_screenshots = []

        if frames:
            for shot in screenshots:
                # Find which frame this screenshot belongs to (closest frame)
                best_frame = None
                best_dist = float("inf")
                for frame in frames:
                    fx, fy = frame.x, frame.y
                    fw = frame.width or 2000
                    fh = frame.height or 1500
                    # Check if screenshot is inside the frame bounds (original AI coords)
                    if (
                        fx - fw / 2 <= shot.x <= fx + fw / 2
                        and fy - fh / 2 <= shot.y <= fy + fh / 2
                    ):
                        dist = ((shot.x - fx) ** 2 + (shot.y - fy) ** 2) ** 0.5
                        if dist < best_dist:
                            best_dist = dist
                            best_frame = frame
                if best_frame:
                    frame_to_screenshots[best_frame.id].append(shot)
                else:
                    unframed_screenshots.append(shot)
        else:
            unframed_screenshots = screenshots[:]

        # If no frames or poor grouping, create virtual groups by screenshot theme
        if not frames or len(unframed_screenshots) > len(screenshots) * 0.5:
            # Reset — group all screenshots by theme
            frame_to_screenshots.clear()
            theme_groups = defaultdict(list)
            for shot in screenshots:
                # Try to find theme from nearby text/sticky or from AI label
                theme = shot.label or "pages"
                # Normalize theme
                theme = theme.split(":")[0].strip().lower()[:30]
                theme_groups[theme].append(shot)

            # Create or reuse frames for each theme
            existing_frame_titles = {(f.title or "").lower(): f for f in frames}
            for theme, shots in theme_groups.items():
                matched_frame = existing_frame_titles.get(theme)
                if matched_frame:
                    frame_to_screenshots[matched_frame.id] = shots
                else:
                    # Create a virtual frame
                    frame_id = "auto_frame_{}".format(theme.replace(" ", "_"))
                    new_frame = BoardElement(
                        id=frame_id,
                        type="frame",
                        title=theme.title(),
                        x=0,
                        y=0,
                        width=2000,
                        height=1500,
                    )
                    plan.elements.append(new_frame)
                    element_map[frame_id] = new_frame
                    frames.append(new_frame)
                    frame_to_screenshots[frame_id] = shots
            unframed_screenshots = []

        # --- Step 2: Layout each frame as a grid of screenshots ---
        frame_sizes = {}  # frame_id -> (width, height)

        for frame_id, shots in frame_to_screenshots.items():
            if not shots:
                continue
            n = len(shots)
            cols = min(n, COLS_PER_FRAME)
            rows = math.ceil(n / cols)

            # Calculate frame inner content size
            content_w = cols * SCREENSHOT_W + (cols - 1) * INNER_GAP_X
            content_h = rows * (SCREENSHOT_H + INNER_GAP_Y)

            frame_w = content_w + 2 * FRAME_PADDING
            frame_h = content_h + 2 * FRAME_PADDING + 100  # extra for title

            frame_sizes[frame_id] = (frame_w, frame_h)

            # Position screenshots inside the frame (relative, will be offset later)
            for idx, shot in enumerate(shots):
                col = idx % cols
                row = idx // cols
                shot.x = col * (SCREENSHOT_W + INNER_GAP_X)
                shot.y = row * (SCREENSHOT_H + INNER_GAP_Y)

        # --- Step 3: Layout frames in a grid on the board ---
        frame_ids_with_content = [
            fid for fid in frame_to_screenshots if frame_to_screenshots[fid]
        ]
        n_frames = len(frame_ids_with_content)
        frame_cols = min(n_frames, 3)  # max 3 frames per row

        cursor_x = 0
        cursor_y = 0
        row_max_height = 0
        frame_positions = {}  # frame_id -> (x, y)

        for i, frame_id in enumerate(frame_ids_with_content):
            fw, fh = frame_sizes.get(frame_id, (2000, 1500))

            if i > 0 and i % frame_cols == 0:
                # New row
                cursor_x = 0
                cursor_y += row_max_height + FRAME_GAP
                row_max_height = 0

            frame_positions[frame_id] = (cursor_x, cursor_y)
            row_max_height = max(row_max_height, fh)
            cursor_x += fw + FRAME_GAP

        # --- Step 4: Apply absolute positions ---
        for frame_id, (fx, fy) in frame_positions.items():
            frame_el = element_map.get(frame_id)
            fw, fh = frame_sizes.get(frame_id, (2000, 1500))

            if frame_el:
                # Miro frames are positioned by center
                frame_el.x = fx + fw / 2
                frame_el.y = fy + fh / 2
                frame_el.width = fw
                frame_el.height = fh

            # Offset screenshots to be inside the frame
            for shot in frame_to_screenshots.get(frame_id, []):
                shot.x = fx + FRAME_PADDING + shot.x + SCREENSHOT_W / 2
                shot.y = (
                    fy + FRAME_PADDING + 100 + shot.y + SCREENSHOT_H / 2
                )  # +100 for frame title

        # --- Step 5: Position unframed screenshots in a row below all frames ---
        if unframed_screenshots:
            below_y = cursor_y + row_max_height + FRAME_GAP + 500
            for idx, shot in enumerate(unframed_screenshots):
                shot.x = idx * (SCREENSHOT_W + INNER_GAP_X) + SCREENSHOT_W / 2
                shot.y = below_y

        # --- Step 6: Position texts/stickies near their original associated screenshots ---
        # Map text/sticky to closest screenshot by original AI coordinates
        non_positional = texts + stickies + shapes
        # We already moved screenshots. Rebuild a spatial index from original AI coords.
        # Since we can't recover original coords, place texts/stickies relative to their
        # associated screenshots using connector relationships.

        connected_to = {}  # element_id -> connected screenshot element_id
        for conn in plan.connectors:
            # Find which end is a screenshot
            from_el = element_map.get(conn.from_id)
            to_el = element_map.get(conn.to_id)
            if (
                from_el
                and from_el.type == "screenshot"
                and to_el
                and to_el.type != "screenshot"
            ):
                connected_to[conn.to_id] = conn.from_id
            elif (
                to_el
                and to_el.type == "screenshot"
                and from_el
                and from_el.type != "screenshot"
            ):
                connected_to[conn.from_id] = conn.to_id

        # Position stickies that aren't connected — place them in a notes section
        notes_x = (
            cursor_x + FRAME_GAP
            if cursor_x > 0
            else (cursor_y + row_max_height + FRAME_GAP + 500)
        )
        notes_y = 0
        sticky_idx = 0

        for el in non_positional:
            if el.id in connected_to:
                # Place near the associated screenshot
                ref_el = element_map.get(connected_to[el.id])
                if ref_el:
                    el.x = ref_el.x + SCREENSHOT_W / 2 + 200
                    el.y = ref_el.y
                    continue

            # Place in a "Notes" column to the right
            col = sticky_idx % 2
            row = sticky_idx // 2
            el.x = notes_x + col * (STICKY_WIDTH + 50)
            el.y = notes_y + row * (STICKY_HEIGHT + 50)
            sticky_idx += 1

        return plan

    def _format_screenshots(self, screenshots: list[Screenshot]) -> str:
        sorted_shots = sorted(screenshots, key=lambda s: s.order_index)
        lines = []
        for i, s in enumerate(sorted_shots):
            title = s.title or "Untitled"
            theme = s.theme or "uncategorized"
            desc = (s.description or "No description")[:200]
            parent = s.parent_url or "(root)"
            lines.append(
                '[{i}] "{title}" (theme: {theme})\n'
                "    URL: {url}\n"
                "    Parent: {parent}\n"
                "    Description: {desc}".format(
                    i=i,
                    title=title,
                    theme=theme,
                    url=s.url,
                    parent=parent,
                    desc=desc,
                )
            )
        return "\n\n".join(lines)

    def _validate_plan(self, plan: BoardPlan, num_screenshots: int) -> BoardPlan:
        MAX_COORD = 20000

        valid_ids: set[str] = set()
        valid_elements = []

        for el in plan.elements:
            el.x = max(-MAX_COORD, min(MAX_COORD, el.x))
            el.y = max(-MAX_COORD, min(MAX_COORD, el.y))

            if el.type == "screenshot":
                if (
                    el.screenshot_ref is None
                    or el.screenshot_ref < 0
                    or el.screenshot_ref >= num_screenshots
                ):
                    logger.warning(
                        "Dropping element {} with invalid screenshot_ref={}".format(
                            el.id,
                            el.screenshot_ref,
                        )
                    )
                    continue

            if el.type == "frame":
                el.width = el.width or 2000
                el.height = el.height or 1500
            if el.type == "screenshot":
                el.width = el.width or 800
            if el.type == "shape":
                el.width = el.width or 200
                el.height = el.height or 100

            valid_ids.add(el.id)
            valid_elements.append(el)

        valid_connectors = []
        for conn in plan.connectors:
            if conn.from_id in valid_ids and conn.to_id in valid_ids:
                valid_connectors.append(conn)
            else:
                logger.warning(
                    "Dropping connector {}->{}: invalid element ref".format(
                        conn.from_id,
                        conn.to_id,
                    )
                )

        plan.elements = valid_elements
        plan.connectors = valid_connectors
        return plan

    def _call_openai(self, user_prompt: str) -> str:
        try:
            time.sleep(0.5)
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_completion_tokens=16384,
                response_format={"type": "json_object"},
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error("OpenAI board plan generation failed: {}".format(e))
            raise BoardPlannerError("AI board generation failed: {}".format(e))

    def _parse_json(self, raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse AI board plan JSON: {}".format(e))
            raise BoardPlannerError("AI returned invalid JSON: {}".format(e))
