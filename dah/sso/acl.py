"""Access policies for SSO data endpoints: who may read, which rows and which columns.

Policy JSON (all keys optional)::

    {
      "providers": ["okta", "adfs"],          # identity providers accepted (empty = any)
      "allowed_users": ["ann@x.org"],         # emails or subjects
      "allowed_groups": ["finance"],          # any one group suffices
      # with neither allowed_users nor allowed_groups, every authenticated principal may read
      "row_filters": [                        # all must match (AND); values come from the principal
        {"column": "department", "claim": "attributes.department"},
        {"column": "region", "claim": "groups", "op": "in"}
      ],
      "unrestricted_groups": ["auditors"],    # bypass row_filters
      "columns": ["id", "path", "amount"],    # column allowlist (empty = all)
      "column_groups": {"salary": ["hr"]},    # column visible only to these groups
      "max_rows": 10000,
      "allow_user_filters": true              # ?f.<column>=value equality filters
    }

Row filter ops: ``eq`` (default), ``in`` (claim is a list; column must be one of them),
``contains`` (column is a list/JSON array that must contain the claim value).
A row filter whose claim is missing on the principal matches nothing (deny by default).
"""

from __future__ import annotations

import re

from .core import Principal

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OPS = ("eq", "in", "contains")
POLICY_KEYS = {"providers", "allowed_users", "allowed_groups", "row_filters", "unrestricted_groups", "columns", "column_groups", "max_rows", "allow_user_filters"}


class PolicyError(ValueError):
    pass


def validate_policy(p: dict) -> dict:
    unknown = set(p) - POLICY_KEYS
    if unknown:
        raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
    for f in p.get("row_filters") or []:
        if not IDENT.match(f.get("column", "")):
            raise PolicyError(f"row filter column must be an identifier: {f}")
        if f.get("op", "eq") not in OPS:
            raise PolicyError(f"row filter op must be one of {OPS}")
        if not f.get("claim") and "value" not in f:
            raise PolicyError("row filter needs 'claim' (principal field) or a fixed 'value'")
    for c in list(p.get("columns") or []) + list((p.get("column_groups") or {}).keys()):
        if not IDENT.match(c):
            raise PolicyError(f"invalid column name {c!r}")
    if "max_rows" in p and (not isinstance(p["max_rows"], int) or p["max_rows"] < 1):
        raise PolicyError("max_rows must be a positive integer")
    return p


def authorize(policy: dict, who: Principal) -> tuple[bool, str]:
    providers = policy.get("providers") or []
    if providers and who.provider not in providers:
        return False, f"identity provider {who.provider!r} not accepted"
    users = {u.lower() for u in policy.get("allowed_users") or []}
    groups = set(policy.get("allowed_groups") or [])
    if not users and not groups:
        return True, "any authenticated principal"
    if users and ((who.email or "").lower() in users or who.subject.lower() in users):
        return True, "user allowed"
    if groups and groups & set(who.groups):
        return True, "group allowed"
    return False, "not in allowed users or groups"


def visible_columns(policy: dict, who: Principal, available: list[str]) -> list[str]:
    allow = policy.get("columns") or available
    restricted = policy.get("column_groups") or {}
    groups = set(who.groups)
    return [c for c in available if c in allow and (c not in restricted or groups & set(restricted[c]))]


def _q(col: str) -> str:
    return '"' + col + '"'


def row_filter_sql(policy: dict, who: Principal, available: list[str], params: list) -> list[str]:
    if set(policy.get("unrestricted_groups") or []) & set(who.groups):
        return []
    clauses = []
    for f in policy.get("row_filters") or []:
        col, op = f["column"], f.get("op", "eq")
        if col not in available:
            # Misconfigured filter: fail closed rather than return unfiltered rows.
            clauses.append("FALSE")
            continue
        value = f["value"] if "value" in f else who.claim(f["claim"])
        if value is None or value == []:
            clauses.append("FALSE")
            continue
        if op == "in":
            vals = value if isinstance(value, list) else [value]
            params.append([str(v) for v in vals])
            clauses.append(f"CAST({_q(col)} AS VARCHAR) IN (SELECT unnest(${len(params)}::VARCHAR[]))")
        elif op == "contains":
            params.append(str(value))
            clauses.append(f"list_contains(CAST({_q(col)} AS VARCHAR[]), ${len(params)})")
        else:
            if isinstance(value, list):  # eq against a list claim => any match
                params.append([str(v) for v in value])
                clauses.append(f"CAST({_q(col)} AS VARCHAR) IN (SELECT unnest(${len(params)}::VARCHAR[]))")
            else:
                params.append(str(value))
                clauses.append(f"CAST({_q(col)} AS VARCHAR) = ${len(params)}")
    return clauses


def build_query(source_sql: str, available: list[str], policy: dict, who: Principal, *, columns: list[str] | None = None,
                user_filters: dict[str, str] | None = None, order_by: str | None = None, limit: int = 1000, offset: int = 0) -> tuple[str, list, list[str]]:
    """Returns (sql, params, selected columns). ``source_sql`` is admin-defined; everything user-supplied is bound."""
    visible = visible_columns(policy, who, available)
    if columns:
        bad = [c for c in columns if c not in visible]
        if bad:
            raise PolicyError(f"columns not available: {bad}")
        selected = columns
    else:
        selected = visible
    if not selected:
        raise PolicyError("no columns visible to this principal")
    params: list = []
    where = row_filter_sql(policy, who, available, params)
    if user_filters:
        if not policy.get("allow_user_filters", True):
            raise PolicyError("filters are disabled for this endpoint")
        for col, val in user_filters.items():
            if col not in visible:
                raise PolicyError(f"cannot filter on {col!r}")
            params.append(str(val))
            where.append(f"CAST({_q(col)} AS VARCHAR) = ${len(params)}")
    sql = f"SELECT {', '.join(_q(c) for c in selected)} FROM ({source_sql}) AS src"
    if where:
        sql += " WHERE " + " AND ".join(f"({w})" for w in where)
    if order_by:
        desc = order_by.startswith("-")
        col = order_by.lstrip("-")
        if col not in visible:
            raise PolicyError(f"cannot order by {col!r}")
        sql += f" ORDER BY {_q(col)}{' DESC' if desc else ''}"
    max_rows = int(policy.get("max_rows") or 10000)
    sql += f" LIMIT {max(1, min(int(limit), max_rows))} OFFSET {max(0, int(offset))}"
    return sql, params, selected
