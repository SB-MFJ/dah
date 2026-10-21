# Feature 08 — Multiple SAML / OIDC SSO providers + access-controlled data endpoints

Status: NOT STARTED

## Tasks
- [ ] Identity provider registry (many SAML + OIDC providers)
- [ ] OIDC: discovery, auth-code + PKCE login, ID-token validation via JWKS
- [ ] SAML 2.0: SP metadata, AuthnRequest, ACS validation (python3-saml)
- [ ] Session tokens (signed JWT) + direct bearer IdP tokens for machine clients
- [ ] Data endpoint registry (source table/SQL, providers, users/groups, row filters, column rules)
- [ ] ACL evaluation + parameterised row-level filtering
- [ ] `/data/{slug}` read API (json/csv, paging, filters) + access audit log
- [ ] Admin API + UI for providers / endpoints
- [ ] Tests

## Log
