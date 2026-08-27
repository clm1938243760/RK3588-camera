"""Local patient intake and immutable report archive for RK3588.

Submodules are deliberately not imported here so database and archive tools can
run without loading the service's optional YAML and HTTP dependencies.
"""

__all__ = ["config", "store", "archive", "connectors", "coordinator", "web"]
