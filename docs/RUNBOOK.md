# 运行手册 fieldmap

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18202/api/health
```

宿主端口 `18202`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18202/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `fieldmap-data` 挂着 `/data/fieldmap.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |
| GET | `/api/position/verdicts` | 位置消费方三档裁定与激活态 |
| GET | `/api/position/evidence` | 逐列号位置证据与指纹 |
| GET | `/api/position/views` | 列序兼容视图 SQL |
| POST | `/api/position/switch` | 切兼容视图（可带 `simulate_fail` 演练失败恢复） |
| POST | `/api/position/rollback` | 回滚到旧版裁定并校验导出复原 |
| GET | `/api/position/audit` | 切换/回滚审计 |
| GET | `/api/export/position.csv` | 列序裁定 CSV |

## 6b 列序裁定

1. 打开 `/position`，先核对位置证据指纹与每张表的逐列号位移，再看消费方裁定。
2. `PROCEED` 可继续按位置读写；`USE_VIEW` 在页面点「切换兼容视图」，
   任务改读 `表__pos_compat`；`HALT` 必须人工核清旧位置映射，不允许放行。
3. 上线前用「模拟切换失败」演练：返回 409 且激活态全部回到「旧版」才算恢复可用。
4. 切换出问题点「回滚」，返回 `exports_match=true` 才表示 CSV 与迁移单已逐字复原。
5. 同一结构重跑命中批次键，裁定记录不新增，已切的激活态保留。

## 6 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次 | 先在 `/batches` 点「跑一次」 |
| 409 `POSITION_SWITCH_FAILED` | 兼容视图切换失败 | 已自动回滚旧裁定与导出；看 `/api/position/audit` 定位失败消费方 |
| 400 `POSITION_HALT` 类 | 对 HALT 消费方切视图，或没有可切换项 | 先核清旧位置映射，HALT 不生成兼容视图 |

## 7 端口与并发

同一批其他项目占用 18200-18209；本服务固定 18202。同时起多个栈时用批目录里的 `scripts/slot.sh` 限流。
