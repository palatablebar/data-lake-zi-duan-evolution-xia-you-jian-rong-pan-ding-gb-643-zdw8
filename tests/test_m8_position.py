"""M8 列序裁定：位置证据、三档裁定、兼容视图、切换失败恢复、幂等。

场景（sample_data/schema_pos_*.sql + refs_position.json）：
- fct_txn 中间插列，旧身份全在 → USE_VIEW，视图按旧列序恢复；
- dim_shop 存量列调位 → USE_VIEW；
- dim_pay 删掉 coupon_amt → HALT（视图补不回）；
- 不存在的表 / 越界列号 → HALT（旧位置映射未核清）；
- 只读前两列（位置没变）→ PROCEED；
- 按名消费方不进裁定。
"""

from __future__ import annotations

import csv
import io

import pytest

from app import errors, store
from app.modules.m8_position import claims, evidence, service, verdict
from app.modules.m1_intake import service as intake
from app.modules.m2_types import service as types
from app.modules.m6_batches import service as batch_service

POS_OLD = "schema_pos_old.sql"
POS_NEW = "schema_pos_new.sql"
POS_REFS = "refs_position.json"


@pytest.fixture()
def pos_run(client):
    return batch_service.execute(
        old_file=POS_OLD, new_file=POS_NEW, refs_file=POS_REFS, base_file=None,
        params={"wave": "position"},
    )


def _by_consumer(run) -> dict:
    return {
        item["consumer"]: item
        for item in run["position"]["verdicts"] if item.get("is_position")
    }


def test_evidence_hashes_ordinal_identity_and_change(pos_run):
    position = pos_run["position"]
    assert position["evidence_hash"].startswith("pos-")
    index = evidence.entry_index(position)
    # 旧第 3 列 txn_amt 被新插入的 channel 挤到第 4 列
    row = index[("fct_txn", 3)]
    assert row["old_name"] == "txn_amt"
    assert row["new_ordinal"] == 4
    assert row["change"] == evidence.SHIFT_INSERT
    assert row["reason"] == "POSITION_INSERT_SHIFT"
    # 现占位列是另一个身份：这就是“交易值/状态读串”的来源证据
    assert row["occupant"]["name"] == "channel"
    # 新列按 appended 单列，不占用旧列号
    appended = [r for r in position["entries"] if r["change"] == evidence.SHIFT_APPEND]
    assert {r["new_name"] for r in appended} == {"channel", "settled_at", "manager"}


def test_three_tier_verdicts(pos_run):
    by_name = _by_consumer(pos_run)
    assert by_name["etl.txn_amt_reader"]["verdict"] == evidence.USE_VIEW
    assert by_name["etl.txn_wide_loader"]["verdict"] == evidence.USE_VIEW
    assert by_name["etl.shop_wide_loader"]["verdict"] == evidence.USE_VIEW
    assert by_name["etl.shop_first_two"]["verdict"] == evidence.USE_VIEW
    assert by_name["etl.txn_id_reader"]["verdict"] == evidence.PROCEED
    # 删列、表消失、列号越界一律 HALT
    assert by_name["etl.pay_coupon_reader"]["verdict"] == evidence.HALT
    assert by_name["etl.pay_coupon_reader"]["reason"] == "POSITION_OCCUPANT_DROPPED"
    assert by_name["etl.ghost_table_loader"]["verdict"] == evidence.HALT
    assert by_name["etl.ghost_table_loader"]["reason"] == "POSITION_EVIDENCE_MISSING"
    assert by_name["etl.bad_ordinal_reader"]["verdict"] == evidence.HALT
    assert by_name["etl.bad_ordinal_reader"]["reason"] == "POSITION_EVIDENCE_MISSING"


def test_name_based_consumer_is_not_adjudicated(pos_run):
    assert all(item["consumer"] != "etl.name_based_reader"
               for item in pos_run["position"]["verdicts"]
               if item.get("is_position"))
    assert pos_run["position"]["summary"]["position_consumers"] == 8


def test_compat_view_reconstructs_old_order(pos_run):
    views = {view["table"]: view for view in pos_run["position"]["views"]}
    assert set(views) == {"fct_txn", "dim_shop"}
    txn_view = views["fct_txn"]["sql"]
    # 视图按旧列序排列，新名 channel/settled_at 不在旧宽度内
    assert "txn_amt, status" in txn_view
    assert "channel" not in txn_view and "settled_at" not in txn_view
    # HALT 的 dim_pay 不生成视图
    assert "dim_pay" not in views


def test_blocked_table_forces_halt(client):
    """默认批次里 dim_order 有阻断列，位置证据不完整，任何位置消费方都得停下。"""
    run = store.latest_run()
    by_name = _by_consumer(run)
    assert run["position"]["blocked_tables"] == ["dim_order"]
    for name in ("dw.task_order_posload", "dw.task_order_stat_pos"):
        assert by_name[name]["verdict"] == evidence.HALT
        assert by_name[name]["reason"] == "POSITION_EVIDENCE_MISSING"


def test_verdict_records_are_idempotent_on_rerun(pos_run):
    evidence_hash = pos_run["position"]["evidence_hash"]
    before = store.list_position_verdicts(evidence_hash)
    again = batch_service.execute(
        old_file=POS_OLD, new_file=POS_NEW, refs_file=POS_REFS, base_file=None,
        params={"wave": "position"},
    )
    assert again["reused"] is True
    after = store.list_position_verdicts(evidence_hash)
    # 同一结构重跑：裁定记录条数不增加，证据指纹不变
    assert len(before) == len(after)
    assert again["position"]["evidence_hash"] == evidence_hash


def test_switch_only_allows_use_view(client, pos_run):
    with pytest.raises(errors.BadInput):
        service.switch_to_view(pos_run, consumers=["etl.pay_coupon_reader"])
    result = service.switch_to_view(pos_run, consumers=["etl.txn_amt_reader"])
    assert result["reason"] == "POSITION_VIEW_SWITCHED"
    record = store.get_position_verdict(
        pos_run["position"]["evidence_hash"], "etl.txn_amt_reader")
    assert record["activation"] == "view"


def test_switch_failure_restores_verdicts_and_exports(client, pos_run):
    before_csv = service.position_csv_text(pos_run)
    from app.modules.m7_migration import service as migration
    before_md = migration.spec_markdown(pos_run)

    with pytest.raises(errors.Conflict) as caught:
        service.switch_to_view(pos_run, simulate_fail=True)
    assert caught.value.detail["reason"] == "POSITION_SWITCH_FAILED"
    assert caught.value.detail["exports_restored"] is True

    # 激活态全部回到 none，导出逐字复原
    for record in store.list_position_verdicts(pos_run["position"]["evidence_hash"]):
        assert record["activation"] == "none"
    assert service.position_csv_text(pos_run) == before_csv
    assert migration.spec_markdown(pos_run) == before_md

    audit = service.audit_trail(pos_run)
    assert audit[0]["status"] == "failed"
    assert audit[0]["action"] == "switch"


def test_rollback_restores_old_verdicts_and_exports(client, pos_run):
    before_csv = service.position_csv_text(pos_run)
    service.switch_to_view(pos_run)
    assert service.position_csv_text(pos_run) != before_csv
    result = service.rollback(pos_run)
    assert result["exports_match"] is True
    assert service.position_csv_text(pos_run) == before_csv


def test_position_csv_and_page_share_same_evidence(client, pos_run):
    rows = list(csv.DictReader(io.StringIO(service.position_csv_text(pos_run))))
    consumers = {row["consumer"] for row in rows}
    assert "etl.txn_amt_reader" in consumers
    assert "etl.name_based_reader" not in consumers  # 按名消费方不导出
    page = client.get("/position")
    assert page.status_code == 200
    assert pos_run["position"]["evidence_hash"] in page.text
    spec = client.get("/api/export/spec.md").text
    assert pos_run["position"]["evidence_hash"] in spec
    assert "## 7 列序裁定" in spec


def test_claims_reject_bad_registration():
    bad = claims.parse_consumer({
        "name": "x", "access": "position", "mode": "both",
        "positions": [{"table": "t", "ordinals": [0]}],
    })
    assert bad["registration_error"] is not None
    # 登记不合法直接 HALT，不放行
    result = verdict.adjudicate(bad, {"evidence_hash": "pos-test", "tables": [], "entries": []})
    assert result["verdict"] == evidence.HALT
    assert result["reason"] == "POSITION_EVIDENCE_MISSING"


def test_claims_reject_unknown_mode():
    with pytest.raises(errors.BadInput):
        claims.parse_consumer({"name": "x", "access": "position", "mode": "sideways"})


def test_pure_evidence_pipeline_on_snapshots():
    old = types.prepare(intake.ingest(POS_OLD))
    new = types.prepare(intake.ingest(POS_NEW))
    built = evidence.build_evidence(old["fields"], new["fields"])
    # 确定性：同样的两版字段必然得到同一指纹
    again = evidence.build_evidence(old["fields"], new["fields"])
    assert built["evidence_hash"] == again["evidence_hash"]
    # 输入重排不改变指纹
    shuffled = dict(built)
    shuffled["entries"] = list(reversed(built["entries"]))
    assert evidence.evidence_hash_of(built["entries"]) == evidence.evidence_hash_of(shuffled["entries"])


def test_http_switch_failure_and_rollback_restore_exports(client):
    run = batch_service.execute(
        old_file=POS_OLD, new_file=POS_NEW, refs_file=POS_REFS, base_file=None,
        params={"wave": "http"},
    )
    evidence_hash = run["position"]["evidence_hash"]
    before_csv = client.get("/api/export/position.csv").text

    # 模拟失败：409，且旧版裁定与导出恢复
    fail = client.post("/api/position/switch", json={"simulate_fail": True})
    assert fail.status_code == 409
    assert fail.json()["detail"]["reason"] == "POSITION_SWITCH_FAILED"
    assert fail.json()["detail"]["exports_restored"] is True
    assert client.get("/api/export/position.csv").text == before_csv
    assert all(row["activation"] == "none"
               for row in store.list_position_verdicts(evidence_hash))

    # 正常切换后再回滚，导出逐字回到切换前
    ok = client.post("/api/position/switch")
    assert ok.status_code == 200
    assert len(ok.json()["switched"]) == 4
    back = client.post("/api/position/rollback")
    assert back.status_code == 200
    assert back.json()["exports_match"] is True
    assert client.get("/api/export/position.csv").text == before_csv


def test_http_rerun_same_structure_adds_no_verdict_records(client):
    run = batch_service.execute(
        old_file=POS_OLD, new_file=POS_NEW, refs_file=POS_REFS, base_file=None,
        params={"wave": "http-rerun"},
    )
    evidence_hash = run["position"]["evidence_hash"]
    first = store.list_position_verdicts(evidence_hash)
    # 先切一个消费方，验证重跑不会把激活态冲掉、也不会新增记录
    service.switch_to_view(run, consumers=["etl.txn_amt_reader"])
    again = batch_service.execute(
        old_file=POS_OLD, new_file=POS_NEW, refs_file=POS_REFS, base_file=None,
        params={"wave": "http-rerun"},
    )
    assert again["reused"] is True
    second = store.list_position_verdicts(evidence_hash)
    assert len(first) == len(second)
    record = store.get_position_verdict(evidence_hash, "etl.txn_amt_reader")
    assert record["activation"] == "view"
