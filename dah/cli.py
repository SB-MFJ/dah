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

    up = sub.add_parser("user", help="manage local accounts")
    usub = up.add_subparsers(dest="ucmd", required=True)
    uc = usub.add_parser("create", help="create a local user (prompts for the password)")
    uc.add_argument("username")
    uc.add_argument("--admin", action="store_true")
    uc.add_argument("--email")
    uc.add_argument("--group", action="append", default=[])
    upw = usub.add_parser("passwd", help="set a local user's password (prompts)")
    upw.add_argument("username")
    usub.add_parser("list", help="list users")

    lp = sub.add_parser("licenses", help="print the open-source license inventory")
    lp.add_argument("--markdown", action="store_true", help="THIRD_PARTY_LICENSES.md format")

    a = p.parse_args(argv)
    if a.cmd == "serve":
        import uvicorn

        from .api.app import create_app

        uvicorn.run(create_app(), host=a.host, port=a.port, proxy_headers=True, forwarded_allow_ips="*")
    elif a.cmd == "ingest":
        from pathlib import Path

        from .ingest.scan import InfectedFile, ScanError
        from .ingest.service import ingest_file

        rc = 0
        for f in a.files:
            path = Path(f)
            try:
                with path.open("rb") as fh:
                    res = ingest_file(fh, filename=path.name, logical_path=(a.prefix.rstrip("/") + "/" + path.name).lstrip("/"), sync=True)
            except (InfectedFile, ScanError) as e:
                print(f"{path}: {e}", file=sys.stderr)
                rc = 1
                continue
            print(f"{res['path']}: v{res['version']} {res['status']}")
        return rc
    elif a.cmd == "user":
        return _user_cmd(a)
    elif a.cmd == "licenses":
        from . import licenses

        if a.markdown:
            print(licenses.markdown())
        else:
            for pkg in licenses.inventory()["python_packages"]:
                flag = "" if pkg["open_source"] else "   <-- REVIEW"
                print(f"{pkg['name']:28} {pkg['version']:14} {pkg['license']}{flag}")
    elif a.cmd == "mcp":
        from .mcp_server import main as mcp_main

        return mcp_main(["--transport", a.transport, "--host", a.host, "--port", str(a.port)])
    return 0


def _user_cmd(a) -> int:
    import getpass

    from .auth import identity as idm
    from .db import User, session_scope

    with session_scope() as db:
        if a.ucmd == "list":
            for u in db.query(User).order_by(User.username):
                flags = ",".join(f for f, on in (("admin", u.admin), ("disabled", u.disabled)) if on)
                print(f"{u.username:32} {u.source:12} {flags:16} {' '.join(u.groups)}")
            return 0
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat: "):
            print("passwords differ")
            return 1
        try:
            if a.ucmd == "create":
                idm.create_local_user(db, a.username, pw, email=a.email, groups=a.group, is_admin=a.admin)
            else:
                u = idm.find_user(db, a.username)
                if u is None or u.source != "local":
                    print("no such local user")
                    return 1
                idm.set_password(u, pw)
        except idm.AuthError as e:
            print(e)
            return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
