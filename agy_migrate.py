#!/usr/bin/env python3
"""
agy-migrate: Antigravity Cross-MacBook Migration Tool
=====================================================
A self-contained, production-grade utility to migrate complete Antigravity
installations, configurations, MCP servers, browser profiles, credentials,
and complete conversation history across MacBook endpoints.

Author: Antigravity Migration Suite
License: Apache 2.0
"""

import argparse
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Version info
VERSION = "1.0.0"
MANIFEST_VERSION = "1.0"

# ANSI Colors for Terminal Output
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

def log_info(msg: str):
    print(f"{Colors.BLUE}[INFO]{Colors.RESET} {msg}")

def log_success(msg: str):
    print(f"{Colors.GREEN}[SUCCESS]{Colors.RESET} {msg}")

def log_warn(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {msg}")

def log_error(msg: str):
    print(f"{Colors.RED}[ERROR]{Colors.RESET} {msg}")

def log_header(title: str):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN} {title}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}\n")

# Volatile caches that should be excluded to prevent bloat and cross-GPU crashes
EXCLUDED_DIR_NAMES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "ShaderCache",
    "Crashpad",
    "blob_storage",
    "tmp",
    "temp",
    ".DS_Store",
}

EXCLUDED_EXTENSIONS = {
    ".tmp",
    ".lock",
    ".log.old",
}

def should_exclude(path_str: str) -> bool:
    """Determine whether a file or directory path should be excluded from the archive."""
    parts = Path(path_str).parts
    for p in parts:
        if p in EXCLUDED_DIR_NAMES:
            return True
        if p.startswith("Cache_Data"):
            return True
    _, ext = os.path.splitext(path_str)
    if ext in EXCLUDED_EXTENSIONS:
        return True
    return False

def calculate_sha256(filepath: str) -> str:
    """Calculate SHA-256 checksum of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_running_processes() -> List[str]:
    """Check for active Antigravity processes that might lock databases."""
    active = []
    targets = ["Antigravity", "Antigravity IDE", "agy", "language_server"]
    for t in targets:
        try:
            res = subprocess.run(
                ["pgrep", "-f", t],
                capture_output=True,
                text=True,
                check=False
            )
            if res.returncode == 0 and res.stdout.strip():
                pids = res.stdout.strip().splitlines()
                my_pid = str(os.getpid())
                if any(p != my_pid for p in pids):
                    active.append(t)
        except Exception:
            pass
    return list(set(active))

def kill_running_processes(prompt: bool = True, auto_confirm: bool = False) -> bool:
    """Prompt user or terminate active Antigravity processes safely."""
    procs = get_running_processes()
    if not procs:
        return True
    
    log_warn(f"Detected running Antigravity processes: {', '.join(procs)}")
    if prompt and not auto_confirm and sys.stdin.isatty():
        try:
            answer = input(f"{Colors.YELLOW}Would you like to gracefully close these processes now? [Y/n]: {Colors.RESET}").strip().lower()
            if answer not in ("", "y", "yes"):
                log_warn("Continuing without stopping Antigravity processes.")
                return True
        except (KeyboardInterrupt, EOFError):
            print()
            return False
    elif not sys.stdin.isatty() and not auto_confirm:
        # Running in a non-interactive environment without explicit -y
        log_info("Non-interactive terminal detected: skipping process termination prompt.")
        return True

    for p in procs:
        try:
            log_info(f"Stopping {p}...")
            subprocess.run(["pkill", "-TERM", "-f", p], check=False)
        except Exception as e:
            log_warn(f"Failed to stop {p}: {e}")
    time.sleep(1)
    
    remaining = get_running_processes()
    if remaining:
        log_warn(f"Processes still running ({', '.join(remaining)}), sending KILL signal...")
        for p in remaining:
            subprocess.run(["pkill", "-9", "-f", p], check=False)
        time.sleep(0.5)
    return True

# ---------------------------------------------------------------------------
# Path & Discovery Definitions
# ---------------------------------------------------------------------------

class MigrationPaths:
    """Encapsulates all paths required for an Antigravity instance on macOS."""
    def __init__(self, home: Optional[str] = None):
        self.home = os.path.expanduser(home or "~")
        self.gemini_dir = os.path.join(self.home, ".gemini")
        
        # 1. Complete Brain & History
        self.brain_dir = os.path.join(self.gemini_dir, "antigravity", "brain")
        self.antigravity_data = os.path.join(self.gemini_dir, "antigravity")
        
        # 2. Configurations, Customizations & Skills
        self.config_dir = os.path.join(self.gemini_dir, "config")
        self.skills_dir = os.path.join(self.gemini_dir, "skills")
        self.settings_json = os.path.join(self.gemini_dir, "settings.json")
        self.state_json = os.path.join(self.gemini_dir, "state.json")
        self.gemini_md = os.path.join(self.gemini_dir, "GEMINI.md")
        self.agents_md = os.path.join(self.gemini_dir, "AGENTS.md")
        
        # 3. CLI and IDE state
        self.cli_dir = os.path.join(self.gemini_dir, "antigravity-cli")
        self.ide_dir = os.path.join(self.gemini_dir, "antigravity-ide")
        self.browser_profile = os.path.join(self.gemini_dir, "antigravity-browser-profile")
        
        # 4. Authentication & Licensing
        self.oauth_creds = os.path.join(self.gemini_dir, "oauth_creds.json")
        self.jetski_token = os.path.join(self.gemini_dir, "jetski-standalone-oauth-token")
        self.installation_id = os.path.join(self.gemini_dir, "installation_id")
        
        # 5. Application Support (Electron data)
        self.app_support_antigravity = os.path.join(self.home, "Library", "Application Support", "Antigravity")
        self.app_support_ide = os.path.join(self.home, "Library", "Application Support", "Antigravity IDE")
        
        # 6. Applications
        self.apps = [
            "/Applications/Antigravity.app",
            "/Applications/Antigravity IDE.app",
            os.path.join(self.home, "Applications", "Antigravity.app"),
            os.path.join(self.home, "Applications", "Antigravity IDE.app"),
        ]
        
        # 7. CLI Launchers & Symlinks
        self.cli_launchers = [
            os.path.join(self.home, ".local", "bin", "agy"),
            os.path.join(self.home, ".local", "bin", "antigravity-ide"),
            "/usr/local/bin/agy",
            "/usr/local/bin/antigravity-ide",
        ]

    def get_export_candidates(self, include_apps: bool = True, include_credentials: bool = True) -> List[Tuple[str, str]]:
        """
        Returns list of (absolute_source_path, archive_relative_path).
        """
        candidates: List[Tuple[str, str]] = []
        
        def add_if_exists(abs_path: str, arc_rel: str):
            if os.path.exists(abs_path):
                candidates.append((abs_path, arc_rel))
        
        # Brain & History (Critical)
        add_if_exists(self.antigravity_data, "home/.gemini/antigravity")
        
        # Customizations & Configs
        add_if_exists(self.config_dir, "home/.gemini/config")
        add_if_exists(self.skills_dir, "home/.gemini/skills")
        add_if_exists(self.settings_json, "home/.gemini/settings.json")
        add_if_exists(self.state_json, "home/.gemini/state.json")
        add_if_exists(self.gemini_md, "home/.gemini/GEMINI.md")
        add_if_exists(self.agents_md, "home/.gemini/AGENTS.md")
        
        # CLI & IDE & Browser
        add_if_exists(self.cli_dir, "home/.gemini/antigravity-cli")
        add_if_exists(self.ide_dir, "home/.gemini/antigravity-ide")
        add_if_exists(self.browser_profile, "home/.gemini/antigravity-browser-profile")
        
        # Identity
        add_if_exists(self.installation_id, "home/.gemini/installation_id")
        
        # Credentials
        if include_credentials:
            add_if_exists(self.oauth_creds, "home/.gemini/oauth_creds.json")
            add_if_exists(self.jetski_token, "home/.gemini/jetski-standalone-oauth-token")
            
        # Application Support
        add_if_exists(self.app_support_antigravity, "home/Library/Application Support/Antigravity")
        add_if_exists(self.app_support_ide, "home/Library/Application Support/Antigravity IDE")
        
        # Apps
        if include_apps:
            for app_path in self.apps:
                if os.path.exists(app_path):
                    if app_path.startswith("/Applications/"):
                        arc_name = f"apps/{os.path.basename(app_path)}"
                    else:
                        arc_name = f"home/Applications/{os.path.basename(app_path)}"
                    candidates.append((app_path, arc_name))
                    
        # CLI Launchers
        for cli_path in self.cli_launchers:
            if os.path.exists(cli_path):
                if cli_path.startswith(self.home):
                    rel = os.path.relpath(cli_path, self.home)
                    candidates.append((cli_path, f"home/{rel}"))
                else:
                    candidates.append((cli_path, f"system_bin/{os.path.basename(cli_path)}"))
                    
        return candidates

# ---------------------------------------------------------------------------
# Path Translation Engine
# ---------------------------------------------------------------------------

class PathRemapper:
    """
    Translates absolute paths between different Mac usernames and Homebrew architectures:
    e.g. /Users/olduser -> /Users/newuser
    and  /usr/local (Intel) -> /opt/homebrew (Apple Silicon) if applicable.
    """
    def __init__(self, src_user_home: str, dst_user_home: str, src_arch: str, dst_arch: str):
        self.src_home = src_user_home.rstrip("/")
        self.dst_home = dst_user_home.rstrip("/")
        self.src_arch = src_arch
        self.dst_arch = dst_arch
        
        self.brew_remap = False
        if src_arch in ("x86_64", "i386") and dst_arch in ("arm64", "aarch64"):
            self.old_brew = "/usr/local"
            self.new_brew = "/opt/homebrew"
            self.brew_remap = True
        elif src_arch in ("arm64", "aarch64") and dst_arch in ("x86_64", "i386"):
            self.old_brew = "/opt/homebrew"
            self.new_brew = "/usr/local"
            self.brew_remap = True

    def remap_string(self, content: str) -> str:
        """Remap home directories and optional brew paths within a text string."""
        if self.src_home and self.dst_home and self.src_home != self.dst_home:
            content = content.replace(self.src_home, self.dst_home)
        if self.brew_remap:
            content = content.replace(f"{self.old_brew}/bin", f"{self.new_brew}/bin")
            content = content.replace(self.old_brew, self.new_brew)
        return content

    def remap_json_obj(self, obj):
        """Recursively remap string values and object keys in JSON structures."""
        if isinstance(obj, dict):
            new_dict = {}
            for k, v in obj.items():
                new_k = self.remap_string(k) if isinstance(k, str) else k
                new_dict[new_k] = self.remap_json_obj(v)
            return new_dict
        elif isinstance(obj, list):
            return [self.remap_json_obj(i) for i in obj]
        elif isinstance(obj, str):
            return self.remap_string(obj)
        else:
            return obj

    def remap_file_in_place(self, filepath: str) -> bool:
        """Read file, remap paths, and write back if changes were made."""
        try:
            if not os.path.isfile(filepath):
                return False
            if filepath.endswith((".json", ".jsonl", ".md", ".txt", ".sh", ".plist", ".yaml", ".yml")):
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                new_content = self.remap_string(content)
                if new_content != content:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    return True
        except Exception as e:
            log_warn(f"Could not remap paths in {filepath}: {e}")
        return False

# ---------------------------------------------------------------------------
# Core Operations: EXPORT
# ---------------------------------------------------------------------------

def run_export(
    output_path: Optional[str] = None,
    include_apps: bool = False,
    include_credentials: bool = True,
    auto_confirm: bool = False,
    dry_run: bool = False
) -> Optional[str]:
    """
    Exports Antigravity instance, configurations, and conversation history.
    """
    mode_str = "FULL (+Apps)" if include_apps else "DATA (Configs, History & Settings)"
    log_header(f"Antigravity Migration Export ({mode_str})")
    
    paths = MigrationPaths()
    
    # 1. Pre-flight check
    if not dry_run:
        kill_running_processes(prompt=True, auto_confirm=auto_confirm)
    
    # 2. Discover files
    candidates = paths.get_export_candidates(
        include_apps=include_apps,
        include_credentials=include_credentials
    )
    
    if not candidates:
        log_error("No Antigravity installation, settings, or history found to export.")
        return None

    # Calculate default output filename if not provided
    if not output_path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        suffix = "full" if include_apps else "data"
        output_path = os.path.expanduser(f"~/Desktop/antigravity_migration_{suffix}_{ts}.tar.gz")
    else:
        output_path = os.path.expanduser(output_path)
        
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    # 3. Collect files and calculate manifest
    log_info("Analyzing and indexing components...")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "tool_version": VERSION,
        "timestamp": time.time(),
        "date_str": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "mode": "full" if include_apps else "data",
        "include_apps": include_apps,
        "include_credentials": include_credentials,
        "source_system": {
            "os": platform.system(),
            "mac_ver": platform.mac_ver()[0],
            "arch": platform.machine(),
            "username": os.getlogin() if hasattr(os, 'getlogin') else os.environ.get("USER", "unknown"),
            "home": paths.home,
        },
        "components": [],
        "conversations": {
            "count": 0,
            "ids": []
        }
    }

    # Count conversations in brain
    if os.path.exists(paths.brain_dir):
        try:
            for item in os.listdir(paths.brain_dir):
                full_p = os.path.join(paths.brain_dir, item)
                if os.path.isdir(full_p) and not item.startswith("."):
                    manifest["conversations"]["ids"].append(item)
            manifest["conversations"]["count"] = len(manifest["conversations"]["ids"])
        except Exception:
            pass

    total_files = 0
    total_uncompressed_bytes = 0
    
    print(f"\n{Colors.BOLD}{'Component':<45} {'Size':>12}{Colors.RESET}")
    print("-" * 60)
    
    filtered_items: List[Tuple[str, str]] = [] # (abs_path, arc_path)
    
    for src_path, arc_base in candidates:
        component_size = 0
        component_files = 0
        
        if os.path.isfile(src_path):
            if not should_exclude(src_path):
                filtered_items.append((src_path, arc_base))
                size = os.path.getsize(src_path)
                component_size += size
                component_files += 1
        elif os.path.isdir(src_path):
            for root, dirs, files in os.walk(src_path):
                dirs[:] = [d for d in dirs if not should_exclude(d)]
                for f in files:
                    full_f = os.path.join(root, f)
                    if not should_exclude(full_f):
                        rel_in_dir = os.path.relpath(full_f, src_path)
                        arc_rel = os.path.join(arc_base, rel_in_dir)
                        filtered_items.append((full_f, arc_rel))
                        try:
                            size = os.path.getsize(full_f)
                            component_size += size
                            component_files += 1
                        except OSError:
                            pass
                            
        total_files += component_files
        total_uncompressed_bytes += component_size
        
        manifest["components"].append({
            "source_path": src_path,
            "archive_base": arc_base,
            "file_count": component_files,
            "size_bytes": component_size
        })
        
        size_display = f"{component_size / (1024 * 1024):.2f} MB"
        disp_name = src_path.replace(paths.home, "~")
        if len(disp_name) > 42:
            disp_name = "..." + disp_name[-39:]
        print(f"{disp_name:<45} {size_display:>12}")
        
    print("-" * 60)
    print(f"{Colors.BOLD}{'Total:':<45} {total_uncompressed_bytes / (1024 * 1024):.2f} MB ({total_files} files){Colors.RESET}")
    print(f"{Colors.CYAN}Conversations to migrate:{Colors.RESET} {manifest['conversations']['count']} sessions\n")

    if dry_run:
        log_info(f"[DRY-RUN] Would create archive at: {output_path}")
        return output_path

    # 4. Create Archive
    log_info(f"Packaging archive to: {output_path}")
    temp_dir = tempfile.mkdtemp(prefix="agy_export_")
    try:
        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2)

        start_t = time.time()
        with tarfile.open(output_path, "w:gz", compresslevel=6) as tar:
            # Add manifest first
            tar.add(manifest_path, arcname="manifest.json")
            
            # Add all items
            idx = 0
            for abs_path, arc_path in filtered_items:
                idx += 1
                if idx % 500 == 0 or idx == len(filtered_items):
                    pct = (idx / len(filtered_items)) * 100
                    print(f"\rPacking: {pct:5.1f}% [{idx}/{len(filtered_items)} files]", end="", flush=True)
                tar.add(abs_path, arcname=arc_path, recursive=False)
                
        print()
        elapsed = time.time() - start_t
        
        archive_size = os.path.getsize(output_path)
        sha256 = calculate_sha256(output_path)
        
        # Write checksum file alongside archive
        checksum_file = f"{output_path}.sha256"
        with open(checksum_file, "w") as cf:
            cf.write(f"{sha256}  {os.path.basename(output_path)}\n")

        log_success(f"Archive successfully generated in {elapsed:.1f}s")
        log_info(f"Archive Size: {archive_size / (1024 * 1024):.2f} MB")
        log_info(f"SHA-256: {sha256}")
        log_info(f"Checksum file: {checksum_file}")
        
        print(f"\n{Colors.BOLD}{Colors.GREEN}Next Steps to Transfer to New MacBook:{Colors.RESET}")
        print(f" 1. Transfer {output_path} via AirDrop, Thunderbolt cable, or USB SSD.")
        print(f" 2. On the destination MacBook, run:")
        print(f"    {Colors.CYAN}./agy-migrate import --archive {os.path.basename(output_path)}{Colors.RESET}\n")
        
        return output_path
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Core Operations: INSPECT
# ---------------------------------------------------------------------------

def run_inspect(archive_path: str):
    """
    Inspects a migration bundle without extracting it.
    """
    archive_path = os.path.expanduser(archive_path)
    if not os.path.isfile(archive_path):
        log_error(f"Archive not found: {archive_path}")
        return False

    log_header("Inspecting Antigravity Migration Archive")
    log_info(f"Path: {archive_path}")
    log_info(f"Archive Size: {os.path.getsize(archive_path) / (1024 * 1024):.2f} MB")

    try:
        with tarfile.open(archive_path, "r:*") as tar:
            try:
                manifest_member = tar.getmember("manifest.json")
                f = tar.extractfile(manifest_member)
                if f:
                    manifest = json.loads(f.read().decode("utf-8"))
                else:
                    log_error("Could not read manifest.json from archive.")
                    return False
            except KeyError:
                log_error("Archive does not contain a valid manifest.json.")
                return False
    except Exception as e:
        log_error(f"Failed to open archive: {e}")
        return False

    src = manifest.get("source_system", {})
    print(f"\n{Colors.BOLD}Source System Info:{Colors.RESET}")
    print(f"  OS Version:     macOS {src.get('mac_ver', 'Unknown')}")
    print(f"  Architecture:   {src.get('arch', 'Unknown')}")
    print(f"  Source User:    {src.get('username', 'Unknown')}")
    print(f"  Source Home:    {src.get('home', 'Unknown')}")
    print(f"  Export Date:    {manifest.get('date_str', 'Unknown')}")
    print(f"  Export Mode:    {manifest.get('mode', 'Unknown').upper()}")
    print(f"  Credentials:    {'Included' if manifest.get('include_credentials') else 'Excluded'}")
    
    convs = manifest.get("conversations", {})
    print(f"\n{Colors.BOLD}Conversation History:{Colors.RESET}")
    print(f"  Total Sessions: {convs.get('count', 0)}")
    sample_ids = convs.get("ids", [])[:5]
    if sample_ids:
        print("  Sample IDs:    ", ", ".join(sample_ids))
        if len(convs.get("ids", [])) > 5:
            print(f"                  ... and {len(convs.get('ids', [])) - 5} more")

    print(f"\n{Colors.BOLD}Components Packaged:{Colors.RESET}")
    print(f"{'Source Path':<45} {'Files':>8} {'Size':>12}")
    print("-" * 68)
    for comp in manifest.get("components", []):
        p = comp.get("source_path", "")
        f_count = comp.get("file_count", 0)
        s_bytes = comp.get("size_bytes", 0)
        size_str = f"{s_bytes / (1024 * 1024):.2f} MB"
        disp = p if len(p) <= 44 else "..." + p[-41:]
        print(f"{disp:<45} {f_count:>8} {size_str:>12}")
    print("-" * 68)
    return True

# ---------------------------------------------------------------------------
# Core Operations: IMPORT
# ---------------------------------------------------------------------------

def create_snapshot(paths: MigrationPaths) -> Optional[str]:
    """
    Creates an emergency rollback snapshot of the destination machine's current state.
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    snapshot_dir = os.path.join(paths.gemini_dir, f"antigravity-backup-{ts}")
    log_info(f"Creating safety rollback snapshot at: {snapshot_dir}")
    
    try:
        os.makedirs(snapshot_dir, exist_ok=True)
        for item in ["antigravity", "config", "skills", "settings.json", "state.json", "antigravity-cli", "antigravity-ide"]:
            src = os.path.join(paths.gemini_dir, item)
            dst = os.path.join(snapshot_dir, item)
            if os.path.exists(src):
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=True, ignore=lambda d, files: [f for f in files if should_exclude(os.path.join(d, f))])
                else:
                    shutil.copy2(src, dst)
                    
        meta = {
            "timestamp": time.time(),
            "date": ts,
            "paths_backed_up": os.listdir(snapshot_dir)
        }
        with open(os.path.join(snapshot_dir, "snapshot_meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
            
        log_success("Safety snapshot created successfully.")
        return snapshot_dir
    except Exception as e:
        log_warn(f"Failed to create complete safety snapshot: {e}")
        return None

def clear_quarantine(target_path: str):
    """
    Removes macOS Gatekeeper quarantine attribute (com.apple.quarantine).
    """
    if not os.path.exists(target_path):
        return
    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", target_path],
            check=False,
            capture_output=True
        )
    except Exception:
        pass

def setup_cli_launchers(paths: MigrationPaths):
    """
    Ensures `agy` and `antigravity-ide` CLI commands are available on PATH.
    """
    local_bin = os.path.join(paths.home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    
    # 1. Antigravity IDE launcher inside app bundle
    ide_app_bin = "/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide"
    target_ide_symlink = os.path.join(local_bin, "antigravity-ide")
    if os.path.exists(ide_app_bin):
        try:
            if os.path.islink(target_ide_symlink) or os.path.exists(target_ide_symlink):
                os.remove(target_ide_symlink)
            os.symlink(ide_app_bin, target_ide_symlink)
            os.chmod(target_ide_symlink, 0o755)
            log_info(f"Provisioned CLI launcher: {target_ide_symlink} -> {ide_app_bin}")
        except Exception as e:
            log_warn(f"Could not link antigravity-ide: {e}")

    # 2. Check or create `agy` launcher
    agy_launcher = os.path.join(local_bin, "agy")
    if not os.path.exists(agy_launcher):
        content = """#!/usr/bin/env bash
# Auto-generated agy launcher
if [ -d "/Applications/Antigravity.app" ]; then
    open -a "/Applications/Antigravity.app" --args "$@"
elif [ -d "/Applications/Antigravity IDE.app" ]; then
    "/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide" "$@"
else
    echo "Error: Antigravity.app is not found in /Applications" >&2
    exit 1
fi
"""
        try:
            with open(agy_launcher, "w") as f:
                f.write(content)
            os.chmod(agy_launcher, 0o755)
            log_info(f"Created agy CLI launcher at: {agy_launcher}")
        except Exception as e:
            log_warn(f"Could not create agy launcher: {e}")

    # 3. Check shell PATH in ~/.zshrc or ~/.bash_profile
    zshrc = os.path.join(paths.home, ".zshrc")
    path_export = 'export PATH="$HOME/.local/bin:$PATH"'
    if os.path.exists(zshrc):
        try:
            with open(zshrc, "r") as f:
                lines = f.read()
            if ".local/bin" not in lines:
                with open(zshrc, "a") as f:
                    f.write(f"\n# Added by agy-migrate\n{path_export}\n")
                log_info(f"Appended ~/.local/bin to PATH in {zshrc}")
        except Exception as e:
            log_warn(f"Could not update {zshrc}: {e}")

def run_import(
    archive_path: str,
    skip_snapshot: bool = False,
    remap_paths: bool = True,
    auto_confirm: bool = False,
    dry_run: bool = False
) -> bool:
    """
    Restores Antigravity setup from archive into destination MacBook.
    """
    archive_path = os.path.expanduser(archive_path)
    if not os.path.isfile(archive_path):
        log_error(f"Archive not found: {archive_path}")
        return False

    log_header("Antigravity Migration Restore")
    log_info(f"Restoring from: {archive_path}")
    
    paths = MigrationPaths()
    
    # 1. Inspect manifest
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            f = tar.extractfile(tar.getmember("manifest.json"))
            if not f:
                log_error("Invalid archive: missing manifest.json")
                return False
            manifest = json.loads(f.read().decode("utf-8"))
    except Exception as e:
        log_error(f"Failed to read archive manifest: {e}")
        return False

    src_sys = manifest.get("source_system", {})
    src_home = src_sys.get("home", "")
    src_arch = src_sys.get("arch", "")
    dst_home = paths.home
    dst_arch = platform.machine()
    
    print(f"\n{Colors.BOLD}Migration Mapping:{Colors.RESET}")
    print(f"  Source User Home:      {src_home}")
    print(f"  Destination Home:      {dst_home}")
    print(f"  Source Architecture:   {src_arch}")
    print(f"  Dest Architecture:     {dst_arch}")
    
    # Architecture compatibility check
    if src_arch in ("x86_64", "i386") and dst_arch in ("arm64", "aarch64"):
        log_warn("Restoring x86_64 backup onto Apple Silicon (arm64).")
        try:
            rosetta_lib = os.path.exists("/Library/Apple/usr/libexec/oah/libRosettaRuntime")
            if not rosetta_lib:
                log_warn("Rosetta 2 may not be installed. If apps fail to launch, run: softwareupdate --install-rosetta")
        except Exception:
            pass

    # 2. Stop running processes
    if not dry_run:
        if not kill_running_processes(prompt=True, auto_confirm=auto_confirm):
            return False

    # 3. Create rollback safety snapshot
    if not skip_snapshot and not dry_run:
        create_snapshot(paths)

    if dry_run:
        log_info(f"[DRY-RUN] Would extract and remap components from {archive_path}")
        return True

    # 4. Extract archive
    temp_extract = tempfile.mkdtemp(prefix="agy_import_")
    try:
        log_info("Unpacking migration payload...")
        with tarfile.open(archive_path, "r:*") as tar:
            tar.extractall(temp_extract)

        remapper = PathRemapper(src_home, dst_home, src_arch, dst_arch)
        
        # 5. Move files into destination and perform remapping
        log_info("Deploying components to system and applying path translation...")
        
        # Deploy "home/" files
        src_home_dir = os.path.join(temp_extract, "home")
        if os.path.exists(src_home_dir):
            for root, dirs, files in os.walk(src_home_dir):
                rel = os.path.relpath(root, src_home_dir)
                target_root = paths.home if rel == "." else os.path.join(paths.home, rel)
                os.makedirs(target_root, exist_ok=True)
                
                for f in files:
                    src_file = os.path.join(root, f)
                    dst_file = os.path.join(target_root, f)
                    shutil.copy2(src_file, dst_file)
                    
                    if remap_paths:
                        remapper.remap_file_in_place(dst_file)

        # Deploy "apps/" to /Applications
        src_apps_dir = os.path.join(temp_extract, "apps")
        if os.path.exists(src_apps_dir):
            for app_name in os.listdir(src_apps_dir):
                src_app = os.path.join(src_apps_dir, app_name)
                dst_app = os.path.join("/Applications", app_name)
                log_info(f"Deploying application: {dst_app}")
                if os.path.exists(dst_app):
                    try:
                        shutil.rmtree(dst_app)
                    except Exception as e:
                        log_warn(f"Could not overwrite {dst_app} directly: {e}. Moving to backup.")
                        shutil.move(dst_app, f"{dst_app}.old_{int(time.time())}")
                try:
                    shutil.copytree(src_app, dst_app, symlinks=True)
                    clear_quarantine(dst_app)
                except Exception as e:
                    log_warn(f"Permission denied copying to /Applications: {e}. Placing into ~/Applications.")
                    user_apps = os.path.join(paths.home, "Applications")
                    os.makedirs(user_apps, exist_ok=True)
                    user_dst_app = os.path.join(user_apps, app_name)
                    shutil.copytree(src_app, user_dst_app, symlinks=True)
                    clear_quarantine(user_dst_app)

        # 6. Post-install fixes: permissions, quarantine, and executables
        log_info("Configuring permissions and clearing macOS quarantine flags...")
        
        try:
            os.chmod(paths.gemini_dir, 0o700)
            if os.path.exists(paths.oauth_creds):
                os.chmod(paths.oauth_creds, 0o600)
            if os.path.exists(paths.jetski_token):
                os.chmod(paths.jetski_token, 0o600)
        except Exception:
            pass

        clear_quarantine(paths.gemini_dir)
        clear_quarantine(paths.app_support_antigravity)
        clear_quarantine(paths.app_support_ide)

        for app in paths.apps:
            if os.path.exists(app):
                for root, dirs, files in os.walk(app):
                    for f in files:
                        if f in ("language_server", "sandbox-wrapper.sh", "antigravity-ide", "Antigravity"):
                            full_bin = os.path.join(root, f)
                            try:
                                os.chmod(full_bin, 0o755)
                                clear_quarantine(full_bin)
                            except Exception:
                                pass

        # 7. Setup CLI launchers and shell PATH
        setup_cli_launchers(paths)

        log_success("Restore and configuration finished successfully!")
        print(f"\n{Colors.BOLD}{Colors.GREEN}Restored Summary:{Colors.RESET}")
        print(f"  Conversations:    {manifest.get('conversations', {}).get('count', 0)} sessions")
        print(f"  Target Home:      {paths.home}")
        print(f"  Brain Location:   {paths.brain_dir}")
        print(f"  Customizations:   {paths.config_dir}")
        print(f"\n{Colors.CYAN}Run `./agy-migrate doctor` to verify system health.{Colors.RESET}\n")
        return True
    finally:
        shutil.rmtree(temp_extract, ignore_errors=True)

# ---------------------------------------------------------------------------
# Core Operations: ROLLBACK
# ---------------------------------------------------------------------------

def run_rollback(snapshot_dir: Optional[str] = None, auto_confirm: bool = False) -> bool:
    """
    Rolls back the target machine to a previous state using a safety snapshot.
    """
    paths = MigrationPaths()
    if not snapshot_dir:
        pattern = os.path.join(paths.gemini_dir, "antigravity-backup-*")
        snapshots = sorted(glob.glob(pattern))
        if not snapshots:
            log_error(f"No safety snapshots found in {paths.gemini_dir}")
            return False
        snapshot_dir = snapshots[-1]

    snapshot_dir = os.path.expanduser(snapshot_dir)
    if not os.path.isdir(snapshot_dir):
        log_error(f"Snapshot directory not found: {snapshot_dir}")
        return False

    log_header("Rolling Back Antigravity State")
    log_info(f"Restoring from snapshot: {snapshot_dir}")
    
    kill_running_processes(prompt=True, auto_confirm=auto_confirm)
    
    try:
        for item in os.listdir(snapshot_dir):
            if item == "snapshot_meta.json":
                continue
            src = os.path.join(snapshot_dir, item)
            dst = os.path.join(paths.gemini_dir, item)
            log_info(f"Reverting {dst}...")
            if os.path.exists(dst):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
                
        log_success("Rollback completed successfully.")
        return True
    except Exception as e:
        log_error(f"Rollback encountered an error: {e}")
        return False

# ---------------------------------------------------------------------------
# Core Operations: DOCTOR / HEALTH CHECK
# ---------------------------------------------------------------------------

def run_doctor() -> bool:
    """
    Audits the local environment and verifies that Antigravity is ready to run.
    """
    log_header("Antigravity System Health & Doctor")
    paths = MigrationPaths()
    
    checks_passed = 0
    checks_total = 0
    
    def check(name: str, passed: bool, details: str, warn_only: bool = False):
        nonlocal checks_passed, checks_total
        checks_total += 1
        if passed:
            checks_passed += 1
            status = f"{Colors.GREEN}[OK]{Colors.RESET}"
        elif warn_only:
            checks_passed += 1
            status = f"{Colors.YELLOW}[WARN]{Colors.RESET}"
        else:
            status = f"{Colors.RED}[FAIL]{Colors.RESET}"
        print(f"  {status} {Colors.BOLD}{name:<32}{Colors.RESET} {details}")

    print(f"{Colors.BOLD}1. Application & CLI Binaries:{Colors.RESET}")
    app_exists = any(os.path.exists(a) for a in paths.apps)
    check("Desktop Apps (/Applications)", app_exists, "Antigravity.app or Antigravity IDE.app found" if app_exists else "Apps missing from /Applications")
    
    agy_on_path = shutil.which("agy") is not None
    local_agy = os.path.exists(os.path.join(paths.home, ".local", "bin", "agy"))
    check("CLI `agy` on PATH", agy_on_path or local_agy, f"Found ({shutil.which('agy') or local_agy})" if (agy_on_path or local_agy) else "Not in PATH. Ensure ~/.local/bin is in $PATH", warn_only=True)
    
    ide_cli = shutil.which("antigravity-ide") is not None or os.path.exists(os.path.join(paths.home, ".local", "bin", "antigravity-ide"))
    check("CLI `antigravity-ide`", ide_cli, "Launcher found" if ide_cli else "Missing launcher", warn_only=True)

    print(f"\n{Colors.BOLD}2. Brain & Conversation History:{Colors.RESET}")
    brain_exists = os.path.isdir(paths.brain_dir)
    conv_count = 0
    if brain_exists:
        try:
            conv_count = len([c for c in os.listdir(paths.brain_dir) if not c.startswith(".") and os.path.isdir(os.path.join(paths.brain_dir, c))])
        except Exception:
            pass
    check("Brain Directory", brain_exists, f"{paths.brain_dir} ({conv_count} conversations found)")

    print(f"\n{Colors.BOLD}3. Configuration & Customizations:{Colors.RESET}")
    settings_exists = os.path.isfile(paths.settings_json)
    check("Settings File", settings_exists, paths.settings_json if settings_exists else "settings.json not present")
    
    config_dir_exists = os.path.isdir(paths.config_dir)
    mcp_config = os.path.join(paths.config_dir, "mcp_config.json")
    mcp_exists = os.path.isfile(mcp_config)
    check("Global Config Directory", config_dir_exists, paths.config_dir if config_dir_exists else "Missing ~/.gemini/config")
    check("MCP Configuration", mcp_exists, mcp_config if mcp_exists else "None configured", warn_only=True)

    print(f"\n{Colors.BOLD}4. Authentication & Licensing:{Colors.RESET}")
    oauth_exists = os.path.isfile(paths.oauth_creds)
    token_exists = os.path.isfile(paths.jetski_token)
    id_exists = os.path.isfile(paths.installation_id)
    check("OAuth Credentials", oauth_exists or token_exists, "Tokens present" if (oauth_exists or token_exists) else "No credentials found. First launch will request login.", warn_only=True)
    check("Installation Identity", id_exists, "Identity present" if id_exists else "Will generate new ID on launch", warn_only=True)

    print(f"\n{Colors.BOLD}5. Runtime Dependencies for MCP Servers:{Colors.RESET}")
    node_on_path = shutil.which("node") is not None
    python_on_path = shutil.which("python3") is not None
    check("Node.js (for JS MCPs)", node_on_path, shutil.which("node") or "Not installed (required for standard npm MCP servers)", warn_only=True)
    check("Python 3 (for Python MCPs)", python_on_path, shutil.which("python3") or "Not found", warn_only=False)

    print("-" * 68)
    if checks_passed >= checks_total - 2:
        log_success(f"System audit passed ({checks_passed}/{checks_total} checks). Antigravity is ready to run!")
        return True
    else:
        log_warn(f"System audit completed with warnings ({checks_passed}/{checks_total} checks).")
        return False

# ---------------------------------------------------------------------------
# Core Operations: SYNC (Mac-to-Mac direct SSH transfer)
# ---------------------------------------------------------------------------

def run_sync(target_host: str, include_apps: bool = False, dry_run: bool = False) -> bool:
    """
    Directly synchronizes Antigravity from this MacBook to another MacBook over SSH.
    """
    log_header(f"Direct Peer-to-Peer Sync -> {target_host}")
    log_info(f"Target: {target_host}")
    
    try:
        res = subprocess.run(["ssh", target_host, "echo connected"], capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            log_error(f"Cannot connect to {target_host} via SSH: {res.stderr.strip()}")
            return False
    except Exception as e:
        log_error(f"SSH connection failed: {e}")
        return False
        
    log_success("SSH connection verified.")
    
    temp_archive = tempfile.mktemp(suffix=".tar.gz", prefix="agy_sync_")
    try:
        bundle = run_export(output_path=temp_archive, include_apps=include_apps, auto_confirm=True, dry_run=dry_run)
        if not bundle or dry_run:
            return True
            
        remote_tmp = f"/tmp/{os.path.basename(bundle)}"
        log_info(f"Transferring migration bundle to {target_host}:{remote_tmp}...")
        subprocess.run(["scp", bundle, f"{target_host}:{remote_tmp}"], check=True)
        
        remote_script = "/tmp/agy_migrate.py"
        subprocess.run(["scp", __file__, f"{target_host}:{remote_script}"], check=True)
        
        log_info(f"Executing remote restore on {target_host}...")
        cmd = f"python3 {remote_script} import --archive {remote_tmp} -y && rm -f {remote_tmp} {remote_script}"
        subprocess.run(["ssh", "-t", target_host, cmd], check=True)
        
        log_success(f"Peer-to-peer sync to {target_host} completed successfully!")
        return True
    finally:
        if os.path.exists(temp_archive):
            os.remove(temp_archive)

# ---------------------------------------------------------------------------
# CLI Argument Parser & Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="agy-migrate: Cross-MacBook Antigravity Migration Suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 1. Export (Default: Lightweight data bundle of configs, history, skills, and settings)
  agy-migrate export

  # 2. Export with custom output path
  agy-migrate export -o ~/Desktop/my_agy_backup.tar.gz

  # 3. Full Export including desktop .app bundles (optional)
  agy-migrate export --include-apps

  # 4. Inspect an archive without restoring
  agy-migrate inspect --archive ~/Desktop/antigravity_migration_data_20260906.tar.gz

  # 5. Restore on the new MacBook endpoint
  agy-migrate import --archive ~/Desktop/antigravity_migration_data_20260906.tar.gz

  # 6. Direct Peer-to-Peer Transfer over local Wi-Fi / SSH
  agy-migrate sync --target user@new-macbook.local

  # 7. Check installation health
  agy-migrate doctor

  # 8. Undo a restore
  agy-migrate rollback
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    
    # Export Parser
    p_export = subparsers.add_parser("export", help="Export Antigravity instance and conversation history to an archive")
    p_export.add_argument("-o", "--output", help="Target archive path (.tar.gz)")
    p_export.add_argument("--include-apps", action="store_true", help="Include /Applications/Antigravity*.app bundles (default: False, data-only)")
    p_export.add_argument("--no-credentials", action="store_true", help="Exclude OAuth tokens and credential files")
    p_export.add_argument("-y", "--yes", action="store_true", help="Auto-confirm prompts (non-interactive)")
    p_export.add_argument("--dry-run", action="store_true", help="Simulate export without creating archive")
    
    # Import Parser
    p_import = subparsers.add_parser("import", help="Import Antigravity instance and history from an archive")
    p_import.add_argument("-a", "--archive", required=True, help="Path to migration archive (.tar.gz)")
    p_import.add_argument("--skip-snapshot", action="store_true", help="Skip creating safety rollback snapshot")
    p_import.add_argument("--no-remap", action="store_true", help="Disable path and username remapping")
    p_import.add_argument("-y", "--yes", action="store_true", help="Auto-confirm prompts (non-interactive)")
    p_import.add_argument("--dry-run", action="store_true", help="Simulate import without modifying files")
    
    # Inspect Parser
    p_inspect = subparsers.add_parser("inspect", help="Inspect contents and metadata of an archive without extracting")
    p_inspect.add_argument("-a", "--archive", required=True, help="Path to migration archive (.tar.gz)")
    
    # Rollback Parser
    p_rollback = subparsers.add_parser("rollback", help="Revert to a pre-restore safety snapshot")
    p_rollback.add_argument("-s", "--snapshot", help="Specific snapshot directory (defaults to latest)")
    p_rollback.add_argument("-y", "--yes", action="store_true", help="Auto-confirm prompts (non-interactive)")
    
    # Doctor Parser
    subparsers.add_parser("doctor", help="Audit local Antigravity health and configuration")
    
    # Sync Parser
    p_sync = subparsers.add_parser("sync", help="Direct Mac-to-Mac live migration via SSH")
    p_sync.add_argument("-t", "--target", required=True, help="Remote host in ssh format (e.g. user@new-macbook.local)")
    p_sync.add_argument("--include-apps", action="store_true", help="Include /Applications/Antigravity*.app bundles (default: False)")
    p_sync.add_argument("--dry-run", action="store_true", help="Simulate sync")

    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
        
    if args.command == "export":
        run_export(
            output_path=args.output,
            include_apps=args.include_apps,
            include_credentials=not args.no_credentials,
            auto_confirm=args.yes,
            dry_run=args.dry_run
        )
    elif args.command == "import":
        run_import(
            archive_path=args.archive,
            skip_snapshot=args.skip_snapshot,
            remap_paths=not args.no_remap,
            auto_confirm=args.yes,
            dry_run=args.dry_run
        )
    elif args.command == "inspect":
        run_inspect(archive_path=args.archive)
    elif args.command == "rollback":
        run_rollback(snapshot_dir=args.snapshot, auto_confirm=args.yes)
    elif args.command == "doctor":
        run_doctor()
    elif args.command == "sync":
        run_sync(target_host=args.target, include_apps=args.include_apps, dry_run=args.dry_run)

if __name__ == "__main__":
    main()
