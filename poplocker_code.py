"""Keep the full code of every project in the claude_chats database, as versions you can pull back out and run.

Anyone can use it with plain SQL - another AI, a local model, or a person with a query tool. Start with:
    SELECT * FROM start_here;

Tables
  code_projects  one row per project: its folder, how to start it, folders left out on purpose
  code_versions  one row per saved state of a project. 'working' marks a state known to work: pushed to GitHub,
                 a git tag, or marked by hand
  code_files     every file in a version: its path, size, the blob that holds it - or why it was left out
  code_blobs     file contents, stored once however many versions share them: text in 'text', anything else in 'data'
Views
  start_here     how to use these tables
  latest_code    the newest working version of every project (the newest version when none is marked), one row per file

  python poplocker_code.py list
  python poplocker_code.py add "<project>" <folder> [--run "<command>"] [--skip dir1,dir2] [--notes "..."]
  python poplocker_code.py rename "<project>" "<new name>"
  python poplocker_code.py snap "<project>" | --all          save a new version when anything changed
  python poplocker_code.py working "<project>" [version] [--note "..."]
  python poplocker_code.py pull "<project>" [folder] [--version N] [--newest] [--force]
  python poplocker_code.py run "<project>" [--version N] [--newest]
  python poplocker_code.py git-tags "<project>"              save every git tag of the project as a working version
  python poplocker_code.py files "<project>" [--version N]   list a version's files
"""
import datetime, hashlib, io, os, re, subprocess, sys, tarfile, time

from claude_chat_save import connect, is_secret_name, redact

HERE = os.path.dirname(os.path.abspath(__file__))
TEXT_MAX = 3_000_000           # bigger text files are data dumps, not code
BIN_MAX = 25_000_000           # pictures, icons, sounds, small programs - not models or videos
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "env", "site-packages", "build", "dist", ".godot",
             "intermediate", "saved", "deriveddatacache", "binaries", ".idea", ".vs", ".vscode", "target", ".gradle",
             ".pytest_cache", ".mypy_cache", "logs"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".tmp", ".pid", ".dmp", ".mp4", ".mov", ".webm", ".avi", ".mkv", ".zip", ".7z",
            ".rar", ".whl", ".apk", ".aab", ".blend1", ".bak", ".gguf", ".safetensors", ".ckpt"}
NO_WINDOW = 0x08000000
AUTO_ROOT_MARKS = (".git", "package.json", "pyproject.toml", "requirements.txt", "project.godot", "setup.py")
SNAP_ALL_EVERY = 6 * 3600      # the after-reply save also checks every project this often (catches edits made by hand)
SNAP_ALL_STAMP = os.path.join(HERE, "code_snap_all.txt")
# files Pop Locker itself rewrites while it runs - never a reason for a new version
RUNTIME_FILES = {"code_snap_all.txt", "last_reply.json", "ears_status.txt", "skipped_files.txt", "ears_off",
                 "talk_now", "control.txt", "route.json", "player_settings.json"}

START_HERE = """This database keeps the full code of every project, as versions.

1. See the projects:            SELECT * FROM code_projects;
2. See a project's versions:    SELECT id, taken_at, working, git_commit, note, file_count FROM code_versions
                                WHERE project = 'NAME' ORDER BY taken_at DESC;
3. The latest working code:     SELECT path, text FROM latest_code WHERE project = 'NAME' ORDER BY path;
   ('working' = pushed to GitHub, a git tag, or marked by hand. With none marked, latest_code shows the newest.)
4. One file of one version:     SELECT b.text FROM code_files f JOIN code_blobs b ON b.sha1 = f.sha1
                                WHERE f.version_id = N AND f.path = 'folder/file.py';
   Text files are in code_blobs.text, other files (pictures, programs) in code_blobs.data as bytes.
5. What a version left out:     SELECT path, size, note FROM code_files WHERE version_id = N AND sha1 IS NULL;
   Left out on purpose: virtual environments, caches, build output, logs, videos, archives, files over 25 MB
   (models), and files whose names look like keys or passwords. Passwords and API keys written inside files are
   replaced with <removed>; those files have note = 'passwords/keys blanked' - put your own back before running.
6. How the project was built and why: the chats are in the messages table -
                                SELECT role, content, at FROM messages WHERE content ILIKE '%NAME%' ORDER BY at;
7. Write a whole version back to disk and start it:
                                python poplocker_code.py pull "NAME" [folder]     /    python poplocker_code.py run "NAME"
   code_projects.run_cmd is the command that starts it (run inside the pulled folder), code_projects.notes says
   anything else it needs (Python packages, a database, a model to download).
"""


def setup(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS code_projects (
            name      text PRIMARY KEY,
            folder    text NOT NULL,
            run_cmd   text,
            notes     text,
            skip      text[] NOT NULL DEFAULT '{}',   -- folders (or relative paths) under it that are left out
            added_at  timestamptz NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS code_blobs (
            sha1  text PRIMARY KEY,
            size  bigint NOT NULL,
            text  text,          -- text files (UTF-8)
            data  bytea);        -- anything else
        CREATE TABLE IF NOT EXISTS code_versions (
            id          bigserial PRIMARY KEY,
            project     text NOT NULL REFERENCES code_projects (name) ON UPDATE CASCADE ON DELETE CASCADE,
            taken_at    timestamptz NOT NULL,
            working     boolean NOT NULL DEFAULT false,
            git_commit  text,
            note        text,
            file_count  int NOT NULL DEFAULT 0,
            total_bytes bigint NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS code_versions_project ON code_versions (project, taken_at DESC);
        CREATE TABLE IF NOT EXISTS code_files (
            version_id  bigint NOT NULL REFERENCES code_versions (id) ON DELETE CASCADE,
            path        text NOT NULL,            -- relative to the project folder, with forward slashes
            size        bigint NOT NULL,
            mtime       double precision NOT NULL,
            sha1        text REFERENCES code_blobs (sha1),   -- NULL: left out, see note
            note        text,
            PRIMARY KEY (version_id, path));
        CREATE INDEX IF NOT EXISTS code_files_path ON code_files (path);
        CREATE OR REPLACE VIEW start_here AS SELECT %s::text AS read_me;
        CREATE OR REPLACE VIEW latest_code AS
            WITH pick AS (
                SELECT DISTINCT ON (project) project, id, working, taken_at FROM code_versions
                ORDER BY project, working DESC, taken_at DESC, id DESC)
            SELECT p.project, p.id AS version_id, p.working, p.taken_at, f.path, f.size, b.text,
                   (b.sha1 IS NOT NULL AND b.text IS NULL) AS is_binary, f.note
            FROM pick p JOIN code_files f ON f.version_id = p.id LEFT JOIN code_blobs b ON b.sha1 = f.sha1;
    """, (START_HERE,))


# ---------- reading a folder ----------

def left_out_reason(name, size):
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext in SKIP_EXT:
        return f"{ext} files are left out"
    if ".before_" in low or low.endswith((".orig", "~")):
        return "old copy"
    if is_secret_name(name):
        return "name looks like a key or password"
    return None


def files_of(folder, skip):
    """(relative path, full path, size, mtime, reason left out or None) for every file under folder."""
    skip_names = {s.lower() for s in skip if "/" not in s} | RUNTIME_FILES
    skip_paths = {s.lower().strip("/") for s in skip if "/" in s}
    for root, dirs, names in os.walk(folder):
        rel_root = os.path.relpath(root, folder).replace("\\", "/")
        rel_root = "" if rel_root == "." else rel_root + "/"
        dirs[:] = sorted(d for d in dirs
                         if d.lower() not in SKIP_DIRS and not d.lower().startswith(("venv", "backup", "bat_backup"))
                         and d.lower() not in skip_names and (rel_root + d).lower() not in skip_paths
                         and not os.path.exists(os.path.join(root, d, "pyvenv.cfg")))
        for n in sorted(names):
            if n.lower() in skip_names or (rel_root + n).lower() in skip_paths:
                continue                                 # left out on purpose for this project (it changes all the time)
            full = os.path.join(root, n)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield rel_root + n, full, st.st_size, st.st_mtime, left_out_reason(n, st.st_size)


def blob_of(data):
    """(sha1, text or None, bytes or None, note) for a file's bytes - text is decoded and its secrets blanked."""
    text = None
    if b"\x00" not in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    note = None
    if text is not None:
        clean = redact(text)
        if clean != text:
            note = "passwords/keys blanked"
        data = clean.encode("utf-8")
        text = clean
    return hashlib.sha1(data).hexdigest(), text, (None if text is not None else data), note, len(data)


def store_blob(cur, sha1, text, data, size):
    cur.execute("SELECT 1 FROM code_blobs WHERE sha1 = %s", (sha1,))
    if not cur.fetchone():
        cur.execute("INSERT INTO code_blobs (sha1, size, text, data) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (sha1, size, text, data))


def file_entry(cur, rel, data, size, mtime):
    """(path, size, mtime, sha1, note) after saving the file's contents - or why it was left out."""
    if size > BIN_MAX:
        return rel, size, mtime, None, f"too big to keep ({size / 1e6:.0f} MB)"
    sha1, text, raw, note, stored = blob_of(data)
    if text is not None and size > TEXT_MAX:
        return rel, size, mtime, None, f"text file too big to keep ({size / 1e6:.0f} MB)"
    store_blob(cur, sha1, text, raw, stored)
    return rel, size, mtime, sha1, note


def git_state(folder):
    """(short commit, working) - working when the files git tracks match a commit that is on GitHub."""
    if not os.path.isdir(os.path.join(folder, ".git")):
        return None, False

    def git(*args):
        try:
            return subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=30,
                                  creationflags=NO_WINDOW).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    head = git("rev-parse", "--short", "HEAD")
    if not head:
        return None, False
    clean = not git("status", "--porcelain", "--untracked-files=no")
    pushed = bool(git("branch", "-r", "--contains", "HEAD"))
    return head, clean and pushed


# ---------- saving versions ----------

def project_row(cur, name):
    cur.execute("SELECT name, folder, run_cmd, notes, skip FROM code_projects WHERE lower(name) = lower(%s)", (name,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f'no project called "{name}" - see: python poplocker_code.py list')
    return row


def newest_version(cur, project, working_only=False):
    cur.execute(f"""SELECT id FROM code_versions WHERE project = %s {'AND working' if working_only else ''}
                    ORDER BY taken_at DESC, id DESC LIMIT 1""", (project,))
    row = cur.fetchone()
    return row[0] if row else None


def snap(conn, name):
    """Save the project as a new version if any file changed since the last one. Returns a one-line report."""
    with conn, conn.cursor() as cur:
        project, folder, _run, _notes, skip = project_row(cur, name)
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (project,))   # one save per project at a time
        if not os.path.isdir(folder):
            return f"{project}: folder not found ({folder})"
        last = newest_version(cur, project)
        before = {}
        if last:
            cur.execute("SELECT path, size, mtime, sha1, note FROM code_files WHERE version_id = %s", (last,))
            before = {r[0]: r for r in cur.fetchall()}
        entries = []
        for rel, full, size, mtime, reason in files_of(folder, skip):
            old = before.get(rel)
            if reason:
                entries.append((rel, size, mtime, None, reason))
            elif old and old[1] == size and old[2] == mtime and old[3]:
                entries.append(old)                       # unchanged since the last version: not read again
            else:
                try:
                    with open(full, "rb") as f:
                        data = f.read() if size <= BIN_MAX else b""
                except OSError as e:
                    entries.append((rel, size, mtime, None, f"could not be read ({e.strerror})"))
                    continue
                entries.append(file_entry(cur, rel, data, size, mtime))
        commit, working = git_state(folder)
        same = last and {(e[0], e[3], e[4]) for e in entries} == {(r[0], r[3], r[4]) for r in before.values()}
        if same:
            # nothing changed - but a push since the last save makes that version a working one
            cur.execute("""UPDATE code_versions SET git_commit = coalesce(%s, git_commit), working = working OR %s
                           WHERE id = %s""", (commit, working, last))
            return f"{project}: no change (version {last})"
        kept = [e for e in entries if e[3]]
        cur.execute("""INSERT INTO code_versions (project, taken_at, working, git_commit, note, file_count, total_bytes)
                       VALUES (%s, now(), %s, %s, %s, %s, %s) RETURNING id""",
                    (project, working, commit, "pushed to GitHub" if working else None, len(kept),
                     sum(e[1] for e in kept)))
        vid = cur.fetchone()[0]
        from psycopg2.extras import execute_values
        execute_values(cur, "INSERT INTO code_files (version_id, path, size, mtime, sha1, note) VALUES %s",
                       [(vid,) + tuple(e) for e in entries], page_size=500)
        return (f"{project}: saved version {vid} - {len(kept)} files, {sum(e[1] for e in kept) / 1e6:.1f} MB"
                f", {len(entries) - len(kept)} left out{' (working: pushed to GitHub)' if working else ''}")


def git_tags(conn, name):
    """Save every git tag of the project as a working version (only the files git tracks are in a tag)."""
    with conn, conn.cursor() as cur:
        project, folder, *_ = project_row(cur, name)
        out = subprocess.run(["git", "-C", folder, "tag", "--sort=creatordate",
                              "--format=%(refname:short)|%(creatordate:iso-strict)|%(objectname:short)"],
                             capture_output=True, text=True, creationflags=NO_WINDOW).stdout.split()
        report = []
        for line in out:
            tag, when, commit = line.split("|")
            note = f"git tag {tag} (files git tracks)"
            cur.execute("SELECT id FROM code_versions WHERE project = %s AND note = %s", (project, note))
            if cur.fetchone():
                continue
            tar = subprocess.run(["git", "-C", folder, "archive", "--format=tar", tag], capture_output=True,
                                 creationflags=NO_WINDOW).stdout
            entries = []
            with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                for m in t.getmembers():
                    if not m.isfile():
                        continue
                    reason = left_out_reason(os.path.basename(m.name), m.size)
                    if reason:
                        entries.append((m.name, m.size, float(m.mtime), None, reason))
                    else:
                        entries.append(file_entry(cur, m.name, t.extractfile(m).read(), m.size, float(m.mtime)))
            kept = [e for e in entries if e[3]]
            cur.execute("""INSERT INTO code_versions (project, taken_at, working, git_commit, note, file_count,
                                                      total_bytes)
                           VALUES (%s, %s, true, %s, %s, %s, %s) RETURNING id""",
                        (project, when, commit, note, len(kept), sum(e[1] for e in kept)))
            vid = cur.fetchone()[0]
            from psycopg2.extras import execute_values
            execute_values(cur, "INSERT INTO code_files (version_id, path, size, mtime, sha1, note) VALUES %s",
                           [(vid,) + tuple(e) for e in entries], page_size=500)
            report.append(f"{tag} -> version {vid} ({len(kept)} files)")
        return f"{project}: " + (", ".join(report) if report else "every tag was already saved")


# ---------- the after-reply save ----------

def auto_root(path):
    """The project folder a file Claude edited belongs to: the nearest folder above it that looks like a project.
    Temp, scratch and settings folders never count."""
    low = path.lower().replace("/", "\\")
    if any(p in low for p in ("\\appdata\\", "\\temp\\", "\\.claude\\", "\\scratchpad")):
        return None
    d = os.path.dirname(path)
    home = os.path.expanduser("~").lower()
    while d and len(d) > 3 and d.lower() != home:
        try:
            names = os.listdir(d)
        except OSError:
            return None
        if any(m in names for m in AUTO_ROOT_MARKS):     # Unreal projects are added by hand (Content is huge)
            return d
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return None


def snap_touched(paths, everything=False):
    """Called after every reply with the files Claude wrote or edited in the chat: saves each project they belong
    to (adding new project folders it finds), and every project now and then."""
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            setup(cur)
            cur.execute("SELECT name, folder FROM code_projects")
            projects = cur.fetchall()
        hit = set()
        for p in paths:
            lp = os.path.normcase(os.path.abspath(p))
            owners = [(len(f), n) for n, f in projects if lp.startswith(os.path.normcase(os.path.abspath(f)) + os.sep)]
            if owners:
                hit.add(max(owners)[1])                  # the most specific project folder
                continue
            root = auto_root(p)
            if root:
                name = os.path.basename(root)
                with conn, conn.cursor() as cur:
                    cur.execute("INSERT INTO code_projects (name, folder) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                (name, root))
                projects.append((name, root))
                hit.add(name)
        try:
            due = time.time() - os.path.getmtime(SNAP_ALL_STAMP) > SNAP_ALL_EVERY
        except OSError:
            due = True
        if due or everything:
            hit.update(n for n, _ in projects)
            with open(SNAP_ALL_STAMP, "w") as f:
                f.write(datetime.datetime.now().isoformat())
        for name in sorted(hit):
            try:
                snap(conn, name)
            except Exception as e:                       # one bad project never stops the others
                print(f"{name}: {e}", file=sys.stderr)
    finally:
        conn.close()


# ---------- getting code back out ----------

def pick_version(cur, project, version=None, newest=False):
    if version:
        cur.execute("SELECT id FROM code_versions WHERE id = %s AND project = %s", (version, project))
        if not cur.fetchone():
            raise SystemExit(f"{project} has no version {version}")
        return int(version), ""
    if not newest:
        vid = newest_version(cur, project, working_only=True)
        if vid:
            return vid, ""
    vid = newest_version(cur, project)
    if not vid:
        raise SystemExit(f"{project} has no saved versions yet - run: python poplocker_code.py snap \"{project}\"")
    return vid, "" if newest else " (none is marked working - this is the newest)"


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*]', "_", s).strip() or "project"


def pull(conn, name, dest=None, version=None, newest=False, force=False):
    with conn.cursor() as cur:
        project, folder, run_cmd, notes, _skip = project_row(cur, name)
        vid, why = pick_version(cur, project, version, newest)
        cur.execute("SELECT taken_at, working, git_commit, note FROM code_versions WHERE id = %s", (vid,))
        taken, working, commit, vnote = cur.fetchone()
    dest = os.path.abspath(dest or os.path.join(os.path.expanduser("~"), "CodeFromDB", f"{safe_name(project)} v{vid}"))
    if os.path.isdir(dest) and os.listdir(dest) and not force:
        raise SystemExit(f"{dest} already has files in it - give another folder, or add --force to write over it")
    os.makedirs(dest, exist_ok=True)
    left, blanked, n = [], [], 0
    with conn.cursor(name="pull_files") as cur:       # streamed, so a big version never sits in memory at once
        cur.execute("""SELECT f.path, f.size, f.mtime, f.note, b.text, b.data FROM code_files f
                       LEFT JOIN code_blobs b ON b.sha1 = f.sha1 WHERE f.version_id = %s ORDER BY f.path""", (vid,))
        for path, size, mtime, note, text, data in cur:
            if text is None and data is None:
                left.append(f"{path}  ({size / 1e6:.1f} MB) - {note}")
                continue
            target = os.path.abspath(os.path.join(dest, path))
            if not target.startswith(dest + os.sep):
                continue                                 # never write outside the folder
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(text.encode("utf-8") if text is not None else bytes(data))
            os.utime(target, (mtime, mtime))
            n += 1
            if note:
                blanked.append(path)
    with open(os.path.join(dest, "_FROM_DATABASE.txt"), "w", encoding="utf-8") as f:
        f.write(f"{project} - version {vid}, saved {taken:%Y-%m-%d %H:%M}"
                f"{' - WORKING' if working else ''}{f' - git {commit}' if commit else ''}{f' - {vnote}' if vnote else ''}\n"
                f"Original folder: {folder}\n\n"
                f"Start it: {run_cmd or '(no start command saved)'}\n")
        if notes:
            f.write(f"\nNotes:\n{notes}\n")
        if blanked:
            f.write("\nPasswords/keys were blanked to <removed> in these files - put your own back:\n"
                    + "".join(f"  {p}\n" for p in blanked))
        if left:
            f.write("\nNot in the database (get these again yourself if the project needs them):\n"
                    + "".join(f"  {p}\n" for p in left))
    print(f"{project} version {vid}{why}: {n} files written to {dest}")
    if blanked:
        print(f"  {len(blanked)} files had passwords/keys blanked - see _FROM_DATABASE.txt")
    if left:
        print(f"  {len(left)} files were not saved (big models, videos, secrets...) - listed in _FROM_DATABASE.txt")
    return dest, run_cmd


def run(conn, name, version=None, newest=False):
    with conn.cursor() as cur:
        project, *_ = project_row(cur, name)
        vid, _why = pick_version(cur, project, version, newest)
    dest = os.path.join(os.path.expanduser("~"), "CodeFromDB", f"{safe_name(project)} v{vid}")
    if os.path.isfile(os.path.join(dest, "_FROM_DATABASE.txt")):
        with conn.cursor() as cur:
            cur.execute("SELECT run_cmd FROM code_projects WHERE name = %s", (project,))
            run_cmd = cur.fetchone()[0]
        print(f"{project} version {vid} is already pulled to {dest}")
    else:
        dest, run_cmd = pull(conn, project, dest, vid)
    if not run_cmd:
        print("No start command is saved for this project - opening the folder. Set one with:\n"
              f'  python poplocker_code.py add "{project}" "<folder>" --run "<command>"')
        os.startfile(dest)
        return
    print(f"Starting: {run_cmd}  (in {dest})")
    subprocess.Popen(run_cmd, cwd=dest, shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE)


# ---------- the rest ----------

def list_projects(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT p.name, p.folder, count(v.id), max(v.taken_at),
                              (SELECT id FROM code_versions w WHERE w.project = p.name AND w.working
                               ORDER BY taken_at DESC, id DESC LIMIT 1),
                              (SELECT file_count FROM code_versions w WHERE w.project = p.name
                               ORDER BY taken_at DESC, id DESC LIMIT 1)
                       FROM code_projects p LEFT JOIN code_versions v ON v.project = p.name
                       GROUP BY p.name, p.folder ORDER BY p.name""")
        for name, folder, n, newest, working, files in cur.fetchall():
            print(f"{name}\n    {folder}\n    {n} versions, newest {newest:%Y-%m-%d %H:%M} ({files} files)"
                  f", newest working: {working or 'none marked'}" if n else f"{name}\n    {folder}\n    no versions yet")


def list_files(conn, name, version=None):
    with conn.cursor() as cur:
        project, *_ = project_row(cur, name)
        vid, why = pick_version(cur, project, version, newest=not version)
        cur.execute("SELECT path, size, sha1 IS NOT NULL, note FROM code_files WHERE version_id = %s ORDER BY path",
                    (vid,))
        print(f"{project} version {vid}{why}")
        for path, size, kept, note in cur.fetchall():
            print(f"  {'   ' if kept else 'OUT'} {size:>11,}  {path}{f'   [{note}]' if note else ''}")


def arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        value = sys.argv[i + 1] if i + 1 < len(sys.argv) else default
        del sys.argv[i:i + 2]
        return value
    return default


def main():
    force, newest = "--force" in sys.argv, "--newest" in sys.argv
    sys.argv = [a for a in sys.argv if a not in ("--force", "--newest")]
    run_cmd, skip, notes = arg("--run"), arg("--skip"), arg("--notes")
    version, note = arg("--version"), arg("--note")
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else "help"), sys.argv[2:]
    conn = connect()
    with conn, conn.cursor() as cur:
        setup(cur)
    if cmd == "list":
        list_projects(conn)
    elif cmd == "add" and len(rest) == 2:
        with conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO code_projects (name, folder, run_cmd, notes, skip)
                           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET folder = EXCLUDED.folder,
                             run_cmd = coalesce(EXCLUDED.run_cmd, code_projects.run_cmd),
                             notes = coalesce(EXCLUDED.notes, code_projects.notes),
                             skip = CASE WHEN %s THEN EXCLUDED.skip ELSE code_projects.skip END""",
                        (rest[0], os.path.abspath(rest[1]), run_cmd, notes,
                         [s.strip() for s in skip.split(",") if s.strip()] if skip else [], skip is not None))
        print(f"{rest[0]} -> {os.path.abspath(rest[1])}")
    elif cmd == "rename" and len(rest) == 2:
        with conn, conn.cursor() as cur:
            name = project_row(cur, rest[0])[0]
            cur.execute("UPDATE code_projects SET name = %s WHERE name = %s", (rest[1], name))
        print(f"{name} is now {rest[1]}")
    elif cmd == "snap" and rest:
        names = rest
        if rest == ["--all"]:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM code_projects ORDER BY name")
                names = [r[0] for r in cur.fetchall()]
        for n in names:
            print(snap(conn, n))
    elif cmd == "working" and rest:
        with conn, conn.cursor() as cur:
            project = project_row(cur, rest[0])[0]
            vid = int(rest[1]) if len(rest) > 1 else newest_version(cur, project)
            cur.execute("UPDATE code_versions SET working = true, note = coalesce(%s, note) WHERE id = %s "
                        "AND project = %s", (note or "marked working by hand", vid, project))
            print(f"{project} version {vid} marked working" if cur.rowcount else f"{project} has no version {vid}")
    elif cmd == "pull" and rest:
        pull(conn, rest[0], rest[1] if len(rest) > 1 else None, version, newest, force)
    elif cmd == "run" and rest:
        run(conn, rest[0], version, newest)
    elif cmd == "git-tags" and rest:
        print(git_tags(conn, rest[0]))
    elif cmd == "files" and rest:
        list_files(conn, rest[0], version)
    else:
        print(__doc__)
    conn.close()


if __name__ == "__main__":
    main()
