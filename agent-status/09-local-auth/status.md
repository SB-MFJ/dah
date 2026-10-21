# Feature 09 — Local authentication, users and API tokens

Status: COMPLETE

## Tasks
- [x] User model (local + externally provisioned), local groups, admin flag, disable
- [x] Password hashing (scrypt), policy, login throttling
- [x] Session cookie login/logout, session invalidation on password change / disable
- [x] CSRF protection for cookie-authenticated writes
- [x] Personal access tokens (hashed) for API / MCP use
- [x] Role-aware API dependencies (admin vs user); admin token kept as break-glass
- [x] Bootstrap admin (env) + `dah user` CLI
- [x] UI: login dialog (password / SSO / token), account page, users admin page
- [x] MCP acts as the calling user (per-request bearer token)
- [x] Tests

## Log
- 2026-10-21: Catalog: `users` (local or provisioned by an IdP; local groups + IdP groups; admin flag
  or admin through IdP groups; disabled; `session_version`), `api_tokens` (SHA-256 hashed, prefix
  shown, optional expiry), `folder_grants` (used by feature 11).
- 2026-10-21: `auth/passwords.py` uses scrypt (stdlib, N=2^15) with a constant-time check, and
  verifies against a dummy hash for unknown users so response timing doesn't reveal which usernames
  exist. `auth/identity.py` resolves the caller in this order: admin token, personal API token,
  dah session JWT, OIDC provider JWT, then session cookie. Sessions carry uid + session_version,
  so changing a password, disabling a user or "sign out everywhere" invalidates them. Cookie
  sessions are HttpOnly, SameSite=Lax and Secure on https, and writes made with a cookie need
  the `X-DAH-CSRF: 1` header. Login throttle: 10 failures per username+IP per 15 minutes → 429.
- 2026-10-21: API: `/auth/methods` (public), `/auth/login` (JSON, or an HTML form with a safe `next`),
  `/auth/logout`, `/auth/me`, `/auth/password`, `/auth/tokens` (create shows the token once; list;
  revoke). Admin: `/api/users` CRUD, set password, revoke sessions, `/api/groups`. Every
  existing admin route now requires an admin *identity*: an admin user or the
  `DAH_ADMIN_TOKEN` break-glass token. Non-admins get 403.
- 2026-10-21: `DAH_BOOTSTRAP_ADMIN_USER` / `DAH_BOOTSTRAP_ADMIN_PASSWORD` create the first admin;
  `dah user create|passwd|list` CLI.
- 2026-10-21: MCP: tools forward the caller's own `Authorization` header (via `ctx.headers`) to
  the API, and the HTTP transport accepts any credential the API accepts (checked against
  `/auth/me`, cached 60 s). Each MCP client therefore acts with its own user's role (and folder
  permissions, feature 11).
- 2026-10-21: UI: sign-in dialog built from `/auth/methods` (password form with provider choice,
  SSO buttons, admin-token fallback), cookie sessions with the CSRF header, admin-only views
  hidden from non-admins, Account page (profile, change password, personal tokens), Users
  admin page (create/edit/disable/delete with confirmation, groups, admin, set password, sign out
  everywhere).
- 2026-10-21: 13 auth tests pass (62 total); a live server smoke test with curl confirmed
  bootstrap, login, the cookie session and CSRF enforcement.
- Note: the LDAP module (`dah/auth/ldap.py`) is committed and tested with feature 10.
