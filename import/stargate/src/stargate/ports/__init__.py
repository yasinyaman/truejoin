"""Ports: the interfaces the application core needs from the outside world.

The application layer depends only on these Protocols; concrete adapters
(files, HTTP, FastMCP, CLI) implement them and are wired in the composition
roots (stargate.bootstrap, stargate.runtime).
"""
