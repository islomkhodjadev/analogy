from pydantic import BaseModel


class MiroExportRequest(BaseModel):
    board_name: str | None = None
    board_id: str | None = None
    prompt: str | None = None


class MiroExportResponse(BaseModel):
    board_id: str
    board_url: str
    message: str
