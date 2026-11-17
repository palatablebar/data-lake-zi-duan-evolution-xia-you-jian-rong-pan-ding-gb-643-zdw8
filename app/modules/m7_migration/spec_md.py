"""迁移单：把一次判定的结论整理成带条款号与脚注的 Markdown。"""

from __future__ import annotations

from app import config, reasons

FOOTNOTES = [
    "字段身份按 field_id 对齐，改名不算新字段。",
    "类型放宽指新类型能无损容纳旧类型的全部取值。",
    f"生效区间按 {config.WINDOW_SEMANTICS} 处理。",
    "账目差额应为 0；不为 0 说明判定漏项。",
]


def clauses(run: dict) -> list[dict]:
    """按优先级列出破坏项，每条一个条款号。"""
    rows = [row for row in (run.get("diff") or []) if row["verdict"] == "BREAKING"]
    rows.sort(key=lambda row: (row["table"], row["old_name"] or row["new_name"] or ""))
    out: list[dict] = []
    for index, row in enumerate(rows, start=1):
        out.append({
            "no": f"3.{index}",
            "field_id": row["field_id"],
            "table": row["table"],
            "old_name": row["old_name"],
            "new_name": row["new_name"],
            "old_type": row["old_type"],
            "new_type": row["new_type"],
            "change": row["change"],
            "type_detail": row["type_detail"],
            "primary": row["primary"],
            "primary_label": reasons.label(row["primary"]),
            "reader_ok": row["reader_ok"],
            "writer_ok": row["writer_ok"],
            "ref_count": row["ref_count"],
            "reasons": row["reasons"],
        })
    return out


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _position_section(run: dict) -> list[str]:
    """列序裁定：页面与迁移单必须展示同一份位置证据（同一 evidence_hash）。"""
    from app.modules.m8_position import evidence as evidence_mod

    position = run.get("position") or {}
    summary = position.get("summary") or {}
    lines = [
        "## 7 列序裁定",
        "",
        f"- 位置证据指纹：`{position.get('evidence_hash', '—')}`",
        f"- 位置消费方：{summary.get('position_consumers', 0)} 个，"
        f"可继续 {summary.get('proceed', 0)}、需切兼容视图 {summary.get('use_view', 0)}、"
        f"停止放行 {summary.get('halt', 0)}",
        "",
        _md_table(
            ["消费方", "模式", "表", "裁定", "原因码", "受影响旧列号"],
            [
                [
                    item["consumer"],
                    item.get("mode") or "—",
                    claim["table"],
                    evidence_mod.VERDICT_LABEL.get(claim["verdict"], claim["verdict"]),
                    claim["reason"],
                    " ".join(str(x) for x in claim.get("affected_ordinals", [])) or "—",
                ]
                for item in position.get("verdicts", [])
                if item.get("is_position")
                for claim in (item.get("claims") or [{}])
            ],
        ),
        "",
    ]
    views = position.get("views") or []
    if views:
        lines.append("列序兼容视图：")
        lines.append("")
        lines.append("```sql")
        for view in views:
            lines.append(view["sql"])
        lines.append("```")
        lines.append("")
    blocked = position.get("blocked_tables") or []
    if blocked:
        lines.append(f"阻断表（旧位置映射未核清，禁止放行）：{', '.join(blocked)}")
        lines.append("")
    return lines


def render(run: dict) -> str:
    ledger = run.get("ledger") or {}
    fields = ledger.get("fields") or {}
    refs = ledger.get("refs") or {}
    buckets = fields.get("buckets") or {}
    digests = run.get("digests") or {}
    parts = [
        f"# 迁移单 {run['run_id']}",
        "",
        f"- 版本：{run.get('version_label_old')} → {run.get('version_label_new')}",
        f"- 规则集：{run.get('ruleset')}",
        f"- 生成时间：{config.to_local_text(_created(run))}",
        f"- 生效区间：{run.get('effective_from')} 起，半开区间[^3]",
        "",
        "## 1 输入",
        "",
        _md_table(
            ["角色", "文件", "sha256"],
            [[role, name, digest] for role, (name, digest) in sorted(_inputs(run).items())],
        ),
        "",
        "## 2 字段账",
        "",
        _md_table(
            ["项", "数"],
            [["旧版本字段", fields.get("old_total", 0)],
             ["未变", buckets.get("unchanged", 0)],
             ["改名", buckets.get("renamed", 0)],
             ["换类型", buckets.get("retyped", 0)],
             ["改必填", buckets.get("required", 0)],
             ["冲突", buckets.get("conflict", 0)],
             ["删除", buckets.get("deleted", 0)],
             ["新增", buckets.get("added", 0)],
             ["新版本字段", fields.get("new_total", 0)],
             ["旧侧差额", fields.get("old_gap", 0)],
             ["新侧差额", fields.get("new_gap", 0)]],
        ),
        "",
        f"字段身份按 field_id 对齐[^1]，类型放宽按值域与刻度判定[^2]，账目差额应为 0[^4]。",
        "",
        "## 3 破坏项",
        "",
    ]

    items = clauses(run)
    if not items:
        parts.append("无破坏项。")
        parts.append("")
    for item in items:
        parts.append(f"### {item['no']} {item['table']}.{item['old_name'] or '—'}"
                     f" → {item['new_name'] or '—'}")
        parts.append("")
        parts.append(f"- 原因码：{item['primary']}（{item['primary_label']}）")
        parts.append(f"- 类型：{item['old_type'] or '—'} → {item['new_type'] or '—'}"
                     f"，变化 {item['change']}")
        parts.append(f"- 读者：{'通过' if item['reader_ok'] else '不通过'}"
                     f"；写者：{'通过' if item['writer_ok'] else '不通过'}")
        parts.append(f"- 下游引用：{item['ref_count']} 处")
        parts.append("")

    parts.extend([
        "## 4 映射",
        "",
        "```sql",
    ])
    aliases = (run.get("mapping") or {}).get("aliases") or []
    if aliases:
        for item in aliases:
            parts.append(f"{item['sql']}  -- {item['table']}")
    else:
        parts.append("-- 无改名，无需别名")
    parts.extend([
        "```",
        "",
        "## 5 影响面",
        "",
        _md_table(
            ["项", "数"],
            [["下游节点", (run.get("impact") or {}).get("summary", {}).get("consumers", 0)],
             ["登记引用", refs.get("refs_total", 0)],
             ["可达", refs.get("reachable", 0)],
             ["断链", refs.get("broken", 0)],
             ["受影响节点", (run.get("impact") or {}).get("impact", {}).get("total", 0)],
             ["环", (run.get("impact") or {}).get("cycles", {}).get("cycle_count", 0)],
             ["自引用", (run.get("impact") or {}).get("cycles", {}).get("self_loop_count", 0)],
             ["单边登记", len((run.get("impact") or {}).get("one_sided") or [])]],
        ),
        "",
        "## 6 回滚",
        "",
        "1. 按第 4 节建兼容视图，旧名字继续可用。",
        "2. 破坏项逐个补默认值或改类型，再重跑判定。",
        "3. 版本切回上一版，生效区间端点保持半开。",
        "",
    ])
    parts.extend(_position_section(run))
    for index, note in enumerate(FOOTNOTES, start=1):
        parts.append(f"[^{index}]: {note}")
    parts.append("")
    return "\n".join(parts)


def _created(run: dict):
    from datetime import datetime

    raw = run.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _inputs(run: dict) -> dict[str, tuple[str, str]]:
    names = run.get("input_names") or {}
    digests = run.get("digests") or {}
    return {role: (names.get(role, "—"), digests.get(role, "—")) for role in digests}
