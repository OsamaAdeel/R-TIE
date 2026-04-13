"""RTIE Backend Launcher.

Sets the correct event loop policy for Windows before starting uvicorn.
Usage: python run.py
"""

import asyncio
import platform

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=False)
