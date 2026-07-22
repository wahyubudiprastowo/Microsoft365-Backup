"""Unified backup registry for legacy flat and tenant-aware backup layouts."""
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from app.config_manager import load_config
from app.tenant_manager import TenantManager

log = logging.getLogger("spo_backup")


def slugify_tenant(value: str) -> str:
    value = (value or "").strip().lower()
    chars = []
    last_dash = False
    for ch in value:
        if ch.isalnum():
            chars.append(ch)
            last_dash = False
            continue
        if not last_dash:
            chars.append("-")
            last_dash = True
    return "".join(chars).strip("-") or "default-tenant"


class BackupRegistry:
    """Scan and manage backups across both supported disk layouts."""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.legacy_root = Path(self.config["backup"]["root_dir"])
        self.tenant_root = self.legacy_root / "m365"
        self._cache = {}
        self._tenant_manager = TenantManager()

    def list_all(self, use_cache: bool = True) -> list:
        if use_cache and "all" in self._cache:
            cached, ts = self._cache["all"]
            if time.time() - ts < 15 and self._cache_entries_exist(cached):
                return cached

        results = []
        results.extend(self._list_legacy())
        results.extend(self._list_tenant_aware())
        results.sort(key=lambda item: item["date"], reverse=True)
        self._cache["all"] = (results, time.time())
        return results

    def _list_legacy(self) -> list:
        backups = []
        if not self.legacy_root.exists():
            return backups

        for entry in self.legacy_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == "m365" or entry.name.startswith("."):
                continue
            if not (entry.name.startswith("backup_") or entry.name.startswith("custom_")):
                continue
            tenant_slug, tenant_name = self._resolve_legacy_owner(entry)
            backups.append(
                self._describe_backup(
                    entry,
                    tenant_slug=tenant_slug,
                    tenant_name=tenant_name,
                    workload="sharepoint",
                    layout="legacy",
                )
            )
        return backups

    def _resolve_legacy_owner(self, backup_dir: Path) -> tuple[str, str]:
        manifest = backup_dir / "_workload_manifest.json"
        if manifest.exists():
            try:
                data = json.load(open(manifest))
                tenant_slug = str(data.get("tenant_slug") or "").strip()
                tenant_name = str(data.get("tenant_name") or "").strip()
                if tenant_slug:
                    return tenant_slug, tenant_name or tenant_slug.replace("-", " ").title()
            except Exception:
                pass
        return "legacy-default", "Legacy SharePoint"

    def _list_tenant_aware(self) -> list:
        results = []
        if not self.tenant_root.exists():
            return results

        for tenant_dir in self.tenant_root.iterdir():
            if not tenant_dir.is_dir() or tenant_dir.name.startswith("."):
                continue
            for workload_dir in tenant_dir.iterdir():
                if not workload_dir.is_dir() or workload_dir.name.startswith("."):
                    continue
                for backup_dir in workload_dir.iterdir():
                    if not backup_dir.is_dir() or not backup_dir.name.startswith("backup_"):
                        continue
                    results.append(
                        self._describe_backup(
                            backup_dir,
                            tenant_slug=tenant_dir.name,
                            tenant_name=self._guess_tenant_name(tenant_dir.name),
                            workload=workload_dir.name,
                            layout="tenant-aware",
                        )
                    )
        return results

    def _describe_backup(self, backup_dir: Path, tenant_slug: str, tenant_name: str, workload: str, layout: str) -> dict:
        size_bytes = self._read_or_compute_size(backup_dir)
        files_count = 0
        targets_count = 0
        status = None
        manifest = None

        manifest_candidates = [
            backup_dir / "_workload_manifest.json",
            backup_dir / "_backup_metadata.json",
        ]
        for manifest_file in manifest_candidates:
            if not manifest_file.exists():
                continue
            try:
                manifest = json.load(open(manifest_file))
            except Exception:
                continue
            tenant_name = manifest.get("tenant_name", tenant_name)
            status = manifest.get("status") or status
            files_count = (
                manifest.get("files_downloaded")
                or manifest.get("total_files")
                or files_count
            )
            targets_count = (
                manifest.get("sites_count")
                or manifest.get("users_count")
                or manifest.get("mailbox_count")
                or manifest.get("targets_processed")
                or targets_count
            )
            break

        if not status:
            if size_bytes > 0:
                status = "interrupted"
            else:
                status = "unknown"

        target_names = self._list_root_target_names(backup_dir)
        summary = self._build_backup_summary(workload, manifest, target_names, targets_count)

        return {
            "tenant_slug": tenant_slug,
            "tenant_name": tenant_name,
            "workload": workload,
            "backup_name": backup_dir.name,
            "backup_path": str(backup_dir),
            "layout": layout,
            "date": datetime.fromtimestamp(backup_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes": size_bytes,
            "size_human": self._format_size(size_bytes),
            "files_count": files_count or 0,
            "targets_count": targets_count or 0,
            "status": status,
            "target_names_preview": target_names[:3],
            "target_names_more": max(0, len(target_names) - 3),
            "summary": summary,
        }

    def _list_root_target_names(self, backup_dir: Path) -> list[str]:
        names = []
        try:
            for entry in sorted(backup_dir.iterdir(), key=lambda item: item.name.lower()):
                if entry.name.startswith(".") or entry.name.startswith("_"):
                    continue
                if entry.is_dir():
                    names.append(entry.name)
        except Exception:
            pass
        return names

    def _build_backup_summary(self, workload: str, manifest: dict | None, target_names: list[str], targets_count: int) -> str:
        if workload == "sharepoint":
            if target_names:
                if len(target_names) == 1:
                    return f"Site: {target_names[0]}"
                return f"Sites: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            if targets_count:
                return f"{targets_count} site target(s)"
            return "SharePoint backup folder"

        if workload == "onedrive":
            if targets_count:
                return f"Users in scope: {targets_count}"
            if target_names:
                return f"Users: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            return "OneDrive target backup"

        if workload == "outlook":
            if targets_count:
                return f"Mailboxes in scope: {targets_count}"
            if target_names:
                return f"Mailboxes: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            return "Outlook mailbox backup"

        if workload == "teams":
            if targets_count:
                return f"Teams in scope: {targets_count}"
            if target_names:
                return f"Teams: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            return "Teams export backup"

        if target_names:
            return ", ".join(target_names[:3]) + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
        if targets_count:
            return f"Targets: {targets_count}"
        return "Backup folder"

    def _guess_tenant_name(self, tenant_slug: str) -> str:
        for tenant in self._tenant_manager.list_tenants(include_secrets=False):
            slug = slugify_tenant(
                tenant.get("primary_domain")
                or tenant.get("sharepoint_host")
                or tenant.get("name")
            )
            if slug == tenant_slug:
                return tenant.get("name", tenant_slug)
        return tenant_slug.replace("-", " ").title()

    def _read_or_compute_size(self, backup_dir: Path) -> int:
        size_cache = backup_dir / "_size_cache.json"
        if size_cache.exists():
            try:
                cached = json.load(open(size_cache))
                return int(cached.get("size_bytes", 0))
            except Exception:
                pass

        size_bytes = self._calc_size(backup_dir)
        try:
            with open(size_cache, "w") as handle:
                json.dump({"size_bytes": size_bytes, "computed_at": datetime.now().isoformat()}, handle)
        except Exception:
            pass
        return size_bytes

    def _calc_size(self, path: Path) -> int:
        total = 0
        try:
            for entry in os.scandir(path):
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                    elif entry.is_dir() and not entry.name.startswith("."):
                        total += self._calc_size(Path(entry.path))
                except (OSError, PermissionError):
                    pass
        except (OSError, PermissionError):
            pass
        return total

    def _format_size(self, size_bytes: int) -> str:
        value = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"

    def get_tenant_backup_root(self, tenant: dict, workload: str = "sharepoint") -> Path:
        tenant_slug = slugify_tenant(
            tenant.get("primary_domain")
            or tenant.get("sharepoint_host")
            or tenant.get("name")
        )
        return self.tenant_root / tenant_slug / workload

    def resolve_backup_path(self, tenant_slug: str, workload: str, backup_name: str) -> Path | None:
        if not (backup_name.startswith("backup_") or backup_name.startswith("custom_")):
            return None
        candidates = [
            self.tenant_root / tenant_slug / workload / backup_name,
            self.legacy_root / backup_name,
        ]
        for path in candidates:
            if path.exists() and path.is_dir():
                return path.resolve()
        return None

    def get_backup(self, tenant_slug: str, workload: str, backup_name: str) -> dict | None:
        for item in self.list_all(use_cache=False):
            if (
                item.get("tenant_slug") == tenant_slug
                and item.get("workload") == workload
                and item.get("backup_name") == backup_name
            ):
                return item
        return None

    def browse_backup(self, tenant_slug: str, workload: str, backup_name: str, relative_path: str = "") -> dict:
        backup_path = self.resolve_backup_path(tenant_slug, workload, backup_name)
        if not backup_path:
            raise FileNotFoundError("Backup not found")

        clean_relative = str(relative_path or "").strip().strip("/")
        current_path = (backup_path / clean_relative).resolve() if clean_relative else backup_path
        if current_path != backup_path and backup_path not in current_path.parents:
            raise ValueError("Invalid backup path")
        if not current_path.exists():
            raise FileNotFoundError("Path not found inside backup")
        if not current_path.is_dir():
            raise ValueError("Requested path is not a directory")

        entries = []
        for entry in sorted(current_path.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            child_relative = str(entry.relative_to(backup_path)).replace("\\", "/")
            item = {
                "name": entry.name,
                "relative_path": child_relative,
                "type": "directory" if entry.is_dir() else "file",
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            if entry.is_dir():
                try:
                    item["children_count"] = sum(1 for child in entry.iterdir() if not child.name.startswith("."))
                except OSError:
                    item["children_count"] = None
            else:
                item["size_bytes"] = stat.st_size
                item["size_human"] = self._format_size(stat.st_size)
            entries.append(item)

        backup = self.get_backup(tenant_slug, workload, backup_name) or {
            "tenant_slug": tenant_slug,
            "workload": workload,
            "backup_name": backup_name,
            "backup_path": str(backup_path),
        }
        parent_path = None
        if clean_relative:
            parent_bits = clean_relative.split("/")[:-1]
            parent_path = "/".join(parent_bits)

        return {
            "backup": backup,
            "backup_root": str(backup_path),
            "current_path": clean_relative,
            "parent_path": parent_path,
            "entries": entries,
        }

    def delete(self, tenant_slug: str, workload: str, backup_name: str) -> dict:
        path = self.resolve_backup_path(tenant_slug, workload, backup_name)
        if path:
            shutil.rmtree(path)
            self.invalidate_cache()
            log.info(f"Deleted backup: {path}")
            return {"status": "deleted", "path": str(path), "layout": "legacy" if path.parent == self.legacy_root else "tenant-aware"}
        return {"error": "Not found"}

    def get_history(self, tenant_slug: str, limit: int = 20) -> list:
        history_dir = self.tenant_root / tenant_slug / ".history"
        if history_dir.exists():
            files = sorted(history_dir.glob("backup_*.json"), reverse=True)[:limit]
            results = []
            for item in files:
                try:
                    results.append(json.load(open(item)))
                except Exception:
                    pass
            if results:
                return results

        backups = [b for b in self.list_all(use_cache=False) if b["tenant_slug"] == tenant_slug][:limit]
        return [
            {
                "tenant_slug": backup["tenant_slug"],
                "tenant_name": backup["tenant_name"],
                "workload": backup["workload"],
                "backup_name": backup["backup_name"],
                "date": backup["date"],
                "size_bytes": backup["size_bytes"],
                "size_human": backup["size_human"],
                "layout": backup["layout"],
            }
            for backup in backups
        ]

    def get_stats(self) -> dict:
        backups = self.list_all()
        total_size = sum(item["size_bytes"] for item in backups)
        by_tenant = {}
        by_workload = {}
        for backup in backups:
            by_tenant[backup["tenant_slug"]] = by_tenant.get(backup["tenant_slug"], 0) + 1
            by_workload[backup["workload"]] = by_workload.get(backup["workload"], 0) + 1
        return {
            "total_backups": len(backups),
            "total_size_bytes": total_size,
            "total_size_human": self._format_size(total_size),
            "by_tenant": by_tenant,
            "by_workload": by_workload,
            "tenant_root": str(self.tenant_root),
            "legacy_root": str(self.legacy_root),
        }

    def invalidate_cache(self):
        self._cache.clear()

    def _cache_entries_exist(self, entries: list) -> bool:
        for item in entries:
            backup_path = item.get("backup_path")
            if not backup_path or not Path(backup_path).exists():
                return False
        return True
