# Checklist Eksekusi Status

Tanggal update: 2026-07-28  
Scope: backend, frontend, flow operator, readiness produksi

## Backend Broken

- [~] Legacy backup history lama kini tidak lagi ikut `active tenant`; backup flat historis tanpa manifest sekarang di-infer ke tenant compatibility aktif, tetapi masih belum 100% forensik untuk kasus multi-tenant lama
- [ ] Reload schedule belum benar-benar mengubah proses `celery-beat` yang sedang jalan; masih perlu restart container beat
- [~] Per-tenant notification dan tenant test hasilnya lebih jujur, tetapi flow test operasionalnya belum async/terstandar penuh
- [x] Worker/web/beat tidak lagi berjalan sebagai `root`
- [~] Remote upload source/path handling dan destination write-check sudah membaik, tetapi validasi end-to-end ke destination nyata belum selesai
- [~] Boundary runtime restore legacy (`/api/restore/site`, `/api/restore/jobs`) vs Restore V2 sudah dialiaskan ke flow modern, tetapi endpoint compatibility masih coexist
- [x] Hardening queue/retry/lease sudah masuk source dan runtime worker aktif lagi setelah refresh aman pada Selasa, 28 Juli 2026 saat tidak ada task backup/download yang sedang berjalan
- [x] Backup queue recovery pasca web restart kini bisa auto-dispatch lagi lewat polling status/overview, sehingga antrean backup tidak lagi diam selamanya hanya karena task lama sudah mati
- [~] Hardening pause/resume modern Graph download sudah aktif di runtime worker terbaru, tetapi masih perlu uji end-to-end tambahan untuk file Graph besar dan interruption jaringan nyata
- [~] Masih ada alias teknis `/restore-v2`, tetapi metadata rekomendasi operator kini diarahkan ke `/restore`

## Frontend Broken

- [~] Halaman backup/history tenant-aware masih perlu kehati-hatian untuk backup layout `legacy`; owner kini bisa di-infer dari tenant compatibility, tetapi backup lama tanpa manifest belum setara dengan metadata native tenant-aware
- [x] Halaman backup/history tenant-aware kini collapse split runs menjadi satu project row per tenant/workload, lengkap dengan status badge, summary, local backup path copy action, dan penanda owner legacy hasil inferensi
- [x] Halaman backup/history kini tidak lagi menggantung lama karena endpoint history/stats dan polling task aktif sudah diringankan; smoke test web runtime `2026-07-28` memberi respons sekitar `~2s` untuk history dan cepat untuk polling aktif/queue
- [x] Halaman backup/history kini punya bulk checklist + bulk delete serta kontrol `Pause`/`Resume`/`Cancel` untuk backup aktif
- [x] Halaman backup/history kini membedakan project `completed`, `resume available`, `retry recommended`, dan `hard failed`, plus CTA `Resume / Retry` yang menjalankan flow yang sama tanpa memecah folder proyek lagi
- [x] Panel progress dashboard dan history kini bisa menampilkan progres workload modern secara lebih jujur dengan normalisasi `progress_total/progress_done/current_site`, sehingga backup OneDrive aktif tidak lagi terlihat `0 / 0` saat live size terus bertambah
- [x] Dashboard dan halaman backup kini mengecek task aktif secara periodik, jadi panel progress bisa muncul lagi tanpa perlu reload manual saat backup baru mulai setelah halaman lama terbuka
- [x] Dashboard dan halaman backup kini menyimpan cache panel progres terakhir di browser dan memakai fallback speed turunan dari delta byte, jadi reload halaman tidak lagi langsung blank saat task masih aktif walau `speed_human` backend belum siap
- [ ] UX `Reload Beat` sebelumnya memberi kesan schedule sudah aktif; perlu terus dijaga tetap eksplisit sebagai `staged only`
- [ ] Failure-state lintas halaman belum sepenuhnya seragam untuk seluruh kontrak backend terbaru
- [x] Halaman tenant kini menampilkan warning hasil test Graph secara cukup actionable
- [~] Halaman workload kini sudah operasional untuk toggle, target scope, dan trigger backup modern, tetapi masih perlu validasi live lebih luas pada tenant/target nyata
- [~] Branding `Restore` di UI sudah dirapikan; alias teknis `Restore V2` masih dipertahankan untuk kompatibilitas
- [x] Settings tidak lagi memunculkan schedule card kedua; surface utama scheduling kini hanya di `/schedules`
- [x] Remote destination save/test flow kini tervalidasi lebih kuat di UI dan tidak lagi bergantung pada modal instance yang rawan macet

## Partial But Usable

- [~] SharePoint backup legacy: usable, progres scan lebih jujur, dan kini bisa ditahan pada satu folder proyek kerja yang sama pada run berikutnya; benchmark tenant besar masih belum ada
- [~] Resume operator untuk history backup kini lebih jelas di UI dan runtime worker terbaru sudah aktif, tetapi masih perlu lebih banyak bukti end-to-end untuk interruption multi-run tenant besar
- [~] OneDrive backup: engine ada, checkpoint antar-run kini lebih matang karena folder kerja terakhir tetap dipakai sebagai project folder; source juga sudah mulai memfilter guest/disabled user, menandai drive `404/423` sebagai target unavailable, memakai cache token Graph, dan memprioritaskan `@microsoft.graph.downloadUrl`, tetapi readiness tenant nyata masih bergantung pada permission Graph dan benchmark throughput live masih perlu dibuktikan
- [~] OneDrive backup aktif pada `2026-07-28` kini sudah terbaca sebagai satu project merge dengan status/progress yang benar di web runtime, tetapi respons pause per-file Graph besar tetap perlu uji tambahan
- [~] Outlook backup: engine ada, checkpoint antar-run kini lebih matang karena folder kerja terakhir tetap dipakai sebagai project folder, tetapi readiness tenant nyata masih bergantung pada permission Graph
- [~] Teams backup/export: usable untuk export/archive, checkpoint antar-run kini lebih matang dan project folder tidak lagi pecah per retry, tetapi bukan restore native Teams messages
- [~] Workload control surface: UI sudah bisa menyimpan target selection dan memicu backup modern, dan worker runtime terbaru sudah aktif; yang tersisa sekarang adalah validasi operasional lebih luas
- [~] Optimasi throughput Graph worker terbaru sudah aktif di runtime sejak Rabu, 29 Juli 2026 saat tidak ada task aktif: chunk download `4 MiB`, page size `999`, pool HTTP `64`, cache token Graph, direct download URL, dan flush manifest periodik; performa live tenant besar masih perlu benchmark tambahan
- [~] Restore modern: usable dengan preview/preflight lebih baik, daftar target library SharePoint live, auto-create folder, source tenant -> target tenant untuk cross-tenant copy, dan ringkasan operasi copy/restore yang lebih jelas; validasi end-to-end tenant nyata masih perlu diperluas
- [~] Restore modern: usable dengan preview/preflight lebih baik, daftar target library SharePoint live, auto-create folder, source tenant -> target tenant untuk cross-tenant copy, ringkasan operasi yang lebih jelas, dan metrics hasil job yang lebih mudah diaudit; validasi end-to-end tenant nyata masih perlu diperluas
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
- [x] Healthcheck `spo-backup-web` kini memakai probe lokal yang lebih stabil dan `start_period`, sehingga status container tidak mudah `unhealthy` palsu saat boot

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

- [x] Refactor settings agar raw JSON editor lebih terpisah dari mode standar
- [ ] Tambahkan matriks readiness fitur ke README publik
- [ ] Tambahkan benchmark backup/download tenant besar
- [~] Validasi tuning `GRAPH_DOWNLOAD_CHUNK_SIZE` baru sudah aktif di runtime worker produksi; benchmark throughput dan dampak tenant besar masih perlu diukur
- [x] Sinkronkan semua dokumen status dengan code/runtime terbaru
