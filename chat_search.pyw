"""Search Claude's saved chats by keyword, date, project, or any mix of them. Reads the claude_chats database that claude_chat_save.py fills.

Chats / Artifacts picks what to search: the messages, or the files Claude wrote (and any imported artifacts).
Leave any box empty to skip it. Several keywords must all be in the message. Dates can be 10/30/2026 or 2026-10-30;
a From date on its own finds that day onward, a To date on its own finds everything up to and including that day.
Click a result to read the whole message.
"""
import datetime, os, re, sys, tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from claude_chat_save import connect

ALL = "All projects"
WHO = {"user": "You", "claude": "Claude", "artifact": "Artifact", "file": "File", "drive file": "USB file"}
LIMIT = 500


def parse_date(s):
    s = s.strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"I can't read the date '{s}'. Use 10/30/2026.")


def projects():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT project FROM projects ORDER BY project")
        return [r[0] for r in cur.fetchall()]


def search():
    try:
        start, end = parse_date(from_box.get()), parse_date(to_box.get())
    except ValueError as e:
        status.config(text=str(e))
        return
    arts = what_var.get() == "Artifacts"
    text, at = ("title || ' ' || content", "updated_at") if arts else ("content", "at")
    where, args, in_title = [], [], []
    for word in key_box.get().split():
        # the keyword has to start a word: "trend" finds trend, trends, trending - not the middle of other words
        where.append(f"{text} ~* %s")
        args.append(r"\m" + re.escape(word))
        in_title.append(r"\m" + re.escape(word))
    if start:
        where.append(f"{at} >= %s")
        args.append(start)
    if end:
        where.append(f"{at} < %s")
        args.append(end + datetime.timedelta(days=1))
    if project_var.get() != ALL:
        where.append("project = %s")
        args.append(project_var.get())
    if arts:     # same four columns as messages: the "who" is artifact/file and the text starts with the title
        sql = "SELECT project, updated_at, kind, title || E'\\n\\n' || content FROM artifacts"
    else:
        sql = "SELECT project, at, role, content FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = f"project, {at}"
    if arts and in_title:    # artifacts (or projects) named after the keyword come first, then ones that only
        order = "(" + " AND ".join(["(title || ' ' || project) ~* %s"] * len(in_title)) + ") DESC, " + order
        args += in_title
    sql += f" ORDER BY {order} LIMIT {LIMIT + 1}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    found.clear()
    table.delete(*table.get_children())
    for i, (project, at, role, content) in enumerate(rows[:LIMIT]):
        found.append((project, at, role, content))
        table.insert("", tk.END, iid=str(i), values=(project, at.astimezone().strftime("%m/%d/%Y %I:%M %p"),
                                                     WHO.get(role, role),
                                                     " ".join(content.split())[:150]))
    more = f" - showing the first {LIMIT}, narrow it down to see the rest" if len(rows) > LIMIT else ""
    status.config(text=f"{min(len(rows), LIMIT)} found{more}")
    show("")


def show(text):
    reader.config(state=tk.NORMAL)
    reader.delete("1.0", tk.END)
    reader.insert("1.0", text)
    reader.config(state=tk.DISABLED)


def picked(_event):
    sel = table.selection()
    if sel:
        project, at, role, content = found[int(sel[0])]
        who = WHO.get(role, role)
        show(f"{project}  -  {at.astimezone().strftime('%m/%d/%Y %I:%M %p')}  -  {who}\n\n{content}")


def clear():
    for box in (key_box, from_box, to_box):
        box.delete(0, tk.END)
    project_var.set(ALL)
    key_box.focus_set()


root = tk.Tk()
root.title("Pop Locker - History")
try:
    root.iconbitmap(os.path.join(HERE, "icon", "poplocker.ico"))
except tk.TclError:
    pass
root.geometry("1000x650")
found = []

bar = tk.Frame(root, padx=8, pady=8)
bar.pack(fill=tk.X)
what_var = tk.StringVar(value="Chats")      # search the chat messages or the artifacts/files Claude made
ttk.Combobox(bar, textvariable=what_var, values=["Chats", "Artifacts"], state="readonly", width=9).pack(
    side=tk.LEFT, padx=(0, 12))
tk.Label(bar, text="Keywords").pack(side=tk.LEFT)
key_box = tk.Entry(bar, width=28)
key_box.pack(side=tk.LEFT, padx=(4, 12))
tk.Label(bar, text="From").pack(side=tk.LEFT)
from_box = tk.Entry(bar, width=11)
from_box.pack(side=tk.LEFT, padx=(4, 12))
tk.Label(bar, text="To").pack(side=tk.LEFT)
to_box = tk.Entry(bar, width=11)
to_box.pack(side=tk.LEFT, padx=(4, 12))
project_var = tk.StringVar(value=ALL)
ttk.Combobox(bar, textvariable=project_var, values=[ALL] + projects(), state="readonly", width=30).pack(
    side=tk.LEFT, padx=(0, 12))
tk.Button(bar, text="Search", command=search, bg="#1565C0", fg="white", width=9).pack(side=tk.LEFT)
tk.Button(bar, text="Clear", command=clear, width=7).pack(side=tk.LEFT, padx=4)

status = tk.Label(root, text="Type a keyword or a date (10/30/2026), then Search", anchor="w", padx=8)
status.pack(fill=tk.X)

panes = tk.PanedWindow(root, orient=tk.VERTICAL, sashwidth=6)
panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
top = tk.Frame(panes)
table = ttk.Treeview(top, columns=("project", "when", "who", "text"), show="headings", selectmode="browse")
for col, title, width in (("project", "Project", 180), ("when", "When", 140), ("who", "Who", 60),
                          ("text", "Message", 600)):
    table.heading(col, text=title)
    table.column(col, width=width, stretch=(col == "text"))
scroll = ttk.Scrollbar(top, command=table.yview)
table.config(yscrollcommand=scroll.set)
scroll.pack(side=tk.RIGHT, fill=tk.Y)
table.pack(fill=tk.BOTH, expand=True)
table.bind("<<TreeviewSelect>>", picked)
reader = tk.Text(panes, wrap=tk.WORD, font=("Segoe UI", 10), state=tk.DISABLED, padx=6, pady=6)
panes.add(top, height=360)
panes.add(reader)

for box in (key_box, from_box, to_box):
    box.bind("<Return>", lambda _e: search())
key_box.focus_set()
root.mainloop()
