"""SharePoint restore implementation."""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app.task_control import PauseException
from app.restore.base import BaseRestore

import logging

log = logging.getLogger("spo_backup")


def _join_remote_path(*parts: str) -> str:
    cleaned = [str(part or "").strip("/").strip() for part in parts if str(part or "").strip("/").strip()]
    return "/".join(cleaned)


class SharePointRestore(BaseRestore):
    WORKLOAD_NAME = "sharepoint"

    def __init__(self, tenant, backup_path, target_site_id=None, target_site_path=None, target_library_name=None, target_folder_path=None, **kwargs):
        super().__init__(tenant, backup_path, **kwargs)
        self.target_site_id = target_site_id
        self.target_site_path = target_site_path
        self.target_library_name = target_library_name
        self.target_folder_path = target_folder_path

    def _normalize_target_site_ref(self) -> tuple[str, str]:
        raw_value = str(self.target_site_path or "").strip()
        if not raw_value:
            raise Exception("No target site specified")
        if raw_value.startswith("http://") or raw_value.startswith("https://"):
            parsed = urlparse(raw_value)
            host = parsed.netloc.strip()
            path = parsed.path.strip().lstrip("/")
        else:
            host = str(self.tenant.get("sharepoint_host") or "").strip()
            path = raw_value.lstrip("/")
        if not host or not path:
            raise Exception("Invalid SharePoint target site path")
        return host, path

    def _normalize_target_folder_path(self) -> str:
        raw_value = str(self.target_folder_path or "").strip()
        if not raw_value:
            return ""
        if raw_value.startswith("http://") or raw_value.startswith("https://"):
            parsed = urlparse(raw_value)
            query_id = unquote((parse_qs(parsed.query).get("id") or [""])[0]).strip()
            candidate = query_id or unquote(parsed.path)
            if "/Forms/" in candidate:
                candidate = candidate.split("/Forms/", 1)[0]
            site_path = ""
            try:
                _, site_path = self._normalize_target_site_ref()
            except Exception:
                site_path = ""
            normalized = candidate.strip()
            if site_path and normalized.lower().startswith(f"/{site_path.lower()}"):
                normalized = normalized[len(site_path) + 1:]
            normalized = normalized.lstrip("/")
            parts = [segment for segment in normalized.split("/") if segment]
            if parts and parts[0].lower() in {"shared documents", "documents"}:
                parts = parts[1:]
            return "/".join(parts)
        return raw_value.strip().strip("/")

    def _resolve_target_site(self) -> str:
        if self.target_site_id:
            return self.target_site_id
        if self.target_site_path:
            host, site_path = self._normalize_target_site_ref()
            data = self._get(f"{self.GRAPH}/sites/{host}:/{site_path}")
            return data["id"]
        raise Exception("No target site specified")

    def _get_target_drives(self, target_site_id: str) -> dict[str, str]:
        drives_data = self._get(f"{self.GRAPH}/sites/{target_site_id}/drives")
        target_drives = {drive["name"]: drive["id"] for drive in drives_data.get("value", [])}
        if not target_drives:
            raise Exception("Target site has no document libraries")
        return target_drives

    def _resolve_drive_plan(self, source_library_name: str, target_drives: dict[str, str], target_folder_prefix: str = "") -> dict:
        source_library_name = str(source_library_name or "").strip()
        preferred_drive_name = str(self.target_library_name or "").strip()
        preferred_drive_id = None
        if preferred_drive_name:
            preferred_drive_id = target_drives.get(preferred_drive_name)
            if not preferred_drive_id:
                raise Exception(f"Destination library not found: {preferred_drive_name}")

        matched_drive_id = target_drives.get(source_library_name)
        if preferred_drive_id:
            selected_drive_name = preferred_drive_name
            selected_drive_id = preferred_drive_id
            resolution = "manual_library"
        elif matched_drive_id:
            selected_drive_name = source_library_name
            selected_drive_id = matched_drive_id
            resolution = "source_library_match"
        elif "Documents" in target_drives:
            selected_drive_name = "Documents"
            selected_drive_id = target_drives["Documents"]
            resolution = "documents_fallback"
        else:
            selected_drive_name = next(iter(target_drives.keys()))
            selected_drive_id = target_drives[selected_drive_name]
            resolution = "first_available_fallback"

        remote_root = target_folder_prefix
        if preferred_drive_id:
            if source_library_name and source_library_name != preferred_drive_name:
                remote_root = _join_remote_path(target_folder_prefix, source_library_name)
        elif not matched_drive_id:
            remote_root = _join_remote_path(target_folder_prefix, source_library_name)

        resolved_path = _join_remote_path(selected_drive_name, remote_root)
        return {
            "source_library": source_library_name,
            "selected_library": selected_drive_name,
            "selected_drive_id": selected_drive_id,
            "remote_root": remote_root,
            "resolved_path": resolved_path or selected_drive_name,
            "resolution": resolution,
        }

    def restore(self) -> dict:
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        self.emit("restore_start", {"backup_path": str(self.backup_path)})
        destination_plan = []
        try:
            target_site_id = self._resolve_target_site()
            target_folder_prefix = self._normalize_target_folder_path()
            target_drives = self._get_target_drives(target_site_id)

            site_dirs = self._iter_site_dirs()
            for site_dir in site_dirs:
                if not site_dir.is_dir() or site_dir.name.startswith("_"):
                    continue
                self._check_control()
                self.emit("target_start", {"target_name": site_dir.name})
                try:
                    destination_plan.extend(
                        self._restore_site_dir(site_dir, target_drives, target_folder_prefix=target_folder_prefix)
                    )
                    self.stats["targets_processed"] += 1
                    self.emit("target_done", {"target_name": site_dir.name, "status": "success"})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["targets_failed"] += 1
                    self.stats["errors"].append(f"[{site_dir.name}] {e}")
                    self.emit("target_done", {"target_name": site_dir.name, "status": "failed", "error": str(e)})
        except PauseException:
            self.stats["cancelled"] = True
        except Exception as e:
            log.error(f"SharePoint restore failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")

        if destination_plan:
            self.stats["destination_plan"] = destination_plan
            self.stats["resolved_destination_summary"] = "; ".join(
                sorted({item.get("resolved_path") for item in destination_plan if item.get("resolved_path")})
            )

        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
        self.emit("restore_done")
        return self.stats

    def _iter_site_dirs(self) -> list[Path]:
        meta_file = self.backup_path / "_backup_metadata.json"
        if meta_file.exists():
            return [self.backup_path]
        return [site_dir for site_dir in self.backup_path.iterdir()]

    def _restore_site_dir(self, site_dir: Path, target_drives: dict, target_folder_prefix: str = ""):
        destination_plan = []
        for lib_dir in site_dir.iterdir():
            if not lib_dir.is_dir() or lib_dir.name.startswith("_"):
                continue
            plan = self._resolve_drive_plan(lib_dir.name, target_drives, target_folder_prefix=target_folder_prefix)
            drive_id = plan["selected_drive_id"]
            remote_root = plan["remote_root"]
            destination_plan.append(plan)
            self._upload_folder(lib_dir, drive_id, remote_root)
        return destination_plan

    def _upload_folder(self, local_dir: Path, drive_id: str, remote_path: str):
        for item in local_dir.iterdir():
            self._check_control()
            if item.name.startswith("_") or item.name.startswith("."):
                continue
            if item.is_dir():
                sub_remote = f"{remote_path}/{item.name}" if remote_path else item.name
                self._ensure_remote_folder(drive_id, sub_remote)
                self._upload_folder(item, drive_id, sub_remote)
            elif item.is_file():
                self._upload_file(item, drive_id, remote_path)

    def _ensure_remote_folder(self, drive_id: str, remote_path: str) -> str | None:
        normalized = str(remote_path or "").strip().strip("/")
        if not normalized:
            return None
        parent_id = "root"
        current_path = ""
        for segment in [part for part in normalized.split("/") if part]:
            current_path = _join_remote_path(current_path, segment)
            try:
                existing = self._get(f"{self.GRAPH}/drives/{drive_id}/root:/{current_path}")
                parent_id = existing["id"]
                continue
            except Exception:
                pass
            created = self._post(
                f"{self.GRAPH}/drives/{drive_id}/items/{parent_id}/children",
                json_body={
                    "name": segment,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "replace",
                },
            )
            parent_id = created["id"]
        return parent_id

    def _upload_file(self, local_file: Path, drive_id: str, remote_path: str):
        file_name = local_file.name
        remote_full = f"{remote_path}/{file_name}" if remote_path else file_name
        if remote_path:
            self._ensure_remote_folder(drive_id, remote_path)
        if self.mode == "merge":
            try:
                existing = self._get(f"{self.GRAPH}/drives/{drive_id}/root:/{remote_full}")
                if existing.get("id"):
                    self.stats["items_skipped"] += 1
                    return
            except Exception:
                pass
        try:
            size = local_file.stat().st_size
            if size < 4 * 1024 * 1024:
                with open(local_file, "rb") as handle:
                    self._put(f"{self.GRAPH}/drives/{drive_id}/root:/{remote_full}:/content", data=handle)
            else:
                self._upload_large_file(local_file, drive_id, remote_full)
            self.stats["items_processed"] += 1
            self.stats["bytes_uploaded"] += size
            self.emit("file_uploaded", {"current_file": file_name})
        except PauseException:
            raise
        except Exception as e:
            self.stats["items_failed"] += 1
            self.stats["errors"].append(f"{file_name}: {str(e)[:120]}")

    def _upload_large_file(self, local_file: Path, drive_id: str, remote_path: str):
        session = self._post(
            f"{self.GRAPH}/drives/{drive_id}/root:/{remote_path}:/createUploadSession",
            json_body={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        upload_url = session["uploadUrl"]
        chunk_size = 5 * 1024 * 1024
        total = local_file.stat().st_size
        with open(local_file, "rb") as handle:
            start = 0
            while start < total:
                self._check_control()
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                end = start + len(chunk) - 1
                response = self.session.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{total}",
                    },
                    timeout=300,
                )
                response.raise_for_status()
                start = end + 1

    def dry_run(self) -> dict:
        result = {"workload": "sharepoint", "files_to_upload": 0, "total_size_bytes": 0}
        target_site_id = self._resolve_target_site()
        target_drives = self._get_target_drives(target_site_id)
        target_folder_prefix = self._normalize_target_folder_path()
        destination_plan = []
        for site_dir in self._iter_site_dirs():
            if not site_dir.is_dir() or site_dir.name.startswith("_"):
                continue
            for lib_dir in site_dir.iterdir():
                if not lib_dir.is_dir() or lib_dir.name.startswith("_"):
                    continue
                destination_plan.append(
                    self._resolve_drive_plan(lib_dir.name, target_drives, target_folder_prefix=target_folder_prefix)
                )
                for item in lib_dir.rglob("*"):
                    if item.is_file() and not item.name.startswith("_"):
                        result["files_to_upload"] += 1
                        result["total_size_bytes"] += item.stat().st_size
        result["target_site_id"] = target_site_id
        result["target_library_requested"] = str(self.target_library_name or "").strip() or None
        result["target_folder_requested"] = target_folder_prefix or None
        result["available_target_libraries"] = sorted(target_drives.keys())
        result["target_folder_behavior"] = (
            "will_create_if_missing" if target_folder_prefix else "library_root"
        )
        result["destination_plan"] = destination_plan
        result["resolved_destination_summary"] = "; ".join(
            sorted({item.get("resolved_path") for item in destination_plan if item.get("resolved_path")})
        )
        return result
