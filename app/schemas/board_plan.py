from typing import Literal

from pydantic import BaseModel


class BoardElement(BaseModel):
    id: str
    type: Literal["frame", "screenshot", "text", "sticky_note", "shape"]
    x: float = 0
    y: float = 0
    width: float | None = None
    height: float | None = None

    # frame
    title: str | None = None
    color: str | None = None

    # screenshot
    screenshot_ref: int | None = None
    label: str | None = None

    # text / sticky_note / shape
    content: str | None = None
    font_size: int | None = None
    bold: bool = False

    # sticky_note
    shape: str | None = None  # "square" or "rectangle"

    # shape
    shape_type: str | None = None
    fill_color: str | None = None
    border_color: str | None = None
    text_color: str | None = None


class BoardConnector(BaseModel):
    from_id: str
    to_id: str
    label: str | None = None
    style: Literal["straight", "elbowed", "curved"] = "elbowed"
    color: str = "#4262ff"


class BoardPlan(BaseModel):
    board_title: str = "AI Generated Board"
    board_description: str = ""
    elements: list[BoardElement] = []
    connectors: list[BoardConnector] = []
