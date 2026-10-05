"""Pydantic 请求与响应模型。

时间一律使用带时区的 ISO 8601（如 2026-03-01T09:00:00+08:00），
不传时区时按 UTC 处理。
有效期统一按半开区间 [valid_from, valid_to) 解释，首尾相接不算重叠。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CertKind = Literal["REFERENCE", "CALIBRATION"]
CertStatus = Literal["ACTIVE", "REVOKED", "INVALID"]
CertConclusion = Literal["PASS", "FAIL"]


class _IssueBase(BaseModel):
    cert_no: str = Field(..., min_length=1, max_length=128, description="证书编号，全局唯一")
    device_id: str = Field(..., min_length=1, max_length=128, description="被校/基准设备标识")
    device_name: str | None = Field(None, max_length=256, description="设备名称")
    calibrated_at: datetime | None = Field(
        None, description="校准时刻；缺省取 valid_from"
    )
    valid_from: datetime = Field(..., description="有效期开始")
    valid_to: datetime = Field(..., description="有效期结束（不含该时刻）")
    operator: str | None = Field(None, max_length=128)
    remark: str | None = Field(None, max_length=1024)

    @model_validator(mode="after")
    def _check_window(self):
        if self.valid_from >= self.valid_to:
            raise ValueError("valid_from 必须早于 valid_to")
        if self.calibrated_at is None:
            self.calibrated_at = self.valid_from
        if not (self.valid_from <= self.calibrated_at < self.valid_to):
            raise ValueError("calibrated_at 必须落在 [valid_from, valid_to) 内")
        return self


class ReferenceIssueRequest(_IssueBase):
    """登记基准（标准）设备合格证书——追溯起点，结论恒为合格。"""


class CertificateIssueRequest(_IssueBase):
    """签发普通校准证书。"""
    conclusion: CertConclusion = Field(..., description="校准结论 PASS/FAIL")
    parent_cert_no: str = Field(..., min_length=1, description="引用的标准设备证书编号")


class CertificateReviewSubmitRequest(CertificateIssueRequest):
    """录入人提交“待双人复核”的普通证书签发申请。

    申请只保存待审内容与完整签发资料，不占用证书编号或设备有效期；
    application_no 为幂等键：同号同内容重试返回原申请（200），
    同号异内容返回 409 review_request_conflict。
    录入人凭据经 X-Entry-Key 请求头校验（不在 body 中）。
    """
    application_no: str = Field(
        ..., min_length=1, max_length=128,
        description="申请号（幂等键，业务唯一）：同号同内容重试返回原申请",
    )


class CertificateReviewDecisionRequest(BaseModel):
    """复核人对申请的定稿操作：APPROVE 批准 / REJECT 驳回。

    REJECT 必须填写非空白原因；APPROVE 不得携带原因。批准与真正签发在同一
    事务中按批准当时的标准证书状态、临时停用与现有签发规则完成；校验失败
    时申请仍为 PENDING、不产生证书。驳回后不能再批准。同申请同操作（同
    复核人同结论同原因）重试返回首次结果（200），其余再定稿一律 409。
    复核人凭据经 X-Reviewer-Key 请求头校验，且不得与录入凭据相同。
    申请号取自 URL 路径，不放在 body 中。
    """
    decision: Literal["APPROVE", "REJECT"] = Field(
        ..., description="APPROVE=批准并在同事务真正签发；REJECT=附原因驳回"
    )
    reason: str | None = Field(
        None, max_length=1024,
        description="驳回原因：REJECT 必填且非空；APPROVE 不得填写",
    )


class RevokeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1024, description="撤销原因")
    operator: str | None = Field(None, max_length=128)


class CorrectionDeclarationRequest(BaseModel):
    """登记更正声明：仅可更正 device_name / operator / remark 三个展示字段。

    corrections 为本次提交的修正字段集合（非空）：字段值为 null 表示显式置空，
    未提交的字段保持原值——两者严格区分。declaration_no 为幂等键：同编号同内容
    重试返回首次声明，同编号不同内容报冲突。
    """
    declaration_no: str = Field(
        ..., min_length=1, max_length=128,
        description="声明编号（全局唯一，幂等键）",
    )
    cert_no: str = Field(..., min_length=1, max_length=128, description="被更正的证书编号")
    corrections: dict[str, str | None] = Field(
        ...,
        description="修正字段集合（非空）：仅 device_name/operator/remark；"
        "值为 null 表示显式置空，未提交的字段保持不变",
    )
    reason: str = Field(..., max_length=1024, description="更正原因（非空）")


class BatchIssueItem(_IssueBase):
    """批次中的单张证书，沿用单张签发的字段与规则。

    kind=REFERENCE   基准设备合格证书（追溯起点）：不得填写 parent_cert_no，
                     结论恒为 PASS（conclusion 可省略或显式传 "PASS"）。
    kind=CALIBRATION 普通校准证书：parent_cert_no 与 conclusion 必填；
                     parent_cert_no 可引用库内已有证书或本批证书。
    """
    kind: CertKind = Field(..., description="REFERENCE=基准证书；CALIBRATION=普通校准证书")
    conclusion: CertConclusion | None = Field(
        None, description="校准结论；基准证书恒为 PASS，普通证书必填"
    )
    parent_cert_no: str | None = Field(
        None, description="引用的上游证书编号（库内或本批）；普通证书必填"
    )

    @model_validator(mode="after")
    def _check_kind_fields(self):
        if self.kind == "REFERENCE":
            if self.parent_cert_no is not None:
                raise ValueError("基准证书(REFERENCE)不得填写 parent_cert_no")
            if self.conclusion not in (None, "PASS"):
                raise ValueError("基准证书结论恒为合格(PASS)，不得指定其他结论")
        else:
            if not self.parent_cert_no:
                raise ValueError("普通校准证书(CALIBRATION)必须填写 parent_cert_no")
            if self.conclusion is None:
                raise ValueError("普通校准证书(CALIBRATION)必须填写 conclusion")
        return self


class BatchIssueRequest(BaseModel):
    """离线采集交接批次：唯一批次键 + 非空证书列表（列表顺序不代表引用顺序）。"""
    batch_key: str = Field(
        ..., min_length=1, max_length=128,
        description="批次键（幂等键）：同键同请求重放首次签发结果，同键不同请求报冲突",
    )
    certificates: list[BatchIssueItem] = Field(
        ..., min_length=1, max_length=500,
        description="本批证书列表（非空）；列表顺序不代表引用顺序",
    )


class CertificateSuspensionRequest(BaseModel):
    """登记/解除标准证书临时停用：管理密钥经 X-Management-Key 头校验（非 body）。

    登记停用：证书必须存在且当前为生效（ACTIVE）证书，原因非空白；
    同一张证书重复停用（已有生效中的停用记录）报 409 suspension_conflict。
    解除停用：原因非空白；没有生效中的停用记录时报 409 suspension_not_active。
    停用从登记成功（事务提交）时刻起生效，不改变证书自身状态与内容。
    """
    cert_no: str = Field(..., min_length=1, max_length=128, description="被停用/解除停用的标准证书编号")
    reason: str = Field(..., max_length=1024, description="停用原因（登记停用）或复核解除原因（解除停用），均须非空")


class DeliveryArchiveRequest(BaseModel):
    """客户交付归档：归档包编号 + 非空目标证书编号列表（顺序即交付顺序）。

    仅接受编号互不重复、目标均为 CALIBRATION，且目标及各自完整上游链均
    ACTIVE、结论 PASS 的组合；任一证书不存在或不合格，整包不保存。
    archive_no 为幂等键：同编号同有序列表重试返回首次快照，同编号不同列表
    报冲突（列表顺序不同即视为不同列表）。
    """
    archive_no: str = Field(
        ..., min_length=1, max_length=128,
        description="归档包编号（幂等键）：同编号同列表重放首次快照，同编号不同列表报冲突",
    )
    cert_nos: list[str] = Field(
        ..., min_length=1, max_length=500,
        description="目标证书编号列表（非空，按交付顺序；编号必须互不重复）",
    )


class DeliveryInvitationRequest(BaseModel):
    """创建交付签收邀请：归档包编号 + 收件人 + 未来截止时间。

    交付方须在 X-Management-Key 头中携带本地配置的管理密钥。服务端生成
    邀请编号与一次性随机密钥；随机密钥仅在本次响应中明文出现一次，
    库内仅保存其 SHA-256 指纹。归档包不存在、管理密钥错误或截止时间
    非未来值时拒绝创建。
    """
    archive_no: str = Field(..., min_length=1, max_length=128, description="已创建的归档包编号")
    recipient: str = Field(
        ..., min_length=1, max_length=256,
        description="收件人标识（交付对象，如客户单位/联系人）",
    )
    deadline: datetime = Field(..., description="签收截止时刻（必须晚于当前时刻，带时区 ISO 8601）")


class DeliveryReceiptRequest(BaseModel):
    """收件人提交签收结论：邀请编号 + 创建邀请时一次性展示的随机密钥 + 结论。

    必须在对应邀请的截止时刻之前提交；REJECT 必须填写非空 reason，
    ACCEPT 不得携带原因。每包只允许一份定稿：同一邀请同内容重试返回
    首次结果（截止后亦然），不同内容或其他邀请再提交均报冲突。
    """
    invitation_no: str = Field(..., min_length=1, max_length=128, description="邀请编号")
    key: str = Field(..., min_length=1, max_length=256, description="创建邀请时仅展示一次的随机密钥")
    decision: Literal["ACCEPT", "REJECT"] = Field(..., description="签收结论：ACCEPT 接收 / REJECT 拒收")
    reason: str | None = Field(
        None, max_length=1024,
        description="拒收原因：REJECT 必填且非空；ACCEPT 不得填写",
    )


class CertificateOut(BaseModel):
    cert_no: str
    kind: CertKind
    device_id: str
    device_name: str | None = None
    calibrated_at: datetime
    valid_from: datetime
    valid_to: datetime
    conclusion: CertConclusion
    parent_cert_no: str | None = None
    operator: str | None = None
    remark: str | None = None
    status: CertStatus
    revoked_reason: str | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class ChainNode(BaseModel):
    depth: int
    certificate: CertificateOut


class TraceChainOut(BaseModel):
    cert_no: str
    nodes: list[ChainNode] = Field(..., description="从追溯起点(基准)到目标证书的引用链")
    root_cert_no: str | None = None
    length: int


class IssueResponse(BaseModel):
    certificate: CertificateOut
    chain: TraceChainOut


class CertificateReviewDecisionOut(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reviewer: str = Field(..., description="复核人身份名（本地凭据配置名）")
    reason: str | None = Field(None, description="驳回原因（仅 REJECT）")
    reviewed_at: datetime


class CertificateReviewApplicationOut(BaseModel):
    """复核申请视图：待审签发资料、状态与定稿结果。

    - PENDING：仅保存待审内容，不占用证书编号/设备有效期，review/certificate/chain 为空；
    - APPROVED：certificate/chain 为批准当时真正签发的结果（与直接签发同构）；
    - REJECTED：review.reason 给出驳回原因，不产生证书。
    """
    application_no: str
    status: Literal["PENDING", "APPROVED", "REJECTED"]
    request: dict = Field(..., description="完整签发资料（普通证书字段）")
    submitter: str = Field(..., description="录入人身份名（本地凭据配置名）")
    submitted_at: datetime
    review: CertificateReviewDecisionOut | None = Field(
        None, description="复核定稿信息（结论、复核人、原因与时刻）；待审时为 null"
    )
    certificate: CertificateOut | None = Field(
        None, description="批准后真正签发的证书（与直接签发响应中的 certificate 同构）"
    )
    chain: TraceChainOut | None = Field(
        None, description="批准后签发证书的完整追溯链（与直接签发响应中的 chain 同构）"
    )


class BatchIssueItemOut(BaseModel):
    certificate: CertificateOut
    chain: TraceChainOut


class BatchIssueResponse(BaseModel):
    batch_key: str
    count: int
    issued_at: datetime
    items: list[BatchIssueItemOut] = Field(
        ..., description="各证书及其从基准到自身的追溯链，按请求列表顺序返回"
    )


class InvalidatedItem(BaseModel):
    cert_no: str
    chain: list[CertificateOut]
    depth: int = Field(..., description="相对于被撤销证书的下游层数，1 为直接引用者")


class RevocationOut(BaseModel):
    batch_id: int
    revoked: CertificateOut
    reason: str
    operator: str | None = None
    revoked_at: datetime
    affected_count: int
    affected: list[InvalidatedItem]


class DownstreamItem(BaseModel):
    cert_no: str
    depth: int
    status: CertStatus


class CorrectionDeclarationOut(BaseModel):
    declaration_no: str
    cert_no: str
    seq: int = Field(..., description="该证书内按成功登记顺序分配的序号，从 1 开始")
    corrections: dict[str, str | None] = Field(
        ..., description="本次提交的修正字段集合；null 表示显式置空"
    )
    reason: str
    created_at: datetime


class CorrectionDisplay(BaseModel):
    """按登记顺序合并全部声明后的当前展示值（仅三个可更正字段）。"""
    device_name: str | None = None
    operator: str | None = None
    remark: str | None = None


class CertificateCorrectionsOut(BaseModel):
    """证书的更正视图：原证书 + 声明历史 + 合并后的当前展示。"""
    cert_no: str
    certificate: CertificateOut = Field(..., description="原证书（签发内容，不受更正影响）")
    declarations: list[CorrectionDeclarationOut] = Field(
        ..., description="声明历史，按登记顺序（seq）升序"
    )
    display: CorrectionDisplay = Field(..., description="合并后的当前展示")


class CorrectionDeclarationResponse(BaseModel):
    """登记结果 / 按声明编号查询：首次声明 + 所属证书的当前更正视图。"""
    declaration: CorrectionDeclarationOut
    certificate: CertificateOut = Field(..., description="原证书（签发内容，不受更正影响）")
    declarations: list[CorrectionDeclarationOut] = Field(
        ..., description="该证书的全部声明历史，按登记顺序（seq）升序"
    )
    display: CorrectionDisplay = Field(..., description="合并后的当前展示")


class DownstreamOut(BaseModel):
    cert_no: str
    descendants: list[DownstreamItem]


class PagedCertificates(BaseModel):
    items: list[CertificateOut]
    total: int
    limit: int
    offset: int


class DeliveryArchiveItemOut(BaseModel):
    """单张目标证书的交付凭据：原始签发内容 + 从基准到目标的链 + 合并展示字段。"""
    cert_no: str
    certificate: CertificateOut = Field(..., description="原始签发内容（不受更正影响）")
    chain: TraceChainOut = Field(..., description="从追溯起点(基准)到目标证书的引用链")
    display: CorrectionDisplay = Field(
        ..., description="按归档时刻已登记更正声明合并后的展示字段"
    )


class DeliveryArchiveResponse(BaseModel):
    archive_no: str
    count: int
    created_at: datetime = Field(..., description="归档包创建时刻（快照冻结时点）")
    items: list[DeliveryArchiveItemOut] = Field(
        ..., description="各目标证书的交付凭据，按提交列表顺序返回"
    )


class DeliveryInvitationResponse(BaseModel):
    invitation_no: str
    archive_no: str
    recipient: str
    deadline: datetime
    created_at: datetime
    key: str = Field(
        ...,
        description="一次性随机密钥（仅本次响应明文展示；库内仅存其 SHA-256，无法再次取回）",
    )


class DeliveryReceiptResponse(BaseModel):
    """签收录制定稿记录：可凭管理密钥按归档包编号查询。"""
    archive_no: str
    invitation_no: str
    recipient: str
    decision: Literal["ACCEPT", "REJECT"]
    reason: str | None = None
    signed_at: datetime


class SuspensionEventOut(BaseModel):
    """一次停用/解除事件（停用登记或复核解除，按登记时间升序）。"""
    suspension_id: int
    action: Literal["SUSPEND", "LIFT"] = Field(
        ..., description="SUSPEND=登记停用；LIFT=复核后解除停用"
    )
    reason: str = Field(..., description="停用原因或解除原因（各自非空）")
    at: datetime = Field(..., description="停用登记时刻（停用从该时刻起生效）或解除时刻")
    lifted_at: datetime | None = Field(
        None, description="停用记录上的解除时刻（action=LIFT 时等于 at；SUSPEND 时为空）"
    )
    lift_reason: str | None = Field(None, description="停用记录上的解除原因（action=SUSPEND 时为空）")


class CertificateSuspensionOut(BaseModel):
    """证书停用状态视图：当前状态 + 历次停用/解除的时间与原因。"""
    cert_no: str
    certificate: CertificateOut = Field(
        ..., description="证书当前内容（停用不改写：状态仍为其自身 ACTIVE/REVOKED/INVALID）"
    )
    currently_suspended: bool = Field(
        ..., description="是否存在生效中的停用记录（与证书自身状态相互独立）"
    )
    active: SuspensionEventOut | None = Field(
        None, description="当前生效中的停用事件；无生效停用时为 null"
    )
    history: list[SuspensionEventOut] = Field(
        ..., description="历次停用/解除事件，按登记时间（suspension_id）升序；每次停用随后可能有一次解除"
    )


class ErrorBody(BaseModel):
    error: dict
