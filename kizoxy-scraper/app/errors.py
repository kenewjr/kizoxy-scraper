from enum import Enum

from pydantic import BaseModel


class ErrorCode(str, Enum):
    BLOCKED = "BLOCKED"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"
    POOL_EXHAUSTED = "POOL_EXHAUSTED"


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str


class ScraperException(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class BlockedException(ScraperException):
    def __init__(self, message: str = "Target site blocked the request"):
        super().__init__(ErrorCode.BLOCKED, message)


class NotFoundException(ScraperException):
    def __init__(self, message: str = "Requested resource not found"):
        super().__init__(ErrorCode.NOT_FOUND, message)


class TimeoutException(ScraperException):
    def __init__(self, message: str = "Request timed out"):
        super().__init__(ErrorCode.TIMEOUT, message)


class RateLimitedException(ScraperException):
    def __init__(self, message: str = "Rate limited by target site"):
        super().__init__(ErrorCode.RATE_LIMITED, message)


class InternalException(ScraperException):
    def __init__(self, message: str = "Internal scraper error"):
        super().__init__(ErrorCode.INTERNAL, message)


class BrowserPoolExhaustedError(ScraperException):
    def __init__(self, message: str = "Browser pool exhausted"):
        super().__init__(ErrorCode.POOL_EXHAUSTED, message)
