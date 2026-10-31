<img src="icon/poplocker_256.png" alt="Pop Locker" width="96" align="left">

# Pop Locker

**POP = Projects On Premises.** Your projects, chats and code, locked away on your own PC.

<a href="https://buymeacoffee.com/vioprime1h"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="50"></a>

Pop Locker is a companion for Claude Code on Windows. It isn't made by or affiliated with Anthropic.

- **Reads Claude Code's replies out loud** in a small window, and skips the code, tables, file paths and links.
  Back, Pause, Skip and Stop buttons, or click any paragraph to read from there. Male or female voice.
- **Talk back by voice.** Say "Hey Computer, ..." (you can change the wake word) or press Talk, and your words are
  typed into the open Claude Code chat.
- **Talk from your phone.** iPhone through a Siri Shortcut, or Android with the NuePost app in this repo.
- **Every chat, project and file saved on your PC.** Each chat, and the latest version of every file Claude writes
  or edits, goes into your own PostgreSQL database, filed under its project. Search by keyword, date or project.
  Passwords and keys in the code are blanked out first. Nothing leaves your PC.

## Install

1. Download `PopLocker_Setup_<date>.zip` from the [Releases](../../releases) page and unzip it to `C:\PopLocker`.
2. Double-click `Setup.bat`, say Yes to the administrator prompt, and choose a password for your chat database.

The zip includes its own Python, PostgreSQL, speech model and voices, so nothing else needs to be installed.
The full instructions are in [README.txt](README.txt).

## What's in this repo

| Path | What it is |
|------|------------|
| `claude_voice_player.pyw` | The reader window |
| `claude_voice_ears.py` | Wake word, Talk button, and the phone door on port 5055 |
| `claude_voice_hook.py` | Claude Code Stop hook: picks out the readable parts of each reply |
| `claude_chat_save.py` | Saves the chats to the database |
| `chat_search.pyw` | The History search window |
| `Setup.bat`, `setup_claude_voice.ps1`, `setup_hooks.py` | The installer |
| `NuePost/` | Android app (Kotlin, Android Studio): hold to talk, posts your words to Pop Locker |

The speech engine, the voices, Python and PostgreSQL are left out of the repo because of their size. They're in
the Releases zip.

## Credits

- [PostgreSQL](https://www.postgresql.org) (PostgreSQL License)
- [Python 3.13](https://www.python.org) (PSF License)
- [Piper](https://github.com/rhasspy/piper) speech (MIT)
- Voices "Northern English Male" and "Southern English Female": Piper voices trained on the OpenSLR 83 dataset,
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
- Speech recognition: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) with OpenAI's Whisper
  base.en model (MIT)

## License

MIT for the Pop Locker code. See [LICENSE](LICENSE).
