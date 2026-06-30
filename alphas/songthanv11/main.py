import asyncio

from app.engine import AlphaEngine
from base.runtime import install_uvloop_if_available


if __name__ == "__main__":
    install_uvloop_if_available()
    asyncio.run(AlphaEngine().run())
