"""Load optional runtime packages before legacy tests install fallback stubs."""

try:
    import aiohttp  # noqa: F401
except ImportError:
    pass
