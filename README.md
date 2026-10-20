# agy-migrate: Antigravity Cross-MacBook Migration Suite

`agy-migrate` is a self-contained, production-grade migration tool designed to transfer a complete Google Antigravity setup—including desktop applications, CLI binaries, application state, global configurations, MCP server definitions, browser automation profiles, credentials, and full conversation history—between MacBook endpoints.

---

## Key Features

- **Turnkey Setup**: Migrates everything required to immediately launch and run Agy on the new endpoint without manual configuration or re-downloading.
- **Complete History & Brain Transfer**: Transfers all conversation sessions (`transcript.jsonl`, `transcript_full.jsonl`), plans, walkthroughs, artifacts, and scratch directories.
- **Cross-User Path Translation**: Automatically rewrites absolute paths in configuration manifests (`settings.json`, `mcp_config.json`, workspace lists) and conversation logs if the macOS username differs (e.g. `/Users/alice` -> `/Users/bob`).
- **Apple Silicon & Intel Homebrew Translation**: Intelligently updates binary search paths in MCP configurations when migrating between architectures (`/usr/local` on Intel vs `/opt/homebrew` on Apple Silicon).
- **GPU Cache Pruning**: Excludes volatile GPU shader caches (`GPUCache`, `DawnCache`, `Code Cache`) to prevent hardware driver crashes across different Mac display hardware while preserving all persistent databases (`IndexedDB`, `LocalStorage`, sessions).
- **macOS Gatekeeper Quarantine Clearance**: Automatically strips `com.apple.quarantine` attributes and sets executable permissions on language server binaries.
- **Automatic CLI Provisioning**: Restores the `agy` binary, links `antigravity-ide` to the installed IDE app, and adds `~/.local/bin` to your zsh `PATH` (creating `~/.zshrc` if the new Mac has none).
- **Zero External Dependencies**: Pure Python 3 standard library implementation. Runs out-of-the-box on any Mac with macOS system Python (`/usr/bin/python3`).
- **Safety Snapshots & Instant Rollback**: Creates an automated timestamped backup of the target endpoint before applying any changes, allowing one-command rollbacks.

---

## What Gets Migrated

| Category | Migrated Paths | Description |
| :--- | :--- | :--- |
| **Applications & CLI** | `/Applications/Antigravity.app`<br/>`/Applications/Antigravity IDE.app`<br/>`~/.local/bin/agy`, `antigravity-ide` | Full desktop apps, language server binaries, and command-line launchers. |
| **Brain & History** | `~/.gemini/antigravity/brain/*` | Complete conversation transcripts, system-generated logs, diffs, plans, walkthroughs, and artifacts. |
| **Configurations** | `~/.gemini/settings.json`<br/>`~/.gemini/state.json`<br/>`~/.gemini/GEMINI.md`, `AGENTS.md` | Global preferences, auto-execution policies, model choices, and agent behavior rules. |
| **Customizations & Skills** | `~/.gemini/config/`<br/>`~/.gemini/skills/` | Custom skills (`SKILL.md`), rules, hooks (`hooks.json`), and plugins (`plugins.json`). |
| **MCP Integrations** | `~/.gemini/config/mcp_config.json` | Model Context Protocol server configurations with path remapping. |
| **Desktop App State** | `~/Library/Application Support/Antigravity/`<br/>`~/Library/Application Support/Antigravity IDE/` | Persistent LocalStorage, SessionStorage, IndexedDB, and window states (with volatile caches safely pruned). |
| **Browser Profile** | `~/.gemini/antigravity-browser-profile/` | Saved session cookies and credentials for embedded browser automation and UI testing tools. |
| **Authentication** | `~/.gemini/oauth_creds.json`<br/>`~/.gemini/jetski-standalone-oauth-token`<br/>`~/.gemini/installation_id` | Authentication tokens and machine identity (can be excluded with `--no-credentials`). |

---

## Quick Start Guide

### Step 1: Export on Source MacBook

Run the export command on your existing MacBook:

```bash
# Default export: packages all history, configurations, skills, and settings (fast, excludes .app binaries)
./agy-migrate export

# Optional: include desktop .app binaries if target machine does not have Antigravity installed
./agy-migrate export --include-apps
```

By default, this creates a timestamped archive on your Desktop (e.g. `~/Desktop/antigravity_migration_data_<timestamp>.tar.gz`) along with a `.sha256` checksum file.

### Step 2: Transfer the Archive to the New MacBook

Transfer the `.tar.gz` archive to the new MacBook using one of the following methods:
- **AirDrop**: Fast and wireless.
- **External SSD / USB-C drive**: High throughput.
- **Direct Thunderbolt bridge**: Fastest cable transfer.

### Step 3: Restore on Destination MacBook

On the new MacBook, clone or copy this repository, then run:

```bash
./agy-migrate import --archive ~/Downloads/antigravity_migration_data_<timestamp>.tar.gz
```

The script will:
1. Validate the archive and manifest.
2. Prompt to stop any active Antigravity processes.
3. Create an automatic safety snapshot in `~/.gemini/antigravity-backup-<timestamp>`.
4. Translate all usernames and paths in configuration files.
5. Deploy configuration, history, and browser data to `~/.gemini` and `~/Library/Application Support`.
6. Strip macOS quarantine attributes and fix executable bits.
7. Provision the `agy` and `antigravity-ide` CLI launchers into `~/.local/bin/`.

### Step 4: Verify Installation Health

Run the diagnostic health checker:

```bash
./agy-migrate doctor
```

---

## Advanced Usage

### Direct MacBook-to-MacBook Sync (Over Local Network)

To migrate directly between two MacBooks on the same Wi-Fi or Thunderbolt network without using an intermediate drive:

```bash
./agy-migrate sync --target alice@new-macbook.local
```

### Inspect an Archive Without Restoring

View the manifest, conversation count, file inventory, and size breakdown of any archive:

```bash
./agy-migrate inspect --archive ~/Desktop/antigravity_migration.tar.gz
```

### Rollback / Undo a Restore

If you ever need to revert to the state prior to the last import:

```bash
./agy-migrate rollback
```

Or roll back to a specific snapshot:

```bash
./agy-migrate rollback --snapshot ~/.gemini/antigravity-backup-20260906_120000
```

### Simulating Operations (Dry Run)

Test what will be packaged or modified without altering files:

```bash
./agy-migrate export --dry-run
./agy-migrate import --archive ~/Desktop/antigravity_migration.tar.gz --dry-run
```

---

## CLI Reference

```text
agy-migrate [command] [options]

Commands:
  export     Export Antigravity instance and conversation history to an archive
             Options:
               -o, --output PATH     Specify custom output archive path
               --include-apps        Include full /Applications/Antigravity*.app binaries (default: False, data-only)
               --no-credentials      Exclude OAuth credentials and session tokens
               --ignore-errors       Export even if some directories are unreadable (incomplete bundle)
               -y, --yes             Auto-confirm prompts (non-interactive mode)
               --dry-run             Simulate export without writing archive

  import     Import Antigravity instance and history from an archive
             Options:
               -a, --archive PATH    Path to .tar.gz migration archive (required)
               --skip-snapshot       Skip creating pre-restore rollback snapshot
               --no-remap            Disable username and path remapping
               -y, --yes             Auto-confirm prompts
               --dry-run             Simulate import without modifying files

  inspect    Inspect metadata and contents of an archive
             Options:
               -a, --archive PATH    Path to .tar.gz migration archive (required)

  doctor     Audit local installation health and readiness

  rollback   Revert to previous pre-restore safety snapshot
             Options:
               -s, --snapshot PATH   Specific snapshot directory (defaults to latest)
               -y, --yes             Auto-confirm prompts

  sync       Direct peer-to-peer Mac-to-Mac live migration via SSH
             Options:
               -t, --target USER@HOST Remote destination host
               --include-apps         Include .app bundles (default: False)
               --dry-run              Simulate sync
```

---

## Safety & Known Limitations

- **Integrity**: `export` writes `<archive>.sha256`; `import` verifies it (keep the two files together) and refuses a corrupted archive.
- **Credentials**: the archive contains OAuth tokens and is created owner-only (`0600`). Transfer it over a trusted channel and delete it afterwards.
- **Incomplete exports fail loudly**: if a directory can't be read (typically macOS privacy protection on `~/Library` or `~/.gemini`), `export` aborts with exit code 1. Grant your terminal **Full Disk Access** and retry, or pass `--ignore-errors` to accept an incomplete bundle.
- **Running app**: Antigravity must be closed during `export`/`import`. In a non-interactive session pass `--yes` to stop it automatically.
- **Exit codes**: every command exits non-zero on failure, so it is safe to script (`sync` relies on this).
- **Rollback scope**: `rollback` reverts everything under `~/.gemini`, the Antigravity folders in `~/Library/Application Support`, and the `~/.local/bin` launchers, including removing files the import newly created. App bundles installed with `--include-apps` are not rolled back.
- **Path remapping** only rewrites text files (`.json`, `.jsonl`, `.md`, `.txt`, `.sh`, `.plist`, `.yaml`) and symlink targets. Binary state (protobuf `.pb` conversation files, SQLite databases such as `state.vscdb`) is copied verbatim, so absolute paths inside them keep the source username. This only matters when the username differs between Macs.
- **Electron secrets**: cookies and tokens stored by the desktop apps are encrypted with a per-Mac key in the login Keychain and will not decrypt on the new Mac, so expect to sign in to those apps again. `oauth_creds.json` is plain JSON and migrates as-is.
- **`agy` CLI**: it is migrated as a binary copy from `~/.local/bin/agy`. If it isn't present on the source Mac, install the CLI separately on the new one.

---

## Testing

Run the automated test suite:

```bash
python3 -m unittest test_agy_migrate.py -v
```
