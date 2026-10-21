"""Subprocess entry point that executes one pipeline job.

Invoked as ``python -I -m dah.pipelines.worker`` with a JSON job on stdin; writes a JSON result
to ``job["result_path"]``. Anything the pipeline prints ends up in the run log.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path


def _limits(timeout_s: int, memory_mb: int) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return
    resource.setrlimit(resource.RLIMIT_CPU, (timeout_s, timeout_s + 5))
    if sys.platform.startswith("linux") and memory_mb > 0:
        b = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (b, b))


def run_job(job: dict) -> dict:
    from .context import Context

    staging = Path(job["staging_dir"])
    ctx = Context(job["pipeline"], job.get("params"), staging, job.get("output_table"))
    result: dict = {"ok": True, "outputs": {}, "metadata": {}, "errors": {}}
    code, kind, mode = job["code"], job["kind"], job["mode"]

    if kind == "sql":
        if mode != "run":
            raise ValueError("SQL pipelines cannot be on_ingest transforms; use a Python pipeline")
        out = job.get("output_table")
        if not out:
            raise ValueError("SQL pipelines need an output_table")
        ctx.write_table(out, ctx.arrow(code.strip().rstrip(";")))
        result["outputs"] = ctx.flush_outputs()
        return result

    ns: dict = {"__name__": "__pipeline__", "ctx": ctx}
    exec(compile(code, f"<pipeline:{job['pipeline']}>", "exec"), ns)

    if mode == "run":
        fn = ns.get("run")
        if not callable(fn):
            raise ValueError("python pipeline must define run(ctx)")
        ret = fn(ctx)
        if ret is not None:
            if not job.get("output_table"):
                raise ValueError("run(ctx) returned data but the pipeline has no output_table")
            ctx.write_table(job["output_table"], ret)
    else:  # transform
        fn = ns.get("transform")
        if not callable(fn):
            raise ValueError("on_ingest pipeline must define transform(doc, ctx)")
        for doc_id, version in job["documents"]:
            doc = ctx.document(doc_id, version)
            if doc is None:
                result["errors"][doc_id] = "document not found"
                continue
            try:
                ret = fn(doc, ctx)
            except Exception:
                result["errors"][doc_id] = traceback.format_exc(limit=5)
                print(f"[{doc.path}] transform failed:\n{result['errors'][doc_id]}", file=sys.stderr)
                continue
            if isinstance(ret, dict):
                result["metadata"][doc_id] = {"version": version, "values": ret}
            elif ret is not None:
                if not job.get("output_table"):
                    raise ValueError("transform returned rows but the pipeline has no output_table")
                ctx.write_table(job["output_table"], ret, mode="append")
    result["outputs"] = ctx.flush_outputs()
    return result


def main() -> int:
    job = json.load(sys.stdin)
    _limits(int(job.get("timeout_s", 300)), int(job.get("memory_mb", 0)))
    os.chdir(job["staging_dir"])
    try:
        result = run_job(job)
    except BaseException:
        result = {"ok": False, "error": traceback.format_exc(limit=8)}
        print(result["error"], file=sys.stderr)
    from ..lake import jsonable

    Path(job["result_path"]).write_text(json.dumps(jsonable(result)))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
