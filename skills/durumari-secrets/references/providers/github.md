# GitHub

## Issue and register

- GitHub CLI login token: `durumari-mcp secret put GITHUB_TOKEN --from-cmd -- gh auth token`
  (only after the user agrees; it stores the CLI's current token).
- Fine-grained personal access tokens are created in the GitHub web page. The person
  copies it; then pipe the clipboard into `secret put --stdin`, or use the 비밀값 view.
- Ask the user which repositories and permissions the token needs; prefer the narrowest.

## Deploy to Actions secrets

`gh secret set` reads the value from stdin when no `--body` is given:

```sh
durumari-mcp secret run --env VALUE=DEPLOY_KEY -- \
  sh -c 'printf %s "$VALUE" | gh secret set DEPLOY_KEY --repo owner/repo'
```

Add `--env <environment>` to `gh secret set` for environment secrets, or `--org` for
organization secrets. Never use `--body`.

## Rotate or revoke

Replacing a value in Durumari happens on the secure screen. Revoke the old token on
GitHub separately; Durumari revocation does not recall a value already delivered.
