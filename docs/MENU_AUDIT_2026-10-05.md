# Audit menu aplikasi - 2026-10-05

Catatan: ini adalah snapshot temuan sebelum patch lanjutan. Status perbaikan dan
risiko tersisa tercatat di `PATCH_STATUS_2026-10-05.md`.

Audit kode, image aktif, HTML terender, endpoint baca-saja, Redis, dan log.
Seluruh 11 halaman navigasi merespons HTTP 200 setelah redirect `/restore`;
script inline pada halaman utama lolos pemeriksaan sintaks. Ini adalah smoke test,
bukan pembuktian backup/restore Microsoft 365 end-to-end. Tidak ada job baru dijalankan.

| Menu | Hasil audit | Prioritas berikutnya |
| --- | --- | --- |
| Dashboard | Bug parser JavaScript `??` bercampur `||` menyebabkan seluruh script berhenti. Sudah diperbaiki. Marker task diperbarui berkala dan task aktif dapat ditemukan dari worker bila key Redis hilang. Status backup selesai kini tidak dipaksa menjadi `PROGRESS`. Endpoint antrean yang sebelumnya timeout 25 detik saat kosong kini sekitar 2 detik. Persentase backup masih bisa berubah saat scan menemukan file baru. | Sedang: pisahkan tahap scan, transfer, dan verifikasi; tampilkan progress yang monotonik. |
| Tenants | Halaman dan API baca-saja berjalan. `TenantManager.update_tenant` menerima perubahan tenant ID, domain, dan workload tanpa validasi format/duplikasi; tes koneksi hanya memeriksa organization, satu site, dan users, sehingga izin workload tertentu belum dibuktikan. | Tinggi: validasi field dan tes izin per workload. |
| Workloads | Empat workload tersedia. Estimasi ukuran batch melakukan hingga 50 permintaan Graph secara berurutan dalam request web (`app/main_routes.py:415-470`); dapat melampaui timeout Gunicorn. Sinkronisasi SharePoint memakai nama tampilan site sebagai nama backup, yang tidak unik. | Tinggi: pindahkan estimasi massal ke queue; gunakan site ID/URL sebagai identitas backup. |
| Sites | Halaman dan daftar 630 site aktif berhasil dibuka. Tombol Refresh Estimates memproses seluruh site secara serial melalui GET yang juga menulis config (`app/main.py:1204-1277`); berpotensi macet pada tenant besar. Form tambah site hanya mewajibkan nama, tanpa cek path duplikat (`app/main.py:1280-1287`). | Tinggi: estimasi asinkron per batch, validasi dan deduplikasi site path. |
| Download URL | Penanda task diperbarui seperti backup. Endpoint status memiliki bug terminal dan PENDING yang sama; sudah diperbaiki. Jalur unduhan kustom tetap dapat mengembalikan task `SUCCESS` ketika `stats.errors` berisi file gagal (`app/backup_engine.py:907-978`). Subfolder yang tidak ditemukan dapat diam-diam jatuh kembali ke root library. | Tinggi: status `partial/failed` per file dan kegagalan eksplisit saat folder tidak ditemukan. |
| Schedules | Halaman terbuka. Menyimpan jadwal tenant mengubah config, tetapi endpoint reload hanya mengubah jadwal di proses web; UI sendiri meminta restart `celery-beat` (`app/main_routes_v12.py:88-101`). Validasi cron hanya menghitung lima bagian. | Sedang: reload beat yang nyata dan validasi ekspresi/timezone. |
| Backups | Halaman dan daftar API berjalan. Parser JavaScript yang sama sudah diperbaiki. Ukuran dapat memakai cache/manifest lama tanpa cek kondisi disk terbaru (`app/backup_registry.py:555-581`). Tombol hapus langsung menghapus folder lokal melalui `shutil.rmtree` (`app/backup_registry.py:695-702`). | Tinggi: verifikasi ukuran dan konfirmasi berbasis identitas path; lindungi backup dari salah hapus. |
| Restore | Halaman redirect ke restore v2 dan job lama tetap tampil. Polling daftar job sebelumnya memanggil inspect worker walau tak ada queued job; sudah dioptimasi. Persentase `processed/(processed+failed)` menjadi 99% sesudah satu file berhasil dan dapat turun jika muncul error (`app/restore_manager_v2.py:242-253`). File gagal dapat tetap berakhir dengan status `completed` (`app/restore_manager_v2.py:275-299`). Teams restore adalah ekspor lokal, bukan reimport ke Teams. | Kritis: total pekerjaan, progress monotonik, status partial, verifikasi file target. |
| Logs | Halaman dan endpoint log cepat. Tombol Clear mengosongkan file log tanpa arsip/jejak audit (`app/main.py:1905-1914`); histori per-task tidak disimpan permanen. | Sedang: log terstruktur, rotasi/retensi, dan arsip sebelum clear. |
| Settings | Halaman terbuka. `/api/config` sebelumnya membuka `tenants[].client_secret` dan token Telegram; respons sudah dimasking, dan penyimpanan mempertahankan secret lama saat nilai masked dikirim kembali. Endpoint konfigurasi tetap mengganti seluruh dokumen tanpa validasi skema. | Kritis: autentikasi/otorisasi API dan patch parsial dengan validasi. |
| Advanced Settings | Bergantung pada GET/POST `/api/config`, sehingga turut terdampak kebocoran secret dan risiko overwrite konfigurasi. Masking/preservasi sudah diperbaiki. | Tinggi: perubahan field terarah, diff sebelum simpan, backup config versi sebelumnya. |

## Risiko lintas menu

- **Keamanan akses:** belum ditemukan autentikasi aplikasi untuk route administrasi.
  Port `5050` dipetakan ke semua interface. Firewall/reverse proxy di luar container
  tidak diaudit, jadi cakupan paparan jaringan eksternal belum diketahui.
- **Integritas backup:** klaim `614/614` belum membuktikan 614 salinan site unik;
  lihat `AUDIT_2026-10-05.md` untuk hasil rekonsiliasi folder dan manifest.
- **Konsistensi status:** label `success/completed`, `partial`, dan `failed` belum
  konsisten di backup, download, restore, manifest, dan email.
- **Pengujian:** regresi progress/Redis/UI yang baru ditambahkan mencakup kasus
  parser, marker hilang, terminal/PENDING, antrean kosong, dan secret masking.
  Pengujian Graph end-to-end masih belum ada.

## Urutan kerja berikutnya

1. Terapkan autentikasi/otorisasi API dan audit konfigurasi jaringan port 5050.
2. Rekonsiliasi data backup per site ID dan item ID; ganti nama folder yang tidak unik.
3. Verifikasi ukuran/isi file pada backup dan restore, lalu perbaiki status parsial.
4. Pindahkan estimasi seluruh site dan pekerjaan panjang lain dari request web ke queue.
5. Jadikan jadwal, histori, log, dan progress persisten dengan tes end-to-end.
