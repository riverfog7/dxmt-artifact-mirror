import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .models.api import ErrorDetail, ErrorResponse, ValidationIssue

logger = logging.getLogger(__name__)


class APIError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code, detail=ErrorDetail(code=code, message=message))


def install_error_handlers(app: FastAPI):
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        detail = error.detail if isinstance(error.detail, ErrorDetail) else ErrorDetail(
            code={404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error"),
            message=str(error.detail),
        )
        return JSONResponse(
            status_code=error.status_code, headers=error.headers,
            content=ErrorResponse(error=detail).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        detail = ErrorDetail(code="invalid_request", message="Request validation failed", details=[
            ValidationIssue(location=list(issue["loc"]), message=issue["msg"], type=issue["type"])
            for issue in error.errors()
        ])
        return JSONResponse(status_code=422, content=ErrorResponse(error=detail).model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception):
        logger.error("Unhandled API error", exc_info=error)
        detail = ErrorDetail(code="internal_error", message="An unexpected server error occurred")
        return JSONResponse(status_code=500, content=ErrorResponse(error=detail).model_dump(mode="json"))
