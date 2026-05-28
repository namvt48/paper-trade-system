import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.engine import WilderEngine


if __name__ == "__main__":
    engine = WilderEngine()
    asyncio.run(engine.run())
