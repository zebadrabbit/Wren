# Plans and specs

Design specs (`specs/`) and implementation plans (`plans/`), one pair per
feature, named by the date the work started.

**Anything dated before 2026-08-09 was written for Wren v1** and uses v1
module paths and env var names (`wren/surfaces/…`, `OWNER_ID`, `SURFACES`, …).
They are kept as written on purpose: they are the record of why each feature
behaves the way it does, and that reasoning did not go stale when the names
did. Translate names as you read with the rename tables in `CLAUDE.md`
("Env vars that changed in the v2 restart"). Later files use v2 names.
