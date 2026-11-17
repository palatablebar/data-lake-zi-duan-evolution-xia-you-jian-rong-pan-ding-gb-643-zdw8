"""列序裁定规则：给每个位置消费方下三档结论。

三档结论（与原因码一一对应）：
- PROCEED 可继续：消费的每个旧列号在新版本同位置还是同一身份，列序位移不影响它。
- USE_VIEW 需切兼容视图：消费的旧身份全部还在、只是挪了位置（插列后移或存量列调位，
  甚至同位置身份换人），按旧列序建兼容视图即可无损恢复。
- HALT 停止放行：旧位置映射没核清（证据缺失/阻断、表消失、列被删、登记不合法），
  缺的身份任何视图都补不回来，必须人工介入。

读写两向都按位置走，所以结论不区分 read/write：写错位比读错位更危险，一律按同一档。
裁定是纯函数，只吃位置证据与登记主张，不碰数据库，保证重排输入结论不变。
"""

from __future__ import annotations

from app.modules.m8_position import evidence


def _table_map(position_evidence: dict) -> dict[str, dict]:
    return {row["table"]: row for row in position_evidence.get("tables", [])}


def _claim_verdict(claim: dict, position_evidence: dict,
                   entries: dict[tuple[str, int], dict]) -> dict:
    """单个 (消费方, 表) 主张的裁定。"""
    table = claim["table"]
    table_summary = _table_map(position_evidence).get(table)

    if table_summary is None:
        return {
            "table": table,
            "verdict": evidence.HALT,
            "reason": "POSITION_EVIDENCE_MISSING",
            "detail": "新版本与旧版本都没有这张表的位置证据",
            "affected_ordinals": list(claim["ordinals"] or []),
        }
    if table_summary.get("old_evidence_blocked") or table_summary.get("new_evidence_blocked"):
        return {
            "table": table,
            "verdict": evidence.HALT,
            "reason": "POSITION_EVIDENCE_MISSING",
            "detail": "该表存在阻断列，列序证据不完整",
            "affected_ordinals": list(claim["ordinals"] or []),
        }
    if table_summary.get("missing_in_new"):
        return {
            "table": table,
            "verdict": evidence.HALT,
            "reason": "POSITION_EVIDENCE_MISSING",
            "detail": "表在新版本被删除，旧列号无处安放",
            "affected_ordinals": list(claim["ordinals"] or []),
        }

    # 整行宽度消费：对旧版本全部列号负责
    if claim["ordinals"] is None:
        ordinals = list(range(1, table_summary["old_width"] + 1))
    else:
        ordinals = list(claim["ordinals"])

    problems: list[dict] = []
    shifted: list[dict] = []
    for ordinal in ordinals:
        row = entries.get((table, ordinal))
        if row is None or ordinal > table_summary["old_width"]:
            problems.append({
                "ordinal": ordinal,
                "reason": "POSITION_EVIDENCE_MISSING",
                "detail": "旧版本没有这个列号，旧位置映射未核清",
            })
            continue
        if row["change"] == evidence.SHIFT_DROP:
            problems.append({
                "ordinal": ordinal,
                "reason": "POSITION_OCCUPANT_DROPPED",
                "detail": f"旧列 {row['old_name']} 在新版本已删除，兼容视图无法补回",
            })
            continue
        if row["change"] in (evidence.SHIFT_INSERT, evidence.SHIFT_REORDER):
            occupant = row.get("occupant")
            shifted.append({
                "ordinal": ordinal,
                "new_ordinal": row["new_ordinal"],
                "old_name": row["old_name"],
                "new_name": row["new_name"],
                "reason": ("POSITION_REORDERED" if row["change"] == evidence.SHIFT_REORDER
                           else "POSITION_INSERT_SHIFT"),
                "hazard": (f"第 {ordinal} 列现在坐着 {occupant['name']}"
                           if occupant else None),
            })

    if problems:
        # 多条问题取原因码排序最靠前的一条作为主原因，明细全部保留
        primary = sorted({item["reason"] for item in problems})[0]
        return {
            "table": table,
            "verdict": evidence.HALT,
            "reason": primary,
            "detail": "；".join(item["detail"] for item in problems),
            "affected_ordinals": sorted({item["ordinal"] for item in problems}),
            "problems": problems,
            "shifted": shifted,
        }
    if shifted:
        primary = sorted({item["reason"] for item in shifted})[0]
        return {
            "table": table,
            "verdict": evidence.USE_VIEW,
            "reason": primary,
            "detail": f"{len(shifted)} 个旧列号发生位移，按旧列序建兼容视图可恢复",
            "affected_ordinals": sorted(item["ordinal"] for item in shifted),
            "shifted": shifted,
        }
    return {
        "table": table,
        "verdict": evidence.PROCEED,
        "reason": "POSITION_PROCEED",
        "detail": "消费的列号在两侧同位置同身份",
        "affected_ordinals": ordinals,
        "shifted": [],
    }


_RANK = {evidence.HALT: 0, evidence.USE_VIEW: 1, evidence.PROCEED: 2}


def adjudicate(parsed_consumer: dict, position_evidence: dict) -> dict:
    """给一个位置消费方汇总三档结论；非位置消费方不裁定。"""
    if not parsed_consumer.get("is_position"):
        return {
            "consumer": parsed_consumer["consumer"],
            "is_position": False,
            "verdict": None,
            "reason": None,
            "mode": None,
            "claims": [],
        }

    if parsed_consumer.get("registration_error"):
        return {
            "consumer": parsed_consumer["consumer"],
            "is_position": True,
            "verdict": evidence.HALT,
            "reason": "POSITION_EVIDENCE_MISSING",
            "mode": parsed_consumer.get("mode"),
            "kind": parsed_consumer.get("kind"),
            "claims": [],
            "detail": f"登记不合法：{parsed_consumer['registration_error']}",
            "affected_tables": [],
        }

    entries = evidence.entry_index(position_evidence)
    claim_results = [
        _claim_verdict(claim, position_evidence, entries)
        for claim in parsed_consumer["claims"]
    ]
    verdict = sorted((item["verdict"] for item in claim_results),
                     key=lambda value: _RANK[value])[0]
    reason = sorted({item["reason"] for item in claim_results
                     if item["verdict"] == verdict})[0]
    return {
        "consumer": parsed_consumer["consumer"],
        "is_position": True,
        "verdict": verdict,
        "reason": reason,
        "mode": parsed_consumer.get("mode"),
        "kind": parsed_consumer.get("kind"),
        "claims": claim_results,
        "detail": "；".join(item["detail"] for item in claim_results
                            if item["verdict"] == verdict),
        "affected_tables": sorted({
            item["table"] for item in claim_results if item["verdict"] == verdict
        }),
        "affected_ordinals": sorted({
            ordinal
            for item in claim_results if item["verdict"] == verdict
            for ordinal in item.get("affected_ordinals", [])
        }),
    }


def adjudicate_all(parsed_consumers: list[dict], position_evidence: dict) -> list[dict]:
    return [adjudicate(item, position_evidence) for item in parsed_consumers]


def summary(verdicts: list[dict], evidence_hash: str | None = None) -> dict:
    positioned = [item for item in verdicts if item.get("is_position")]
    counts = {evidence.PROCEED: 0, evidence.USE_VIEW: 0, evidence.HALT: 0}
    for item in positioned:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    return {
        "position_consumers": len(positioned),
        "proceed": counts[evidence.PROCEED],
        "use_view": counts[evidence.USE_VIEW],
        "halt": counts[evidence.HALT],
        "switched": sum(1 for item in positioned if item.get("activation") == "view"),
        "evidence_hash": evidence_hash,
    }
