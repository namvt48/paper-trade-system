import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.engine import Alpha1FixedEngine


if __name__ == "__main__":
    engine = Alpha1FixedEngine()
    asyncio.run(engine.run())
