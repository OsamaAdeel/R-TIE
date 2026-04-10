"""
RTIE Correlation ID Middleware.

Generates a unique correlation ID for each incoming HTTP request and
injects it into the request state and response headers. All downstream
log entries reference this ID for end-to-end traceability.
"""

import uuid
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Context variable holding the current request's correlation ID
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="N/A")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that assigns a UUID correlation ID to every request.

    The correlation ID is stored in a context variable accessible to all
    downstream code (including loggers) and returned in the
    X-Correlation-ID response header.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process the request by assigning and propagating a correlation ID.

        Args:
            request: The incoming HTTP request.
            call_next: Callable to pass the request to the next middleware or route.

        Returns:
            The HTTP response with X-Correlation-ID header attached.
        """
        cid = str(uuid.uuid4())
        correlation_id_var.set(cid)
        request.state.correlation_id = cid

        response: Response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response


def get_correlation_id() -> str:
    """Retrieve the current correlation ID from the context variable.

    Returns:
        The correlation ID string for the current request, or 'N/A' if
        called outside a request context.
    """
    return correlation_id_var.get()
