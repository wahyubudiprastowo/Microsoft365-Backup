# Public Source Review

Review date: 14 October 2026.

The public release contains application source, synthetic examples, developer
tests, and documentation. Runtime credentials, production configuration,
customer-specific operational reports, log files, backup data, historical ZIP
patches, and obsolete deployment archives are excluded.

The release is prepared as a fresh Git history so excluded artifacts and
environment-specific commit metadata are not ancestors of the public branch.
The original local repository history is preserved privately for recovery.

## Verification

- Scan publishable files against local configured secret values and tenant
  identifiers without printing those values.
- Scan the complete release history with a pinned Gitleaks image.
- Check file names and contents for runtime data, credential files, archives,
  private keys, and organization-specific identifiers.
- Generate README screenshots from application templates using synthetic demo
  data; generate the GIF and MP4 from those screenshots.
- Validate Compose, documentation links, and the application regression suite.
- Run publication-scanner tests and verify the local push gate.

Public environment examples leave credential values blank. Compose requires
an explicit Flask key and admin password. Configuration examples use
placeholders. Production configuration must be provided through runtime mounts.

This review covers source publication. It does not certify the application as
penetration-tested, rotate credentials in a running deployment, or remove
objects from a hosting provider's caches, forks, or previously downloaded
copies. Follow the secret-rotation procedure in [SECURITY.md](../SECURITY.md)
for any credential that has already been exposed.
