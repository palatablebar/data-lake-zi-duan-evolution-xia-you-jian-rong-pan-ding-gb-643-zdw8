"""M8 入口：列序裁定页、位置证据接口、兼容视图切换/回滚、列序导出。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import errors, store, web
from app.modules.m8_position import service

router = APIRouter()

ALLOWED_SWITCH_KEYS = {"consumers", "simulate_fail"}


def _latest_run() -> dict:
    run = store.latest_run()
    if not run:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    return run


@router.get("/position", response_class=HTMLResponse)
def position_page(request: Request):
    run = store.latest_run()
    position = service.with_activation(run) if run else {}
    return web.page_or_404(
        request,
        "position.html",
        "position",
        run,
        position=position,
        verdicts=[item for item in position.get("verdicts", []) if item.get("is_position")],
        entries=position.get("entries", []),
        views=position.get("views", []),
        summary=position.get("summary", {}),
        audit=service.audit_trail(run) if run else [],
    )


@router.get("/api/position/verdicts")
def position_verdicts_api():
    run = _latest_run()
    return service.with_activation(run)


@router.get("/api/position/evidence")
def position_evidence_api():
    run = _latest_run()
    position = service.with_activation(run)
    return {
        "evidence_hash": position.get("evidence_hash"),
        "tables": position.get("tables", []),
        "entries": position.get("entries", []),
        "blocked_tables": position.get("blocked_tables", []),
        "summary": position.get("summary", {}),
    }


@router.get("/api/position/views")
def position_views_api():
    run = _latest_run()
    position = service.with_activation(run)
    return {"views": position.get("views", []),
            "evidence_hash": position.get("evidence_hash")}


@router.post("/api/position/switch")
def position_switch(payload: dict | None = None):
    body = payload or {}
    unknown = set(body) - ALLOWED_SWITCH_KEYS
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})
    consumers = body.get("consumers")
    if consumers is not None and (
        not isinstance(consumers, list) or not all(isinstance(x, str) for x in consumers)
    ):
        raise errors.BadInput("consumers 必须是字符串数组", detail={"field": "consumers"})
    run = _latest_run()
    return service.switch_to_view(
        run,
        consumers=consumers,
        simulate_fail=bool(body.get("simulate_fail")),
    )


@router.post("/api/position/rollback")
def position_rollback():
    run = _latest_run()
    return service.rollback(run)


@router.get("/api/position/audit")
def position_audit_api():
    run = _latest_run()
    return {"audit": service.audit_trail(run),
            "evidence_hash": (run.get("position") or {}).get("evidence_hash")}


@router.get("/api/export/position.csv")
def export_position():
    run = _latest_run()
    return Response(
        content=service.position_csv_text(run),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="fieldmap-position.csv"'},
    )
