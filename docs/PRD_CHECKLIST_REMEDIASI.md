# PRD Checklist Remediasi

Tanggal draft: 2026-07-09  
Product: `Microsoft 365 Backup`  
Project folder: `spo-backup-final`

## Tujuan Produk

- [ ] Menjadikan aplikasi stabil sebagai backup platform Microsoft 365 multi-tenant
- [ ] Menyatukan pengalaman legacy dan tenant-aware tanpa membingungkan user
- [ ] Menyediakan status fitur yang jujur: siap produksi, parsial, atau butuh permission tenant
- [ ] Mengurangi gap antara backend state dan frontend UX

## Non-Goals Sementara

- [ ] Tidak menghapus seluruh flow legacy dalam fase awal
- [ ] Tidak membangun import ulang pesan Teams native
- [ ] Tidak menambah provider storage baru sebelum fondasi stabil

## Definisi Sukses

- [ ] User bisa mengetahui dengan jelas workload mana yang benar-benar siap dipakai
- [ ] Backup yang gagal fatal selalu terlihat gagal di UI
- [ ] Multi-workload backup bisa upload hasil yang tepat ke remote destination
- [ ] Schedule tidak punya dua source of truth yang membingungkan
- [ ] Permission issue dibedakan jelas dari bug aplikasi

## Persona Utama

- [ ] Admin infra/self-hosting
- [ ] Operator backup harian
- [ ] Admin Microsoft 365 / Azure App registration

## Problem Statement

- [ ] Produk sudah kaya fitur, tetapi banyak flow bertumpuk dari patch legacy dan modern
- [ ] Sebagian error sudah benar di backend, namun belum terpresentasi benar di UI
- [ ] Beberapa area produk belum punya contract yang tegas: state backup, scheduling, readiness workload, legacy-vs-modern navigation

## Scope PRD Fase 1: Stabilization

### A. Backup State & Execution

- [ ] Finalisasi state model task: `PROGRESS`, `SUCCESS`, `BACKUP_FAILED`, `REVOKED`, `UNKNOWN`
- [ ] Sinkronkan polling dashboard dan semua UI yang membaca status task
- [ ] Rapikan cleanup control state task selesai
- [ ] Pastikan remote upload memakai `backup_path` workload yang benar
- [ ] Progressive discovery harus menampilkan progress sejak fase scan awal
- [ ] Progress task download harus bisa dipulihkan setelah refresh halaman
- [ ] Statistik `downloaded/skipped/resumed` harus konsisten di dashboard, download page, dan backup history page

### B. Workload Readiness

- [ ] Tambahkan matriks readiness per workload di UI dan docs
- [ ] Tandai workload yang `implemented but permission-blocked`
- [ ] Tambahkan guidance scopes Graph per workload

### C. Product Navigation

- [~] Bedakan flow `legacy` dan `modern` di navigasi
- [~] Putuskan posisi `/restore` vs `/restore-v2`
- [x] Putuskan posisi global schedule vs per-tenant schedule

### D. Operational Safety

- [x] Jalankan container non-root
- [~] Review secret handling pada settings/raw editor
- [x] Tambahkan validasi operasional untuk remote destinations dan notification config

## Scope PRD Fase 2: Product Coherence

- [x] Satukan source of truth tenant slug
- [ ] Normalisasi struktur response API yang dibaca UI
- [ ] Refactor halaman settings menjadi section yang lebih aman
- [ ] Tambahkan health panel yang membedakan bug aplikasi, auth error, dan permission error
- [~] Sempitkan boundary restore legacy agar tidak sejajar diam-diam dengan Restore V2

## Scope PRD Fase 3: Production Readiness

- [ ] Uji end-to-end nyata per workload pada tenant dengan permission lengkap
- [ ] Tambahkan regression smoke suite
- [ ] Tambahkan restore validation matrix
- [ ] Tambahkan release checklist sebelum tagging versi publik

## Functional Requirements

### Backup Status UX

- [ ] UI dashboard harus mengenali semua terminal states
- [ ] UI harus menampilkan reason ketika task fatal
- [ ] User harus tahu apakah task gagal karena permission, config, atau error internal
- [ ] User harus bisa membedakan fase `scanning`, `downloading`, `paused`, dan `finishing`
- [ ] Refresh browser tidak boleh membuat operator kehilangan konteks task yang masih berjalan

### Backup History UX

- [ ] Semua workload aktif harus bisa difilter di history page
- [ ] Semua layout backup harus terbaca dan diberi label benar
- [ ] User harus bisa memahami backup milik tenant/workload mana tanpa ambiguity

### Scheduling UX

- [x] Hanya ada satu flow scheduling utama yang direkomendasikan
- [ ] Jika global schedule tetap ada, harus diberi label `legacy`
- [x] UI harus memberi tahu kebutuhan restart/reload dengan jelas

### Restore UX

- [x] Restore page harus menampilkan kesiapan tiap workload
- [ ] Dry-run harus bisa menjadi preflight validation utama
- [~] Error restore harus informatif untuk operator

### Settings UX

- [ ] Secret tidak tampil kembali secara plaintext
- [ ] Raw config editor diberi warning keras atau dipisahkan dari mode standar
- [ ] Remote destination test result harus spesifik dan actionable

## Technical Requirements

- [ ] Satu util state/status dipakai lintas dashboard, backup API, dan worker
- [ ] Satu util slug tenant dipakai lintas backend dan frontend-facing API
- [ ] Background task post-processing memakai output workload nyata, bukan path asumsi
- [ ] Logging membedakan auth failure, permission failure, validation failure, dan internal exception
- [ ] Engine backup/download mendukung skip unchanged, resume partial `.tmp`, dan progressive byte estimation
- [x] Tenant test UI harus memantulkan warning backend secara penuh, bukan hanya toast ringkas

## Acceptance Checklist

### P0 Acceptance

- [ ] Backup fatal tampil sebagai gagal di dashboard tanpa stuck
- [ ] `control_state` task selesai tidak lagi misleading
- [ ] Remote upload bekerja benar untuk backup tenant-aware multi-workload

### P1 Acceptance

- [ ] Backup history mendukung `teams`
- [x] Schedule product story jelas: global vs tenant
- [ ] Docs fitur dan readiness terbarui
- [~] Restore legacy vs Restore V2 boundary tidak lagi membingungkan operator

### P2 Acceptance

- [x] Slug tenant konsisten penuh
- [~] Legacy vs modern UI lebih jelas
- [x] Security warning root-run hilang

## Deliverables

- [ ] Perbaikan code backend state/task/upload
- [ ] Perbaikan UI dashboard/backups/settings/navigation
- [ ] Dokumen readiness fitur
- [ ] Dokumen permission Microsoft Graph
- [ ] Smoke test checklist operasional

## Checklist Eksekusi Implementasi

### Sprint 1

- [x] Perbaiki dashboard state handling
- [x] Perbaiki cleanup/state presentasi task terminal
- [x] Perbaiki remote upload path resolution
- [x] Tambahkan workload `teams` di backup history filter
- [ ] Validasi remote upload dengan destinasi nyata

### Sprint 2

- [x] Putuskan dan labeli source of truth scheduling
- [x] Labeli global schedule sebagai compatibility flow
- [x] Tambahkan readiness guidance dasar per workload di restore UI
- [x] Tambahkan permission guidance minimum di tenant/workload UI
- [x] Pindahkan tenant slug utama ke backend response untuk flow restore v2
- [x] Rapikan validasi preview restore agar error input menjadi `400` yang manusiawi
- [x] Bersihkan backup phantom akibat cache registry basi
- [x] Rapikan contract API workload target discovery untuk error permission/admin consent
- [x] Normalisasi parser URL download untuk URL SharePoint view-style

### Sprint 3

- [ ] Rapikan settings/raw editor
- [x] Samakan tenant slug contract
- [x] Jalankan container non-root
- [ ] Tambahkan benchmark backup/download tenant besar dan tuning throughput
- [~] Perjelas label flow modern vs compatibility di navigasi utama
- [~] Rapikan boundary restore legacy vs Restore V2

## Dependency Checklist

- [ ] Akses tenant Microsoft 365 dengan admin consent yang benar
- [ ] App registration Azure dengan scopes lengkap
- [ ] Host storage untuk backup dan remote upload
- [ ] Waktu uji end-to-end restore pada tenant non-produksi

## Risiko Utama

- [ ] Patch compatibility lama vs refactor baru
- [ ] Tenant permission berbeda-beda antar environment
- [ ] UI tetap hidup tetapi contract backend berubah jika tidak disinkronkan penuh
- [ ] Operator mengubah raw JSON dan merusak config tenant-aware

## Catatan Implementasi Berikutnya

- [ ] Gunakan dokumen `AUDIT_CHECKLIST_TEMUAN.md` sebagai sumber truth issue
- [ ] Tutup P0 dulu sebelum menambah fitur baru
