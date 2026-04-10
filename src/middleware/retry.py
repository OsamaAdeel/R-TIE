"""
RTIE Retry Middleware.

Provides exponential-backoff retry decorators for Oracle database operations
using the tenacity library. Retries are triggered only on transient
OracleDB errors to avoid masking permanent failures.
"""

from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import oracledb


def oracle_retry(func):
    """Decorator that retries a function on Oracle transient errors.

    Applies exponential backoff starting at 1 second, doubling up to 8 seconds,
    with a maximum of 3 attempts. Only retries on oracledb.DatabaseError.

    Args:
        func: The async or sync function to wrap with retry logic.

    Returns:
        Wrapped function with retry behavior.
    """
    return retry(
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(oracledb.DatabaseError),
    )(func)
