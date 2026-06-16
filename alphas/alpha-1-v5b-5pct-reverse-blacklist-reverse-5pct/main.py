import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.engine import Alpha1V5b5PctReverseEngine
from base.runtime import install_uvloop_if_available


if __name__ == "__main__":
    install_uvloop_if_available()
    engine = Alpha1V5b5PctReverseEngine()
    asyncio.run(engine.run())
