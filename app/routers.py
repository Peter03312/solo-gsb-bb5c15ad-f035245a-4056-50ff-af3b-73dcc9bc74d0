"""HTTP API 路由。

约定：
- POST   /api/v1/reference-certificates        登记基准设备合格证书（追溯起点）
- POST   /api/v1/certificates                  签发普通校准证书
- POST   /api/v1/certificate-review-requests   录入人提交普通证书签发复核申请（X-Entry-Key，申请号幂等）
- POST   /api/v1/certificate-review-requests/{application_no}/decision  复核人驳回/批准（X-Reviewer-Key，批准同事务真正签发）
- GET    /api/v1/certificate-review-requests/{application_no}  按申请号查询状态与结果（录入/复核凭据）
- POST   /api/v1/certificate-batches           批量签发交接批次（整批原子、批次键幂等）
- GET    /api/v1/certificate-batches/{batch_key}  查询签发批次（首次签发结果快照）
- GET    /api/v1/certificates                  证书列表（可按设备/状态/类型过滤）
- GET    /api/v1/certificates/{cert_no}        证书详情（历史内容始终可查）
- GET    /api/v1/certificates/{cert_no}/chain  完整追溯引用链
- GET    /api/v1/certificates/{cert_no}/downstream  全部下游证书（影响面预览）
- POST   /api/v1/certificates/{cert_no}/revoke 撤销证书并级联失效下游
- POST   /api/v1/correction-declarations       登记更正声明（展示字段纠错，声明编号幂等）
- GET    /api/v1/correction-declarations/{declaration_no}  按声明编号查询
- GET    /api/v1/certificates/{cert_no}/corrections  证书更正视图（声明历史+合并展示）
- POST   /api/v1/certificates/{cert_no}/suspensions      登记标准证书临时停用（管理密钥，待复核）
- POST   /api/v1/certificates/{cert_no}/suspension-lift  复核后解除临时停用（管理密钥）
- GET    /api/v1/certificates/{cert_no}/suspensions      查询当前停用状态及历次停用/解除（管理密钥）
- POST   /api/v1/delivery-archives             创建客户交付归档包（冻结可交付凭据，编号幂等）
- GET    /api/v1/delivery-archives/{archive_no}    按归档包编号查询创建时刻快照
- POST   /api/v1/delivery-invitations          交付方创建签收邀请（管理密钥，返回仅展示一次的随机密钥）
- POST   /api/v1/delivery-receipts             收件人在截止前提交 ACCEPT/REJECT（每包一份定稿）
- GET    /api/v1/delivery-archives/{archive_no}/receipt  凭管理密钥查询定稿记录
- GET    /api/v1/devices/{device_id}/certificates  某设备全部证书（含历史）
- GET    /api/v1/devices/{device_id}/coverage-audit  设备校准覆盖审计（合规/空窗片段）
- GET    /api/v1/revocations                   撤销批次列表
- GET    /api/v1/revocations/{batch_id}        撤销批次详情（受影响证书及引用链）
- GET    /api/v1/revocations/{batch_id}/archive-impact  该批撤销对已交付归档凭据的影响面
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from psycopg_pool import ConnectionPool

from . import repository as repo
from .schemas import (
    BatchIssueRequest,
    CertificateIssueRequest,
    CertificateReviewDecisionRequest,
    CertificateReviewSubmitRequest,
    CertificateSuspensionRequest,
    CorrectionDeclarationRequest,
    DeliveryArchiveRequest,
    DeliveryInvitationRequest,
    DeliveryInvitationResponse,
    DeliveryReceiptRequest,
    DeliveryReceiptResponse,
    ReferenceIssueRequest,
    RevokeRequest,
)

router = APIRouter(prefix="/api/v1")


def pool_dep(request: Request) -> ConnectionPool:
    return request.app.state.pool


@router.post("/reference-certificates", status_code=201)
def issue_reference(req: ReferenceIssueRequest, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.issue_reference(conn, req)


@router.post("/certificates", status_code=201)
def issue_certificate(req: CertificateIssueRequest, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.issue_certificate(conn, req)


@router.post("/certificate-review-requests", status_code=201)
def submit_certificate_review(
    req: CertificateReviewSubmitRequest,
    response: Response,
    x_entry_key: str | None = Header(
        default=None,
        alias="X-Entry-Key",
        description="录入人本地配置的身份凭据（CERTIFICATE_ENTRY_KEYS）",
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """录入人提交需双人复核的普通证书签发申请：仅保存待审内容与完整签发资料，
    不占用证书编号或设备有效期。

    申请号为幂等键：同号同内容重试返回原申请（200），同号异内容
    409 review_request_conflict；录入凭据缺失/错误 401 invalid_identity_key。
    """
    with pool.connection() as conn:
        body, created = repo.submit_certificate_review(conn, req, x_entry_key)
    if not created:
        response.status_code = 200
    return body


@router.post("/certificate-review-requests/{application_no}/decision", status_code=201)
def decide_certificate_review(
    application_no: str,
    req: CertificateReviewDecisionRequest,
    response: Response,
    x_reviewer_key: str | None = Header(
        default=None,
        alias="X-Reviewer-Key",
        description="复核人本地配置的身份凭据（CERTIFICATE_REVIEWER_KEYS），须与录入凭据不同",
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """复核人驳回（附原因）或批准：批准与真正签发在同一事务内按批准当时的
    标准证书状态、临时停用与现有签发规则完成，结论/复核人/证书绑定。

    批准校验失败返回与直接签发一致的具体错误码，申请仍待审、不产生证书；
    驳回后不能再批准。同复核人同操作重试返回首次结果（200），其余再定稿
    409 review_already_decided；并发复核只允许一份定稿。
    """
    with pool.connection() as conn:
        body, created = repo.decide_certificate_review(
            conn, application_no, req, x_reviewer_key
        )
    if not created:
        response.status_code = 200
    return body


@router.get("/certificate-review-requests/{application_no}")
def get_certificate_review(
    application_no: str,
    x_entry_key: str | None = Header(default=None, alias="X-Entry-Key"),
    x_reviewer_key: str | None = Header(default=None, alias="X-Reviewer-Key"),
    pool: ConnectionPool = Depends(pool_dep),
):
    """按申请号查询状态与结果（录入人或复核人凭据均可）：PENDING 待审内容、
    APPROVED 签发证书与追溯链（首次结果快照）、REJECTED 驳回原因与复核人。"""
    with pool.connection() as conn:
        return repo.get_certificate_review(conn, application_no, x_entry_key or x_reviewer_key)


@router.post("/certificate-batches", status_code=201)
def issue_certificate_batch(
    req: BatchIssueRequest, response: Response, pool: ConnectionPool = Depends(pool_dep)
):
    """批量签发交接批次：整批原子落库；批次键幂等（重放返回 200 与首次相同的结果）。"""
    with pool.connection() as conn:
        body, created = repo.issue_batch(conn, req)
    if not created:
        response.status_code = 200
    return body


@router.get("/certificate-batches/{batch_key}")
def get_certificate_batch(batch_key: str, pool: ConnectionPool = Depends(pool_dep)):
    """按批次键查询首次签发结果（快照，不受事后撤销影响）。"""
    with pool.connection() as conn:
        return repo.get_issue_batch(conn, batch_key)


@router.get("/certificates")
def list_certificates(
    device_id: str | None = None,
    status: Literal["ACTIVE", "REVOKED", "INVALID"] | None = None,
    kind: Literal["REFERENCE", "CALIBRATION"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    with pool.connection() as conn:
        return repo.list_certificates(conn, device_id, status, kind, limit, offset)


@router.get("/certificates/{cert_no}")
def get_certificate(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_certificate(conn, cert_no)


@router.get("/certificates/{cert_no}/chain")
def get_chain(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_chain(conn, cert_no)


@router.get("/certificates/{cert_no}/downstream")
def get_downstream(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_downstream(conn, cert_no)


@router.post("/certificates/{cert_no}/revoke", status_code=200)
def revoke_certificate(
    cert_no: str, req: RevokeRequest, pool: ConnectionPool = Depends(pool_dep)
):
    with pool.connection() as conn:
        return repo.revoke_certificate(conn, cert_no, req.reason, req.operator)


@router.post("/correction-declarations", status_code=201)
def register_correction(
    req: CorrectionDeclarationRequest, response: Response, pool: ConnectionPool = Depends(pool_dep)
):
    """登记更正声明：仅更正展示字段；声明编号幂等（同内容重放返回 200 与首次声明）。"""
    with pool.connection() as conn:
        body, created = repo.register_correction(conn, req)
    if not created:
        response.status_code = 200
    return body


@router.get("/correction-declarations/{declaration_no}")
def get_correction_declaration(declaration_no: str, pool: ConnectionPool = Depends(pool_dep)):
    """按声明编号查询：首次登记的声明 + 所属证书的当前更正视图。"""
    with pool.connection() as conn:
        return repo.get_correction_declaration(conn, declaration_no)


@router.get("/certificates/{cert_no}/corrections")
def get_certificate_corrections(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    """证书的更正视图：原证书、声明历史（按登记顺序）与合并后的当前展示。"""
    with pool.connection() as conn:
        return repo.get_certificate_corrections(conn, cert_no)


@router.post("/certificates/{cert_no}/suspensions", status_code=201)
def suspend_certificate(
    cert_no: str,
    req: CertificateSuspensionRequest,
    x_management_key: str | None = Header(
        default=None,
        alias="X-Management-Key",
        description="本地配置的管理密钥（与交付管理共用）",
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """登记标准证书临时停用（待复核）：只接受生效证书，原因非空，停用从登记
    成功时刻起生效；重复停用 409 suspension_conflict。停用期间该证书及其
    任何下游证书不得作为新签发普通证书的追溯依据（单张/批次均校验）。
    """
    with pool.connection() as conn:
        return repo.suspend_certificate(conn, cert_no, req.reason, x_management_key)


@router.post("/certificates/{cert_no}/suspension-lift", status_code=200)
def lift_certificate_suspension(
    cert_no: str,
    req: CertificateSuspensionRequest,
    x_management_key: str | None = Header(
        default=None,
        alias="X-Management-Key",
        description="本地配置的管理密钥（与交付管理共用）",
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """复核后凭管理密钥与非空原因解除临时停用；无生效停用时解除
    409 suspension_not_active。解除后该链可重新作为追溯依据。"""
    with pool.connection() as conn:
        return repo.lift_certificate(conn, cert_no, req.reason, x_management_key)


@router.get("/certificates/{cert_no}/suspensions")
def get_certificate_suspension(
    cert_no: str,
    x_management_key: str | None = Header(
        default=None,
        alias="X-Management-Key",
        description="本地配置的管理密钥（与交付管理共用）",
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """查询当前停用状态及历次停用、解除的时间和原因（须凭管理密钥）。"""
    with pool.connection() as conn:
        return repo.get_certificate_suspension(conn, cert_no, x_management_key)


@router.post("/delivery-archives", status_code=201)
def create_delivery_archive(
    req: DeliveryArchiveRequest, response: Response, pool: ConnectionPool = Depends(pool_dep)
):
    """创建客户交付归档包：单事务冻结创建时刻的原始签发内容、追溯链与合并展示。

    编号+有序列表均相同的重试返回首次快照（200）；同编号不同列表 409。
    """
    with pool.connection() as conn:
        body, created = repo.create_delivery_archive(conn, req)
    if not created:
        response.status_code = 200
    return body


@router.get("/delivery-archives/{archive_no}")
def get_delivery_archive(archive_no: str, pool: ConnectionPool = Depends(pool_dep)):
    """按归档包编号查询创建时刻快照；未知编号 404 archive_not_found。"""
    with pool.connection() as conn:
        return repo.get_delivery_archive(conn, archive_no)


@router.post("/delivery-invitations", status_code=201, response_model=DeliveryInvitationResponse)
def create_delivery_invitation(
    req: DeliveryInvitationRequest,
    response: Response,
    x_management_key: str | None = Header(
        default=None,
        alias="X-Management-Key",
        description="交付方本地配置的管理密钥",
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """交付方创建签收邀请：按归档包编号、收件人与未来截止时间生成邀请编号与
    一次性随机密钥。密钥仅在本次响应明文展示一次（库内只存 SHA-256）。

    归档包不存在 404 archive_not_found、管理密钥错误 401 invalid_management_key、
    截止时间非未来值 422 invitation_deadline_past。
    """
    with pool.connection() as conn:
        body, _ = repo.create_delivery_invitation(conn, req, x_management_key)
    return body


@router.post("/delivery-receipts", status_code=201, response_model=DeliveryReceiptResponse)
def submit_delivery_receipt(
    req: DeliveryReceiptRequest,
    response: Response,
    pool: ConnectionPool = Depends(pool_dep),
):
    """收件人在截止前提交 ACCEPT/REJECT 定稿；REJECT 须填写原因。

    每包一份定稿：同一邀请同内容重试（截止后亦然）返回首次结果（200）；
    不同内容或其他邀请再提交 409 receipt_conflict。ACCEPT 时若包内目标或
    冻结追溯链中任一证书已撤销/失效，422 receipt_targets_revoked 并点名
    受影响目标；REJECT 不受此限。未定稿时过期 409 invitation_expired、
    密钥错误 401 invalid_receipt_key、邀请未知 404 invitation_not_found。
    """
    with pool.connection() as conn:
        body, created = repo.submit_delivery_receipt(conn, req)
    if not created:
        response.status_code = 200
    return body


@router.get("/delivery-archives/{archive_no}/receipt", response_model=DeliveryReceiptResponse)
def get_delivery_receipt(
    archive_no: str,
    x_management_key: str | None = Header(
        default=None,
        alias="X-Management-Key",
        description="交付方本地配置的管理密钥",
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """凭管理密钥按归档包编号查询定稿记录（归档号、收件人、结论、原因、签收时刻）。

    管理密钥错误 401；归档包不存在 404 archive_not_found；
    尚未定稿 404 receipt_not_found。
    """
    with pool.connection() as conn:
        return repo.get_delivery_receipt(conn, archive_no, x_management_key)


@router.get("/devices/{device_id}/certificates")
def device_history(device_id: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_device_history(conn, device_id)


@router.get("/devices/{device_id}/coverage-audit")
def device_coverage_audit(
    device_id: str,
    from_: datetime = Query(
        ..., alias="from", description="审计起点（含），带时区 ISO 8601"
    ),
    to: datetime = Query(
        ..., description="审计终点（不含），带时区 ISO 8601；不得晚于查询时刻"
    ),
    pool: ConnectionPool = Depends(pool_dep),
):
    """审计设备在过去半开区间 [from, to) 内的校准覆盖：按时间顺序返回完整、
    无重叠的合规（COMPLIANT）与空窗（GAP）片段；未知设备返回整段空窗。"""
    with pool.connection() as conn:
        return repo.get_device_coverage_audit(conn, device_id, from_, to)


@router.get("/revocations")
def list_revocations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    with pool.connection() as conn:
        return repo.list_revocations(conn, limit, offset)


@router.get("/revocations/{batch_id}")
def get_revocation(batch_id: int, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_revocation(conn, batch_id)


@router.get("/revocations/{batch_id}/archive-impact")
def get_revocation_archive_impact(
    batch_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    """按撤销批次追查已交付归档凭据的影响面：冻结追溯链命中本批被撤销证书
    （含级联失效）的归档包及包内目标证书，按 (归档包编号, 原交付序位) 分页。"""
    with pool.connection() as conn:
        return repo.get_revocation_archive_impact(conn, batch_id, limit, offset)
