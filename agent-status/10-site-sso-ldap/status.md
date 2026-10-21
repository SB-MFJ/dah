# Feature 10 — Site login via OIDC / SAML / LDAP

Status: COMPLETE

## Tasks
- [x] LDAP identity provider kind (search + bind, groups via memberOf and group search, StartTLS/LDAPS)
- [x] Just-in-time user provisioning for OIDC, SAML and LDAP logins
- [x] IdP groups refreshed each login; `admin_groups` grant site admin
- [x] Same session works for the UI, admin API and `/data`
- [x] Login dialog lists all providers; `/sso` page supports password providers
- [x] Tests (LDAP mock server, OIDC/SAML provisioning)

## Log
- 2026-10-21: `auth/ldap.py` (ldap3, pure Python, LGPL-3.0): binds as the service account, searches
  the user with an RFC 4515-escaped filter (exactly one match required), then binds *as the user*
  to verify the password. Empty passwords are refused, since an empty password would make an
  unauthenticated bind that succeeds. Groups come from `memberOf` CNs and/or a group search
  (`{user_dn}` / `{username}` placeholders, escaped). Supports LDAPS / StartTLS with certificate
  verification and an optional CA bundle; extra attributes map to principal attributes.
  Defaults work for OpenLDAP; set `username_attribute=sAMAccountName` for Active Directory.
- 2026-10-21: Provider kinds are now `oidc | saml | ldap`; `bind_password` is encrypted and masked
  like the other secrets; every kind supports `admin_groups`. `POST /api/sso/providers/{id}/test`
  checks connectivity (LDAP bind + base lookup, OIDC discovery, SAML metadata).
- 2026-10-21: Every SSO login (OIDC callback, SAML ACS, LDAP password) now provisions or refreshes
  a user account (keyed on provider + subject; username = email when free) and issues a normal
  account session. One cookie serves the UI, the admin API (when the user is an admin) and `/data`.
  Disabled accounts can't sign in. OIDC bearer JWTs resolve to the provisioned account
  (local groups + the token's current groups).
- 2026-10-21: `/sso` page shows a password form for local and LDAP providers; `/auth/methods`
  lists password providers and redirect providers for the UI's sign-in dialog. The admin UI has
  an LDAP provider form, admin groups and a "Test connection" button.
- 2026-10-21: 6 tests pass (68 total), using an in-memory LDAP directory (ldap3 MOCK_SYNC):
  provisioning with memberOf and group-search groups, attribute mapping, admin via group,
  wrong/unknown/empty passwords, filter-injection attempts (`*`, `ann)(uid=*`), disabled
  accounts, form login + connectivity test, OIDC site login with admin groups and the bearer
  token → account mapping, and SAML login provisioning.
- Not verified: a real LDAP / Active Directory server (none available here).
