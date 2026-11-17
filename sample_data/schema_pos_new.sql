-- fieldmap 元数据导出 v2026_10（列序裁定专用新版）
-- fct_txn：只在中间插入新列，旧身份相对顺序不变（纯位移 → 兼容视图可救）
-- dim_shop：存量列被挪位，第 3 列身份换人（错位 → 必须停下）
CREATE TABLE fct_txn (
  `txn_id`     BIGINT        NOT NULL,  -- fid=5001
  `order_no`   VARCHAR(64)   NOT NULL,  -- fid=5002
  `channel`    VARCHAR(16)   NULL,      -- fid=5005
  `txn_amt`    DECIMAL(18,2) NOT NULL,  -- fid=5003
  `status`     VARCHAR(16)   NOT NULL,  -- fid=5004
  `settled_at` TIMESTAMP     NULL       -- fid=5006
);
CREATE TABLE dim_shop (
  `shop_id`    BIGINT        NOT NULL,  -- fid=6001
  `region`     VARCHAR(24)   NULL,      -- fid=6003
  `shop_name`  VARCHAR(32)   NOT NULL,  -- fid=6002
  `manager`    VARCHAR(32)   NULL       -- fid=6004
);
CREATE TABLE dim_pay (
  `pay_id`     BIGINT        NOT NULL,  -- fid=7001
  `pay_amt`    DECIMAL(18,2) NOT NULL   -- fid=7002
);
