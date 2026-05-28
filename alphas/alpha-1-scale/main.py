import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.engine import Alpha1ScaleEngine


if __name__ == "__main__":
    engine = Alpha1ScaleEngine()
    asyncio.run(engine.run())
