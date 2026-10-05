"""应用配置：全部通过环境变量注入，方便容器化与离线部署。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_identity_keys(raw: str) -> dict[str, str]:
    """解析“身份凭据表”：逗号分隔，每项形如 ``身份名=凭据``。

    - 凭据仅保存在本地配置中，不入库、不下发；响应中只回显“身份名”；
    - 省略身份名（纯凭据）时按顺序补默认名 ``<role>-1/<role>-2/...``；
    - 空白项忽略；同一身份名重复时后项覆盖前项。
    """
    identities: dict[str, str] = {}
    auto = 0
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        if "=" in item:
            name, secret = item.split("=", 1)
            name, secret = name.strip(), secret.strip()
            if not name or not secret:
                raise ValueError("身份凭据项格式必须为 身份名=凭据，且两侧均非空")
        else:
            auto += 1
            name, secret = f"identity-{auto}", item
        identities[name] = secret
    return identities


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "caltrace"
    db_user: str = "caltrace"
    db_password: str = "caltrace"
    # 连接池大小
    db_min_size: int = 2
    db_max_size: int = 10
    # 启动时等待数据库就绪的最长秒数（compose 已配 healthcheck，这里再兜底）
    db_connect_timeout: int = 60

    app_host: str = "0.0.0.0"
    app_port: int = 8080

    # 交付方管理密钥：创建签收邀请、查询定稿记录时凭 X-Management-Key 头校验。
    # 仅本地配置、不落库；生产部署必须通过环境变量覆盖默认值。
    delivery_management_key: str = "caltrace-delivery-admin"

    # 普通证书“双人复核签发”的本地身份凭据表（仅本地配置、不落库）：
    # 逗号分隔，每项为 身份名=凭据，例如
    #   CERTIFICATE_ENTRY_KEYS="li=caltrace-entry-li,wang=caltrace-entry-wang"
    #   CERTIFICATE_REVIEWER_KEYS="zhao=caltrace-review-zhao,sun=caltrace-review-sun"
    # 录入人提交申请经 X-Entry-Key 头校验；复核人驳回/批准经 X-Reviewer-Key 头校验；
    # 录入与复核必须为不同凭据（同凭据复核 403）。默认值仅供本机开发，生产必须覆盖。
    certificate_entry_keys: str = "entry-1=caltrace-entry-key"
    certificate_reviewer_keys: str = (
        "reviewer-1=caltrace-review-key-1,reviewer-2=caltrace-review-key-2"
    )

    @property
    def entry_identities(self) -> dict[str, str]:
        return _parse_identity_keys(self.certificate_entry_keys)

    @property
    def reviewer_identities(self) -> dict[str, str]:
        return _parse_identity_keys(self.certificate_reviewer_keys)


settings = Settings()
