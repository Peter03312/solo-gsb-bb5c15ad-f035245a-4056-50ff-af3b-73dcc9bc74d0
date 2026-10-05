"""PostgreSQL 连接池管理（psycopg3）。"""
import logging
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

logger = logging.getLogger("caltrace")

_pool: ConnectionPool | None = None

# 幂等结构补丁：为沿用旧数据卷的部署补充后续版本新增的表。
# 与 db/init.sql 中同名对象定义保持一致（全新数据卷由 init.sql 建表，此处为 no-op；
# 触发器用 CREATE OR REPLACE 保证重复执行幂等，要求 PostgreSQL 14+）。
_MIGRATION_SQL = (
    """
CREATE TABLE IF NOT EXISTS issue_batches (
    batch_key    TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request      JSONB NOT NULL,
    response     JSONB NOT NULL,
    cert_nos     TEXT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    """
CREATE TABLE IF NOT EXISTS delivery_archives (
    archive_no   TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request      JSONB NOT NULL,
    response     JSONB NOT NULL,
    cert_nos     TEXT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    """
CREATE TABLE IF NOT EXISTS correction_declarations (
    declaration_no TEXT PRIMARY KEY,
    cert_no        TEXT NOT NULL REFERENCES certificates(cert_no),
    seq            INT NOT NULL,
    corrections    JSONB NOT NULL,
    reason         TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cert_no, seq)
);
""",
    """
CREATE TABLE IF NOT EXISTS delivery_invitations (
    invitation_no TEXT PRIMARY KEY,
    archive_no    TEXT NOT NULL REFERENCES delivery_archives(archive_no),
    recipient     TEXT NOT NULL,
    deadline      TIMESTAMPTZ NOT NULL,
    key_hash      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_delivery_invitations_archive
    ON delivery_invitations(archive_no);
""",
    """
CREATE TABLE IF NOT EXISTS delivery_receipts (
    archive_no    TEXT PRIMARY KEY REFERENCES delivery_archives(archive_no),
    invitation_no TEXT NOT NULL REFERENCES delivery_invitations(invitation_no),
    recipient     TEXT NOT NULL,
    decision      TEXT NOT NULL CHECK (decision IN ('ACCEPT', 'REJECT')),
    reason        TEXT,
    request_hash  TEXT NOT NULL,
    signed_at     TIMESTAMPTZ NOT NULL
);
""",
    """
CREATE TABLE IF NOT EXISTS cert_suspensions (
    suspension_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cert_no       TEXT NOT NULL REFERENCES certificates(cert_no),
    reason        TEXT NOT NULL,
    suspended_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lifted_at     TIMESTAMPTZ,
    lift_reason   TEXT,
    CONSTRAINT cert_suspension_lift_metadata
        CHECK (lifted_at IS NULL OR
               (lift_reason IS NOT NULL AND lifted_at >= suspended_at))
);
CREATE INDEX IF NOT EXISTS idx_cert_suspensions_cert
    ON cert_suspensions(cert_no, suspension_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cert_suspensions_one_active
    ON cert_suspensions(cert_no) WHERE (lifted_at IS NULL);
""",
    """
CREATE TABLE IF NOT EXISTS certificate_review_requests (
    application_no      TEXT PRIMARY KEY,
    request_hash        TEXT NOT NULL,
    request             JSONB NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    submitter           TEXT NOT NULL,
    submitter_key_hash  TEXT NOT NULL,
    submitted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewer            TEXT,
    decision_reason     TEXT,
    reviewed_at         TIMESTAMPTZ,
    cert_no             TEXT REFERENCES certificates(cert_no),
    result              JSONB
);
ALTER TABLE certificate_review_requests DROP CONSTRAINT IF EXISTS cert_review_decision_metadata;
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
""",
    """
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
""",
    """
CREATE OR REPLACE TRIGGER trg_cert_review_decide_once
    BEFORE UPDATE OR DELETE ON certificate_review_requests
    FOR EACH ROW EXECUTE FUNCTION cert_review_decide_once();
""",
    """
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
""",
    """
CREATE OR REPLACE TRIGGER trg_cert_suspensions_lift_only
    BEFORE UPDATE OR DELETE ON cert_suspensions
    FOR EACH ROW EXECUTE FUNCTION cert_suspensions_lift_only();
""",
    """
CREATE OR REPLACE FUNCTION append_only_sealed() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only and sealed (trigger=%)', TG_TABLE_NAME, TG_NAME
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
""",
    """
CREATE OR REPLACE TRIGGER trg_corrections_no_update
    BEFORE UPDATE ON correction_declarations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_corrections_no_delete
    BEFORE DELETE ON correction_declarations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_archives_no_update
    BEFORE UPDATE ON delivery_archives
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_archives_no_delete
    BEFORE DELETE ON delivery_archives
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_invitations_no_update
    BEFORE UPDATE ON delivery_invitations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_invitations_no_delete
    BEFORE DELETE ON delivery_invitations
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_receipts_no_update
    BEFORE UPDATE ON delivery_receipts
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
    """
CREATE OR REPLACE TRIGGER trg_delivery_receipts_no_delete
    BEFORE DELETE ON delivery_receipts
    FOR EACH ROW EXECUTE FUNCTION append_only_sealed();
""",
)


def _build_dsn() -> str:
    return (
        f"host={settings.db_host} port={settings.db_port} "
        f"dbname={settings.db_name} user={settings.db_user} "
        f"password={settings.db_password}"
    )


def init_pool() -> ConnectionPool:
    """创建连接池，并在数据库尚未就绪时重试等待。"""
    global _pool
    if _pool is not None:
        return _pool

    deadline = time.monotonic() + settings.db_connect_timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool = ConnectionPool(
                _build_dsn(),
                min_size=settings.db_min_size,
                max_size=settings.db_max_size,
                kwargs={"row_factory": dict_row},
                open=True,
                timeout=10,
            )
            # 确认连接真正可用
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _pool = pool
            logger.info("database connection pool ready")
            return pool
        except Exception as exc:  # 数据库容器可能仍在初始化
            last_err = exc
            logger.warning("waiting for database: %s", exc)
            time.sleep(2)
    raise RuntimeError(f"database not ready after {settings.db_connect_timeout}s: {last_err}")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_schema(pool: ConnectionPool) -> None:
    """启动时执行幂等结构补丁（不改动既有表与数据）。"""
    with pool.connection() as conn:
        with conn.transaction():
            for stmt in _MIGRATION_SQL:
                conn.execute(stmt)
    logger.info("schema migration ensured")


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool not initialized")
    return _pool
