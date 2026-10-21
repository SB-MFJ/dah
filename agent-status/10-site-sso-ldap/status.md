# Feature 10 — Site login via OIDC / SAML / LDAP

Status: NOT STARTED

## Tasks
- [ ] LDAP identity provider kind (search + bind, groups via memberOf and group search, StartTLS/LDAPS)
- [ ] Just-in-time user provisioning for OIDC, SAML and LDAP logins
- [ ] IdP groups refreshed each login; `admin_groups` grant site admin
- [ ] Same session works for the UI, admin API and `/data`
- [ ] Login dialog lists all providers; `/sso` page supports password providers
- [ ] Tests (LDAP mock server, OIDC/SAML provisioning)

## Log
