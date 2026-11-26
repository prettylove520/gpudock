"""Errors raised by the Colab API client, with hints a user can act on."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"

# google.rpc.Code numbers -> canonical names (subset we care about).
RPC_CODE_NAMES = {
    0: "OK",
    1: "CANCELLED",
    2: "UNKNOWN",
    3: "INVALID_ARGUMENT",
    4: "DEADLINE_EXCEEDED",
    5: "NOT_FOUND",
    6: "ALREADY_EXISTS",
    7: "PERMISSION_DENIED",
    8: "RESOURCE_EXHAUSTED",
    9: "FAILED_PRECONDITION",
    10: "ABORTED",
    13: "INTERNAL",
    14: "UNAVAILABLE",
    16: "UNAUTHENTICATED",
}

# ErrorInfo.reason (or status) -> what the user should do, in Chinese.
HINTS = {
    "SERVICE_DISABLED": "OAuth 客户端所在的 GCP 项目还没有启用 Colab API：在 Cloud Console 的 API 库里启用 colaboratory.googleapis.com。",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "登录时没有授予 colaboratory 权限：重新运行 `gpudock-m0 login`，并在授权页勾选 Colab 权限。",
    "USER_PROJECT_DENIED": "quota project 不可用：确认 --quota-project 指向的项目已启用 Colab API，且你在该项目上有 serviceusage.services.use 权限。",
    "NO_RUNTIMES": "这张卡在 Colab 那边暂时没有空闲资源（没货），稍等一会儿再试，或者换一张卡。",
    "TOO_MANY_ACTIVE_RUNTIMES": "活跃运行时太多：先释放一个（`gpudock-m0 cleanup` 只会删本工具创建的运行时），或在 Colab 网页里断开不用的运行时。",
    "QUOTA_EXCEEDED_USAGE_TIME": "本账号的使用时长额度已用完，需要等待额度恢复或购买计算单元。",
    "DENYLISTED": "该账号目前被限制使用 Colab 运行时。",
    "RESOURCE_EXHAUSTED": "触发了速率限制，稍后自动重试。",
    "FAILED_PRECONDITION": "暂时拿不到这个运行时：可能是该显卡没容量、额度不足或活跃运行时太多。换一种显卡或稍后再试。",
    "UNAUTHENTICATED": "登录状态失效：重新运行 `gpudock-m0 login`。",
}


@dataclass
class ColabApiError(Exception):
    """An error returned by colaboratory.googleapis.com (HTTP or Operation error)."""

    http_status: int | None
    code: int | None
    status: str | None
    message: str
    reason: str | None = None
    domain: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def hint(self) -> str | None:
        for key in (self.reason, self.status):
            if key and key in HINTS:
                return HINTS[key]
        return None

    def to_record(self) -> dict[str, Any]:
        """A JSON-safe summary for the results log (no tokens in here)."""
        return {
            "http_status": self.http_status,
            "code": self.code,
            "status": self.status,
            "reason": self.reason,
            "domain": self.domain,
            "metadata": self.metadata,
            "message": self.message[:500],
        }

    def __str__(self) -> str:
        parts = [self.status or str(self.code or self.http_status), self.message]
        if self.reason:
            parts.insert(1, f"[{self.reason}]")
        text = " ".join(p for p in parts if p)
        if self.hint:
            text += f"\n提示：{self.hint}"
        return text

    @classmethod
    def from_status(cls, status: dict[str, Any], http_status: int | None = None) -> ColabApiError:
        """Build from a google.rpc.Status dict (``{"code", "message", "details"}``).

        Accepts both the Operation.error shape (numeric ``code``) and the HTTP
        error body shape (``{"code": 403, "status": "PERMISSION_DENIED", ...}``).
        """
        code = status.get("code")
        status_name = status.get("status")
        if status_name is None and isinstance(code, int) and http_status is None:
            status_name = RPC_CODE_NAMES.get(code)
        reason = domain = None
        metadata: dict[str, Any] = {}
        for detail in status.get("details") or []:
            if isinstance(detail, dict) and detail.get("@type") == ERROR_INFO_TYPE:
                reason = detail.get("reason")
                domain = detail.get("domain")
                metadata = detail.get("metadata") or {}
                break
        return cls(
            http_status=http_status,
            code=code,
            status=status_name,
            message=status.get("message") or "",
            reason=reason,
            domain=domain,
            metadata=metadata,
            raw=status,
        )


class AuthError(Exception):
    """Login is missing or unusable."""


class KernelError(Exception):
    """The Jupyter kernel died, restarted, or the channel broke."""
