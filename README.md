# claude-session-sync

See and resume your Claude desktop **Code** sessions from every Claude account you use on the same computer.

If you switch between two (or more) Claude accounts in the desktop app, for example when one hits its usage limit, the Code sidebar of the other account is empty. The transcripts are still on disk, but the sidebar is per account. This tool keeps the sidebars in sync so you can pick up any session from any account.

> Unofficial. Not affiliated with or endorsed by Anthropic. It works with files the desktop app keeps on your computer and may need an update if the app changes how it stores them.

## Install

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/bdarbaz/claude-session-sync/main/install.sh | bash
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/bdarbaz/claude-session-sync/main/install.ps1 | iex
```

Or clone the repo and run `bash install.sh` / `powershell -ExecutionPolicy Bypass -File install.ps1`.

Then **quit the Claude app completely once** (macOS: Cmd+Q; Windows: also quit it from the system tray) and open it again. That is only needed the first time. Afterwards the sessions are already in place whenever you switch accounts.

Requirements: Python 3.8+ (macOS: Command Line Tools, `xcode-select --install`; Windows: `winget install Python.Python.3.12`; Linux: your distro's `python3`). No other dependencies, no admin rights.

## How it works

The desktop app stores Code transcripts in `~/.claude/projects`, which every account on the computer shares. It stores the sidebar list separately for each account and organization:

```
<app data>/claude-code-sessions/<account-id>/<org-id>/local_<session>.json
```

| OS | `<app data>` |
|---|---|
| macOS | `~/Library/Application Support/Claude` |
| Windows | `%APPDATA%\Claude`, or `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude` for the Store version |
| Linux | `~/.config/Claude` |

`claude_session_sync.py` runs in the background (LaunchAgent on macOS, systemd user service or autostart entry on Linux, Startup-folder shortcut on Windows) and every 5 seconds copies those small `local_*.json` records between all account/org folders it finds. It follows these rules so it is safe while the app is running:

- **The signed-in account is never edited.** Its folder only receives new files. The app keeps those records in memory and would overwrite an edit anyway.
- **Signed-out accounts get updates**, but only from a copy that is at least as recent. Renames, archiving and new turns show up after you switch.
- **Account-specific fields stay put.** Connector configuration, Remote Control links and quota-limit errors are not copied between accounts.
- **Deletes follow you.** If you delete a session in one account, the copies in the others are moved to `~/.claude-session-sync/trash/`, not deleted.
- **First run makes a backup** of every folder it touches in `~/.claude-session-sync/backups/`.

Scheduled tasks and Cowork sessions are not synced.

## Commands

```bash
python3 ~/.claude-session-sync/claude_session_sync.py --status    # what it found
python3 ~/.claude-session-sync/claude_session_sync.py --dry-run   # what it would change
tail -f ~/.claude-session-sync/sync.log                            # what it did
```

To leave an account or organization alone, list its id (from `--status`) in `~/.claude-session-sync/config.json`:

```json
{ "exclude": ["00000000-0000-0000-0000-000000000000"] }
```

## Uninstall

```bash
bash ~/.claude-session-sync/install.sh --uninstall
```

```powershell
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.claude-session-sync\install.ps1 -Uninstall
```

Sessions that were copied stay in the sidebars. Delete `~/.claude-session-sync` if you don't need the backups, trash and logs.

## Good to know

- **Same computer only.** Transcripts live on each computer's disk, so this does not move sessions between computers.
- **One account at a time per session.** Don't keep the same session running in two accounts at once. Both would append to the same transcript. Let a running turn finish before you switch.
- **Connectors are per account.** GitHub, Figma, Linear and other claude.ai connectors have to be connected in each account. A session resumed in the other account uses that account's connectors.
- **Folders must exist.** A session opens in its original working folder, so external drives must be mounted.
- **Chats on claude.ai are not affected.** They are stored on Anthropic's servers per account; this tool only deals with the desktop app's local Code sessions.

## Development

```bash
python3 -m unittest discover -s tests -v
```

CI runs the tests on macOS, Linux and Windows, and runs each installer against a fake two-account setup.

## Türkçe

Claude masaüstü uygulamasında birden fazla hesap arasında geçiş yaptığında (örneğin birinin limiti dolduğunda), diğer hesabın Code kenar çubuğu boş görünür. Bu araç her hesabın session listesini diğerlerine kopyalar, böylece hangi hesaptaysan tüm session'larına devam edebilirsin. Kurulum için yukarıdaki tek satırlık komutu çalıştır, ardından Claude uygulamasını bir kez tamamen kapatıp aç. Resmi değildir, Anthropic ile bağlantısı yoktur.

## License

MIT
