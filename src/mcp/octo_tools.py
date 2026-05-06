"""
Entry shim for the requested path ``src/mcp/octo_tools.py``.

The implementation lives in :mod:`octo_spork_mcp.octo_tools` so the local directory name
``mcp`` does not shadow the PyPI ``mcp`` SDK on ``import mcp``.

Run::

    PYTHONPATH=src python -m octo_spork_mcp.octo_tools
    # or
    PYTHONPATH=src python src/mcp/octo_tools.py
"""

from __future__ import annotations

from octo_spork_mcp.octo_tools import main

if __name__ == "__main__":
    main()
