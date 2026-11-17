"""列序裁定编排：位置证据 → 位置主张 → 三档裁定 → 兼容视图 → 切换与回滚。

批次内的裁定是纯计算，随 run 落盘，页面与迁移单共用同一份位置证据；
切换/回滚是运维动作，单独落 position_verdicts / position_audit，
按证据指纹幂等：同一结构重跑只更新裁定，不新增记录。
"""

from __future__ import annotations

from app import config, errors, manifest as manifest_mod, store
from app.modules.m8_position import claims as claims_mod
from app.modules.m8_position import compat_view, evidence, verdict as verdict_mod


def build_position(run: dict) -> dict:
    """从一次批次的两版快照与下游登记，算出完整列序裁定块。"""
    position_evidence = evidence.build_evidence(
        run.get("fields_old") or [],
        run.get("fields_new") or [],
        run.get("quarantine_old") or [],
        run.get("quarantine_new") or [],
    )
    consumers = (run.get("refs") or {}).get("consumers") or []
    parsed = claims_mod.parse_all(consumers)
    verdicts = verdict_mod.adjudicate_all(parsed, position_evidence)
    views = compat_view.build_for_verdicts(verdicts, position_evidence)
    result = {
        "evidence_hash": position_evidence["evidence_hash"],
        "tables": position_evidence["tables"],
        "entries": position_evidence["entries"],
        "blocked_tables": position_evidence["blocked_tables"],
        "verdicts": verdicts,
        "views": views,
        "summary": verdict_mod.summary(verdicts, position_evidence["evidence_hash"]),
    }
    result["summary"]["view_count"] = len(views)
    return result


def sync_verdicts(run: dict) -> None:
    """把本次批次的位置裁定幂等写入库；已有的切换激活态必须保留。"""
    position = run.get("position") or build_position(run)
    evidence_hash = position["evidence_hash"]
    existing = {row["consumer"]: row.get("activation", "none")
                for row in store.list_position_verdicts(evidence_hash)}
    updated_at = config.now_utc().isoformat()
    for item in position["verdicts"]:
        if not item.get("is_position"):
            continue
        store.upsert_position_verdict({
            "evidence_hash": evidence_hash,
            "consumer": item["consumer"],
            "run_id": run["run_id"],
            "verdict": item["verdict"],
            "reason": item["reason"],
            "mode": item.get("mode"),
            "activation": existing.get(item["consumer"], "none"),
            "detail": item.get("detail") or "",
            "updated_at": updated_at,
        })


def _stored_activation_map(run: dict) -> dict[str, str]:
    position = run.get("position") or {}
    rows = store.list_position_verdicts(position.get("evidence_hash") or "")
    return {row["consumer"]: row.get("activation", "none") for row in rows}


def with_activation(run: dict) -> dict:
    """把库里的切换态合并回批次裁定，页面与导出都走这里，保证同一份证据。"""
    position = run.get("position") or {}
    activations = _stored_activation_map(run)
    for item in position.get("verdicts", []):
        item["activation"] = activations.get(item["consumer"], "none")
    return position


def _export_snapshot(run: dict) -> dict[str, str]:
    """切换前留存现有导出文本，切换后必须能复算成完全一致的内容才算恢复。"""
    from app.modules.m7_migration import service as migration

    return {
        "position_csv": position_csv_text(run),
        "spec_md": migration.spec_markdown(run),
    }


def _backup_key(evidence_hash: str) -> str:
    return f"position_switch_backup:{evidence_hash}"


def _use_view_consumers(run: dict) -> list[str]:
    position = run.get("position") or {}
    return sorted(
        item["consumer"] for item in position.get("verdicts", [])
        if item.get("is_position") and item.get("verdict") == evidence.USE_VIEW
    )


def switch_to_view(run: dict, consumers: list[str] | None = None,
                   simulate_fail: bool = False) -> dict:
    """把 USE_VIEW 消费方切到列序兼容视图。

    先备份旧裁定激活态与现有导出，再切换；任何失败都按备份恢复，
    并写 POSITION_SWITCH_FAILED 审计，返回 409，绝不留下半切换状态。
    """
    position = run.get("position") or build_position(run)
    evidence_hash = position["evidence_hash"]
    stored = store.list_position_verdicts(evidence_hash)
    by_name = {row["consumer"]: row for row in stored}
    targets = sorted(consumers) if consumers else _use_view_consumers(run)
    if not targets:
        raise errors.BadInput("没有需要切换的位置消费方", detail={"reason": "POSITION_HALT"})

    for name in targets:
        record = by_name.get(name)
        if record is None:
            raise errors.NotFound("没有这条位置裁定", detail={"consumer": name})
        if record["verdict"] != evidence.USE_VIEW:
            raise errors.BadInput(
                "只有 USE_VIEW 裁定才能切兼容视图",
                detail={"consumer": name, "verdict": record["verdict"],
                        "reason": record["reason"]},
            )

    backup = {
        "evidence_hash": evidence_hash,
        "run_id": run["run_id"],
        "activations": {name: rec.get("activation", "none") for name, rec in by_name.items()},
        "exports": _export_snapshot(run),
        "created_at": config.now_utc().isoformat(),
    }
    store.set_kv(_backup_key(evidence_hash), backup)

    if simulate_fail:
        # 先制造一次激活态改动，验证恢复路径真的能把它撤回
        store.set_position_activation(evidence_hash, targets, "view")
        restored = _restore_backup(run, backup)
        store.save_position_audit({
            "audit_id": manifest_mod.run_id_for(
                f"fail:{evidence_hash}:{config.now_utc().isoformat()}:{','.join(targets)}"),
            "evidence_hash": evidence_hash,
            "action": "switch",
            "status": "failed",
            "consumers": targets,
            "detail": "模拟切换失败，已恢复旧版裁定及导出",
            "created_at": config.now_utc().isoformat(),
        })
        raise errors.Conflict(
            "兼容视图切换失败，已恢复旧版裁定及导出",
            detail={"reason": "POSITION_SWITCH_FAILED", "consumers": targets,
                    "exports_restored": restored["exports_match"]},
        )

    changed = store.set_position_activation(evidence_hash, targets, "view")
    store.save_position_audit({
        "audit_id": manifest_mod.run_id_for(
            f"ok:{evidence_hash}:{config.now_utc().isoformat()}:{','.join(targets)}"),
        "evidence_hash": evidence_hash,
        "action": "switch",
        "status": "applied",
        "consumers": targets,
        "detail": f"{changed} 条裁定切到兼容视图",
        "created_at": config.now_utc().isoformat(),
    })
    return {
        "evidence_hash": evidence_hash,
        "switched": targets,
        "changed": changed,
        "reason": "POSITION_VIEW_SWITCHED",
        "views": position["views"],
    }


def _restore_backup(run: dict, backup: dict) -> dict:
    """按备份把激活态逐条撤回，并复算导出与备份逐字比对。"""
    evidence_hash = backup["evidence_hash"]
    for name, activation in backup["activations"].items():
        store.set_position_activation(evidence_hash, [name], activation)
    current = _export_snapshot(run)
    exports_match = current == backup["exports"]
    return {"exports_match": exports_match, "exports": current}


def rollback(run: dict) -> dict:
    """回滚最近一次切换：恢复旧版裁定激活态，并验证导出逐字复原。"""
    position = run.get("position") or build_position(run)
    evidence_hash = position["evidence_hash"]
    backup = store.get_kv(_backup_key(evidence_hash))
    if not backup:
        raise errors.NotFound("没有可回滚的切换备份", detail={"evidence_hash": evidence_hash})

    restored = _restore_backup(run, backup)
    if not restored["exports_match"]:
        raise errors.Conflict(
            "回滚后导出与旧版不一致，需要人工核对",
            detail={"reason": "POSITION_SWITCH_FAILED"},
        )
    targets = [name for name, activation in backup["activations"].items()
               if activation != "view"]
    store.save_position_audit({
        "audit_id": manifest_mod.run_id_for(
            f"rb:{evidence_hash}:{config.now_utc().isoformat()}"),
        "evidence_hash": evidence_hash,
        "action": "rollback",
        "status": "applied",
        "consumers": targets,
        "detail": "已恢复旧版裁定及导出",
        "created_at": config.now_utc().isoformat(),
    })
    return {
        "evidence_hash": evidence_hash,
        "reason": "POSITION_SWITCH_ROLLED_BACK",
        "restored_consumers": targets,
        "exports_match": restored["exports_match"],
    }


def audit_trail(run: dict) -> list[dict]:
    position = run.get("position") or {}
    return store.list_position_audit(position.get("evidence_hash"))


# ---------- 导出 ----------

POSITION_CSV_HEADER = [
    "consumer", "kind", "mode", "table", "verdict", "reason", "activation",
    "affected_ordinals", "detail",
]


def position_csv_text(run: dict) -> str:
    import csv
    import io

    position = with_activation(run)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(POSITION_CSV_HEADER)
    for item in position.get("verdicts", []):
        if not item.get("is_position"):
            continue
        claims = item.get("claims") or []
        if not claims:
            writer.writerow([
                item["consumer"], item.get("kind") or "", item.get("mode") or "", "",
                item["verdict"], item["reason"], item.get("activation", "none"),
                " ".join(str(x) for x in item.get("affected_ordinals", [])),
                item.get("detail") or "",
            ])
            continue
        for claim in claims:
            writer.writerow([
                item["consumer"], item.get("kind") or "", item.get("mode") or "",
                claim["table"], claim["verdict"], claim["reason"],
                item.get("activation", "none"),
                " ".join(str(x) for x in claim.get("affected_ordinals", [])),
                claim.get("detail") or "",
            ])
    return buffer.getvalue()
