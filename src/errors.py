"""统一错误码封装：网关内部异常与上游异常归一为 GatewayError。

所有对外错误响应均为 {"error": {"code", "message", "detail"}} 结构，
HTTP 状态由错误码表决定；可重试错误（retryable=True）参与指数退避重试。
"""
from enum import Enum


class ErrorCode(str, Enum):
    """统一错误码：(HTTP 状态, 是否可重试)。"""
    INVALID_REQUEST = "INVALID_REQUEST"                # 400 请求参数非法
    TEMPLATE_VAR_MISSING = "TEMPLATE_VAR_MISSING"      # 400 模板必填变量缺失
    TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"          # 404 模板或版本未登记
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"                # 404 model 字段未注册
    LOCAL_RATE_LIMITED = "LOCAL_RATE_LIMITED"          # 429 网关本地限流（不打上游）
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"              # 408 上游超时（可重试）
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"    # 503 上游限流（可重试）
    UPSTREAM_AUTH = "UPSTREAM_AUTH"                    # 502 上游鉴权失败（不可重试）
    UPSTREAM_ERROR = "UPSTREAM_ERROR"                  # 502 上游其他错误（5xx 可重试）
    INTERNAL_ERROR = "INTERNAL_ERROR"                  # 500 网关内部错误


_ERROR_SPEC: dict[ErrorCode, tuple[int, bool]] = {
    ErrorCode.INVALID_REQUEST: (400, False),
    ErrorCode.TEMPLATE_VAR_MISSING: (400, False),
    ErrorCode.TEMPLATE_NOT_FOUND: (404, False),
    ErrorCode.MODEL_NOT_FOUND: (404, False),
    ErrorCode.LOCAL_RATE_LIMITED: (429, False),
    ErrorCode.UPSTREAM_TIMEOUT: (408, True),
    ErrorCode.UPSTREAM_RATE_LIMITED: (503, True),
    ErrorCode.UPSTREAM_AUTH: (502, False),
    ErrorCode.UPSTREAM_ERROR: (502, True),
    ErrorCode.INTERNAL_ERROR: (500, False),
}


class GatewayError(Exception):
    """网关统一异常：携带错误码、面向用户的消息与可选细节。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    @property
    def http_status(self) -> int:
        return _ERROR_SPEC[self.code][0]

    @property
    def retryable(self) -> bool:
        return _ERROR_SPEC[self.code][1]

    def to_body(self) -> dict:
        """转为对外错误响应体。"""
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "detail": self.detail,
            }
        }


def translate_exception(exc: Exception) -> GatewayError:
    """将上游 SDK / 网络异常归一为 GatewayError（统一错误码的入口）。"""
    # 已是网关错误则原样返回
    if isinstance(exc, GatewayError):
        return exc

    # Python 内置连接/超时异常（网络瞬态故障，可重试）
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return GatewayError(ErrorCode.UPSTREAM_ERROR, "上游连接异常", str(exc))

    cls_name = type(exc).__name__

    # openai SDK 异常（软依赖，不 import 具体类，按类名归一）
    if cls_name in ("APITimeoutError", "Timeout"):
        return GatewayError(ErrorCode.UPSTREAM_TIMEOUT, "上游模型服务超时", str(exc))
    if cls_name in ("APIConnectionError", "ConnectError", "ConnectTimeout"):
        return GatewayError(ErrorCode.UPSTREAM_ERROR, "上游模型服务连接失败", str(exc))
    if cls_name == "RateLimitError":
        return GatewayError(ErrorCode.UPSTREAM_RATE_LIMITED, "上游模型服务限流", str(exc))
    if cls_name == "AuthenticationError":
        return GatewayError(ErrorCode.UPSTREAM_AUTH, "上游模型服务鉴权失败", str(exc))
    if cls_name in ("PermissionDeniedError", "NotFoundError", "BadRequestError",
                    "UnprocessableEntityError"):
        # 上游 4xx：请求本身有问题，不可重试
        return GatewayError(ErrorCode.UPSTREAM_ERROR, f"上游拒绝请求：{cls_name}", str(exc))
    if cls_name == "InternalServerError" or cls_name.endswith("APIStatusError"):
        return GatewayError(ErrorCode.UPSTREAM_ERROR, "上游模型服务错误", str(exc))

    return GatewayError(ErrorCode.INTERNAL_ERROR, "网关内部错误", f"{cls_name}: {exc}")
