POP LOCKER
==========
POP = Projects On Premises: your projects, chats and code, locked away on your own PC.

Reads Claude Code's replies out loud in a small window, and skips code, tables, file paths and links.
You can talk back to Claude through the PC's mic or from your iPhone.

WHAT YOU NEED
- Windows 10 or 11, 64-bit
- The Claude desktop app with Claude Code
Everything else comes in this package: its own Python, its own PostgreSQL database, the speech model and the
voices. Nothing gets downloaded, and nothing is changed in any Python or PostgreSQL you already have.

INSTALL
1. Unzip the package so the app's files are in C:\PopLocker. Any folder works, but keep it there once it's
   set up.
2. Double-click Setup.bat and say Yes when Windows asks for administrator rights.
3. Choose a password for your chat database when it asks. That's the only question.
4. Setup then:
   - installs the Microsoft C++ runtime if Windows needs it;
   - sets up the chat database as a Windows service called PopLockerDB, on port 5488, local only;
   - adds the Claude Code hooks;
   - puts "Pop Locker" and "Pop Locker History" on the Desktop;
   - saves the Claude chats already on this PC.
5. When Windows Firewall asks about Python, click Allow access on Private networks, so your iPhone can reach it.
6. Double-click "Pop Locker" on the Desktop. From then on it also starts with Windows, and it opens by
   itself when Claude Code starts.

YOUR CHAT HISTORY
Every chat with Claude Code on this PC is saved to your own PostgreSQL database (claude_chats) after each reply.
Each chat is filed under its project, which is the chat's name in the Claude app. Nothing leaves your PC.
Every file Claude writes or edits is saved too, at its latest version, filed under the same project. Passwords
and keys written in the code are blanked out first.
- Click History in the window, or open "Pop Locker History", to search by keyword, date (10/30/2026), project,
  or any mix of them. Pick Artifacts to search the files instead. Click a result to read all of it.
- Database connection: host localhost, port 5488 (see db.json), user postgres, the password you chose.
  Any PostgreSQL tool, such as pgAdmin, can open it.

THE WINDOW
- Back / Pause / Skip / Stop work the reading. Click any paragraph to read from there.
- Mic: listens for the wake word on this PC's mic. Talk: sends the next thing you say, no wake word needed.
- Voice: Male - Northern or Female - Southern. You can switch while it's reading.
- The wake word is "Hey Computer". The ear button changes it, for example to "Hey Jarvis". Two words work best. The mic starts using a new
  wake word right away.
- Say "<wake word>, pause / skip / back / stop / play" to work the reader by voice.

TALK FROM YOUR iPHONE (the iPhone has to be on the same Wi-Fi)
Setup puts your address and private key in "iPhone address.txt". In the Shortcuts app, make a shortcut
called "Tell Pop Locker" with these 4 actions:
1. Dictate Text. Stop Listening: After Pause.
2. Get Contents of URL. Use the address from "iPhone address.txt". Then:
   - Method: POST;
   - Request Body: JSON;
   - add one Text field: text = Dictated Text.
3. Get Dictionary Value. Get: Value, for Key: response, in: Contents of URL.
4. Speak Text. Text: Dictionary Value.
Then say "Hey Siri, Tell Pop Locker" and talk. Your words get typed into the Claude chat that's open. The reply
is read on this PC.
Keep the key private: anyone on your Wi-Fi who has it can type to Claude.

TALK FROM AN ANDROID PHONE (same Wi-Fi)
1. Copy Android\NuePost.apk from this folder to the phone and open it. Android asks you to allow installs
   from that app (Files, Drive or similar) the first time - allow it, then tap Install.
2. Open NuePost and allow the microphone.
3. Tap Settings. Endpoint URL: the address from "iPhone address.txt". JSON Key: text. Tap Save.
4. Hold the big mic button, talk, and let go. Your words get typed into the Claude chat that's open, and the
   reply is read on this PC.

MORE THAN ONE PC
Claude runs on one PC, the main PC. A second PC that shows the chat through Remote Control can still be talked
to and read the replies:
1. On the second PC, put the main PC's address in a file called main_pc.txt in this folder, for example
   http://192.168.1.46:5055
2. Use the same key on both PCs: copy ears_key.txt across.
Replies are read on whichever PC you last talked through.

TVs AND HDMI SPEAKERS
A TV or dock speaker puts its sound to sleep when nothing is playing. The window keeps it awake while reading,
and for a minute after. After a quiet spell, the first reply starts about 3 seconds late, so no words get cut.

TURN IT OFF
Make a file named OFF in this folder to stop the reading. Delete the file to turn it back on.

CREDITS
Database: PostgreSQL (PostgreSQL License), https://www.postgresql.org
Python: Python 3.13 (PSF License), https://www.python.org
Speech: Piper (MIT license), https://github.com/rhasspy/piper
Voices: "Northern English Male" and "Southern English Female", Piper voices trained on the OpenSLR 83 dataset,
  licensed CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/).
Speech recognition: faster-whisper (MIT) with OpenAI's Whisper base.en model (MIT).
