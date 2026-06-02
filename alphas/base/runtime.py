def install_uvloop_if_available() -> None:
    try:
        import uvloop
    except ImportError:
        return
    uvloop.install()
