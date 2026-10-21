# Feature 08 — Multiple SAML / OIDC SSO providers + access-controlled data endpoints

Status: COMPLETE

## Tasks
- [x] Identity provider registry (many SAML + OIDC providers)
- [x] OIDC: discovery, auth-code + PKCE login, ID-token validation via JWKS
- [x] SAML 2.0: SP metadata, AuthnRequest, ACS validation (python3-saml)
- [x] Session tokens (signed JWT) + direct bearer IdP tokens for machine clients
- [x] Data endpoint registry (source table/SQL, providers, users/groups, row filters, column rules)
- [x] ACL evaluation + parameterised row-level filtering
- [x] `/data/{slug}` read API (json/csv/ndjson, paging, filters) + access audit log
- [x] Admin API + UI for providers / endpoints
- [x] Tests

## Log
- 2026-10-21: `sso/core.py` — provider registry with per-kind defaults; secrets (`client_secret`,
  `sp_private_key`) encrypted at rest and masked in API output (kept on partial updates);
  `Principal` (subject, provider, email, name, groups, attributes); HS256 session JWTs and
  short-lived signed flow state, each with a purpose-specific key derived by HKDF from
  `DAH_SECRET_KEY`; local-only `next` redirects (no open redirect).
- 2026-10-21: `sso/oidc.py` — discovery + JWKS caching (refetch on unknown `kid`), authorization
  code flow with state, nonce and PKCE S256, client_secret_basic/post, ID-token validation
  (signature, iss, aud, exp, nonce; HS*/none algorithms refused), userinfo fallback for groups,
  and IdP-issued bearer JWTs accepted on /data (routed by `iss`, aud checked).
- 2026-10-21: `sso/saml.py` — python3-saml in strict mode, signed assertions required by default,
  SHA-256 algorithms, SP metadata, optional signed AuthnRequests. URLs are built from
  `DAH_PUBLIC_BASE_URL`, so it works behind reverse proxies. Pending AuthnRequest IDs are kept
  server-side; the ACS only accepts a response whose `InResponseTo` matches one, then consumes it
  (unsolicited and replayed responses are rejected).
- 2026-10-21: `sso/acl.py` — policy: providers, allowed users/groups, row filters taking values
  from principal claims (`eq`/`in`/`contains`, fail closed if the claim is missing),
  unrestricted groups, column allowlist, per-column group grants, max_rows, user equality
  filters. All user-supplied values are bound parameters; identifiers are validated.
- 2026-10-21: API: public `/sso` login page, `/sso/providers`, `/sso/{id}/login|callback|acs|metadata`,
  `/sso/me`, `/sso/logout`, `/data`, `/data/{slug}`, `/data/{slug}/schema`. Admin
  `/api/sso/providers`, `/api/data-endpoints` (+ `/simulate` showing decision, SQL, params and a
  preview) and `/api/access-log`. Every /data request is audited (allowed or denied).
- 2026-10-21: UI "Access" page: provider forms per protocol (shows redirect/ACS/metadata URLs),
  endpoint editor (table or SQL source, JSON policy in Monaco), policy simulator, access log.
- 2026-10-21: 10 SSO tests pass (45 total): row/column security, fail-closed claims, SQL-injection
  attempt via filters, tampered/expired tokens, OIDC login with PKCE against a mock IdP, IdP
  bearer tokens (wrong aud / forged signature / unknown issuer rejected), SAML metadata + login
  + ACS with a really-signed assertion, forged-signer / replayed / unsolicited SAML responses
  rejected, multiple providers listed and togglable.
- Note: python3-saml rejects ACS URLs without a domain (e.g. `http://testserver`), so
  `DAH_PUBLIC_BASE_URL` must use a real hostname or `localhost`.
