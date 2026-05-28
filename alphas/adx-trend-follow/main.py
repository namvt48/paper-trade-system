import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.engine import ADXTrendFollowEngine


if __name__ == "__main__":
    engine = ADXTrendFollowEngine()
    asyncio.run(engine.run())
