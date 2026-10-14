# Remote Storage

Backup data is stored locally. Enabled remote destinations can receive an
additional copy of the matching workload directory after backup.

| Protocol | Authentication / verification |
| --- | --- |
| SMB/CIFS | Share credentials; temporary file write probe and returned byte count |
| FTP/FTPS | Server credentials; temporary write probe; SIZE check when supported |
| SFTP/SSH | Password or mounted private key; trusted host key or SHA256 fingerprint; confirmed transfer |
| WebDAV | Endpoint credentials; temporary write probe; HEAD Content-Length check when exposed |

Each destination has a workload checklist. Upload results distinguish success,
partial failure, and failure. Remote paths reject parent traversal, and WebDAV
paths encode file names. Write tests create and remove a small probe file.

Private-key paths refer to files inside the worker container. Mount keys
read-only and keep them outside Git and the image build context. FTP without
TLS is unencrypted; use FTPS or SFTP for protected transport.

Remote upload currently copies the selected workload backup directory. It does
not implement per-file selection, incremental remote synchronization, a
separate remote-upload queue, or direct remote restore. Size verification
depends on server capabilities and is not a cryptographic integrity guarantee.
