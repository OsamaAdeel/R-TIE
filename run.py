"""RTIE Backend Launcher.

Sets the Windows event loop to SelectorEventLoop before starting uvicorn.
psycopg (PostgreSQL async driver) requires SelectorEventLoop — the default
ProactorEventLoop on Windows is not compatible.

Usage: python run.py
"""

import asyncio
import platform

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=False)
