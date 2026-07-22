# Audit Checklist Temuan

Tanggal audit: 2026-07-22  
Product: `Microsoft 365 Backup`  
Project folder: `spo-backup-final`  
Scope: codebase, UI, feature flow, backend foundation, frontend foundation, deployment surface

## Ringkasan Status

- [x] Container stack bisa build dan start normal
- [x] Halaman utama utama (`/`, `/tenants`, `/workloads`, `/backups`, `/restore-v2`, `/schedules`, `/settings`) merespons `200`
- [x] API inti (`/api/health`, `/api/tenants`, `/api/workloads`, `/api/v2/backups`, `/api/v2/schedules`, `/api/v2/restore/jobs`) merespons `200`
- [ ] Semua flow UI sudah sinkron dengan state backend terbaru
- [ ] Semua workload production-ready di tenant nyata
- [ ] Fondasi observability, security, dan UX failure-state sudah matang

## Metode Audit

- [x] Review struktur file dan dokumen repo
- [x] Review backend route/task/workload/restore flow
- [x] Review template HTML dan JavaScript utama
- [x] Smoke test page dan API
- [x] Review log container web dan worker

## Temuan Kritis

### 1. Dashboard belum memahami state backup baru `BACKUP_FAILED`

- [x] Perbaiki polling dashboard agar menangani `BACKUP_FAILED`
- [x] Tampilkan error fatal dari backend ke user
- [x] Hentikan polling dengan state terminal yang konsisten

Detail:
- Backend status endpoint sudah mengembalikan state `BACKUP_FAILED` di [spo-backup-final/app/main.py](../spo-backup-final/app/main.py#L370)
- Dashboard hanya menangani `PROGRESS`, `SUCCESS`, `FAILURE`, dan `REVOKED` di [spo-backup-final/app/templates/dashboard.html](../spo-backup-final/app/templates/dashboard.html#L156)
- Akibatnya backup yang gagal fatal bisa tidak terlihat selesai dengan benar di UI

### 2. State kontrol task bisa tetap terbaca `running` setelah task selesai fatal

- [x] Pastikan state kontrol dibersihkan atau diberi terminal state final untuk `BACKUP_FAILED`
- [x] Hindari UI menerima kombinasi `state=BACKUP_FAILED` tetapi `control_state=running`

Detail:
- Task menyimpan `BACKUP_FAILED` di [spo-backup-final/app/tasks.py](../spo-backup-final/app/tasks.py#L327)
- Status endpoint tetap membaca `TaskController.get_state()` untuk state non-success di [spo-backup-final/app/main.py](../spo-backup-final/app/main.py#L385)
- Dari hasil tes, task fatal masih bisa mengembalikan `control_state: running`

### 3. Remote upload multi-workload berpotensi memilih folder backup yang salah

- [x] Ubah post-processing upload supaya upload per workload memakai `backup_path` nyata dari workload yang baru selesai
- [x] Hindari fallback ke `custom_root` atau `config["backup"]["root_dir"]` untuk workload tenant-aware lain
- [x] Pertahankan struktur folder backup pada remote destination agar run/workload tidak bercampur di `remote_path` yang sama
- [x] Perkuat remote destination test agar memeriksa `path readiness` dan `write access`, bukan hanya koneksi dasar
- [ ] Validasi end-to-end dengan remote destination nyata
- [ ] Restart `spo-backup-worker` setelah task aktif selesai agar patch post-backup upload terbaru aktif di worker runtime

Detail:
- Upload remote masih mencari folder dari `Path(custom_root or config["backup"]["root_dir"])` di [spo-backup-final/app/tasks.py](../spo-backup-final/app/tasks.py#L279)
- Untuk backup `onedrive`, `outlook`, atau `teams`, lokasi backup nyata sekarang tenant-aware dan workload-specific
- Risiko: upload mengirim backup workload yang salah atau tidak menemukan folder terbaru

Update:
- Worker task kini menghitung `remote_subpath` relatif terhadap root backup lokal, sehingga upload ke SMB/SFTP/WebDAV/FTP mempertahankan struktur seperti `m365/<tenant>/<workload>/<backup>` alih-alih menumpahkan isi backup langsung ke root destination di [spo-backup-final/app/tasks.py](../spo-backup-final/app/tasks.py#L348)
- Modul uploader sekarang mengembalikan `remote_path` efektif dan test per protocol mencoba memastikan destination path bisa dibuat/ditulis dengan probe directory sementara di [spo-backup-final/app/uploader.py](../spo-backup-final/app/uploader.py#L14)
- Halaman settings kini juga punya tombol test langsung pada destination yang sudah tersimpan dan menampilkan status `Connectivity`, `Path`, dan `Write` secara inline di [spo-backup-final/app/templates/settings.html](../spo-backup-final/app/templates/settings.html#L231)
- Pada environment audit saat ini, `config/config.json` masih belum punya remote destination aktif, jadi validasi nyata ke NAS/SFTP/WebDAV produksi belum bisa dicentang

## Temuan Tinggi

### 4. Backup panjang masih rentan saat broker/worker restart atau Redis sempat tidak reachable

- [x] Tangani `/api/task/active` agar tidak `500` saat Redis tidak reachable
- [x] Kurangi toast frontend berulang saat tracked task sudah stale
- [x] Simpan metadata backup parsial sebagai `interrupted` bila task terputus di tengah jalan
- [x] Ubah konfigurasi worker agar task backup/download long-running lebih aman untuk redelivery setelah connection loss
- [ ] Uji chaos sederhana: restart `redis` dan `worker` saat backup aktif lalu verifikasi resume/checkpoint

Detail:
- Pada Senin, 20 Juli 2026 sekitar `15:04 WIB`, worker kehilangan koneksi broker Redis dan task tracker UI mengembalikan `Tracked backup task is no longer running on the worker`
- Log worker menunjukkan `Connection closed by server`, lalu `Connection refused` dan `Name or service not known` ke `redis:6379`
- Ini menjelaskan kenapa backup SharePoint terasa "tiba-tiba putus" walau folder backup parsial di disk masih ada

### 5. Legacy SharePoint backup belum punya resume/snapshot semantics yang matang

- [x] Identifikasi akar masalah folder resume yang membuat run lanjutan bikin `backup_<timestamp>` baru
- [x] Identifikasi mismatch manifest global per-site vs folder backup per-run
- [x] Tambahkan logika source code agar resume mengutamakan folder legacy yang belum selesai
- [x] Tambahkan fallback materialize file `unchanged` dari backup sebelumnya bila folder baru tetap dipakai
- [ ] Verifikasi end-to-end setelah restart service bahwa resume benar-benar lanjut di folder existing
- [ ] Audit restore legacy terhadap backup incremental lama yang sudah telanjur tersebar ke beberapa folder

Detail:
- Pada Senin, 20 Juli 2026, backup aktif `5017f331...` membuat folder baru `backup_20260720_082554` alih-alih melanjutkan `backup_20260720_080053`
- Manifest global `backupsite.json` masih menunjuk path file ke folder lama `backup_20260720_080053`
- Risiko tertingginya bukan hanya resume gagal, tetapi snapshot backup legacy bisa tidak self-contained jika file unchanged di-skip tanpa dimaterialisasi ke folder target run baru

### 6. Halaman backup belum sepenuhnya mendukung workload `teams`

- [x] Tambahkan opsi filter `teams`
- [x] Tambahkan style badge `teams`
- [ ] Audit semua label workload agar sinkron dengan registry

Detail:
- Filter workload di [spo-backup-final/app/templates/backups.html](../spo-backup-final/app/templates/backups.html#L53) hanya memuat `sharepoint`, `onedrive`, `outlook`
- CSS badge workload di file yang sama juga belum punya class `wl-teams`
- Backend sudah mendukung `teams` di registry/API

### 5. Ada dua pusat pengaturan schedule: global legacy vs per-tenant

- [x] Definisikan satu source of truth produk untuk scheduling
- [x] Tandai global schedule sebagai deprecated jika memang tidak lagi utama
- [x] Pastikan user tidak bingung antara `/settings` dan `/schedules`

Detail:
- `Schedules` sekarang menjadi flow utama dan satu-satunya surface edit schedule yang direkomendasikan di [spo-backup-final/app/templates/tenant_schedule.html](../spo-backup-final/app/templates/tenant_schedule.html#L1)
- `Settings` kini hanya menampilkan ringkasan `Legacy Global Schedule` read-only dan CTA ke `/schedules` di [spo-backup-final/app/templates/settings.html](../spo-backup-final/app/templates/settings.html#L29)
- API global schedule tetap dipertahankan untuk kompatibilitas, tetapi tidak lagi diekspos sebagai editor utama di UI

### 6. Restore v2 masih bergantung pada slugify versi frontend

- [x] Samakan slug tenant dari frontend dengan slug backend
- [x] Backend mengembalikan slug tenant resmi untuk dipakai UI

Detail:
- `restore_v2` kini memakai `tenant_slug` langsung dari response API tenant di [spo-backup-final/app/templates/restore_v2.html](../spo-backup-final/app/templates/restore_v2.html#L227)
- Response `GET/POST/PUT /api/tenants` dan `POST /api/tenants/<id>/activate` kini konsisten membawa `tenant_slug` resmi dari backend di [spo-backup-final/app/main_routes.py](../spo-backup-final/app/main_routes.py#L97)
- Backend resmi memakai `slugify_tenant` dari registry

### 7. Workload target discovery nyata masih gagal karena permission Graph tenant aktif belum cukup

- [ ] Audit permission Azure App untuk `User.Read.All`, `Files.Read.All`, `Mail.Read`, `Calendars.Read`, `Contacts.Read`, dan Teams scopes
- [ ] Verifikasi admin consent tenant aktif
- [x] Selaraskan daftar scope minimum yang ditampilkan UI tenant dengan workload yang didukung
- [x] Bedakan error permission vs error implementasi di UI pada halaman workload
- [x] Rapikan contract API target discovery agar tidak menyamarkan error sebagai daftar target kosong
- [x] Terapkan pola error yang sama ke flow restore dan flow lain yang relevan

Detail:
- Tes `GET /api/workloads/onedrive/targets` dan `GET /api/workloads/outlook/targets` mengembalikan `403` ke `/users`
- Sebelumnya `teams` juga gagal `403`
- Ini berarti readiness production belum tuntas walau implementasi code sudah ada

Update:
- API workload discovery kini mengembalikan `error_type`, `error_detail`, dan `required_scopes`
- UI workload kini bisa menampilkan panel `Microsoft Graph Access Blocked` yang lebih actionable
- Preview restore kini juga mengembalikan `permission_preflight` per tenant/workload agar warning scope muncul sebelum eksekusi restore

### 7a. Halaman workload sebelumnya hanya menampilkan discovery, belum menjadi control surface backup modern

- [x] Jelaskan integrasi tiap workload dan cara backup modern dijalankan
- [x] Tambahkan aksi `Run Enabled Workloads` dan `Run This Workload`
- [x] Tambahkan target scope selection untuk `OneDrive`, `Outlook`, dan `Teams`
- [x] Tegaskan bahwa `SharePoint` mengikuti daftar site aktif dari menu `Sites`
- [~] Aktifkan runtime worker image terbaru setelah backup aktif selesai agar target selection berlaku penuh pada job berikutnya

Detail:
- Sebelumnya halaman [spo-backup-final/app/templates/workloads.html](../spo-backup-final/app/templates/workloads.html#L1) hanya punya toggle dan `View Targets`, sehingga operator tidak tahu backup dijalankan dari mana atau target mana yang benar-benar ikut
- Backend modern sebenarnya sudah menerima daftar workload di `POST /api/v2/backup/start`, tetapi tidak ada CTA yang menghubungkannya dari UI workload
- Engine `OneDrive` / `Outlook` / `Teams` juga sebelumnya selalu memproses semua target hasil discovery tanpa mekanisme subset yang tersimpan per tenant

Update:
- UI workload kini menampilkan guidance integrasi, badge status inclusion, CTA untuk menjalankan backup modern, dan modal `Configure Scope` untuk menyimpan target pilihan di [spo-backup-final/app/templates/workloads.html](../spo-backup-final/app/templates/workloads.html#L1)
- Backend kini menyimpan `workload_target_selection` per tenant dan mengekspos endpoint `POST /api/workloads/<wtype>/selection` di [spo-backup-final/app/main_routes.py](../spo-backup-final/app/main_routes.py#L307)
- Engine `OneDrive`, `Outlook`, dan `Teams` kini menghormati mode `all` vs `selected` saat memilih target backup di [spo-backup-final/app/workloads/base.py](../spo-backup-final/app/workloads/base.py#L80)
- Web runtime sudah memakai image baru, tetapi worker masih sengaja belum direstart saat audit ini karena task backup aktif `5017f331-08f9-4382-91be-589fdd00466e` masih berjalan

## Temuan Menengah

### 8. Halaman restore v2 belum mengkomunikasikan readiness workload secara eksplisit

- [x] Tambahkan status readiness per workload di UI restore
- [x] Tampilkan warning jika backup source ada tapi permission target tenant tidak cukup
- [x] Tampilkan validasi restore yang lebih manusiawi untuk input/payload yang tidak valid

Detail:
- UI restore v2 menampilkan semua workload setara di [spo-backup-final/app/templates/restore_v2.html](../spo-backup-final/app/templates/restore_v2.html#L15)
- Saat ini tidak ada indikator bahwa success restore sangat bergantung pada permission write Graph dan struktur backup nyata

Update:
- UI restore kini menampilkan hint readiness per workload
- Preview restore kini mengembalikan `400` untuk field wajib yang hilang dan validasi target SharePoint yang belum diisi
- Preview juga memberi warning saat backup terlihat kosong atau workload Teams bersifat export-only
- Preview restore kini menyisipkan warning permission tenant target berdasarkan `permission_preflight` dan daftar scope yang dibutuhkan

### 9. Notification dan remote destination masih dominan berbasis config mentah

- [x] Tambahkan validasi field yang lebih kuat di backend
- [x] Tambahkan masking dan edit-flow yang lebih aman untuk secret
- [x] Tambahkan error message yang lebih spesifik per protocol/channel

Detail:
- Halaman settings masih mencampur konfigurasi operasional dan raw JSON editor dalam satu page di [spo-backup-final/app/templates/settings.html](../spo-backup-final/app/templates/settings.html#L90)
- Flow ini kuat untuk admin teknis, tetapi raw untuk user operasional

Update:
- Backend `POST /api/remote-destinations` dan `POST /api/remote-destinations/test` kini memvalidasi field wajib per protocol (`smb`, `ftp`, `sftp`, `webdav`) di [spo-backup-final/app/main.py](../spo-backup-final/app/main.py#L45)
- Backend `POST /api/notification/test` kini menolak config email/telegram/teams yang belum lengkap dengan error yang langsung actionable di [spo-backup-final/app/main.py](../spo-backup-final/app/main.py#L170)
- UI settings kini mempertahankan password remote lama saat edit bila field password dibiarkan kosong dan menampilkan hint yang eksplisit di [spo-backup-final/app/templates/settings.html](../spo-backup-final/app/templates/settings.html#L145)
- Tombol test/save remote dan notification test kini menampilkan feedback error yang lebih jujur alih-alih terlihat diam
- Test notification kini benar-benar mengikuti channel yang dipilih (`email`/`telegram`/`teams`) alih-alih berpotensi ikut mengirim email saat operator hanya mengetes Teams di [spo-backup-final/app/notifier.py](../spo-backup-final/app/notifier.py#L16)
- HTTP notification path kini juga lebih jujur terhadap kegagalan webhook/Graph mail karena response Teams/Graph `sendMail` sekarang di-raise dan memakai retry-aware session, jadi kasus alert terlihat `skipped` padahal request sebenarnya gagal menjadi lebih mudah dideteksi

### 10. Legacy flow dan flow baru hidup berdampingan tanpa boundary produk yang tegas

- [x] Tandai halaman/endpoint legacy vs modern di UI
- [ ] Tentukan route mana yang akan dipertahankan jangka panjang
- [ ] Kurangi kebingungan antara `/restore` vs `/restore-v2`, backup legacy vs tenant-aware

Detail:
- Saat ini terdapat dua dunia:
- legacy single-tenant/global flow
- tenant-aware multi-workload flow
- Ini kuat untuk kompatibilitas, tapi belum matang dari sisi product coherence

Update:
- Boundary schedule sudah dipertegas: `Per-Tenant Schedules` sebagai flow modern utama, `Legacy Global Schedule` sebagai compatibility summary
- Sidebar, dashboard, backups, restore, dan settings kini memberi penanda eksplisit untuk flow utama vs compatibility flow
- Route restore lama sudah diarahkan ke `Restore V2`, tetapi cleanup naming dan route jangka panjang masih perlu dituntaskan
- Backend dan metadata workload masih menyimpan referensi langsung ke `/restore-v2` di [spo-backup-final/app/main_routes.py](../spo-backup-final/app/main_routes.py#L97), [spo-backup-final/app/main_routes_v13.py](../spo-backup-final/app/main_routes_v13.py#L12), dan [spo-backup-final/app/workloads/__init__.py](../spo-backup-final/app/workloads/__init__.py#L41), jadi produk secara UX sudah bernama `Restore` tetapi contract internal belum sepenuhnya konsisten

### 10a. Backup history `legacy` masih bisa misleading untuk atribusi tenant

- [ ] Perkaya resolusi owner backup legacy dari manifest/site metadata historis
- [ ] Bedakan backup legacy yang benar-benar tenant lama vs backup flat yang owner-nya belum bisa dipastikan
- [ ] Cegah filter tenant/history terlihat akurat padahal backup masih fallback ke `legacy-default`

Detail:
- Registry masih fallback ke `legacy-default` / `Legacy SharePoint` bila folder backup lama tidak punya `_workload_manifest.json` di [spo-backup-final/app/backup_registry.py](../spo-backup-final/app/backup_registry.py#L68)
- Ini membuat halaman history modern tetap usable, tetapi belum cukup matang untuk audit tenant yang presisi pada backup historis lama

### 10b. Runtime worker modern belum terjamin aktif penuh hanya karena source code sudah dipatch

- [ ] Verifikasi image/runtime `spo-backup-worker` sudah memakai patch terakhir setelah task aktif aman direstart
- [ ] Uji ulang target selection modern, retry Graph, dan remote upload sesudah worker runtime terbaru aktif

Detail:
- Beberapa perbaikan penting sudah ada di source, tetapi perilaku produksi tetap bergantung pada image worker yang benar-benar sedang berjalan
- Ini terutama berdampak ke flow modern `Workloads`, backup multi-workload, retry hardening, dan post-backup remote upload

### 11. Worker masih berjalan sebagai root

- [x] Jalankan web/worker/beat dengan user non-root di container
- [x] Audit permission volume setelah perubahan user

Detail:
- Worker log menampilkan Celery `SecurityWarning` karena berjalan sebagai superuser
- Temuan ini muncul dari `docker logs spo-backup-worker` saat audit

Update:
- Image dan compose kini menjalankan `spo-backup-web`, `spo-backup-worker`, dan `spo-backup-scheduler` sebagai `uid=1000 gid=1000` (`appuser`)
- Installer dan `.env.example` kini menyiapkan `SPO_UID` / `SPO_GID` agar ownership volume host sinkron dengan user runtime non-root

## Temuan Rendah

### 12. README belum mencerminkan readiness aktual seluruh fitur

- [ ] Tambahkan matriks status fitur: `stable`, `partial`, `requires permission`, `experimental`
- [ ] Dokumentasikan workload `OneDrive` dan `Outlook` sebagai implemented-but-needs-tenant-permission
- [ ] Dokumentasikan state `BACKUP_FAILED`

### 13. Folder `docs` sebelumnya belum ada

- [x] Buat folder dokumentasi audit
- [ ] Tambahkan living docs untuk roadmap dan release readiness

### 15. Parser URL download SharePoint belum menormalkan URL view-style

- [x] Normalisasi URL `Forms/AllItems.aspx` agar kembali ke folder/library yang benar
- [x] Prioritaskan query `parent` saat tersedia untuk URL folder browse SharePoint
- [ ] Uji lebih banyak variasi URL share link modern (`:f:`, `:u:`) jika memang ingin didukung

Detail:
- Sebelumnya parser bisa menghasilkan `folder_path=Shared Documents/Forms/AllItems.aspx`
- Dampaknya preview terlihat berhasil tetapi target folder download salah

### 16. Resume backup/download belum cukup matang untuk kasus interruption antar-run

- [x] Lanjutkan file partial `.tmp` saat upstream mendukung range request
- [x] Reuse destination path pada custom download kini bisa skip file lengkap dan lanjut dari checkpoint manifest lokal
- [x] Simpan progress manifest backup site lebih agresif agar interruption tidak membuang seluruh progress site
- [x] Pulihkan progress custom download setelah refresh dengan membaca active task dan cache task terakhir di browser
- [ ] Tambahkan matriks uji untuk interruption jaringan nyata dan file besar

Detail:
- Backup legacy sebelumnya sudah punya manifest `eTag/lastModified` untuk skip file unchanged
- Download custom sebelumnya hanya skip file lengkap berdasarkan size, tetapi belum punya checkpoint manifest lokal yang kuat
- Sekarang custom download menyimpan `_custom_download_manifest.json` di destination path dan mencoba resume `.tmp` file

### 17. Fase discovery backup/download sebelumnya terlalu lama tanpa feedback progres yang jujur

- [x] Ubah enumerasi file menjadi progressive scan agar `files_total` dan `bytes_total` bertumbuh selama proses jalan
- [x] Mulai download file tanpa menunggu seluruh library/folder selesai discan
- [x] Rapikan ETA agar menampilkan `scanning...`, `waiting...`, atau `finishing...` alih-alih selalu `calculating...`
- [x] Putus task/progress zombie yang tersisa di Redis/result backend setelah worker restart atau task hilang
- [ ] Uji performa tenant besar untuk memastikan frekuensi emit progress tidak menjadi bottleneck baru

Detail:
- Sebelumnya engine `backup_site()` dan `download_custom_url()` mengumpulkan semua file lebih dulu sebelum download dimulai
- Dampaknya panel progress bisa lama di `0 / 0`, `0 B/s`, `calculating...` walau proses scan dan penambahan size di disk sebenarnya sudah berlangsung
- Sekarang scan dan transfer berjalan lebih bertahap sehingga UX operator lebih jujur dan resume terasa lebih natural
- API `/api/task/active`, `/api/backup/status/<task_id>`, dan `/api/download/status/<task_id>` kini memvalidasi bahwa task benar-benar masih aktif di worker sebelum UI menandainya `RUNNING`
- Backup SharePoint legacy kini diproses per-file saat file ditemukan, bukan menunggu seluruh library selesai discan, sehingga `files_total` dan aktivitas progres mulai terlihat jauh lebih cepat di [spo-backup-final/app/backup_engine.py](../spo-backup-final/app/backup_engine.py#L326)
- Backup selesai kini juga menulis `_size_cache.json` lagi agar backup history tidak terus terlihat `0.0 B` hanya karena fast-list tidak menghitung ukuran folder secara eager

### 18. Operasi panjang sebelumnya belum punya boundary yang aman saat user memicu task baru di tengah task aktif

- [x] Legacy backup baru kini otomatis masuk antrean jika backup lain masih berjalan
- [x] Tenant-aware backup baru kini otomatis masuk antrean jika backup lain masih berjalan
- [x] Custom download baru kini otomatis masuk antrean jika download lain masih berjalan
- [x] Restore V2 job baru kini otomatis masuk antrean jika restore lain masih berjalan
- [x] Tambahkan lock dispatch Redis agar antrean tidak dobel terpicu saat banyak tab UI polling bersamaan
- [ ] Aktifkan penuh auto-dispatch worker-side setelah `spo-backup-worker` direstart pasca task aktif selesai

Detail:
- Sebelumnya klik `Run Backup` atau `Start Restore` saat task aktif berisiko membingungkan operator karena state backend hanya punya satu tracked task utama
- Sekarang request baru tidak memutus task aktif; request disimpan di antrean Redis dan bisa dilihat dari surface operasi di dashboard
- Web layer juga menambahkan fallback auto-dispatch saat polling overview berjalan, sehingga antrean tetap maju walau worker belum direstart

### 19. Retry, timeout, dan resume koneksi Graph/SharePoint sebelumnya belum cukup kuat untuk error jaringan menengah

- [x] Tambahkan session HTTP dengan retry adapter untuk operasi Graph yang bersifat aman
- [x] Tambahkan backoff terukur untuk `429`, `5xx`, timeout, dan connection error
- [x] Tambahkan retry streaming download dengan resume dari `.tmp` file yang sudah ada
- [x] Sinkronkan progres file saat retry agar progress bar tidak double-count setelah koneksi putus
- [x] Terapkan pola yang sama ke workload modern (`OneDrive`, `Outlook`) dan restore base request
- [x] Tambahkan lease runtime Redis untuk menekan eksekusi task duplikat saat broker/worker bermasalah
- [ ] Restart `spo-backup-worker` setelah task aktif selesai untuk mengaktifkan patch hardening ini di worker runtime

Detail:
- Sebelumnya `backup_engine.py` dan `workloads/base.py` hanya punya retry tipis di request metadata, sementara streaming file besar masih rentan putus total saat `BrokenPipe`, `RemoteDisconnected`, `Read timed out`, atau gangguan DNS sesaat
- Sekarang download file akan mencoba lanjut dari `.tmp` yang sudah ada pada retry berikutnya, dengan backoff dan range request yang lebih terkontrol
- Guard lease baru juga membantu mencegah task backup/download/restore dengan ID yang sama dieksekusi ganda saat terjadi redelivery
- Jalur kompatibilitas seperti `tenant test`, `legacy restore manager`, dan `legacy restore engine` juga sudah dipindahkan ke session HTTP yang lebih resilien agar surface lama tidak tertinggal jauh dari flow modern

### 20. Restore legacy compatibility API masih hidup berdampingan dengan Restore V2 dan memakai engine/job model lama

- [x] Route restore lama kini dialiaskan ke engine dan antrean `Restore V2` untuk operasi baru
- [~] Endpoint compatibility lama masih hidup untuk klien lama, tetapi boundary runtime tidak lagi memakai job engine lama untuk request baru
- [ ] Jika tidak, deprecate bertahap agar operator tidak terjebak memakai flow restore lama yang kontraknya berbeda

Detail:
- Route utama UI sudah mengarahkan `/restore` dan `/restore-jobs` ke `Restore V2`, tetapi endpoint legacy `POST /api/restore/site` masih aktif di [spo-backup-final/app/main.py](../spo-backup-final/app/main.py#L1303)
- Compatibility API `GET/POST /api/restore/jobs` juga masih aktif di [spo-backup-final/app/main_routes.py](../spo-backup-final/app/main_routes.py#L191)
- Worker lama `app.tasks_m365.execute_restore_job` masih hanya menjalankan `restore_sharepoint()` berbasis job model lama di [spo-backup-final/app/tasks_m365.py](../spo-backup-final/app/tasks_m365.py#L10)
- Secara produk ini rawan membingungkan, karena surface UI utama sudah modern tetapi runtime compatibility path restore lama belum dipersempit

Update:
- `POST /api/restore/site` kini tidak lagi memicu `run_restore_task` lama secara langsung; request compatibility sekarang dibungkus ke payload `Restore V2` dan diantrikan lewat queue restore modern di [spo-backup-final/app/main.py](../spo-backup-final/app/main.py#L1303)
- `GET/POST/DELETE /api/restore/jobs` compatibility juga kini membaca dan membuat job dari `RestoreManagerV2`, sambil mengembalikan metadata `deprecated` dan `recommended_endpoint` di [spo-backup-final/app/main_routes.py](../spo-backup-final/app/main_routes.py#L191)
- `SharePointRestore` sekarang bisa menerima `backup_path` yang menunjuk langsung ke satu folder site legacy, sehingga compatibility alias tidak perlu lagi memakai engine restore lama di [spo-backup-final/app/restore/sharepoint.py](../spo-backup-final/app/restore/sharepoint.py#L30)
- Metadata kompatibilitas kini merekomendasikan `/restore` sebagai UI utama, sementara `/restore-v2` dipertahankan sebagai alias teknis agar naming produk tidak lagi rancu di surface operator

### 22. Restore SharePoint sebelumnya membingungkan karena hasil bisa masuk ke library sumber, bukan selalu ke `Documents`

- [x] Tampilkan daftar document library tujuan langsung dari site target di UI restore
- [x] Tampilkan `destination_plan` yang lebih eksplisit saat preview restore
- [x] Auto-create folder tujuan bila belum ada
- [x] Gagalkan lebih awal jika operator memilih `Target Library Name` yang tidak ada di site tujuan
- [ ] Tambahkan validasi end-to-end tambahan untuk skenario site tujuan dengan library kustom yang berubah nama

Detail:
- Backup SharePoint legacy bisa berasal dari subtree library seperti `Product Management`, sehingga tanpa target library eksplisit restore memang akan auto-match ke library sumber, bukan selalu ke `Documents`
- Ini menjelaskan kasus restore terlihat `Completed 100%` tetapi operator memeriksa library yang berbeda dari lokasi hasil restore yang sesungguhnya
- Risiko utamanya adalah false assumption operator, bukan semata-mata upload gagal

Update:
- UI restore kini dapat memuat daftar library live dari site target lewat `POST /api/v2/restore/sharepoint/libraries` di [spo-backup-final/app/main_routes_v13.py](../spo-backup-final/app/main_routes_v13.py#L1)
- Preview restore SharePoint kini mengembalikan `available_target_libraries`, `target_folder_behavior`, `destination_plan`, dan `resolved_destination_summary` di [spo-backup-final/app/restore/sharepoint.py](../spo-backup-final/app/restore/sharepoint.py#L284)
- Jika `Target Library Name` dibiarkan kosong, engine tetap boleh auto-match ke library sumber atau fallback ke `Documents`; tetapi preview sekarang menampilkan rencana finalnya sebelum job dibuat
- Jika `Target Folder` belum ada, restore akan membuat folder tersebut otomatis; jika library yang dipilih manual tidak ada, restore akan gagal lebih awal dan tidak lagi terlihat sukses semu

### 21. Tenant test backend sudah memberi warning yang lebih kaya, tetapi UI tenant belum menampilkannya secara penuh

- [x] Tampilkan `warnings` hasil `test_tenant()` langsung di modal/card tenant, bukan hanya toast ringkas
- [x] Tampilkan scope/action yang perlu dilakukan operator saat `users` atau `sharepoint` terkena `403`
- [~] Bedakan hasil `warning` vs `error` dengan visual yang lebih jelas di halaman tenant

Detail:
- Backend `TenantManager.test_tenant()` kini mengembalikan `warnings` yang cukup actionable di [spo-backup-final/app/tenant_manager.py](../spo-backup-final/app/tenant_manager.py#L192)
- Namun UI tenant saat ini hanya merangkum `details` sebagai badge status dan toast singkat di [spo-backup-final/app/templates/tenants.html](../spo-backup-final/app/templates/tenants.html#L213)
- Akibatnya operator tetap harus menebak langkah berikutnya walau backend sebenarnya sudah tahu problem utamanya adalah `admin consent` atau scope Graph tertentu

Update:
- Halaman tenant kini menampilkan panel hasil test langsung di card tenant, bukan hanya toast, lewat slot `tenantStatus-*` di [spo-backup-final/app/templates/tenants.html](../spo-backup-final/app/templates/tenants.html#L36)
- Warning Graph/admin consent sekarang muncul sebagai blok `Action Needed` dengan pesan backend yang sudah di-sanitasi di file yang sama
- Modal `Test Connection` untuk add/edit tenant juga memakai renderer hasil yang sama, jadi contract backend warning tidak lagi hilang di UI utama

### 22. Dokumen status produk belum sepenuhnya sinkron dengan code dan runtime terkini

- [x] Sinkronkan `PRD_CHECKLIST_REMEDIASI.md` dengan item yang sudah selesai di code
- [x] Sinkronkan `CHECKLIST_EKSEKUSI_STATUS.md` dengan status queue/retry hardening dan boundary restore legacy
- [x] Tambahkan catatan eksplisit bahwa patch worker-side terbaru baru aktif penuh setelah `spo-backup-worker` direstart pasca task aktif selesai

Detail:
- Beberapa checklist di `docs/PRD_CHECKLIST_REMEDIASI.md` masih menandai item seperti slug contract dan non-root runtime seolah belum final
- `docs/CHECKLIST_EKSEKUSI_STATUS.md` juga belum mencerminkan penuh hardening antrean operasi, retry HTTP, dan dependency restart worker
- Ini bukan bug runtime langsung, tetapi riskan membuat audit berikutnya salah baca prioritas dan readiness aktual

## Checklist Per Area

### Backend Foundation

- [x] Flask app boot normal
- [x] Route modular v10-v14 terpasang
- [x] Celery task dasar berjalan
- [~] State model backup jauh lebih baik, tetapi belum konsisten penuh antara task/result/control pada kasus worker lama/redelivery
- [ ] Upload post-processing multi-workload belum matang penuh
- [~] Security runtime container sudah membaik dengan non-root runtime, tetapi audit hardening lanjutan masih perlu

### Frontend Foundation

- [x] Semua page utama render normal
- [x] Layout responsif dasar ada
- [~] Failure-state UI jauh lebih sinkron untuk backup/download/queue, tetapi belum final di semua edge-case permission/runtime
- [~] Product labeling legacy vs modern sudah lebih jelas di flow utama, tetapi belum final di semua surface
- [~] Feedback permission/credential error sudah lebih actionable, tetapi tenant test dan beberapa flow lama masih belum cukup informatif

### Multi-Tenant

- [x] Tenant CRUD dan activation tersedia
- [x] Tenant-aware backup registry tersedia
- [x] Slug source of truth sudah dipindahkan ke response API tenant untuk flow utama restore v2
- [x] Slug source of truth sudah dipakai konsisten di response API tenant utama dan flow restore/backup utama
- [~] UX permission tenant membaik dengan warning inline, tetapi readiness/proactive guidance masih belum matang penuh

### Backup Workloads

- [x] SharePoint backup aktif
- [x] OneDrive backup engine ada
- [x] Outlook backup engine ada
- [x] Teams backup engine ada
- [ ] Tenant nyata belum punya permission cukup untuk OneDrive/Outlook/Teams
- [ ] Remote upload setelah backup multi-workload masih berisiko salah target

### Restore

- [x] Restore v2 page dan API hidup
- [x] OneDrive/Outlook/Teams restorer ada
- [~] UX restore readiness sudah membaik, tetapi warning permission tenant dan hasil validasi tenant nyata belum menyeluruh
- [ ] Belum ada matriks validasi restore end-to-end per workload dengan tenant nyata
- [~] Boundary runtime restore legacy vs restore v2 sudah jauh lebih rapat, tetapi endpoint compatibility lama masih belum dipensiunkan

### Temuan Tambahan

#### 14. Registry backup sempat mengembalikan backup phantom dari cache

- [x] Validasi cache registry agar tidak mengembalikan path backup yang sudah tidak ada
- [x] Paksa halaman backup dan restore memuat data fresh saat diperlukan
- [ ] Tambahkan smoke test otomatis untuk backup list vs filesystem nyata

Detail:
- `BackupRegistry.list_all()` sebelumnya bisa mengembalikan hasil cache lama selama TTL belum habis
- Dampaknya UI backup/restore bisa menampilkan backup yang foldernya sudah hilang, lalu restore gagal di tahap preview/job create

### Scheduling & Notifications

- [x] Per-tenant schedules dan notifications ada
- [x] Global schedule legacy masih coexist tetapi positioning produk sudah jelas
- [ ] Reload/apply schedule masih butuh instruksi manual/restart yang bisa membingungkan user
- [ ] Belum ada benchmark throughput terstandar untuk backup SharePoint custom download vs backup legacy

## Prioritas Eksekusi Rekomendasi

- [x] P0: sinkronkan terminal state backup UI dengan backend (`BACKUP_FAILED`, `UNKNOWN`, cleanup control state)
- [x] P0: perbaiki remote upload agar workload-aware
- [x] P1: rapikan backups UI untuk `teams`
- [x] P1: putuskan source of truth schedule
- [ ] P1: audit dan dokumentasikan permission Graph tenant aktif
- [~] P1: rapikan boundary restore legacy vs restore v2
- [ ] P1: benchmark dan tuning throughput backup/download pada dataset besar
- [x] P2: selaraskan slug tenant frontend-backend
- [~] P2: rapikan positioning legacy vs modern flows
- [x] P2: sinkronkan seluruh dokumen readiness dengan code/runtime terbaru

## Baseline Hasil Tes Audit Ini

- [x] `docker compose` build dan up berhasil
- [x] Page smoke test `200`
- [x] API smoke test `200`
- [x] OneDrive/Outlook code path sudah bisa start dari API v2
- [x] OneDrive/Outlook/Teams di tenant aktif masih gagal karena Graph `403`
