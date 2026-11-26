# Vercel

Tokens are created in the Vercel dashboard (Account Settings → Tokens). The person
copies it; pipe the clipboard into `durumari-mcp secret put VERCEL_TOKEN --stdin`, or use
the 비밀값 view.

## Deploy an environment variable

`vercel env add` reads the value from stdin:

```sh
durumari-mcp secret run --env VALUE=DATABASE_URL -- \
  sh -c 'printf %s "$VALUE" | vercel env add DATABASE_URL production'
```

Replace `production` with `preview` or `development` as the user asks. To change an
existing variable, remove it with `vercel env rm DATABASE_URL production` after the
user agrees, then add it again.
