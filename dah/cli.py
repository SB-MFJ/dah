"""Command line entry point: ``dah serve`` / ``dah ingest`` / ``dah mcp``."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dah")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the API + web UI")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8000)

    ip = sub.add_parser("ingest", help="ingest local files (runs synchronously)")
    ip.add_argument("files", nargs="+")
    ip.add_argument("--prefix", default="", help="logical path prefix")

    mp = sub.add_parser("mcp", help="run the MCP server")
    mp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mp.add_argument("--host", default="0.0.0.0")
    mp.add_argument("--port", type=int, default=8001)

    a = p.parse_args(argv)
    if a.cmd == "serve":
        import uvicorn

        from .api.app import create_app

        uvicorn.run(create_app(), host=a.host, port=a.port, proxy_headers=True, forwarded_allow_ips="*")
    elif a.cmd == "ingest":
        from pathlib import Path

        from .ingest.service import ingest_file

        for f in a.files:
            path = Path(f)
            with path.open("rb") as fh:
                res = ingest_file(fh, filename=path.name, logical_path=(a.prefix.rstrip("/") + "/" + path.name).lstrip("/"), sync=True)
            print(f"{res['path']}: v{res['version']} {res['status']}")
    elif a.cmd == "mcp":
        from .mcp_server import main as mcp_main

        return mcp_main(["--transport", a.transport, "--host", a.host, "--port", str(a.port)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
