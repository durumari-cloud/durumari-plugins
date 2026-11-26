# Cloudflare

API tokens are created in the Cloudflare dashboard (My Profile → API Tokens) with the
narrowest template that fits. The person copies it; pipe the clipboard into
`durumari-mcp secret put CLOUDFLARE_API_TOKEN --stdin`, or use the 비밀값 view.

## Deploy a Worker secret

`wrangler secret put` reads the value from stdin when piped:

```sh
durumari-mcp secret run --env VALUE=STRIPE_SECRET_KEY -- \
  sh -c 'printf %s "$VALUE" | wrangler secret put STRIPE_SECRET_KEY'
```

Add `--env <name>` for a Wrangler environment. Pages projects use
`wrangler pages secret put NAME --project-name <project>` the same way.
