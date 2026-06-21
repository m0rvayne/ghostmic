"""Meeting Transcript MCP — pip-installable entry point."""


def main():
    """Launch the MCP server (console_scripts entry point)."""
    import asyncio
    import sys
    import os

    # Ensure the project root is importable (for server.py, capture.py, watcher.py)
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)

    from server import _async_main
    asyncio.run(_async_main())
