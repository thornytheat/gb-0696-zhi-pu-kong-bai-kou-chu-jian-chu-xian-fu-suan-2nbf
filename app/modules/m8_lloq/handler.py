from pathlib import Path
from fastapi import APIRouter, Body, Request
from app.errors import BusinessError, object_payload
from app.modules.m8_lloq import service

router = APIRouter()
SAMPLE = Path(__file__).parent.parent.parent.parent / "sample_data" / "confirmation_pb_3rep.csv"


@router.get("/api/8")
def state():
    return service.overview()


@router.get("/api/8/sample")
def sample():
    return {"text": SAMPLE.read_text(encoding="utf-8-sig")}


@router.post("/api/8/confirm")
def confirm(payload: object = Body(...)):
    payload = object_payload(payload)
    if not isinstance(payload.get("text"), str):
        raise BusinessError("TEXT", "输入须为文本")
    return service.confirm(payload["text"], payload.get("params"))


@router.post("/api/8/activate")
def activate(payload: object = Body(...)):
    payload = object_payload(payload)
    if not isinstance(payload.get("dossier_id"), str):
        raise BusinessError("DOSSIER", "档案编号须为文本")
    force = payload.get("force") is True
    return service.activate(payload["dossier_id"], force)


@router.post("/api/8/apply")
def apply(payload: object = Body(...)):
    payload = object_payload(payload)
    if not isinstance(payload.get("run_id"), str):
        raise BusinessError("RUN", "批次编号须为文本")
    return service.apply(payload["run_id"])


@router.get("/api/8/report")
def report(request: Request):
    dossier_id = request.query_params.get("dossier_id")
    if not dossier_id:
        raise BusinessError("DOSSIER", "缺少档案编号")
    return service.report(dossier_id)
