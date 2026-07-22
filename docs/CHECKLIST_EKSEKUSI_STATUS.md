# Checklist Eksekusi Status

Tanggal update: 2026-07-21  
Scope: backend, frontend, flow operator, readiness produksi

## Backend Broken

- [~] Legacy backup history lama kini tidak lagi ikut `active tenant`, tetapi backup flat historis yang belum punya manifest masih ditandai sebagai `legacy-default`
- [ ] Reload schedule belum benar-benar mengubah proses `celery-beat` yang sedang jalan; masih perlu restart container beat
- [~] Per-tenant notification dan tenant test hasilnya lebih jujur, tetapi flow test operasionalnya belum async/terstandar penuh
- [x] Worker/web/beat tidak lagi berjalan sebagai `root`
- [~] Remote upload source/path handling dan destination write-check sudah membaik, tetapi validasi end-to-end ke destination nyata belum selesai
- [~] Boundary runtime restore legacy (`/api/restore/site`, `/api/restore/jobs`) vs Restore V2 sudah dialiaskan ke flow modern, tetapi endpoint compatibility masih coexist
- [~] Hardening queue/retry/lease sudah masuk source dan image terbaru, tetapi worker runtime baru aktif penuh setelah `spo-backup-worker` direstart aman
- [~] Masih ada alias teknis `/restore-v2`, tetapi metadata rekomendasi operator kini diarahkan ke `/restore`

## Frontend Broken

- [ ] Halaman backup/history tenant-aware masih bisa misleading untuk backup layout `legacy` karena atribusi tenant dari backend belum akurat
- [ ] UX `Reload Beat` sebelumnya memberi kesan schedule sudah aktif; perlu terus dijaga tetap eksplisit sebagai `staged only`
- [ ] Failure-state lintas halaman belum sepenuhnya seragam untuk seluruh kontrak backend terbaru
- [x] Halaman tenant kini menampilkan warning hasil test Graph secara cukup actionable
- [~] Halaman workload kini sudah operasional untuk toggle, target scope, dan trigger backup modern, tetapi validasi live via worker baru penuh setelah restart worker aman
- [~] Branding `Restore` di UI sudah dirapikan; alias teknis `Restore V2` masih dipertahankan untuk kompatibilitas

## Partial But Usable

- [~] SharePoint backup legacy: usable, progres scan lebih jujur, tetapi benchmark tenant besar belum ada
- [~] OneDrive backup: engine ada, tetapi readiness tenant nyata masih bergantung pada permission Graph
- [~] Outlook backup: engine ada, tetapi readiness tenant nyata masih bergantung pada permission Graph
- [~] Teams backup/export: usable untuk export/archive, bukan restore native Teams messages
- [~] Workload control surface: UI sudah bisa menyimpan target selection dan memicu backup modern, tetapi worker aktif belum direfresh saat task SharePoint masih berjalan
- [~] Restore modern: usable dengan preview/preflight lebih baik, daftar target library SharePoint live, dan auto-create folder; validasi end-to-end tenant nyata masih perlu diperluas
- [~] Restore compatibility API lama masih ada untuk SharePoint, tetapi bukan lagi flow produk yang direkomendasikan
- [~] Settings global: usable, tetapi raw config editor masih terlalu dekat dengan flow operasional

## Sudah Relatif Matang

- [x] Tenant CRUD dan activation
- [x] Dashboard/task stale cleanup
- [x] Global settings validation untuk remote destination dan notification
- [x] Modal tenant dan modal schedule yang sebelumnya gelap/tidak bisa diinteraksikan
- [x] Halaman workload kini menjelaskan integrasi backup modern dengan cukup jelas
- [x] Label boundary utama `legacy` vs `modern` di surface inti
- [x] Tenant test UI kini memantulkan warning backend secara inline
- [x] Remote destination test kini mengecek path dan write access lebih jelas di UI/backend

## Prioritas Eksekusi Disarankan

### P0

- [~] Betulkan atribusi backup `legacy` agar history/filter tenant tidak misleading
- [x] Hilangkan runtime `root` untuk web/worker/beat

### P1

- [x] Rapikan hasil test notification per-tenant agar status per-channel terlihat jelas
- [x] Tegaskan bahwa reload schedule hanya `staged` sampai `celery-beat` direstart
- [ ] Tambahkan smoke test operasional untuk backup history vs filesystem nyata
- [~] Rapikan boundary restore modern vs compatibility alias di route dan docs
- [~] Validasi remote upload nyata ke SMB/SFTP/WebDAV dan aktifkan patch worker-side setelah backup selesai

### P2

- [ ] Refactor settings agar raw JSON editor lebih terpisah dari mode standar
- [ ] Tambahkan matriks readiness fitur ke README publik
- [ ] Tambahkan benchmark backup/download tenant besar
- [x] Sinkronkan semua dokumen status dengan code/runtime terbaru
