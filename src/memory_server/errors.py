class ServiceError(Exception):
    """Business error carrying a safe code and its REST status (§9.2)."""

    def __init__(self, status, code, message, retryable=False, retry_after_ms=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms

    def to_dict(self):
        err = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after_ms is not None:
            err["retry_after_ms"] = self.retry_after_ms
        return {"error": err}


def not_found():
    return ServiceError(404, "NOT_FOUND", "대상을 찾을 수 없습니다.")


def unauthenticated():
    return ServiceError(401, "UNAUTHENTICATED", "인증이 필요하거나 자격증명이 유효하지 않습니다.")


def invalid(message, code="VALIDATION_ERROR"):
    return ServiceError(400, code, message)
