# .env files

`.env.durumari` is a template you can commit: each value is a reference, never a
secret.

```dotenv
# .env.durumari
DATABASE_URL=durumari://PROD_DATABASE_URL
STRIPE_KEY=durumari://STRIPE_SECRET_KEY
LOG_LEVEL=info
```

- `durumari://NAME` names a secret in the project vault. Other lines are copied as is.
- `durumari-mcp secret env` renders it into `.env`, only when git ignores `.env`, with
  owner-only access (0600, or an owner-only ACL on Windows), replaced atomically.
  `--template`, `--output`, `--force` (overwrite) and `--offline` (cache only) adjust it.
- Prefer `durumari-mcp secret run --template .env.durumari -- <command>`: it injects the
  same references into one process without writing a file.

Rules:

1. Confirm with the user before writing or overwriting `.env`.
2. Never open, print or summarize the rendered `.env`. To check it, ask the tool's own
   check command or run the program.
3. If `.env` is not gitignored, add it to `.gitignore` first (after saying so) rather
   than forcing the write.
4. An existing `.env` with real values is the user's: do not read it to "migrate" it.
   Ask which names are needed, register each through `secret put` or the 비밀값 view,
   then write the template.
