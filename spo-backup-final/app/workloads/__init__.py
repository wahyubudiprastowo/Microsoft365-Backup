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
        "required_scopes": ["Sites.Read.All", "Files.Read.All"],
    },
    "onedrive": {
        "name": "OneDrive for Business",
        "icon": "bi-cloud-fill",
        "color": "var(--accent)",
        "description": "User files and folders",
        "supports_backup": True,
        "supports_target_discovery": True,
        "required_scopes": ["Files.Read.All", "User.Read.All"],
    },
    "outlook": {
        "name": "Outlook / Exchange",
        "icon": "bi-envelope-fill",
        "color": "var(--warning)",
        "description": "Mail, calendar, contacts",
        "supports_backup": True,
        "supports_target_discovery": True,
        "required_scopes": ["Mail.Read", "Calendars.Read", "Contacts.Read", "User.Read.All"],
    },
    "teams": {
        "name": "Microsoft Teams",
        "icon": "bi-microsoft-teams",
        "color": "#6264a7",
        "description": "Teams, channels, messages, replies, files metadata",
        "supports_backup": True,
        "supports_target_discovery": True,
        "required_scopes": [
            "Team.ReadBasic.All",
            "Channel.ReadBasic.All",
            "ChannelMessage.Read.All",
            "ChannelSettings.Read.All",
            "TeamMember.Read.All",
            "Files.Read.All",
        ],
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
