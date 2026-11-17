-- fieldmap 元数据导出 v2026_07（列序裁定专用旧版）
CREATE TABLE fct_txn (
  `txn_id`     BIGINT        NOT NULL,  -- fid=5001
  `order_no`   VARCHAR(64)   NOT NULL,  -- fid=5002
  `txn_amt`    DECIMAL(18,2) NOT NULL,  -- fid=5003
  `status`     VARCHAR(16)   NOT NULL   -- fid=5004
);
CREATE TABLE dim_shop (
  `shop_id`    BIGINT        NOT NULL,  -- fid=6001
  `shop_name`  VARCHAR(32)   NOT NULL,  -- fid=6002
  `region`     VARCHAR(24)   NULL       -- fid=6003
);
CREATE TABLE dim_pay (
  `pay_id`     BIGINT        NOT NULL,  -- fid=7001
  `pay_amt`    DECIMAL(18,2) NOT NULL,  -- fid=7002
  `coupon_amt` DECIMAL(18,2) NULL       -- fid=7003
);
