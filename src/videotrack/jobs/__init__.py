"""Persistent job queue, worker pool, and the live event bus.

This is the layer the CLI and the API both sit on: the CLI so a queue is usable
from the terminal, the API so the web UI has something to stream.
"""
