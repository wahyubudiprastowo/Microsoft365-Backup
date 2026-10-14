#!/usr/bin/env python3
"""Render documentation PNGs from app templates using synthetic data only."""
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "docs/assets"


class WithoutScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.skip = True
        elif not self.skip:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == "script":
            self.skip = False
        elif not self.skip:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def handle_entityref(self, name):
        if not self.skip:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip:
            self.parts.append(f"&#{name};")


def main():
    environment = Environment(
        loader=FileSystemLoader(ROOT / "spo-backup-final/app/templates"),
        autoescape=select_autoescape(),
    )
    config = {"sites": [{"name": name, "enabled": True} for name in
                        ["Example Project", "Example Operations", "Example Knowledge Hub"]]}
    summary = {"sites_total": 24, "sites_enabled": 20, "backups_total": 8,
               "schedule": {"cron_expression": "0 2 * * *", "scope": "Tenant schedule",
                            "owner": "Example Tenant", "enabled": True, "timezone": "UTC"}}
    backups = [{"backup_name": "backup_current", "date": "14 Oct 2026", "size_human": size,
                "tenant_name": "Example Tenant", "workload": workload, "summary": "Demo archive"}
               for workload, size in [("sharepoint", "18.4 GB"), ("onedrive", "12.8 GB"), ("outlook", "4.6 GB")]]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1920, "height": 1200}, device_scale_factor=1)
        for filename, template, route in [("dashboard.png", "dashboard.html", "/"),
                                           ("backups.png", "backups.html", "/backups"),
                                           ("restore.png", "restore_v2.html", "/restore")]:
            html = environment.get_template(template).render(
                config=config, dashboard_summary=summary, backups=backups,
                logs=["DEMO: Scheduled backup started for Example Tenant",
                      "DEMO: File inventory verified; background transfer in progress"],
                request=SimpleNamespace(path=route),
            )
            clean = WithoutScripts()
            clean.feed(html)
            page.set_content("".join(clean.parts), wait_until="networkidle")
            page.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important}")
            page.evaluate("""() => {
                const set = (id, value) => {const e = document.getElementById(id); if (e) e.textContent = value;};
                set('currentTime', 'Demo workspace');
                const values = {
                    bCurrentSite:'Example Project', bFiles:'1,024 / 2,048',
                    bFilesHint:'Remaining: 1,024 files', bSpeed:'12 MiB/s',
                    bSpeedHint:'Processed: 24 items/s', bEta:'2m 45s',
                    bFilesDownloaded:'960', bFilesSkipped:'64', bFilesResumed:'12',
                    bLiveSize:'8.2 GB', bLiveSizeHint:'Stored so far: 8.2 GB',
                    bCurrentFile:'Documents/Planning.xlsx', bOverallPct:'50%', bTaskId:'demo-backup-run',
                    bpFiles:'1,024 / 2,048', bpFilesHint:'Remaining: 1,024 files',
                    bpDownloaded:'960', bpSkipped:'64', bpResumed:'12',
                    bpCurrentFile:'Documents/Planning.xlsx', bpPct:'50%', bpSpeed:'12 MiB/s', bpEta:'2m 45s',
                    bpLiveSize:'8.2 GB', bpLiveSizeHint:'Stored so far: 8.2 GB',
                    bpSpeedHint:'Processed: 24 items/s', bpTaskId:'demo-backup-run',
                    statTotal:'8', statSize:'35.8 GB', statTenants:'1', statWorkloads:'4'
                };
                Object.entries(values).forEach(([id,value]) => set(id,value));
                for (const id of ['progressPanel','backupProgressPanel']) {
                    const e = document.getElementById(id); if (e) e.style.display='block';
                }
                for (const id of ['bProgressBar','bpBar']) {
                    const e = document.getElementById(id); if (e) e.style.width='50%';
                }
                set('opsQueueCount', '0 queued'); set('opsQueueList', 'No queued operations');
                const list = document.getElementById('backupsList');
                if (list) list.innerHTML = `<table class="table-modern"><thead><tr><th>Tenant</th><th>Workload</th><th>Backup</th><th>Size</th><th>Status</th></tr></thead><tbody>
                    ${[['SharePoint','18.4 GB'],['OneDrive','12.8 GB'],['Outlook','4.6 GB'],['Teams','640 MB']].map(([name,size]) =>
                    `<tr><td>Example Tenant</td><td>${name}</td><td>backup_current</td><td>${size}</td><td><span class="badge-modern badge-success">Completed</span></td></tr>`).join('')}
                    </tbody></table>`;
                for (const id of ['rSourceTenant','rTargetTenant']) {
                    const e=document.getElementById(id); if (e) e.innerHTML='<option>Example Tenant</option>';
                }
                const backup=document.getElementById('rBackup');
                if (backup) backup.innerHTML='<option>SharePoint / backup_current</option>';
                const first=document.querySelector('.workload-btn'); if(first) first.classList.add('active');
                set('workloadReadiness','SharePoint file and folder recovery');
                set('backupInfo','Example Tenant · SharePoint · Demo backup');
                const jobs=document.getElementById('jobsContainer');
                if(jobs) jobs.innerHTML=`<div style="padding:20px;border:1px solid var(--border);border-radius:8px">
                    <div style="display:flex;justify-content:space-between;gap:16px"><strong>SharePoint restore</strong><span class="badge-modern badge-success">Completed</span></div>
                    <div style="color:var(--text-muted);margin:16px 0">Example Tenant · Merge · 128 files restored</div>
                    <div class="progress-modern"><div class="progress-bar-modern" style="width:100%"></div></div>
                    </div>`;
            }""")
            page.evaluate("document.fonts.ready")
            assert page.locator(".sidebar-nav").count() == 1
            assert page.locator("body").bounding_box()["width"] == 1920
            page.screenshot(path=str(ASSETS / filename), full_page=False)
            print(f"Rendered {filename} with synthetic data")
        browser.close()


if __name__ == "__main__":
    main()
