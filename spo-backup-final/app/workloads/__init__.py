"""Pluggable workload registry."""
from app.workloads.onedrive import OneDriveWorkload
from app.workloads.outlook import OutlookWorkload
from app.workloads.sharepoint import SharePointWorkload
from app.workloads.teams import TeamsWorkload

BACKUP_ENABLED_WORKLOADS = {"sharepoint", "onedrive", "outlook", "teams"}
DISCOVERY_ENABLED_WORKLOADS = {"sharepoint", "onedrive", "outlook", "teams"}

WORKLOADS = {
    "sharepoint": SharePointWorkload,
    "onedrive": OneDriveWorkload,
    "outlook": OutlookWorkload,
    "teams": TeamsWorkload,
}

WORKLOAD_META = {
    "sharepoint": {
        "name": "SharePoint Online",
        "icon": "bi-globe2",
        "color": "var(--primary)",
        "description": "Sites and document libraries",
        "supports_backup": True,
        "supports_target_discovery": True,
        "supports_target_selection": False,
        "required_scopes": ["Sites.Read.All", "Files.Read.All"],
        "backup_scope": "Backs up enabled SharePoint sites from the Sites page.",
        "selection_label": "Scope is controlled from Sites, not from this target picker.",
        "manage_href": "/sites",
        "manage_label": "Manage Sites",
    },
    "onedrive": {
        "name": "OneDrive for Business",
        "icon": "bi-cloud-fill",
        "color": "var(--accent)",
        "description": "User files and folders",
        "supports_backup": True,
        "supports_target_discovery": True,
        "supports_target_selection": True,
        "required_scopes": ["Files.Read.All", "User.Read.All"],
        "backup_scope": "Backs up all discovered user drives by default, or only selected users if a target scope is saved here.",
        "selection_label": "You can switch between all discovered users and a saved selected-user scope.",
        "manage_href": "/backups",
        "manage_label": "Open Backup History",
    },
    "outlook": {
        "name": "Outlook / Exchange",
        "icon": "bi-envelope-fill",
        "color": "var(--warning)",
        "description": "Mail, calendar, contacts",
        "supports_backup": True,
        "supports_target_discovery": True,
        "supports_target_selection": True,
        "required_scopes": ["Mail.Read", "Calendars.Read", "Contacts.Read", "User.Read.All"],
        "backup_scope": "Backs up all discovered mailboxes by default, or only selected users if a target scope is saved here.",
        "selection_label": "You can save a mailbox subset for tenant-aware backup runs.",
        "manage_href": "/restore",
        "manage_label": "Open Restore",
    },
    "teams": {
        "name": "Microsoft Teams",
        "icon": "bi-microsoft-teams",
        "color": "#6264a7",
        "description": "Teams, channels, messages, replies, files metadata",
        "supports_backup": True,
        "supports_target_discovery": True,
        "supports_target_selection": True,
        "required_scopes": [
            "Team.ReadBasic.All",
            "Channel.ReadBasic.All",
            "ChannelMessage.Read.All",
            "ChannelSettings.Read.All",
            "TeamMember.Read.All",
            "Files.Read.All",
        ],
        "backup_scope": "Backs up all discovered Teams by default, or only selected teams if a target scope is saved here.",
        "selection_label": "Use target selection to limit which Teams are included in tenant-aware runs.",
        "manage_href": "/backups",
        "manage_label": "Open Backup History",
    },
}


def get_workload(workload_type, tenant_config):
    cls = WORKLOADS.get(workload_type)
    if not cls:
        raise ValueError(f"Unknown workload: {workload_type}")
    return cls(tenant_config)


def filter_backup_workloads(workloads):
    return [
        str(item).strip().lower()
        for item in (workloads or [])
        if str(item).strip().lower() in BACKUP_ENABLED_WORKLOADS
    ]
