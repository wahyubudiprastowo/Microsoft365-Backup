"""SharePoint workload target discovery."""
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

    def list_targets(self):
        existing_sites = {
            self.normalize_site_path(site.get("path", "")): site
            for site in (load_config().get("sites", []) or [])
        }
        sites = []
        try:
            for site in self._paginate(f"{self.GRAPH}/sites?search=*"):
                if site.get("webUrl"):
                    site_path = self._extract_site_path(site["webUrl"])
                    existing = existing_sites.get(site_path)
                    sites.append({
                        "id": self.target_id_from_path(site_path),
                        "graph_id": site["id"],
                        "name": site.get("displayName") or site.get("name", ""),
                        "url": site["webUrl"],
                        "path": site_path,
                        "type": "site",
                        "configured": bool(existing),
                        "enabled": bool(existing and existing.get("enabled")),
                    })
        except Exception as e:
            return [{"error": str(e)}]
        return sites
