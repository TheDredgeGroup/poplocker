"""Save Claude chats to PostgreSQL, sortable by project name.

Database claude_chats, table messages: one row per thing the user typed or said and per reply Claude wrote, each with
its project - the chat's title in the Claude app, or the folder it runs in. Table artifacts: every file Claude
writes or edits in a chat (its latest version, filed under the chat's project), and imported artifacts.
Sort with:  SELECT * FROM messages ORDER BY project, at;
The Stop hook starts this in the background after every reply; rows already saved are skipped, so running it again
is harmless.

  python claude_chat_save.py <transcript.jsonl> [cwd]     save one chat
  python claude_chat_save.py --all                        save every chat Claude Code has on this PC
  python claude_chat_save.py --setup                      create the database and table
Connection details are in db.json next to this file.
"""
import glob, json, os, re, sys

import psycopg2
from psycopg2.extras import execute_values

HERE = os.path.dirname(os.path.abspath(__file__))
DB_NAME = "claude_chats"
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def connect(database=DB_NAME):
    with open(os.path.join(HERE, "db.json")) as f:
        cfg = json.load(f)
    cfg["database"] = database
    return psycopg2.connect(**cfg)


def setup():
    conn = connect("postgres")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {DB_NAME}")
    conn.close()
    conn = connect()
    with conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         bigserial PRIMARY KEY,
                project    text NOT NULL,
                session_id text NOT NULL,
                role       text NOT NULL,          -- 'user' or 'claude'
                content    text NOT NULL,
                at         timestamptz NOT NULL,
                cwd        text,
                msg_key    text UNIQUE NOT NULL,   -- stops the same message being saved twice
                saved_at   timestamptz NOT NULL DEFAULT now()
            );
            -- moved: filed under another project by hand (part of a chat), so a rename of the chat leaves it there
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS moved boolean NOT NULL DEFAULT false;
            CREATE INDEX IF NOT EXISTS messages_project_at ON messages (project, at);
            CREATE INDEX IF NOT EXISTS messages_session_at ON messages (session_id, at);
            CREATE OR REPLACE VIEW projects AS
                SELECT project, count(*) AS messages, count(DISTINCT session_id) AS chats,
                       min(at) AS started, max(at) AS last_used
                FROM messages GROUP BY project;
            CREATE TABLE IF NOT EXISTS artifacts (
                id           bigserial PRIMARY KEY,
                project      text NOT NULL,
                session_id   text NOT NULL,
                title        text NOT NULL,
                kind         text NOT NULL,      -- 'artifact', 'file' or 'drive file'
                name         text NOT NULL,      -- the artifact id or the file's path
                type         text,
                content      text NOT NULL,      -- the latest version
                edits        int NOT NULL DEFAULT 0,
                created_at   timestamptz NOT NULL,
                updated_at   timestamptz NOT NULL,
                source       text NOT NULL,
                UNIQUE (session_id, kind, name));
            CREATE INDEX IF NOT EXISTS artifacts_project ON artifacts (project, updated_at);
        """)
    conn.close()


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MAX_BYTES = 3_000_000
SKIPPED_LOG = os.path.join(HERE, "skipped_files.txt")
# files that are secrets whatever they're called
SECRET_FILE = re.compile(r"^\.env(\..+)?$|\.env$|^id_(rsa|dsa|ecdsa|ed25519)$|\.(pem|key|pfx|p12|jks|keystore|ppk)$", re.I)
# whole words in a file's name, so tokenizer.py and keyboard.py aren't caught
SECRET_WORDS = {"secret", "secrets", "password", "passwords", "passwd", "pwd", "credential", "credentials", "creds", "apikey"}
SECRET_PAIRS = {("api", "key"), ("api", "keys"), ("private", "key"), ("access", "key"), ("auth", "token"), ("access", "token")}
# "key" or "token" alone: a settings or text file is probably a secret, source code probably isn't (its values are
# still blanked out below)
MAYBE_WORDS = {"key", "keys", "token", "tokens"}
CODE_FILES = {".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb",
              ".php", ".swift", ".gd", ".lua", ".sql", ".html", ".css", ".md", ".ps1", ".bat", ".sh"}
# passwords and API keys written into the code are blanked out - the code stays usable, the secret doesn't get
# copied into one more place
SECRET_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
SECRET_VALUES = [
    re.compile(r"(?:xai-|sk-ant-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}"
               r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"                                 # AWS
               r"|\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])|\bGOCSPX-[0-9A-Za-z_-]{20,}|\bya29\.[0-9A-Za-z._-]{20,}"   # Google
               r"|\bxox[abeoprs]-[0-9A-Za-z-]{10,}|\bxapp-[0-9A-Za-z-]{10,}|hooks\.slack\.com/services/[0-9A-Za-z/]+"  # Slack
               ),
    re.compile(r"""((?:api_?key|password|passwd|pass|pwd|secret|token)\w*["']?\s*[:=]\s*["'])[^"'\s]{6,}(["'])""", re.I),
    re.compile(r"((?:set\s+)?\w*(?:PASSWORD|PASSWD|PWD|KEY|TOKEN|SECRET)\w*=)[^\s\"']{6,}", re.I),
]
# the password in postgres://user:password@host, keeping the rest of the address
SECRET_IN_ADDRESS = re.compile(r"""(\b[a-z][a-z0-9+.-]*://[^\s:/@'"]*:)[^\s/@'"]+(@)""", re.I)


def is_secret_name(path):
    name = os.path.basename(path)
    if SECRET_FILE.search(name):
        return True
    stem, ext = os.path.splitext(name)
    words = re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem).lower())
    if SECRET_WORDS.intersection(words) or SECRET_PAIRS.intersection(zip(words, words[1:])):
        return True
    return bool(MAYBE_WORDS.intersection(words)) and ext.lower() not in CODE_FILES


def note_skipped(paths):
    """Write down each file left out for its name, once, so it's never left out without saying."""
    try:
        with open(SKIPPED_LOG, encoding="utf-8") as f:
            known = set(f.read().splitlines())
    except OSError:
        known = set()
    new = [p for p in paths if p not in known]
    if new:
        try:
            with open(SKIPPED_LOG, "a", encoding="utf-8") as f:
                f.write("".join(p + "\n" for p in new))
        except OSError:
            pass


def redact(text):
    text = SECRET_BLOCK.sub("<removed>", text)
    text = SECRET_IN_ADDRESS.sub(r"\1<removed>\2", text)
    text = SECRET_VALUES[0].sub("<removed>", text)
    text = SECRET_VALUES[1].sub(r"\1<removed>\2", text)
    return SECRET_VALUES[2].sub(r"\1<removed>", text)


def apply_edit(text, old, new, every=False):
    if text is None or old not in text:
        return None              # the file existed before this chat - its text is read from disk instead
    return text.replace(old, new) if every else text.replace(old, new, 1)


def track_file(files, tool, inp, ts):
    """Follow one Write/Edit of a file in the chat, keeping what the chat alone says the file now holds."""
    path = inp.get("file_path") or inp.get("notebook_path")
    if not path:
        return
    f = files.setdefault(os.path.normpath(path), {"text": None, "edits": 0, "first": ts, "last": ts})
    f["edits"] += 1
    f["last"] = ts
    if tool == "Write":
        f["text"] = inp.get("content", "")
    elif tool == "Edit":
        f["text"] = apply_edit(f["text"], inp.get("old_string", ""), inp.get("new_string", ""), inp.get("replace_all"))
    elif tool == "MultiEdit":
        for e in inp.get("edits") or []:
            f["text"] = apply_edit(f["text"], e.get("old_string", ""), e.get("new_string", ""), e.get("replace_all"))
    else:
        f["text"] = None         # a notebook: read from disk


def file_rows(files, project, session):
    """The latest version of every file Claude wrote or edited in the chat: what's on disk now, or, for a file
    deleted since, what the chat wrote. Binary, huge and secret-named files are left out; the secret-named ones
    are listed in skipped_files.txt."""
    rows, skipped = [], []
    for path, f in files.items():
        if is_secret_name(path):
            skipped.append(path)
            continue
        text = f["text"]
        try:
            if os.path.getsize(path) <= MAX_BYTES:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
        except (OSError, UnicodeDecodeError, ValueError):
            pass
        if not text or not text.strip() or "\x00" in text:
            continue
        rows.append((project, session, os.path.basename(path), "file", path,
                     os.path.splitext(path)[1].lstrip(".").lower() or None, redact(text), f["edits"],
                     f["first"], f["last"], "Claude Code"))
    note_skipped(skipped)
    return rows


def rows_from(path, cwd=None):
    """The user's own messages and Claude's written replies (no tool calls or tool results), and the files
    Claude wrote or edited."""
    rows, session, files = [], os.path.splitext(os.path.basename(path))[0], {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            cwd = d.get("cwd") or cwd
            session = d.get("sessionId") or session
            m, ts, uuid = d.get("message") or {}, d.get("timestamp"), d.get("uuid")
            if not (ts and uuid):
                continue
            if d.get("type") == "assistant" and isinstance(m.get("content"), list):
                for c in m["content"]:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in FILE_TOOLS:
                        track_file(files, c["name"], c.get("input") or {}, ts)
            if d.get("type") == "user" and d.get("promptSource") and not d.get("isMeta"):
                role = "user"
            elif d.get("type") == "assistant":
                role = "claude"
            else:
                continue
            text = REMINDER.sub("", text_of(m.get("content"))).strip()
            if text:
                rows.append([role, text, ts, uuid])
    project = project_name(os.path.splitext(os.path.basename(path))[0], cwd, app_titles())
    return ([(project, session, role, text, ts, cwd, uuid) for role, text, ts, uuid in rows],
            file_rows(files, project, session))


def app_titles():
    """Chat titles from the Claude desktop app, by the session id its transcript uses. The project is
    the chat's name in the app, not its folder."""
    titles = {}
    pattern = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data", "*", "claude-code-sessions", "*", "*",
                           "local_*.json")
    for p in glob.glob(pattern):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("cliSessionId") and d.get("title"):
            titles[d["cliSessionId"]] = d["title"]
    return titles


def project_name(session, cwd, titles):
    return titles.get(session) or os.path.basename((cwd or "").rstrip("\\/")) or "(no project)"


def save(path, cwd=None):
    rows, files = rows_from(path, cwd)
    if not rows:
        return 0
    conn = connect()
    with conn, conn.cursor() as cur:
        execute_values(cur, """INSERT INTO messages (project, session_id, role, content, at, cwd, msg_key)
                               VALUES %s ON CONFLICT (msg_key) DO NOTHING""", rows, page_size=500)
        # a chat renamed in the app moves all its messages to the new name
        cur.execute("UPDATE messages SET project = %s WHERE session_id = %s AND project <> %s AND NOT moved",
                    (rows[0][0], rows[0][1], rows[0][0]))
        n = cur.rowcount
        # the files Claude wrote: one row per file per chat, kept at its latest version
        if files:
            execute_values(cur, """INSERT INTO artifacts (project, session_id, title, kind, name, type, content,
                                                          edits, created_at, updated_at, source)
                                   VALUES %s ON CONFLICT (session_id, kind, name) DO UPDATE SET
                                     project = EXCLUDED.project, content = EXCLUDED.content, edits = EXCLUDED.edits,
                                     updated_at = EXCLUDED.updated_at""", files, page_size=100)
    conn.close()
    return n


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup()
        print("database ready")
    elif "--all" in sys.argv:
        files = glob.glob(os.path.expanduser(r"~\.claude\projects\*\*.jsonl"))
        for p in files:
            save(p)
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM artifacts)")
            msgs, arts = cur.fetchone()
            print(f"{len(files)} chats looked at, {msgs} messages and {arts} files in the database")
        if os.path.exists(SKIPPED_LOG):
            with open(SKIPPED_LOG, encoding="utf-8") as f:
                print(f"{len(f.read().splitlines())} files left out because their names look secret: see {SKIPPED_LOG}")
    elif len(sys.argv) > 1:
        save(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
