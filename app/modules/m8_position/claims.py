"""位置消费方登记解析：把下游登记里的按列号读写声明归一成位置主张。

登记约定（refs_downstream.json 的 consumer 节点）：
- access="position" 才是位置消费方；缺省 "name" 走按名解析，不进列序裁定。
- mode：read 只按位置读、write 只按位置写、both 读写都按位置。
- positions：[{"table": "fct_txn", "ordinals": [3, 4]}]，显式声明消费哪些旧列号。
- 只给 position_tables 不给 positions：按整行宽度消费（select * / 无列清单装载）。

朴素实现的死法：把 positions 原样信进去，列号越界、表不存在也照判兼容。
这里逐条校验，登记自身不合法就把该消费方判 HALT，绝不放行。
"""

from __future__ import annotations

from app import errors

ACCESS_NAME = "name"
ACCESS_POSITION = "position"
MODES = ("read", "write", "both")
# 单表登记的列号上限，防止异常登记把裁定拖死
MAX_ORDINALS = 512


def is_position_consumer(consumer: dict) -> bool:
    return str(consumer.get("access") or ACCESS_NAME).strip().lower() == ACCESS_POSITION


def _coerce_ordinals(raw, consumer_name: str) -> tuple[list[int] | None, str | None]:
    """返回 (列号清单, 错误原因)；None 列号表示整行宽度消费。"""
    if raw in (None, []):
        return None, None
    if not isinstance(raw, list):
        return None, "positions 必须是数组"
    ordinals: list[int] = []
    for value in raw:
        if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
            return None, "列号必须是正整数"
        try:
            ordinal = int(value)
        except (TypeError, ValueError):
            return None, "列号必须是正整数"
        if ordinal <= 0:
            return None, "列号必须从 1 开始"
        if ordinal > MAX_ORDINALS:
            return None, f"列号超过上限 {MAX_ORDINALS}"
        ordinals.append(ordinal)
    if not ordinals:
        return None, None
    return sorted(set(ordinals)), None


def parse_consumer(consumer: dict) -> dict:
    """把一个登记节点归一成位置主张；非位置消费方返回空主张。"""
    name = str(consumer.get("name") or "")
    if not is_position_consumer(consumer):
        return {"consumer": name, "is_position": False, "claims": [], "registration_error": None}

    mode = str(consumer.get("mode") or "both").strip().lower()
    if mode not in MODES:
        raise errors.BadInput(
            "位置消费方 mode 只能是 read / write / both",
            detail={"consumer": name, "mode": mode},
        )

    raw_tables = consumer.get("position_tables") or []
    if raw_tables and not isinstance(raw_tables, list):
        raise errors.BadInput("position_tables 必须是数组", detail={"consumer": name})
    table_names = [str(item).strip() for item in raw_tables if str(item).strip()]

    raw_positions = consumer.get("positions") or []
    if raw_positions and not isinstance(raw_positions, list):
        raise errors.BadInput("positions 必须是数组", detail={"consumer": name})

    claims: list[dict] = []
    registration_error: str | None = None
    seen_tables: set[str] = set()

    for item in raw_positions:
        if not isinstance(item, dict):
            registration_error = "positions 项必须是对象"
            continue
        table = str(item.get("table") or "").strip()
        if not table:
            registration_error = "positions 项缺 table"
            continue
        if table in seen_tables:
            registration_error = f"表 {table} 的位置声明重复"
            continue
        seen_tables.add(table.lower())
        ordinals, problem = _coerce_ordinals(item.get("ordinals"), name)
        if problem:
            registration_error = problem
            continue
        claims.append({"table": table, "ordinals": ordinals})

    # 只登记了表、没给具体列号：整行宽度消费
    for table in table_names:
        if table.lower() in seen_tables:
            continue
        seen_tables.add(table.lower())
        claims.append({"table": table, "ordinals": None})

    if not claims and registration_error is None:
        registration_error = "位置消费方没有任何表或列号声明"

    return {
        "consumer": name,
        "is_position": True,
        "mode": mode,
        "kind": str(consumer.get("kind") or "task"),
        "claims": claims,
        "registration_error": registration_error,
    }


def parse_all(consumers: list[dict]) -> list[dict]:
    return [parse_consumer(item) for item in consumers]
