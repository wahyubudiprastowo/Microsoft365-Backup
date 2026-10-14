#!/usr/bin/env python3
"""Scan publishable files and optional Git history without printing secrets."""
import argparse
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")
ARCHIVE_SUFFIXES = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".crt", ".pfx", ".p12", ".jks", ".rdb", ".aof", ".sqlite", ".sqlite3", ".bak", ".backup", ".log"}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def local_values(runtime_root=ROOT):
    values = {}

    def add(value, kind):
        if isinstance(value, str) and len(value) >= 8 and not value.startswith(("YOUR_", "replace-", "https://example")):
            values[value.encode()] = kind

    def visit(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if any(term in key.lower() for term in ("secret", "password", "token", "webhook")):
                    add(value, "local-secret")
                elif key in {"tenant_id", "client_id", "object_id", "host"}:
                    add(value, "local-tenant-identifier")
                visit(value)
        elif isinstance(obj, list):
            for value in obj:
                visit(value)

    for path in (runtime_root / "config/config.json", runtime_root / "spo-backup-final/config.json"):
        if path.is_file():
            visit(json.loads(path.read_text()))
    env = runtime_root / "spo-backup-final/.env"
    if env.is_file():
        for line in env.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#") and any(term in key.upper() for term in ("SECRET", "PASSWORD", "TOKEN")):
                add(value.strip().strip("\"'"), "local-secret")
    return values


def prohibited_path(name):
    path = Path(name)
    base = path.name.lower()
    if path.parts and path.parts[0] in {"data", "logs", "redis", "patch", "app-source"}:
        return True
    if base == ".env.example" or base == "config.example.json":
        return False
    return (base == ".env" or base.startswith(".env.") or base == "config.json"
            or (base.startswith("config.") and base.endswith(".json"))
            or path.suffix.lower() in SENSITIVE_SUFFIXES
            or path.suffix.lower() in ARCHIVE_SUFFIXES
            or ".log." in base
            or base in {"credentials.json", "token_cache.json"})


def scan_payload(label, payload, values, findings, depth=0, deny_terms=()):
    if PRIVATE_KEY.search(payload):
        findings.add(("private-key", label))
    for value, kind in values.items():
        if value in payload:
            findings.add((kind, label))
    if any(term.lower() in payload.lower() for term in deny_terms):
        findings.add(("organization-reference", label))
    if payload.startswith(b"PK\x03\x04"):
        if depth >= 3:
            raise ValueError("Nested archive depth limit reached")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("Archive scan size limit reached")
            for item in archive.infolist():
                if not item.is_dir():
                    scan_payload(label + "!" + item.filename, archive.read(item), values, findings, depth + 1, deny_terms)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="Also scan all locally reachable Git history")
    parser.add_argument("--history-ref", help="Scan only commits reachable from this publication ref")
    parser.add_argument("--runtime-root", type=Path, help="Read private local values from this repository root")
    parser.add_argument("--deny-term", action="append", default=[], help="Reject a private identifier without printing its value")
    parser.add_argument("--snapshot", type=Path, help="Copy publishable working-tree files to a new external directory for Gitleaks")
    args = parser.parse_args()
    snapshot = args.snapshot.resolve() if args.snapshot else None
    if snapshot:
        if snapshot == ROOT or ROOT in snapshot.parents:
            parser.error("Snapshot must be outside the repository")
        if snapshot.exists():
            parser.error("Snapshot destination already exists")

    runtime_setting = subprocess.run(["git", "-C", str(ROOT), "config", "--get", "publication.runtimeRoot"], capture_output=True, text=True).stdout.strip()
    runtime_root = args.runtime_root or (Path(runtime_setting) if runtime_setting else ROOT)
    values = local_values(runtime_root.resolve())
    configured_terms = subprocess.run(["git", "-C", str(ROOT), "config", "--get-all", "publication.denyTerm"], capture_output=True, text=True).stdout.splitlines()
    deny_terms = tuple(term.encode() for term in args.deny_term + configured_terms if term)
    findings = set()
    names = sorted(set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""})
    count = 0
    for name in names:
        path = ROOT / name
        if path.is_symlink():
            findings.add(("symlink-review-required", name))
            continue
        if not path.is_file():
            continue
        count += 1
        if prohibited_path(name):
            findings.add(("prohibited-publication-path", name))
        scan_payload(name, path.read_bytes(), values, findings, deny_terms=deny_terms)

    history_count = 0
    if args.history or args.history_ref:
        revision = git("rev-parse", "--verify", "--end-of-options", args.history_ref + "^{commit}").decode().strip() if args.history_ref else "--all"
        for line in git("rev-list", "--objects", revision).decode().splitlines():
            oid, _, name = line.partition(" ")
            if not name or git("cat-file", "-t", oid).strip() != b"blob":
                continue
            history_count += 1
            label = "history:" + oid[:12] + ":" + name
            if prohibited_path(name):
                findings.add(("historical-prohibited-path", label))
            scan_payload(label, git("cat-file", "blob", oid), values, findings, deny_terms=deny_terms)
        for oid in git("rev-list", revision).decode().splitlines():
            scan_payload("commit:" + oid[:12] + ":metadata", git("show", "-s", "--format=fuller", oid), values, findings, deny_terms=deny_terms)

    if snapshot and not findings:
        snapshot.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name in names:
            source = ROOT / name
            if source.is_file():
                destination = snapshot / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)

    print(json.dumps({"working_tree_files": count, "history_blobs": history_count,
                      "local_values_checked": len(values),
                      "findings": [{"type": kind, "location": label} for kind, label in sorted(findings)]}, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
