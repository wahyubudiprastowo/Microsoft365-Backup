# Status patch audit - 2026-10-05

Patch ini tidak memulai backup, restore, delete M365, atau migrasi folder lama.
Audit data historis 614 site masih terpisah di `AUDIT_2026-10-05.md`.

| Menu | Patch yang diterapkan | Batas yang masih ada |
| --- | --- | --- |
| Dashboard | Parser JavaScript dan tracking task Redis diperbaiki pada patch sebelumnya. | Persentase saat scan file bertambah masih dapat berubah. |
| Tenants | Input ID/domain/workload divalidasi; tes tenant menampilkan matriks role Graph untuk workload aktif. | Role token adalah preflight, bukan bukti operasi tiap API. |
| Workloads | Batch estimasi Graph dipindahkan ke worker queue `estimates`. | Discovery target tetap bergantung pada respons Graph. |
| Sites | Refresh 630 site menjadi job asinkron dengan status yang bisa dipantau; path duplikat ditolak. | Histori site lama dengan nama/folder ambigu belum dimigrasi. |
| Download URL | Range paralel terbatas untuk file >=64 MiB, ukuran byte diverifikasi, resume diikat ke identitas ETag, folder yang tidak ditemukan gagal eksplisit, status partial muncul. | Kecepatan tetap dibatasi throttling Graph, latency, disk, dan banyak file kecil; belum ada benchmark M365 langsung. |
| Schedules | Beat memuat ulang perubahan config tiap <=30 detik dan cron divalidasi. | Per-tenant timezone selain Asia/Jakarta sengaja ditolak hingga scheduler mendukungnya. |
| Backups | Delete lokal mensyaratkan path tepat, konfirmasi UI, dan menolak backup aktif/yang dipakai restore. | Ukuran cache bisa berbeda dari ukuran fisik disk; tidak ada verifikasi checksum semua backup historis. |
| Restore | Status partial/failed, progress berdasarkan jumlah file lokal bila tersedia, validasi ukuran upload Graph. | Teams tetap ekspor lokal, bukan reimport; verifikasi baca-ulang target belum dilakukan. |
| Logs | Clear mengarsipkan file log sebelum mengosongkan file aktif. | Retensi arsip dan audit trail terpusat belum ada. |
| Settings | Basic Auth untuk UI/API; config editor memakai revisi dan validasi struktur awal agar tab lama tidak menimpa perubahan baru. | Basic Auth melalui HTTP belum aman di jaringan tak tepercaya; butuh TLS reverse proxy/VPN. Validasi skema penuh belum ada. |
| Advanced Settings | Secret tetap dimasking; save ditolak bila revisi config sudah berubah. | Editor raw JSON masih tersedia untuk admin yang terautentikasi. |

## Parameter transfer

- `GRAPH_DOWNLOAD_RANGE_WORKERS` default `4`, batas kode `8`. Berlaku per file besar; worker backup bisa menjalankan dua job paralel.
- `GRAPH_DOWNLOAD_RANGE_MIN_SIZE` default `67108864` byte (64 MiB).
- `GRAPH_DOWNLOAD_CHUNK_SIZE` default `4194304` byte (4 MiB).
- Bila server tidak mendukung byte range atau potongan gagal, transfer kembali ke mode serial dengan verifikasi panjang file.
- Nilai default konservatif. Jangan menaikkan worker tanpa mengukur respons `429` Graph, pemakaian disk, dan throughput aktual.

## Prioritas lanjutan

1. Pasang TLS/VPN di depan port 5050 atau batasi akses jaringan. Kredensial Basic Auth tersimpan di `.env` (izin file 0600), tidak boleh dipakai lewat HTTP publik.
2. Rekonsiliasi backup historis berdasarkan site ID, drive ID, item ID, dan ukuran/file hash sebelum menyatakan semua 614 site lengkap atau menghapus sumber.
3. Tambah tes end-to-end terhadap tenant uji untuk backup, restore, dan pengukuran throughput yang nyata. Unit test tidak membuktikan bandwidth produksi.
4. Perbaiki estimasi ukuran fisik cache, retensi log, dan progress scan yang belum monotonik.
