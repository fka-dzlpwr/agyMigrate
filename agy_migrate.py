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
import getpass
import glob
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Version info
VERSION = "1.1.0"
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
    ".DS_Store",
}

EXCLUDED_EXTENSIONS = {
    ".tmp",
    ".lock",
    ".log.old",
}

def should_exclude(path_str: str) -> bool:
    """
    Determine whether a file or directory path should be excluded from the archive.

    Callers should pass a path *relative to the component being packed* so that
    unrelated parent directories (e.g. a home folder living under ``/tmp``) never
    cause a false positive.
    """
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

def verify_checksum(archive_path: str) -> Optional[bool]:
    """
    Verify an archive against the ``<archive>.sha256`` file written by export.

    Returns True/False for match/mismatch, or None when no checksum file exists.
    """
    checksum_file = f"{archive_path}.sha256"
    if not os.path.isfile(checksum_file):
        return None
    try:
        with open(checksum_file, "r") as f:
            expected = f.read().split()[0].strip().lower()
    except (OSError, IndexError):
        return False
    return calculate_sha256(archive_path) == expected

def read_manifest(archive_path: str) -> Dict:
    """
    Read ``manifest.json`` from a migration archive.

    The manifest is always the first member, so only the first tar header is read
    (avoids decompressing a multi-GB archive just to inspect it).
    """
    with tarfile.open(archive_path, "r:*") as tar:
        first = tar.next()
        if first is None or first.name != "manifest.json":
            raise ValueError("archive does not start with manifest.json (not an agy-migrate bundle)")
        f = tar.extractfile(first)
        if f is None:
            raise ValueError("could not read manifest.json from archive")
        return json.loads(f.read().decode("utf-8"))

def safe_extract(tar: tarfile.TarFile, dest: str):
    """Extract an archive without allowing members to escape ``dest``."""
    if hasattr(tarfile, "tar_filter"):
        # Blocks path traversal / absolute member names but keeps symlinks intact.
        tar.extractall(dest, filter="tar")
        return
    dest_real = os.path.realpath(dest)
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if os.path.isabs(member.name) or not (target == dest_real or target.startswith(dest_real + os.sep)):
            raise ValueError(f"unsafe path in archive: {member.name}")
    tar.extractall(dest)

# Command-line fragments that identify Antigravity processes (matched against `ps` output).
_APP_PROCESS_MARKERS = [
    ("Antigravity", "/Antigravity.app/Contents/"),
    ("Antigravity IDE", "/Antigravity IDE.app/Contents/"),
]

def get_running_processes() -> List[Tuple[int, str]]:
    """
    Check for active Antigravity processes that might lock databases.

    Returns ``(pid, label)`` pairs. This process and all of its ancestors (the
    shell, terminal, or agent that launched it) are never reported, so the tool
    can neither detect nor terminate itself.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, check=False
        ).stdout
    except Exception:
        return []

    procs: Dict[int, Tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        procs[int(parts[0])] = (int(parts[1]), parts[2])

    protected: Set[int] = set()
    pid = os.getpid()
    while pid and pid not in protected:
        protected.add(pid)
        pid = procs.get(pid, (0, ""))[0]

    found: List[Tuple[int, str]] = []
    for pid, (_ppid, cmd) in procs.items():
        if pid in protected or "agy_migrate" in cmd or "agy-migrate" in cmd:
            continue
        label = None
        for lbl, marker in _APP_PROCESS_MARKERS:
            if marker in cmd:
                label = lbl
                break
        if label is None and "language_server" in cmd and (".gemini" in cmd or "Antigravity" in cmd):
            label = "language_server"
        if label is None and os.path.basename(cmd.split(None, 1)[0]) == "agy":
            label = "agy"
        if label:
            found.append((pid, label))
    return found

def kill_running_processes(prompt: bool = True, auto_confirm: bool = False) -> bool:
    """
    Prompt user or terminate active Antigravity processes safely.

    Returns True when it is safe to proceed (nothing running, or everything was
    stopped) and False when the user declined, no confirmation was possible in a
    non-interactive session, or processes could not be stopped.
    """
    procs = get_running_processes()
    if not procs:
        return True

    labels = sorted({label for _pid, label in procs})
    log_warn(f"Detected running Antigravity processes: {', '.join(labels)}")
    if not auto_confirm:
        if prompt and sys.stdin.isatty():
            try:
                answer = input(f"{Colors.YELLOW}Would you like to gracefully close these processes now? [Y/n]: {Colors.RESET}").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                return False
            if answer not in ("", "y", "yes"):
                log_error("Cannot proceed safely while Antigravity is running. Quit it and retry.")
                return False
        else:
            log_error("Antigravity is running and no terminal is available to confirm. Quit it, or re-run with --yes to stop it automatically.")
            return False

    def _signal_all(targets, sig):
        for pid, label in targets:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError) as e:
                if not isinstance(e, ProcessLookupError):
                    log_warn(f"Failed to stop {label} (pid {pid}): {e}")

    log_info("Stopping Antigravity processes...")
    _signal_all(procs, signal.SIGTERM)
    for _ in range(10):
        time.sleep(0.5)
        if not get_running_processes():
            return True

    remaining = get_running_processes()
    log_warn("Processes still running, sending KILL signal...")
    _signal_all(remaining, signal.SIGKILL)
    time.sleep(0.5)
    if get_running_processes():
        log_error("Could not stop all Antigravity processes.")
        return False
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
        
        # 7. CLI Launchers & Symlinks (user-level only; /usr/local/bin needs root to restore)
        self.cli_launchers = [
            os.path.join(self.home, ".local", "bin", "agy"),
            os.path.join(self.home, ".local", "bin", "antigravity-ide"),
        ]

    def managed_paths(self) -> List[str]:
        """Home-relative paths that an import may create or overwrite (used for snapshot/rollback)."""
        absolute = [
            self.antigravity_data, self.config_dir, self.skills_dir, self.settings_json,
            self.state_json, self.gemini_md, self.agents_md, self.cli_dir, self.ide_dir,
            self.browser_profile, self.installation_id, self.oauth_creds, self.jetski_token,
            self.app_support_antigravity, self.app_support_ide,
        ] + self.cli_launchers
        return [os.path.relpath(p, self.home) for p in absolute]

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
                rel = os.path.relpath(cli_path, self.home)
                candidates.append((cli_path, f"home/{rel}"))
                    
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
        
        # Only match whole path components: /Users/bob must not rewrite /Users/bobby.
        self._home_re = None
        if self.src_home and self.dst_home and self.src_home != self.dst_home:
            self._home_re = re.compile(re.escape(self.src_home) + r"(?![A-Za-z0-9._-])")

        self.brew_remap = False
        self._brew_re = None
        if src_arch in ("x86_64", "i386") and dst_arch in ("arm64", "aarch64"):
            self.old_brew = "/usr/local"
            self.new_brew = "/opt/homebrew"
            self.brew_remap = True
            # /usr/local also holds non-Homebrew files; only rewrite Homebrew-owned subtrees.
            self._brew_re = re.compile(r"/usr/local(?=/(?:bin|opt|Cellar|Homebrew)(?![A-Za-z0-9._-]))")
        elif src_arch in ("arm64", "aarch64") and dst_arch in ("x86_64", "i386"):
            self.old_brew = "/opt/homebrew"
            self.new_brew = "/usr/local"
            self.brew_remap = True
            self._brew_re = re.compile(r"/opt/homebrew(?![A-Za-z0-9._-])")

    def remap_string(self, content: str) -> str:
        """Remap home directories and optional brew paths within a text string."""
        if self._home_re:
            content = self._home_re.sub(lambda _m: self.dst_home, content)
        if self._brew_re:
            content = self._brew_re.sub(lambda _m: self.new_brew, content)
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
            if not os.path.isfile(filepath) or os.path.islink(filepath):
                return False
            if filepath.endswith((".json", ".jsonl", ".md", ".txt", ".sh", ".plist", ".yaml", ".yml")):
                # surrogateescape + newline="" round-trips undecodable bytes and line endings unchanged.
                with open(filepath, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
                    content = f.read()
                new_content = self.remap_string(content)
                if new_content != content:
                    with open(filepath, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                        f.write(new_content)
                    return True
        except Exception as e:
            log_warn(f"Could not remap paths in {filepath}: {e}")
        return False

# ---------------------------------------------------------------------------
# Core Operations: EXPORT
# ---------------------------------------------------------------------------

def collect_component(
    src_path: str, arc_base: str, errors: Optional[List[str]] = None
) -> Tuple[List[Tuple[str, str]], int, int]:
    """
    Walk one export component and return ``(items, file_count, size_bytes)``.

    ``items`` are ``(absolute_path, archive_path)`` pairs added non-recursively, and
    include directory entries (so empty directories survive) and symlinks
    (including symlinks to directories, which ``os.walk`` reports as dirs).

    Directories that cannot be listed (e.g. missing macOS privacy permissions) are
    appended to ``errors`` instead of being silently skipped by ``os.walk``.
    """
    items: List[Tuple[str, str]] = []
    count = 0
    size = 0

    def _on_error(e: OSError):
        if errors is not None:
            errors.append(f"{e.filename or src_path}: {e.strerror or e}")

    if os.path.isdir(src_path):
        if not os.path.islink(src_path):
            items.append((src_path, arc_base))
        for root, dirs, files in os.walk(src_path, onerror=_on_error):
            kept = []
            for d in dirs:
                if should_exclude(d):
                    continue
                full_d = os.path.join(root, d)
                items.append((full_d, os.path.join(arc_base, os.path.relpath(full_d, src_path))))
                if os.path.islink(full_d):
                    count += 1  # packed as a symlink; os.walk does not descend into it
                else:
                    kept.append(d)
            dirs[:] = kept
            for f in files:
                full_f = os.path.join(root, f)
                rel = os.path.relpath(full_f, src_path)
                if should_exclude(rel):
                    continue
                items.append((full_f, os.path.join(arc_base, rel)))
                try:
                    size += os.lstat(full_f).st_size
                    count += 1
                except OSError:
                    pass
    elif not should_exclude(os.path.basename(src_path)):
        # Top-level file components that are symlinks (e.g. ~/.local/bin/agy) are
        # dereferenced: their target usually lives outside the exported tree and
        # would dangle on the destination Mac.
        real = os.path.realpath(src_path) if os.path.islink(src_path) else src_path
        items.append((real, arc_base))
        try:
            size += os.path.getsize(real)
            count += 1
        except OSError:
            pass
    return items, count, size

def run_export(
    output_path: Optional[str] = None,
    include_apps: bool = False,
    include_credentials: bool = True,
    auto_confirm: bool = False,
    dry_run: bool = False,
    ignore_errors: bool = False
) -> Optional[str]:
    """
    Exports Antigravity instance, configurations, and conversation history.

    Unreadable directories abort the export (an incomplete bundle must never look
    successful) unless ``ignore_errors`` is set.
    """
    mode_str = "FULL (+Apps)" if include_apps else "DATA (Configs, History & Settings)"
    log_header(f"Antigravity Migration Export ({mode_str})")
    
    paths = MigrationPaths()
    
    # 1. Pre-flight check
    if not dry_run:
        if not kill_running_processes(prompt=True, auto_confirm=auto_confirm):
            return None
    
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
            "username": getpass.getuser(),
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
    walk_errors: List[str] = []
    
    for src_path, arc_base in candidates:
        items, component_files, component_size = collect_component(src_path, arc_base, walk_errors)
        filtered_items.extend(items)
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

    if walk_errors:
        for err in walk_errors[:10]:
            log_error(f"Cannot read {err}")
        if len(walk_errors) > 10:
            log_error(f"... and {len(walk_errors) - 10} more unreadable location(s)")
        log_error("The export would be INCOMPLETE. Grant your terminal Full Disk Access "
                  "(System Settings > Privacy & Security) or fix permissions, then retry.")
        if not ignore_errors:
            log_error("Aborting. Re-run with --ignore-errors to export what is readable anyway.")
            return None
        log_warn("--ignore-errors set: continuing with an incomplete export.")

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
        skipped = 0
        # The bundle can contain OAuth tokens and cookies: owner-only permissions from the first byte.
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tar:
            # Add manifest first
            tar.add(manifest_path, arcname="manifest.json")
            
            # Add all items
            idx = 0
            for abs_path, arc_path in filtered_items:
                idx += 1
                if idx % 500 == 0 or idx == len(filtered_items):
                    pct = (idx / len(filtered_items)) * 100
                    print(f"\rPacking: {pct:5.1f}% [{idx}/{len(filtered_items)} files]", end="", flush=True)
                try:
                    tar.add(abs_path, arcname=arc_path, recursive=False)
                except OSError as e:
                    # Live state (databases, sockets) can vanish mid-export.
                    skipped += 1
                    print()
                    log_warn(f"Skipped {abs_path}: {e}")
                
        print()
        elapsed = time.time() - start_t
        
        archive_size = os.path.getsize(output_path)
        sha256 = calculate_sha256(output_path)
        
        # Write checksum file alongside archive
        checksum_file = f"{output_path}.sha256"
        with open(checksum_file, "w") as cf:
            cf.write(f"{sha256}  {os.path.basename(output_path)}\n")

        log_success(f"Archive successfully generated in {elapsed:.1f}s")
        if skipped:
            log_warn(f"{skipped} item(s) could not be read and were skipped.")
        log_info(f"Archive Size: {archive_size / (1024 * 1024):.2f} MB")
        log_info(f"SHA-256: {sha256}")
        log_info(f"Checksum file: {checksum_file}")
        log_warn("The archive contains credentials: transfer it securely and delete it afterwards.")
        
        print(f"\n{Colors.BOLD}{Colors.GREEN}Next Steps to Transfer to New MacBook:{Colors.RESET}")
        print(f" 1. Transfer {output_path} (and its .sha256 file) via AirDrop, Thunderbolt cable, or USB SSD.")
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
        manifest = read_manifest(archive_path)
    except Exception as e:
        log_error(f"Failed to read archive: {e}")
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

    Every path an import may touch is recorded (including whether it existed), so a
    rollback can also remove things the import newly created.
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    snapshot_dir = os.path.join(paths.gemini_dir, f"antigravity-backup-{ts}")
    suffix = 1
    while os.path.exists(snapshot_dir):
        snapshot_dir = os.path.join(paths.gemini_dir, f"antigravity-backup-{ts}-{suffix}")
        suffix += 1
    log_info(f"Creating safety rollback snapshot at: {snapshot_dir}")
    
    try:
        os.makedirs(snapshot_dir)
        tracked: Dict[str, bool] = {}
        for rel in paths.managed_paths():
            src = os.path.join(paths.home, rel)
            existed = os.path.lexists(src)
            tracked[rel] = existed
            if not existed:
                continue
            dst = os.path.join(snapshot_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True, ignore=lambda d, names: [n for n in names if should_exclude(n)])
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
                    
        meta = {
            "timestamp": time.time(),
            "date": ts,
            "home": paths.home,
            "tracked": tracked,
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
            if os.path.islink(target_ide_symlink):
                os.remove(target_ide_symlink)
            if not os.path.exists(target_ide_symlink):
                os.symlink(ide_app_bin, target_ide_symlink)
                log_info(f"Provisioned CLI launcher: {target_ide_symlink} -> {ide_app_bin}")
        except Exception as e:
            log_warn(f"Could not link antigravity-ide: {e}")

    # 2. The `agy` CLI is a standalone binary restored from the archive; do not fake it.
    if not os.path.exists(os.path.join(local_bin, "agy")):
        log_warn("The `agy` CLI binary was not found in ~/.local/bin (it was not on the source Mac, or the archive predates it). Install the Antigravity CLI separately.")

    # 3. Ensure ~/.local/bin is on PATH for zsh (the macOS default shell); a fresh Mac may have no ~/.zshrc yet.
    zshrc = os.path.join(paths.home, ".zshrc")
    path_export = 'export PATH="$HOME/.local/bin:$PATH"'
    try:
        lines = ""
        if os.path.exists(zshrc):
            with open(zshrc, "r") as f:
                lines = f.read()
        if ".local/bin" not in lines:
            with open(zshrc, "a") as f:
                f.write(f"\n# Added by agy-migrate\n{path_export}\n")
            log_info(f"Appended ~/.local/bin to PATH in {zshrc}")
    except Exception as e:
        log_warn(f"Could not update {zshrc}: {e}")

def _place_symlink(src_link: str, dst_link: str, remapper: Optional[PathRemapper]):
    """Recreate a symlink at ``dst_link``, remapping an absolute target if needed."""
    target = os.readlink(src_link)
    if remapper:
        target = remapper.remap_string(target)
    if os.path.islink(dst_link) or os.path.isfile(dst_link):
        os.remove(dst_link)
    elif os.path.isdir(dst_link):
        shutil.rmtree(dst_link)
    os.symlink(target, dst_link)

def deploy_tree(src_root: str, dst_root: str, remapper: Optional[PathRemapper]) -> int:
    """
    Copy an extracted tree into ``dst_root`` preserving symlinks, and remap text files.

    Returns the number of entries that failed to deploy.
    """
    errors = 0
    for root, dirs, files in os.walk(src_root):
        rel = os.path.relpath(root, src_root)
        target_root = dst_root if rel == "." else os.path.join(dst_root, rel)
        os.makedirs(target_root, exist_ok=True)

        for name in list(dirs):
            src_entry = os.path.join(root, name)
            if os.path.islink(src_entry):
                dirs.remove(name)  # symlinks to directories are recreated, never followed
                try:
                    _place_symlink(src_entry, os.path.join(target_root, name), remapper)
                except OSError as e:
                    errors += 1
                    log_warn(f"Could not restore symlink {src_entry}: {e}")

        for name in files:
            src_file = os.path.join(root, name)
            dst_file = os.path.join(target_root, name)
            try:
                if os.path.islink(src_file):
                    _place_symlink(src_file, dst_file, remapper)
                    continue
                # Never write through an existing symlink at the destination.
                if os.path.islink(dst_file):
                    os.remove(dst_file)
                shutil.copy2(src_file, dst_file)
                if remapper:
                    remapper.remap_file_in_place(dst_file)
            except OSError as e:
                errors += 1
                log_warn(f"Could not restore {dst_file}: {e}")
    return errors

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

    # 0. Integrity check
    checksum_ok = verify_checksum(archive_path)
    if checksum_ok is False:
        log_error("SHA-256 mismatch: the archive is corrupted or was modified in transit. Re-transfer it.")
        return False
    elif checksum_ok is None:
        log_warn("No .sha256 file next to the archive; skipping integrity check.")
    else:
        log_success("Archive SHA-256 verified.")
    
    # 1. Inspect manifest
    try:
        manifest = read_manifest(archive_path)
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

    if dry_run:
        log_info(f"[DRY-RUN] Would extract and remap components from {archive_path}")
        return True

    # 2. Stop running processes
    if not kill_running_processes(prompt=True, auto_confirm=auto_confirm):
        return False

    # 3. Create rollback safety snapshot
    if not skip_snapshot:
        if create_snapshot(paths) is None:
            log_error("Aborting: no safety snapshot could be created. Fix the problem above or re-run with --skip-snapshot.")
            return False

    # 4. Extract archive
    temp_extract = tempfile.mkdtemp(prefix="agy_import_")
    try:
        log_info("Unpacking migration payload...")
        with tarfile.open(archive_path, "r:*") as tar:
            safe_extract(tar, temp_extract)

        remapper = PathRemapper(src_home, dst_home, src_arch, dst_arch) if remap_paths else None
        errors = 0
        
        # 5. Move files into destination and perform remapping
        log_info("Deploying components to system and applying path translation...")
        
        # Deploy "home/" files
        src_home_dir = os.path.join(temp_extract, "home")
        if os.path.exists(src_home_dir):
            errors += deploy_tree(src_home_dir, paths.home, remapper)

        # Deploy "apps/" to /Applications
        deployed_apps: List[str] = []
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
                    deployed_apps.append(dst_app)
                except Exception as e:
                    log_warn(f"Permission denied copying to /Applications: {e}. Placing into ~/Applications.")
                    user_apps = os.path.join(paths.home, "Applications")
                    os.makedirs(user_apps, exist_ok=True)
                    user_dst_app = os.path.join(user_apps, app_name)
                    shutil.copytree(src_app, user_dst_app, symlinks=True)
                    deployed_apps.append(user_dst_app)

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

        # Only touch app bundles that this import actually installed.
        for app in deployed_apps:
            clear_quarantine(app)
            for root, dirs, files in os.walk(app):
                for f in files:
                    if f in ("language_server", "sandbox-wrapper.sh", "antigravity-ide", "Antigravity"):
                        try:
                            os.chmod(os.path.join(root, f), 0o755)
                        except Exception:
                            pass

        # 7. Setup CLI launchers and shell PATH
        setup_cli_launchers(paths)

        if errors:
            log_warn(f"Restore finished with {errors} error(s); see warnings above.")
        else:
            log_success("Restore and configuration finished successfully!")
        print(f"\n{Colors.BOLD}{Colors.GREEN}Restored Summary:{Colors.RESET}")
        print(f"  Conversations:    {manifest.get('conversations', {}).get('count', 0)} sessions")
        print(f"  Target Home:      {paths.home}")
        print(f"  Brain Location:   {paths.brain_dir}")
        print(f"  Customizations:   {paths.config_dir}")
        print(f"\n{Colors.CYAN}Run `./agy-migrate doctor` to verify system health.{Colors.RESET}\n")
        return errors == 0
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
        snapshots = sorted(glob.glob(pattern), key=os.path.getmtime)
        if not snapshots:
            log_error(f"No safety snapshots found in {paths.gemini_dir}")
            return False
        snapshot_dir = snapshots[-1]

    snapshot_dir = os.path.expanduser(snapshot_dir)
    if not os.path.isdir(snapshot_dir):
        log_error(f"Snapshot directory not found: {snapshot_dir}")
        return False

    try:
        with open(os.path.join(snapshot_dir, "snapshot_meta.json"), "r") as f:
            tracked = json.load(f).get("tracked")
    except (OSError, ValueError):
        tracked = None
    if not isinstance(tracked, dict):
        log_error(f"{snapshot_dir} is not a valid agy-migrate snapshot (missing or unreadable snapshot_meta.json).")
        return False

    log_header("Rolling Back Antigravity State")
    log_info(f"Restoring from snapshot: {snapshot_dir}")
    
    if not kill_running_processes(prompt=True, auto_confirm=auto_confirm):
        return False
    
    try:
        for rel, existed in tracked.items():
            dst = os.path.join(paths.home, rel)
            src = os.path.join(snapshot_dir, rel)
            log_info(f"Reverting ~/{rel}...")
            if os.path.islink(dst) or os.path.isfile(dst):
                os.remove(dst)
            elif os.path.isdir(dst):
                shutil.rmtree(dst)
            if existed:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst, follow_symlinks=False)
                
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
    
    checks_total = 0
    failures = 0
    warnings = 0
    
    def check(name: str, passed: bool, details: str, warn_only: bool = False):
        nonlocal checks_total, failures, warnings
        checks_total += 1
        if passed:
            status = f"{Colors.GREEN}[OK]{Colors.RESET}"
        elif warn_only:
            warnings += 1
            status = f"{Colors.YELLOW}[WARN]{Colors.RESET}"
        else:
            failures += 1
            status = f"{Colors.RED}[FAIL]{Colors.RESET}"
        print(f"  {status} {Colors.BOLD}{name:<32}{Colors.RESET} {details}")

    print(f"{Colors.BOLD}1. Application & CLI Binaries:{Colors.RESET}")
    app_exists = any(os.path.exists(a) for a in paths.apps)
    check("Desktop Apps (/Applications)", app_exists, "Antigravity.app or Antigravity IDE.app found" if app_exists else "Apps missing from /Applications")
    
    agy_path = shutil.which("agy")
    local_agy = os.path.join(paths.home, ".local", "bin", "agy")
    if agy_path:
        check("CLI `agy` on PATH", True, f"Found ({agy_path})")
    elif os.path.exists(local_agy):
        check("CLI `agy` on PATH", False, f"{local_agy} exists but ~/.local/bin is not in $PATH", warn_only=True)
    else:
        check("CLI `agy` on PATH", False, "Not installed", warn_only=True)
    
    ide_path = shutil.which("antigravity-ide")
    local_ide = os.path.join(paths.home, ".local", "bin", "antigravity-ide")
    if ide_path:
        check("CLI `antigravity-ide`", True, f"Found ({ide_path})")
    else:
        check("CLI `antigravity-ide`", os.path.exists(local_ide), "Launcher found (not on PATH)" if os.path.exists(local_ide) else "Missing launcher", warn_only=True)

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
    summary = f"{checks_total - failures - warnings}/{checks_total} OK, {warnings} warning(s), {failures} failure(s)"
    if failures == 0:
        log_success(f"System audit passed ({summary}). Antigravity is ready to run!")
        return True
    else:
        log_warn(f"System audit found problems ({summary}).")
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
    
    fd, temp_archive = tempfile.mkstemp(suffix=".tar.gz", prefix="agy_sync_")
    os.close(fd)
    try:
        bundle = run_export(output_path=temp_archive, include_apps=include_apps, auto_confirm=True, dry_run=dry_run)
        if not bundle:
            return False
        if dry_run:
            return True
            
        remote_tmp = f"/tmp/{os.path.basename(bundle)}"
        remote_script = "/tmp/agy_migrate.py"
        log_info(f"Transferring migration bundle to {target_host}:{remote_tmp}...")
        subprocess.run(["scp", bundle, f"{target_host}:{remote_tmp}"], check=True)
        subprocess.run(["scp", f"{bundle}.sha256", f"{target_host}:{remote_tmp}.sha256"], check=True)
        subprocess.run(["scp", __file__, f"{target_host}:{remote_script}"], check=True)
        
        log_info(f"Executing remote restore on {target_host}...")
        q_tmp, q_script = shlex.quote(remote_tmp), shlex.quote(remote_script)
        # Always clean up the remote files, but report the import's real exit status.
        cmd = (
            f"python3 {q_script} import --archive {q_tmp} -y; rc=$?; "
            f"rm -f {q_tmp} {shlex.quote(remote_tmp + '.sha256')} {q_script}; exit $rc"
        )
        subprocess.run(["ssh", "-t", target_host, cmd], check=True)
        
        log_success(f"Peer-to-peer sync to {target_host} completed successfully!")
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        log_error(f"Sync failed: {e}")
        return False
    finally:
        for leftover in (temp_archive, f"{temp_archive}.sha256"):
            if os.path.exists(leftover):
                os.remove(leftover)

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
    p_export.add_argument("--ignore-errors", action="store_true", help="Export even if some directories are unreadable (incomplete bundle)")
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
        ok = run_export(
            output_path=args.output,
            include_apps=args.include_apps,
            include_credentials=not args.no_credentials,
            auto_confirm=args.yes,
            dry_run=args.dry_run,
            ignore_errors=args.ignore_errors
        ) is not None
    elif args.command == "import":
        ok = run_import(
            archive_path=args.archive,
            skip_snapshot=args.skip_snapshot,
            remap_paths=not args.no_remap,
            auto_confirm=args.yes,
            dry_run=args.dry_run
        )
    elif args.command == "inspect":
        ok = run_inspect(archive_path=args.archive)
    elif args.command == "rollback":
        ok = run_rollback(snapshot_dir=args.snapshot, auto_confirm=args.yes)
    elif args.command == "doctor":
        ok = run_doctor()
    elif args.command == "sync":
        ok = run_sync(target_host=args.target, include_apps=args.include_apps, dry_run=args.dry_run)
    else:
        ok = False

    # Propagate failure to the shell so scripts (and `sync`'s remote step) can react.
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
