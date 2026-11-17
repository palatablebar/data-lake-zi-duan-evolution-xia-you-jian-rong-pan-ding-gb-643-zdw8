"""列序兼容视图：按旧列序重建一张可按位置读写的视图。

只有 USE_VIEW 的消费方才给视图：它消费的旧身份全部还在，视图按旧列号顺序
`新名 as 旧名` 排好，按位置读写就落回正确的列。HALT 的表（删列、表消失、证据
阻断）不存在安全视图，直接不给，避免制造一个看似可用实则错位的出口。
"""

from __future__ import annotations

from app.modules.m8_position import evidence


def _view_name(table: str) -> str:
    return f"{table}__pos_compat"


def build_table_view(table: str, position_evidence: dict) -> dict | None:
    """一张表的旧列序兼容视图；无法安全重建时返回 None。"""
    entries = evidence.entry_index(position_evidence)
    table_summary = next((row for row in position_evidence.get("tables", [])
                          if row["table"] == table), None)
    if table_summary is None or table_summary.get("missing_in_new") \
            or table_summary.get("old_evidence_blocked") \
            or table_summary.get("new_evidence_blocked"):
        return None

    select_items: list[str] = []
    for ordinal in range(1, table_summary["old_width"] + 1):
        row = entries.get((table, ordinal))
        if row is None or row["change"] == evidence.SHIFT_DROP or row.get("new_name") is None:
            return None
        if row["new_name"] == row["old_name"]:
            select_items.append(row["new_name"])
        else:
            select_items.append(f"{row['new_name']} as {row['old_name']}")

    sql = (
        f"CREATE OR REPLACE VIEW {_view_name(table)} AS\n"
        f"SELECT {', '.join(select_items)}\nFROM {table};"
    )
    return {
        "table": table,
        "view": _view_name(table),
        "sql": sql,
        "width": len(select_items),
        "old_width": table_summary["old_width"],
    }


def build_for_verdicts(verdicts: list[dict], position_evidence: dict) -> list[dict]:
    """给每个 USE_VIEW 消费方涉及的表生成视图，同一张表只生成一份。"""
    tables: set[str] = set()
    for item in verdicts:
        if item.get("verdict") != evidence.USE_VIEW:
            continue
        for claim in item.get("claims", []):
            if claim.get("verdict") == evidence.USE_VIEW:
                tables.add(claim["table"])
    views = []
    for table in sorted(tables):
        view = build_table_view(table, position_evidence)
        if view is not None:
            views.append(view)
    return views
