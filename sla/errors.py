"""Domain errors mapped to HTTP status codes by the API layer."""


class ApiError(Exception):
    status = 500
    code = "INTERNAL"

    def __init__(self, message: str, code: str | None = None, **details):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details

    def payload(self) -> dict:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


class NotFound(ApiError):
    status = 404
    code = "NOT_FOUND"


class Conflict(ApiError):
    """State-machine violation or lifecycle conflict (HTTP 409)."""

    status = 409
    code = "CONFLICT"


class Validation(ApiError):
    status = 400
    code = "VALIDATION"
