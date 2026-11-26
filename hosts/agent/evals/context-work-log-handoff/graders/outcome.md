---
type: llm
---

PASS if the reply plans to find the task's existing `work-log` Context by its issue key, read its summary and progress before continuing, compare that report with the actual Git branch, HEAD and working tree, and append progress lines (`agent-N(display_name) HH:MM branch@SHA: …`) to that same Context; or explains that the Context tools are not available in this session.
FAIL if it creates a new document per checkpoint, overwrites earlier progress lines, treats the stored summary as verified work or approval, or takes the person's name from a local username, device or branch.
