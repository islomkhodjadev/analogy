from pydantic import BaseModel


class MiroExportRequest(BaseModel):
    board_name: str | None = None
    board_id: str | None = None
    prompt: str | None = None


class MiroExportResponse(BaseModel):
    job_id: str
    miro_export_status: str          # pending / running / completed / failed
    board_id: str | None = None
    board_url: str | None = None
    error: str | None = None
    message: str
