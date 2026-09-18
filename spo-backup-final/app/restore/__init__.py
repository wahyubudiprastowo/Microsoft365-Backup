"""Restore workload factory."""
from app.restore.base import BaseRestore
from app.restore.onedrive import OneDriveRestore
from app.restore.outlook import OutlookRestore
from app.restore.sharepoint import SharePointRestore
from app.restore.teams import TeamsRestore

RESTORE_REGISTRY = {
    "sharepoint": SharePointRestore,
    "onedrive": OneDriveRestore,
    "outlook": OutlookRestore,
    "teams": TeamsRestore,
}


def get_restore(workload: str, tenant: dict, backup_path: str, progress_callback=None, task_id=None, mode="merge", **extra_kwargs) -> BaseRestore:
    cls = RESTORE_REGISTRY.get(workload)
    if not cls:
        raise ValueError(f"Unknown workload: {workload}")
    return cls(
        tenant=tenant,
        backup_path=backup_path,
        progress_callback=progress_callback,
        task_id=task_id,
        mode=mode,
        **extra_kwargs,
    )
