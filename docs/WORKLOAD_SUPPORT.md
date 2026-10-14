# Workload Support

| Workload | Backup | Recovery |
| --- | --- | --- |
| SharePoint | Selected sites, document libraries, files and folders | File/folder upload to selected site, library and folder |
| OneDrive | Discovered or selected user drives, files and folders | User mapping and nested destination folders |
| Outlook | Accessible cloud mailbox messages, file attachments, calendar and contacts | Supported items recreated using Graph write permissions |
| Teams | Team metadata, members, owners, channels, messages, replies, tabs, apps and channel files | Local HTML, JSON and text archive export |

Teams does not currently support native import or private-chat backup. Outlook
restore does not recreate the original mailbox folder hierarchy or provide
full-fidelity mailbox recovery; direct file-attachment restore supports files
below 3 MB. Unsupported attachment types and failed items are reported.

SharePoint site estimates use Graph drive quota, not exact SharePoint Admin
storage accounting. Deleted, renamed, or inaccessible sites may have no current
estimate. An unavailable size is an error or unknown state, not proof of zero
data or proof that a local backup is missing.

## Validation in Your Tenant

1. Configure the required application permissions and grant admin consent.
2. Select one test site, user, mailbox, or team and run a backup.
3. Compare file inventories and byte counts with the source.
4. Run restore preflight and review destination/user mapping.
5. Restore into a dedicated test destination and inspect the recovered data.
6. Test interruption, resume, missing-permission, and partial-failure behavior.

Automated tests exercise transfer checks, queue visibility, workload handling,
remote upload behavior, and recovery paths using mocks and temporary files.
They do not establish tenant-wide recovery readiness.
