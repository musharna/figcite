# Lessons

One line per miss: date, class of miss, the mechanism that now catches it.

- 2026-09-18: a non-security `hashlib.md5` (a content fingerprint) reached master as bandit B324 High, because bandit runs only in the CI security job and never in pre-push; the fingerprint now says `usedforsecurity=False`, and `repo-template` 49e92b3 adds the `bandit-medium-plus` pre-commit hook (reaches figcite on the next `.pre-commit-config.yaml` sync).
