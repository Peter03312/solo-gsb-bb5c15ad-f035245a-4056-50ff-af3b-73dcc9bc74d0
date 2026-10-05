-- ============================================================================
-- 计量校准证书追溯服务 - 数据库结构初始化 (PostgreSQL 16)
-- 幂等说明：本脚本面向全新数据卷执行（docker-entrypoint-initdb.d 仅在空库时运行）。
-- ============================================================================

-- btree_gist 提供排他约束中对普通标量列使用 = 操作符的 GiST 支持
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ----------------------------------------------------------------------------
-- 枚举类型
-- ----------------------------------------------------------------------------

-- 证书类型：REFERENCE = 基准/标准设备证书（追溯起点，无上游引用）
--           CALIBRATION = 普通校准证书（必须引用一张有效的标准设备证书）
CREATE TYPE cert_kind AS ENUM ('REFERENCE', 'CALIBRATION');

-- 证书状态：ACTIVE   = 生效中
--           REVOKED  = 被主动撤销（仅上游证书可被主动撤销）
--           INVALID  = 因上游撤销而级联失效
CREATE TYPE cert_status AS ENUM ('ACTIVE', 'REVOKED', 'INVALID');

-- 校准结论：PASS 合格 / FAIL 不合格
CREATE TYPE cert_conclusion AS ENUM ('PASS', 'FAIL');

-- ----------------------------------------------------------------------------
-- 证书主表
-- ----------------------------------------------------------------------------
CREATE TABLE certificates (
    cert_no        TEXT PRIMARY KEY,                 -- 证书编号（业务唯一键）
    kind           cert_kind     NOT NULL,
    device_id      TEXT          NOT NULL,           -- 被校/基准设备标识
    device_name    TEXT,                             -- 设备名称（冗余展示用）
    calibrated_at  TIMESTAMPTZ   NOT NULL,           -- 校准时刻
    valid_from     TIMESTAMPTZ   NOT NULL,           -- 有效期开始
    valid_to       TIMESTAMPTZ   NOT NULL,           -- 有效期结束
    conclusion     cert_conclusion NOT NULL,         -- 校准结论
    parent_cert_no TEXT REFERENCES certificates(cert_no), -- 引用的上游标准设备证书
    operator       TEXT,                             -- 签发/校准人员
    remark         TEXT,                             -- 备注
    status         cert_status   NOT NULL DEFAULT 'ACTIVE',
    revoked_reason TEXT,                             -- 撤销原因（撤销时写入）
    revoked_at     TIMESTAMPTZ,                      -- 撤销/级联失效时刻
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 基本完整性约束
ALTER TABLE certificates ADD CONSTRAINT cert_validity_order
    CHECK (valid_from < valid_to);
ALTER TABLE certificates ADD CONSTRAINT cert_calibrated_in_window
    CHECK (calibrated_at >= valid_from AND calibrated_at < valid_to);
-- 基准证书不得引用上游；普通证书必须引用上游
ALTER TABLE certificates ADD CONSTRAINT cert_reference_root
    CHECK ((kind = 'REFERENCE') = (parent_cert_no IS NULL));
-- 基准证书必须合格（它是量值溯源起点）；被主动撤销的证书必须记录原因与时间
ALTER TABLE certificates ADD CONSTRAINT cert_reference_passes
    CHECK (kind = 'CALIBRATION' OR conclusion = 'PASS');
ALTER TABLE certificates ADD CONSTRAINT cert_revoked_metadata
    CHECK (status = 'ACTIVE' OR (revoked_at IS NOT NULL AND revoked_reason IS NOT NULL));

CREATE INDEX idx_cert_device   ON certificates(device_id);
CREATE INDEX idx_cert_parent   ON certificates(parent_cert_no);
CREATE INDEX idx_cert_status   ON certificates(status);
CREATE INDEX idx_cert_created  ON certificates(created_at);

-- 同一设备“生效中(ACTIVE)”证书的有效期不得重叠。
-- 采用半开区间 [valid_from, valid_to)：首尾相接不算重叠。
-- 已撤销/已失效的证书释放区间，允许重新签发。
-- 该约束由数据库强制，即使高并发下也不会产生重叠证书。
ALTER TABLE certificates
    ADD CONSTRAINT cert_active_no_overlap
    EXCLUDE USING gist (
        device_id WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&
    ) WHERE (status = 'ACTIVE');

-- ----------------------------------------------------------------------------
-- 撤销批次表：一次主动撤销对应一条记录
-- ----------------------------------------------------------------------------
CREATE TABLE revocations (
    batch_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cert_no      TEXT NOT NULL REFERENCES certificates(cert_no),
    reason       TEXT NOT NULL,
    revoked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    operator     TEXT
);
CREATE INDEX idx_revocations_cert ON revocations(cert_no);

-- ----------------------------------------------------------------------------
-- 级联失效明细表：一次撤销导致哪些下游证书失效，及其完整引用链快照
-- chain 按 根 -> ... -> 失效证书 顺序存储证书编号，不可改写，用于追溯回放
-- ----------------------------------------------------------------------------
CREATE TABLE invalidations (
    batch_id     BIGINT NOT NULL REFERENCES revocations(batch_id),
    cert_no      TEXT NOT NULL REFERENCES certificates(cert_no),
    chain        TEXT[] NOT NULL,
    position     INT NOT NULL,   -- 失效证书在 chain 中的下标（从 1 开始）
    invalidated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, cert_no)
);

-- ----------------------------------------------------------------------------
-- 签发批次表：离线采集交接批次的一次性签发记录
-- batch_key 为幂等键；request/request_hash 为规范化请求快照及其指纹，
-- response 为首次签发结果快照——同键同请求重试时原样返回（即使证书事后被撤销），
-- 同键不同请求按 request_hash 判定为冲突。本表同样只增不改。
-- ----------------------------------------------------------------------------
CREATE TABLE issue_batches (
    batch_key    TEXT PRIMARY KEY,                 -- 批次键（幂等键，业务唯一）
    request_hash TEXT NOT NULL,                    -- 规范化请求的 SHA-256 指纹
    request      JSONB NOT NULL,                   -- 规范化请求快照
    response     JSONB NOT NULL,                   -- 首次签发结果快照（重试原样返回）
    cert_nos     TEXT[] NOT NULL,                  -- 本批签发的证书编号（按请求顺序）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 客户交付归档包表：一次客户交付冻结的一组可交付追溯凭据。
-- archive_no 为业务幂等键；request_hash 为规范化请求（有序证书编号列表）的
-- 指纹，response 为创建时刻的完整快照（每张目标证书的原始签发内容、从基准到
-- 目标的引用链、按当时已登记更正声明合并的展示字段）——同编号同列表重试原样
-- 返回（即使证书事后被更正或撤销），同编号不同列表按 request_hash 判冲突。
-- 本表只增不改，快照一经创建不受后续撤销/更正影响。
-- ----------------------------------------------------------------------------
CREATE TABLE delivery_archives (
    archive_no   TEXT PRIMARY KEY,                 -- 归档包编号（幂等键，业务唯一）
    request_hash TEXT NOT NULL,                    -- 规范化请求（有序列表）的 SHA-256
    request      JSONB NOT NULL,                   -- 规范化请求快照
    response     JSONB NOT NULL,                   -- 创建时刻的交付凭据快照（重试原样返回）
    cert_nos     TEXT[] NOT NULL,                  -- 目标证书编号（按提交顺序）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 交付签收邀请表：交付方凭本地配置的管理密钥，按归档包编号、收件人与未来
-- 截止时间创建；invitation_no 为全局唯一邀请编号，由服务端生成。
-- 一次性随机密钥仅在创建响应中明文展示一次，库内只保存其 SHA-256（key_hash），
-- 明文不持久化、不记日志。同一归档包可创建多份邀请；本表只增不改。
-- ----------------------------------------------------------------------------
CREATE TABLE delivery_invitations (
    invitation_no TEXT PRIMARY KEY,                -- 邀请编号（服务端生成，全局唯一）
    archive_no    TEXT NOT NULL REFERENCES delivery_archives(archive_no),
    recipient     TEXT NOT NULL,                   -- 收件人标识
    deadline      TIMESTAMPTZ NOT NULL,            -- 签收截止时刻（必须晚于创建时刻）
    key_hash      TEXT NOT NULL,                   -- 一次性随机密钥的 SHA-256（仅此落库）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_delivery_invitations_archive ON delivery_invitations(archive_no);

-- ----------------------------------------------------------------------------
-- 交付签收录表（定稿记录）：每个归档包至多一份（archive_no 主键）。
-- 收件人在对应邀请的截止前提交一次性密钥与 ACCEPT/REJECT 结论；
-- REJECT 必须填写原因。request_hash 为规范化提交内容指纹，同一邀请同内容
-- 重试原样返回首次定稿（截止后亦然），不同内容或其他邀请再提交判冲突。
-- 本表只增不改：后续撤销不改写签收记录与归档快照。
-- ----------------------------------------------------------------------------
CREATE TABLE delivery_receipts (
    archive_no    TEXT PRIMARY KEY REFERENCES delivery_archives(archive_no),
    invitation_no TEXT NOT NULL REFERENCES delivery_invitations(invitation_no),
    recipient     TEXT NOT NULL,                   -- 邀请指定的收件人（定稿时回填）
    decision      TEXT NOT NULL CHECK (decision IN ('ACCEPT', 'REJECT')),
    reason        TEXT,                            -- REJECT 时必填的拒收原因
    request_hash  TEXT NOT NULL,                   -- 规范化提交内容的 SHA-256 指纹
    signed_at     TIMESTAMPTZ NOT NULL             -- 签收（定稿）时刻
);

-- ----------------------------------------------------------------------------
-- 更正声明表：针对已签发证书的展示字段（设备名称/操作员/备注）登记更正声明。
-- declaration_no 为幂等键（全局唯一）；seq 为同一证书内按成功登记顺序分配的
-- 序号（从 1 开始），合并展示时按 seq 升序逐条覆盖生效。corrections 仅保存
-- 本次提交的字段：值为 JSON null 表示显式置空，未提交的字段保持原值——两者
-- 严格区分。request_hash 为规范化请求指纹：同编号同内容重试返回首次声明，
-- 同编号不同内容判冲突。本表只增不改，原证书内容不受更正影响。
-- ----------------------------------------------------------------------------
CREATE TABLE correction_declarations (
    declaration_no TEXT PRIMARY KEY,                 -- 声明编号（幂等键，业务唯一）
    cert_no        TEXT NOT NULL REFERENCES certificates(cert_no), -- 被更正的证书
    seq            INT NOT NULL,                     -- 该证书内的登记顺序（从 1 开始）
    corrections    JSONB NOT NULL,                   -- 修正字段集合（仅提交的字段）
    reason         TEXT NOT NULL,                    -- 更正原因
    request_hash   TEXT NOT NULL,                    -- 规范化请求的 SHA-256 指纹
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cert_no, seq)
);

-- ----------------------------------------------------------------------------
-- 标准证书临时停用表：计量人员发现标准证书有待复核时，凭管理密钥登记临时停用，
-- 停用期间该证书及其任何下游证书均不得作为新签发普通证书的追溯依据；复核后凭
-- 管理密钥登记解除。每次登记一行，解除不删行、不另建表：lifted_at/lift_reason
-- 在解除事务中一次性写入（触发器强制，写入后不可再改）。部分唯一索引保证同一
-- 证书至多存在一条“生效中”（lifted_at IS NULL）的停用记录。
-- 本表不触碰 certificates：证书内容与 ACTIVE/REVOKED/INVALID 状态不因停用改写。
-- ----------------------------------------------------------------------------
CREATE TABLE cert_suspensions (
    suspension_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cert_no       TEXT NOT NULL REFERENCES certificates(cert_no), -- 被停用的标准证书
    reason        TEXT NOT NULL,                                 -- 停用原因（非空）
    suspended_at  TIMESTAMPTZ NOT NULL DEFAULT now(),           -- 停用登记成功时刻（即生效时刻）
    lifted_at     TIMESTAMPTZ,                                  -- 解除时刻；NULL 表示停用生效中
    lift_reason   TEXT,                                          -- 解除原因（解除时非空）
    CONSTRAINT cert_suspension_lift_metadata
        CHECK (lifted_at IS NULL OR
               (lift_reason IS NOT NULL AND lifted_at >= suspended_at))
);

CREATE INDEX idx_cert_suspensions_cert
    ON cert_suspensions(cert_no, suspension_id);

-- 同一证书至多一条生效中的停用记录：重复停用由数据库兜底判冲突
CREATE UNIQUE INDEX idx_cert_suspensions_one_active
    ON cert_suspensions(cert_no) WHERE (lifted_at IS NULL);

-- ----------------------------------------------------------------------------
-- 普通证书签发复核申请表：需双人复核的普通校准证书，录入人先提交申请（保存
-- 待审内容与完整签发资料，不占用证书编号、不占用设备有效期），复核人再
-- 附原因驳回，或批准——批准与真正签发在同一事务内按批准当时的标准证书状态、
-- 临时停用与既有签发规则完成，结论、复核人与证书绑定。
-- application_no 为业务幂等键：同号同内容重放返回原申请，同号异内容判冲突。
-- status 只允许 PENDING -> APPROVED/REJECTED 一次性推进（触发器强制），
-- 驳回后不能再批准。本表只增不改（状态推进以外的 UPDATE/DELETE 一律拒绝）。
-- ----------------------------------------------------------------------------
CREATE TABLE certificate_review_requests (
    application_no    TEXT PRIMARY KEY,             -- 申请号（幂等键，业务唯一）
    request_hash      TEXT NOT NULL,                 -- 规范化签发资料的 SHA-256 指纹
    request           JSONB NOT NULL,                -- 完整签发资料（普通证书字段）
    status            TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    submitter         TEXT NOT NULL,                 -- 录入人身份标识（本地凭据配置名）
    submitter_key_hash TEXT NOT NULL,                -- 录入凭据 SHA-256：复核人不得与录入人同一凭据
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewer          TEXT,                          -- 复核人身份标识（定稿时写入，只写一次）
    decision_reason   TEXT,                          -- 驳回原因（REJECTED 非空）
    reviewed_at       TIMESTAMPTZ,                   -- 复核定稿时刻
    cert_no           TEXT REFERENCES certificates(cert_no), -- 批准后绑定的真正签发证书
    result            JSONB                          -- 批准时的签发结果快照（同直接签发同构）
);

-- 定稿字段的完整性：
-- * APPROVED 必须已绑定复核人、定稿时刻、证书与签发结果，且不带驳回原因；
-- * REJECTED 必须已绑定复核人、定稿时刻与非空驳回原因，且不产生证书；
-- * PENDING 必须尚无任何复核结论。
ALTER TABLE certificate_review_requests ADD CONSTRAINT cert_review_decision_metadata
    CHECK (
        (status = 'PENDING'
             AND reviewer IS NULL AND reviewed_at IS NULL
             AND decision_reason IS NULL AND cert_no IS NULL AND result IS NULL)
        OR (status = 'APPROVED'
             AND reviewer IS NOT NULL AND reviewed_at IS NOT NULL
             AND decision_reason IS NULL
             AND cert_no IS NOT NULL AND result IS NOT NULL)
        OR (status = 'REJECTED'
             AND reviewer IS NOT NULL AND reviewed_at IS NOT NULL
             AND decision_reason IS NOT NULL
             AND cert_no IS NULL AND result IS NULL)
    );

-- ----------------------------------------------------------------------------
-- 触发器 1：证书签发内容不可改写
-- 仅允许 status / revoked_reason / revoked_at 在撤销事务中变化；
-- 其余业务字段（编号、设备、时间、结论、引用关系等）一律拒绝 UPDATE。
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION certificates_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.cert_no        IS DISTINCT FROM OLD.cert_no
       OR NEW.kind           IS DISTINCT FROM OLD.kind
       OR NEW.device_id      IS DISTINCT FROM OLD.device_id
       OR NEW.device_name    IS DISTINCT FROM OLD.device_name
       OR NEW.calibrated_at  IS DISTINCT FROM OLD.calibrated_at
       OR NEW.valid_from     IS DISTINCT FROM OLD.valid_from
       OR NEW.valid_to       IS DISTINCT FROM OLD.valid_to
       OR NEW.conclusion     IS DISTINCT FROM OLD.conclusion
       OR NEW.parent_cert_no IS DISTINCT FROM OLD.parent_cert_no
       OR NEW.operator       IS DISTINCT FROM OLD.operator
       OR NEW.remark         IS DISTINCT FROM OLD.remark
       OR NEW.created_at     IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'certificate % is sealed: issued content cannot be modified', OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    -- 状态只允许 ACTIVE -> REVOKED / INVALID，且必须由撤销事务写入
    IF NEW.status NOT IN ('REVOKED', 'INVALID') OR OLD.status <> 'ACTIVE' THEN
        RAISE EXCEPTION 'illegal status transition % -> % on certificate %',
            OLD.status, NEW.status, OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_certificates_immutable
    BEFORE UPDATE ON certificates
    FOR EACH ROW EXECUTE FUNCTION certificates_immutable();

-- ----------------------------------------------------------------------------
-- 触发器 2：证书不得物理删除（历史签发内容始终可查）
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION certificates_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'certificates cannot be deleted; revoke instead (cert_no=%)', OLD.cert_no
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_certificates_no_delete
    BEFORE DELETE ON certificates
    FOR EACH ROW EXECUTE FUNCTION certificates_no_delete();

-- ----------------------------------------------------------------------------
-- 触发器 3：只增不改的业务凭据（更正声明、交付归档包）不得 UPDATE/DELETE
-- 声明历史不可篡改、顺序不可重排；交付快照一经创建永久冻结。
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION append_only_sealed() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only and sealed (trigger=%)', TG_TABLE_NAME, TG_NAME
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_corrections_no_update
    BEFORE UPDATE ON correction_declarations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

CREATE TRIGGER trg_corrections_no_delete
    BEFORE DELETE ON correction_declarations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

CREATE TRIGGER trg_delivery_archives_no_update
    BEFORE UPDATE ON delivery_archives
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

CREATE TRIGGER trg_delivery_archives_no_delete
    BEFORE DELETE ON delivery_archives
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

-- 交付签收邀请与定稿记录同样只增不改：邀请不可篡改/删除，
-- 定稿一经写入不可改写（后续撤销不改写签收记录与归档快照）。
CREATE TRIGGER trg_delivery_invitations_no_update
    BEFORE UPDATE ON delivery_invitations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

CREATE TRIGGER trg_delivery_invitations_no_delete
    BEFORE DELETE ON delivery_invitations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

CREATE TRIGGER trg_delivery_receipts_no_update
    BEFORE UPDATE ON delivery_receipts
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

CREATE TRIGGER trg_delivery_receipts_no_delete
    BEFORE DELETE ON delivery_receipts
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();

-- 停用记录仅允许“解除停用”这一次性 UPDATE：从生效中（lifted_at 为 NULL）
-- 变为已解除（lifted_at/lift_reason 写入），其余列一律不可改；DELETE 禁止。
CREATE OR REPLACE FUNCTION cert_suspensions_lift_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'cert_suspensions is append-only: %', OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.lifted_at IS NOT NULL
       OR NEW.lifted_at IS NULL
       OR NEW.lift_reason IS NULL
       OR NEW.suspension_id IS DISTINCT FROM OLD.suspension_id
       OR NEW.cert_no       IS DISTINCT FROM OLD.cert_no
       OR NEW.reason        IS DISTINCT FROM OLD.reason
       OR NEW.suspended_at  IS DISTINCT FROM OLD.suspended_at
    THEN
        RAISE EXCEPTION 'cert_suspensions rows can only be lifted once (suspension_id=%)',
            OLD.suspension_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cert_suspensions_lift_only
    BEFORE UPDATE OR DELETE ON cert_suspensions
    FOR EACH ROW EXECUTE FUNCTION cert_suspensions_lift_only();

-- 复核申请仅允许“PENDING -> APPROVED/REJECTED”这一次性定稿 UPDATE：
-- 申请内容（编号/签发资料/录入人/提交时刻）不可改，已驳回不得再批准、
-- 已批准不得改判；DELETE 禁止。
CREATE OR REPLACE FUNCTION cert_review_decide_once() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'certificate_review_requests is append-only: %', OLD.application_no
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status <> 'PENDING' OR NEW.status NOT IN ('APPROVED', 'REJECTED')
       OR NEW.application_no IS DISTINCT FROM OLD.application_no
       OR NEW.request_hash   IS DISTINCT FROM OLD.request_hash
       OR NEW.request        IS DISTINCT FROM OLD.request
       OR NEW.submitter      IS DISTINCT FROM OLD.submitter
       OR NEW.submitter_key_hash IS DISTINCT FROM OLD.submitter_key_hash
       OR NEW.submitted_at   IS DISTINCT FROM OLD.submitted_at
    THEN
        RAISE EXCEPTION 'review request % can only be decided once (% -> %)',
            OLD.application_no, OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cert_review_decide_once
    BEFORE UPDATE OR DELETE ON certificate_review_requests
    FOR EACH ROW EXECUTE FUNCTION cert_review_decide_once();

-- ----------------------------------------------------------------------------
-- 查询视图：统一将枚举以文本形式暴露给应用层
-- ----------------------------------------------------------------------------
CREATE VIEW v_certificates AS
SELECT cert_no,
       kind::text          AS kind,
       device_id,
       device_name,
       calibrated_at,
       valid_from,
       valid_to,
       conclusion::text    AS conclusion,
       parent_cert_no,
       operator,
       remark,
       status::text        AS status,
       revoked_reason,
       revoked_at,
       created_at
FROM certificates;
