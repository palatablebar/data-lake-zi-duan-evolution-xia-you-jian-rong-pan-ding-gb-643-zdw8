"""列序证据：从两个版本的已采信字段里重建每张表的列位置，并逐位置比对。

身份比对（field_id）看不出列序问题：插入一列、挪动一列之后，按身份对账仍然兼容，
但按列位置（ordinal）读写的任务会把第 N 列读成别人。这里只认字段在 DDL/JSON 里的
声明次序（采信阶段保留的行号），逐表给出 1 基列号、两侧身份与位移分类。

证据是后续裁定、页面、迁移单的唯一来源：同一份证据喂给所有出口，展示必须一致。
"""

from __future__ import annotations

import hashlib
import json

# 列序裁定三档：继续 / 切兼容视图 / 停下
PROCEED = "PROCEED"
USE_VIEW = "USE_VIEW"
HALT = "HALT"

VERDICT_REASON = {
    PROCEED: "POSITION_PROCEED",
    USE_VIEW: "POSITION_USE_VIEW",
    HALT: "POSITION_HALT",
}

VERDICT_LABEL = {
    PROCEED: "可继续",
    USE_VIEW: "需切兼容视图",
    HALT: "停止放行",
}

# 位移分类（位置证据行的 change 字段）
SHIFT_SAME = "same"            # 该位置两侧身份一致
SHIFT_INSERT = "insert_shift"  # 新列插入，把旧身份挤到后面
SHIFT_REORDER = "reordered"    # 存量列相对顺序被调整
SHIFT_SWAP = "occupant_swap"   # 同一位置身份已换人
SHIFT_DROP = "occupant_dropped"  # 旧位置在新版本没有对应字段
SHIFT_APPEND = "appended"      # 新版本尾部追加的列，旧版本没有该位置

# 位移分类 -> 原因码（原因码唯一来源是 app/reasons.py）
SHIFT_REASON = {
    SHIFT_SAME: "POSITION_PROCEED",
    SHIFT_INSERT: "POSITION_INSERT_SHIFT",
    SHIFT_REORDER: "POSITION_REORDERED",
    SHIFT_SWAP: "POSITION_OCCUPANT_SWAP",
    SHIFT_DROP: "POSITION_OCCUPANT_DROPPED",
    SHIFT_APPEND: "FIELD_ADDED",
}


def _table_order(fields: list[dict]) -> dict[str, list[dict]]:
    """每张表的字段按声明行号排序，行号并列时按 field_id 兜底，保证可复算。"""
    tables: dict[str, list[dict]] = {}
    for field in fields:
        tables.setdefault(field["table"], []).append(field)
    for table in tables:
        tables[table].sort(key=lambda item: (int(item.get("line") or 0),
                                             str(item.get("field_id") or "")))
    return dict(sorted(tables.items()))


def _index_by_id(fields: list[dict]) -> dict[str, dict]:
    return {field["field_id"]: field for field in fields}


def _blocked_tables(diagnostics: list[dict] | None) -> set[str]:
    """有阻断列的表，其列序证据不完整，裁定必须停下。"""
    return {
        str(row.get("table") or "")
        for row in (diagnostics or [])
        if row.get("blocking") and row.get("table")
    }


def build_evidence(fields_old: list[dict], fields_new: list[dict],
                   diagnostics_old: list[dict] | None = None,
                   diagnostics_new: list[dict] | None = None) -> dict:
    """逐表逐位置给出列序证据。

    每个旧列号一行；新版本尾部追加的列另外以 appended 行列出。
    """
    old_tables = _table_order(fields_old)
    new_tables = _table_order(fields_new)
    new_by_id = _index_by_id(fields_new)
    old_blocked = _blocked_tables(diagnostics_old)
    new_blocked = _blocked_tables(diagnostics_new)

    entries: list[dict] = []
    table_summaries: list[dict] = []
    for table in sorted(set(old_tables) | set(new_tables)):
        old_cols = old_tables.get(table, [])
        new_cols = new_tables.get(table, [])
        old_ids = [col["field_id"] for col in old_cols]
        new_ids = [col["field_id"] for col in new_cols]
        new_pos = {fid: idx for idx, fid in enumerate(new_ids, start=1)}
        old_pos = {fid: idx for idx, fid in enumerate(old_ids, start=1)}
        survivor_ids = [fid for fid in old_ids if fid in new_pos]
        survivor_new_positions = [new_pos[fid] for fid in survivor_ids]
        # 存量列的相对顺序是否被调整（插入只会整体后移，不会改变相对次序）
        reordered = survivor_new_positions != sorted(survivor_new_positions)

        table_changes: set[str] = set()
        for ordinal, old_col in enumerate(old_cols, start=1):
            fid = old_col["field_id"]
            new_col = new_by_id.get(fid)
            new_ordinal = new_pos.get(fid)
            occupant_new = new_cols[ordinal - 1] if ordinal - 1 < len(new_cols) else None
            same_place = occupant_new is not None and occupant_new["field_id"] == fid

            if new_col is None:
                change = SHIFT_DROP
            elif same_place:
                change = SHIFT_SAME
            elif reordered:
                change = SHIFT_REORDER
            else:
                change = SHIFT_INSERT

            if change in (SHIFT_DROP, SHIFT_REORDER, SHIFT_INSERT) and occupant_new is not None \
                    and occupant_new["field_id"] != fid:
                # 这个位置现在坐着另一个身份：按位置读写会把值读/写到错误的列
                occupant = {
                    "field_id": occupant_new["field_id"],
                    "name": occupant_new["name"],
                    "ordinal": ordinal,
                }
            else:
                occupant = None
            if change != SHIFT_SAME:
                table_changes.add(change)

            entries.append({
                "table": table,
                "ordinal": ordinal,
                "old_field_id": fid,
                "old_name": old_col["name"],
                "old_line": int(old_col.get("line") or 0),
                "new_ordinal": new_ordinal,
                "new_field_id": new_col["field_id"] if new_col else None,
                "new_name": new_col["name"] if new_col else None,
                "new_line": int(new_col.get("line") or 0) if new_col else None,
                "change": change,
                "reason": SHIFT_REASON[change],
                "occupant": occupant,
            })

        for ordinal, new_col in enumerate(new_cols, start=1):
            if new_col["field_id"] in old_pos:
                continue
            table_changes.add(SHIFT_APPEND)
            entries.append({
                "table": table,
                "ordinal": None,
                "old_field_id": None,
                "old_name": None,
                "old_line": None,
                "new_ordinal": ordinal,
                "new_field_id": new_col["field_id"],
                "new_name": new_col["name"],
                "new_line": int(new_col.get("line") or 0),
                "change": SHIFT_APPEND,
                "reason": SHIFT_REASON[SHIFT_APPEND],
                "occupant": None,
            })

        table_summaries.append({
            "table": table,
            "old_width": len(old_cols),
            "new_width": len(new_cols),
            "reordered": reordered,
            "changes": sorted(table_changes),
            "old_evidence_blocked": table in old_blocked,
            "new_evidence_blocked": table in new_blocked,
            "missing_in_new": table not in new_tables,
            "missing_in_old": table not in old_tables,
        })

    evidence_hash = evidence_hash_of(entries)
    return {
        "evidence_hash": evidence_hash,
        "tables": table_summaries,
        "entries": entries,
        "blocked_tables": sorted(old_blocked | new_blocked),
    }


def evidence_hash_of(entries: list[dict]) -> str:
    """位置证据指纹：同一份两版结构必然得到同一个值，重跑不产生新裁定记录。

    先按 (表, 旧列号, 新列号) 排好再哈希，输入重排不改变指纹。
    """
    canonical = sorted(
        (
            [
                row.get("table"),
                row.get("ordinal"),
                row.get("old_field_id"),
                row.get("new_ordinal"),
                row.get("new_field_id"),
                row.get("change"),
            ]
            for row in entries
        ),
        key=lambda item: (str(item[0]), item[1] is None, item[1] or 0, item[3] or 0),
    )
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return "pos-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def entry_index(evidence: dict) -> dict[tuple[str, int], dict]:
    """(表, 旧列号) -> 证据行，裁定按这个索引取位置证据。"""
    return {
        (row["table"], row["ordinal"]): row
        for row in evidence.get("entries", [])
        if row.get("ordinal") is not None
    }
