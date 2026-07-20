"""SharePoint workload target discovery."""
from app.workloads.base import BaseWorkload


class SharePointWorkload(BaseWorkload):
    workload_type = "sharepoint"

    def list_targets(self):
        sites = []
        try:
            for site in self._paginate(f"{self.GRAPH}/sites?search=*"):
                if site.get("webUrl"):
                    sites.append({
                        "id": site["id"],
                        "name": site.get("displayName") or site.get("name", ""),
                        "url": site["webUrl"],
                        "type": "site",
                    })
        except Exception as e:
            return [{"error": str(e)}]
        return sites
