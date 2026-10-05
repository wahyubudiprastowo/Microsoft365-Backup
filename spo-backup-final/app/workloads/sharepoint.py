"""SharePoint workload target discovery."""
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from app.config_manager import load_config
from app.workloads.base import BaseWorkload


class SharePointWorkload(BaseWorkload):
    workload_type = "sharepoint"

    @staticmethod
    def normalize_site_path(value: str) -> str:
        return str(value or "").strip().strip("/")

    @classmethod
    def target_id_from_path(cls, value: str) -> str:
        normalized = cls.normalize_site_path(value)
        return normalized or "__root__"

    def get_target_selection(self):
        enabled_ids = []
        for site in (load_config().get("sites", []) or []):
            if not site.get("enabled"):
                continue
            enabled_ids.append(self.target_id_from_path(site.get("path", "")))
        return {
            "mode": "selected",
            "selected_ids": enabled_ids,
            "selected_count": len(enabled_ids),
        }

    def _extract_site_path(self, web_url: str) -> str:
        parsed = urlparse(str(web_url or "").strip())
        return self.normalize_site_path(unquote(parsed.path or ""))

    @classmethod
    def classify_target_kind(cls, site_path: str, web_url: str = "") -> str:
        normalized = cls.normalize_site_path(site_path)
        lower_url = str(web_url or "").strip().lower()
        if not normalized:
            return "root_site"
        if normalized.startswith("sites/--private--") or "/--private--" in lower_url:
            return "private_site"
        if normalized.startswith("sites/") or normalized.startswith("teams/"):
            return "standard_site"
        if normalized.startswith("personal/"):
            return "personal_site"
        if normalized.startswith("contentstorage/") or "/contentstorage/" in lower_url:
            return "content_storage"
        return "other_site"

    @staticmethod
    def target_kind_label(kind: str) -> str:
        return {
            "root_site": "Root Site",
            "standard_site": "Standard Site",
            "private_site": "Private Site",
            "personal_site": "Personal Site",
            "content_storage": "Content Storage",
            "other_site": "Other Site",
        }.get(kind, "Site")

    @staticmethod
    def _target_kind_priority(kind: str) -> int:
        return {
            "root_site": 0,
            "standard_site": 1,
            "private_site": 2,
            "personal_site": 3,
            "other_site": 4,
            "content_storage": 5,
        }.get(kind, 9)

    @classmethod
    def _target_score(cls, item: dict) -> tuple:
        kind = str(item.get("target_kind") or "")
        return (
            1 if item.get("enabled") else 0,
            1 if item.get("configured") else 0,
            1 if item.get("size_status") == "cached" else 0,
            -cls._target_kind_priority(kind),
            int(item.get("size_bytes") or 0),
            -len(str(item.get("url") or "")),
        )

    @classmethod
    def _target_sort_key(cls, item: dict) -> tuple:
        kind = str(item.get("target_kind") or "")
        return (
            0 if item.get("enabled") else 1,
            0 if item.get("configured") else 1,
            cls._target_kind_priority(kind),
            0 if item.get("size_status") == "cached" else 1,
            -(int(item.get("size_bytes") or 0)),
            str(item.get("name") or "").lower(),
            str(item.get("path") or "").lower(),
            str(item.get("url") or "").lower(),
        )

    def list_targets(self):
        existing_sites = {
            self.normalize_site_path(site.get("path", "")): site
            for site in (load_config().get("sites", []) or [])
        }
        target_size_cache = self.tenant.get("sharepoint_target_size_cache") or {}
        sites_by_id = {}
        try:
            for site in self._paginate(f"{self.GRAPH}/sites?search=*"):
                if site.get("webUrl"):
                    site_path = self._extract_site_path(site["webUrl"])
                    target_id = self.target_id_from_path(site_path)
                    existing = existing_sites.get(site_path)
                    size_estimate = dict((existing or {}).get("size_estimate") or {})
                    if not size_estimate:
                        size_estimate = dict(
                            target_size_cache.get(target_id)
                            or target_size_cache.get(site.get("id"))
                            or target_size_cache.get(site_path)
                            or {}
                        )
                    target_kind = self.classify_target_kind(site_path, site["webUrl"])
                    item = {
                        "id": target_id,
                        "graph_id": site["id"],
                        "name": site.get("displayName") or site.get("name", ""),
                        "url": site["webUrl"],
                        "path": site_path,
                        "type": "site",
                        "target_kind": target_kind,
                        "target_kind_label": self.target_kind_label(target_kind),
                        "is_standard_site": target_kind in {"root_site", "standard_site"},
                        "configured": bool(existing),
                        "enabled": bool(existing and existing.get("enabled")),
                        "size_bytes": size_estimate.get("size_bytes"),
                        "size_human": size_estimate.get("size_human"),
                        "size_confidence": size_estimate.get("confidence"),
                        "size_updated_at": size_estimate.get("updated_at"),
                        "size_status": "cached" if size_estimate else "unknown",
                    }
                    current = sites_by_id.get(target_id)
                    if current is None or self._target_score(item) > self._target_score(current):
                        sites_by_id[target_id] = item
        except Exception as e:
            return [{"error": str(e)}]
        return sorted(sites_by_id.values(), key=self._target_sort_key)

    def _estimate_from_site(self, site: dict, normalized_path: str = "") -> dict:
        site_id = site.get("id")
        if not site_id:
            raise ValueError("Microsoft Graph did not return a site id")

        drives = self._get(
            f"{self.GRAPH}/sites/{site_id}/drives",
            params={"$select": "id,name,quota,webUrl"},
        ).get("value", [])

        # SharePoint document libraries are exposed as Graph drives, but quota.used
        # can be site-level storage repeated across multiple drives/channel sites.
        # Using the max observed drive quota aligns much closer with SharePoint
        # Admin Center "Storage used" than summing every drive quota.
        used_values = []
        drives_with_quota = 0
        drive_summaries = []
        for drive in drives:
            quota = drive.get("quota") or {}
            used = quota.get("used")
            used_int = 0
            if used is not None:
                try:
                    used_int = max(0, int(used))
                    drives_with_quota += 1
                except (TypeError, ValueError):
                    used_int = 0
            if used_int:
                used_values.append(used_int)
            drive_summaries.append({
                "id": drive.get("id"),
                "name": drive.get("name") or "Documents",
                "web_url": drive.get("webUrl"),
                "used_bytes": used_int,
                "has_quota": used is not None,
            })

        used_bytes = max(used_values) if used_values else 0
        raw_sum_bytes = sum(used_values)
        confidence = "high" if used_values else "unknown"
        if drives and drives_with_quota != len(drives):
            confidence = "partial" if used_values else "unknown"
        if not drives:
            confidence = "unknown"

        return {
            "site_id": site_id,
            "site_name": site.get("displayName") or "",
            "site_url": site.get("webUrl") or "",
            "path": normalized_path,
            "size_bytes": used_bytes,
            "raw_drives_used_sum_bytes": raw_sum_bytes,
            "size_method": "graph_drive_quota_max",
            "drives_count": len(drives),
            "drives_with_quota": drives_with_quota,
            "confidence": confidence,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "drives": drive_summaries,
        }

    def estimate_site_size(self, site_path: str = "") -> dict:
        normalized_path = self.normalize_site_path(site_path)
        host = str(self.tenant.get("sharepoint_host") or "").strip()
        if not host:
            raise ValueError("Active tenant does not have a SharePoint host configured")

        site_url = f"{self.GRAPH}/sites/{host}:/{normalized_path}" if normalized_path else f"{self.GRAPH}/sites/{host}"
        site = self._get(site_url, params={"$select": "id,displayName,webUrl"})
        return self._estimate_from_site(site, normalized_path)

    def estimate_target_size(self, graph_id: str = "", site_path: str = "") -> dict:
        graph_id = str(graph_id or "").strip()
        if graph_id:
            site = self._get(f"{self.GRAPH}/sites/{graph_id}", params={"$select": "id,displayName,webUrl"})
            return self._estimate_from_site(site, self.normalize_site_path(site_path))
        return self.estimate_site_size(site_path)
