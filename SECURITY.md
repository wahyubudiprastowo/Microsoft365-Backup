# Security and Publication

Deploy the active application from `spo-backup-final/`. Historical patch bundles,
obsolete source archives, operational notes, and runtime data are excluded from
public releases.

## Before Publishing

Run these checks from the repository root:

```bash
python3 scripts/audit_publication.py
python3 scripts/audit_publication.py --history
python3 scripts/audit_publication.py --history-ref HEAD
python3 scripts/test_publication_security.py
git diff --check
```

The publication scanner checks tracked and non-ignored working files, optionally
all locally reachable Git history, prohibited runtime paths, private-key headers,
and exact matches against locally configured secrets and tenant identifiers.
It inspects nested ZIP content and reports locations without secret values.
Invalid runtime JSON or archive-limit errors stop the scan rather than silently
skipping the input. Keep runtime configuration local so exact-match checks can
work; CI supplements these checks with Gitleaks.

Enable the checked-in local push hook:

```bash
chmod +x .githooks/pre-push
git config core.hooksPath .githooks
```

The hook checks publishable working files and the history of each ref being
pushed, including commit metadata, and runs a pinned Gitleaks Docker image.
Private backup refs that are not pushed are outside that push's scope.
Docker and Python 3 must be available. Git hooks
can be bypassed, so also require the `Secret Scan` workflow on protected branches.
CI runs after publication; it cannot prevent the initial upload of a leaked
secret. The local pre-push check is therefore important.

Private organization names can be added to local Git configuration using
`git config --add publication.denyTerm 'private-organization-name'`. They are
checked case-insensitively without being printed in reports. A clean publication
checkout can compare against a separate private runtime using
`--runtime-root /path/to/private/runtime`, or local `publication.runtimeRoot`.
These settings remain outside the public source.

## Runtime Secrets

Set a unique `SECRET_KEY` and admin password in local `.env`. Compose rejects
blank values. Never reuse public example values. Keep tenant client secrets,
SMTP passwords, bot tokens, webhook URLs, certificates, token caches, and backup
payloads out of Git and Docker build contexts.

Ignore rules do not remove files from old commits. Deleting a secret in a new
commit does not erase its exposure. Rotate exposed credentials first, then
clean affected history or publish a reviewed clean snapshot in a fresh
repository. Rewriting shared history requires coordinated changes; audit work
does not automatically force-push or rewrite existing branches.

For public release verification, see [the publication review](docs/SECURITY_REVIEW.md).

## Reporting a Vulnerability

Use the repository's private vulnerability-reporting channel if enabled. Do not
post active secrets or customer data in a public issue. Provide a minimal
reproduction with synthetic credentials and redact logs and screenshots.
