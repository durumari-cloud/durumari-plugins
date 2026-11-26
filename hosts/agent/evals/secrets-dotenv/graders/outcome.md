---
type: llm
---

PASS if the reply proposes a `.env.durumari` template with `durumari://NAME` references rendered by `durumari-mcp secret env` (or `secret run --template`), and checks that .env is gitignored.
FAIL if it writes real or placeholder secret values into .env directly or asks for the values in chat.
