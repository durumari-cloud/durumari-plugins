# Claude hook JSON encoding

Hook stdout is a JSON protocol decoded by the host as UTF-8. Windows Python
redirected stdout can instead default to cp949. SessionStart guidance includes
untrusted Policy titles and when-lines, which can contain characters that cp949
cannot encode. Previously that print raised UnicodeEncodeError, entered the
advisory error handler and incorrectly reported project_unavailable. The Korean
notice was then emitted in cp949 and appeared corrupted in Claude.

All SessionStart and Stop frames now use ASCII JSON with Unicode escapes. JSON
decoding restores the original Korean, punctuation and supplementary characters,
without requiring host environment changes. Authentication, workspace binding,
bounded Policy retrieval, outbox preservation and actual failure reasons are
unchanged. Lone surrogates anywhere in a frame (untrusted Policy text or
surrogateescape-decoded local values) are replaced with U+FFFD before the ASCII
frame is written, so the whole notice reaches the host as strict UTF-8 and no
output encoding error remains to be misreported as project_unavailable. Other
decoding errors keep the existing advisory path. These hooks provide guidance and pending-write notices, not automatic
conversation capture.

Regression coverage exercises the complete hook path through strict cp949,
ASCII and UTF-8 streams, then decodes the bytes as a UTF-8 host does. It covers
successful startup, actual credential failure, a cached notice and a pending
Stop notice. A successful startup must not create false recovery state.

For live Windows validation, use a separate candidate installation and the
already approved project's profile. Back up the project-local hook settings,
change only the owned hook executable, and start a new interactive Claude
session. Verify the actual SessionStart frame and an MCP Context read response;
an agent's success report alone is insufficient. Never copy credentials to the
candidate environment or bypass host trust. Record the candidate source SHA and
distinguish this local validation from a published release.
