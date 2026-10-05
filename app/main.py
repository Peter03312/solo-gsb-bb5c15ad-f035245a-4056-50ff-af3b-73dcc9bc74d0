"""FastAPI 应用入口。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import close_pool, ensure_schema, init_pool
from .errors import ApiError
from .routers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = init_pool()
    ensure_schema(app.state.pool)
    yield
    close_pool()


app = FastAPI(
    title="计量校准证书追溯服务",
    description=(
        "登记基准设备合格证书作为追溯起点，签发/撤销普通校准证书，"
        "校验引用有效性、防止成环与有效期重叠，撤销时级联失效下游证书；"
        "支持离线采集批次的整批原子签发与批次键幂等重放；"
        "支持对已签发证书的展示字段（设备名称/操作员/备注）登记更正声明，"
        "声明按登记顺序生效且不改写原证书；"
        "支持客户交付归档包：以归档包编号与非空证书编号列表冻结创建时刻的"
        "原始签发内容、追溯链与按更正声明合并的展示字段，编号幂等、可按编号查快照；"
        "支持交付签收：交付方凭本地管理密钥按归档包/收件人/未来截止时间创建签收邀请，"
        "返回邀请编号与仅展示一次的随机密钥（明文不落库）；收件人截止前提交 ACCEPT/REJECT，"
        "每包一份定稿、同邀请同内容重试返回首次结果，ACCEPT 时目标或冻结链证书已撤销/"
        "失效则拒绝并点名，定稿可凭管理密钥查询；"
        "支持标准证书临时停用：计量人员凭管理密钥对待复核的生效标准证书登记停用"
        "（非空原因，登记成功时刻起生效），停用期间该证书及任何下游证书不得作为新签发"
        "普通证书的追溯依据（单张与交接批次、批内引用均校验，命中整批不落库），"
        "复核后凭管理密钥解除停用，停用/解除与并发签发按提交次序生效；"
        "可查询当前停用状态与历次停用/解除的时间和原因，停用不改写证书内容、"
        "撤销状态与已冻结交付快照；"
        "支持普通证书双人复核签发：录入人凭本地录入凭据提交申请号与完整签发资料"
        "（仅保存待审内容，不占证书编号或设备有效期），另一身份的复核人凭独立凭据"
        "附原因驳回，或批准并在同一事务按批准当时的规则真正签发并绑定结论/复核人/证书；"
        "申请号幂等、驳回后不可再批准、并发复核只允许一份定稿，可按申请号查询状态与结果；"
        "支持按撤销批次追查已交付归档凭据的影响面（冻结链命中本批被撤销证书的"
        "归档包与包内目标证书，分页输出）；"
        "支持按设备审计过去半开区间内的校准覆盖：按时间顺序返回完整、无重叠的"
        "合规/空窗片段，合规片段列出当时可用的合格普通证书编号。"
    ),
    version="1.8.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logging.getLogger("caltrace").exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "服务内部错误", "details": {}}},
    )


@app.get("/health", tags=["meta"])
def health(request: Request):
    with request.app.state.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def index():
    return {
        "service": "calibration-certificate-traceability",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "api_base": "/api/v1",
        "endpoints": [
            "POST /api/v1/reference-certificates",
            "POST /api/v1/certificates",
            "POST /api/v1/certificate-review-requests",
            "POST /api/v1/certificate-review-requests/{application_no}/decision",
            "GET  /api/v1/certificate-review-requests/{application_no}",
            "POST /api/v1/certificate-batches",
            "GET  /api/v1/certificate-batches/{batch_key}",
            "GET  /api/v1/certificates",
            "GET  /api/v1/certificates/{cert_no}",
            "GET  /api/v1/certificates/{cert_no}/chain",
            "GET  /api/v1/certificates/{cert_no}/downstream",
            "POST /api/v1/certificates/{cert_no}/revoke",
            "POST /api/v1/correction-declarations",
            "GET  /api/v1/correction-declarations/{declaration_no}",
            "GET  /api/v1/certificates/{cert_no}/corrections",
            "POST /api/v1/certificates/{cert_no}/suspensions",
            "POST /api/v1/certificates/{cert_no}/suspension-lift",
            "GET  /api/v1/certificates/{cert_no}/suspensions",
            "POST /api/v1/delivery-archives",
            "GET  /api/v1/delivery-archives/{archive_no}",
            "POST /api/v1/delivery-invitations",
            "POST /api/v1/delivery-receipts",
            "GET  /api/v1/delivery-archives/{archive_no}/receipt",
            "GET  /api/v1/devices/{device_id}/certificates",
            "GET  /api/v1/devices/{device_id}/coverage-audit",
            "GET  /api/v1/revocations",
            "GET  /api/v1/revocations/{batch_id}",
            "GET  /api/v1/revocations/{batch_id}/archive-impact",
        ],
    }


app.include_router(router)
