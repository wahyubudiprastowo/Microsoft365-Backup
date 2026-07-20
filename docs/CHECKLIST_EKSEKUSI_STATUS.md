# Checklist Eksekusi Status

Tanggal update: 2026-07-20  
Scope: backend, frontend, flow operator, readiness produksi

## Backend Broken

- [~] Legacy backup history lama kini tidak lagi ikut `active tenant`, tetapi backup flat historis yang belum punya manifest masih ditandai sebagai `legacy-default`
- [ ] Reload schedule belum benar-benar mengubah proses `celery-beat` yang sedang jalan; masih perlu restart container beat
- [ ] Per-tenant notification test belum async dan belum sekuat global settings test flow
- [x] Worker/web/beat tidak lagi berjalan sebagai `root`
- [ ] Validasi end-to-end remote upload ke destination nyata belum selesai

## Frontend Broken

- [ ] Halaman backup/history tenant-aware masih bisa misleading untuk backup layout `legacy` karena atribusi tenant dari backend belum akurat
- [ ] UX `Reload Beat` sebelumnya memberi kesan schedule sudah aktif; perlu terus dijaga tetap eksplisit sebagai `staged only`
- [ ] Failure-state lintas halaman belum sepenuhnya seragam untuk seluruh kontrak backend terbaru

## Partial But Usable

- [~] SharePoint backup legacy: usable, progres scan lebih jujur, tetapi benchmark tenant besar belum ada
- [~] OneDrive backup: engine ada, tetapi readiness tenant nyata masih bergantung pada permission Graph
- [~] Outlook backup: engine ada, tetapi readiness tenant nyata masih bergantung pada permission Graph
- [~] Teams backup/export: usable untuk export/archive, bukan restore native Teams messages
- [~] Restore v2: usable dengan preview/preflight lebih baik, tetapi validasi end-to-end tenant nyata belum lengkap
- [~] Settings global: usable, tetapi raw config editor masih terlalu dekat dengan flow operasional

## Sudah Relatif Matang

- [x] Tenant CRUD dan activation
- [x] Dashboard/task stale cleanup
- [x] Global settings validation untuk remote destination dan notification
- [x] Modal tenant dan modal schedule yang sebelumnya gelap/tidak bisa diinteraksikan
- [x] Label boundary utama `legacy` vs `modern` di surface inti

## Prioritas Eksekusi Disarankan

### P0

- [~] Betulkan atribusi backup `legacy` agar history/filter tenant tidak misleading
- [x] Hilangkan runtime `root` untuk web/worker/beat

### P1

- [x] Rapikan hasil test notification per-tenant agar status per-channel terlihat jelas
- [x] Tegaskan bahwa reload schedule hanya `staged` sampai `celery-beat` direstart
- [ ] Tambahkan smoke test operasional untuk backup history vs filesystem nyata

### P2

- [ ] Refactor settings agar raw JSON editor lebih terpisah dari mode standar
- [ ] Tambahkan matriks readiness fitur ke README publik
- [ ] Tambahkan benchmark backup/download tenant besar
