import asyncio
import base64
import json
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

BASE = Path.home() / "JKProject" / "wssh"
DATA_FILE = BASE / "data.json"
STATIC_DIR = BASE / "static"
JKPROJECT = Path.home() / "JKProject"
CLAUDE_BIN = "/opt/homebrew/bin/claude"
IGNORED = {".git", "__pycache__", "node_modules", ".DS_Store"}
IGNORED_FILES = IGNORED | {".env", "venv", ".venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


def scan_projects():
    result = []
    try:
        for p in sorted(JKPROJECT.iterdir()):
            if p.is_dir() and p.name not in IGNORED and not p.name.startswith("."):
                result.append({"name": p.name, "path": str(p)})
    except Exception:
        pass
    return result


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"sessions": {}}


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


VALID_MODELS = {"haiku", "sonnet", "opus", "fable"}
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class Message(BaseModel):
    text: str
    image_data: Optional[str] = None
    image_mime: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None


class ProjectCreate(BaseModel):
    name: str


async def run_claude(cmd, cwd: str, stdin_data: Optional[bytes] = None):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin_data), timeout=3600)
    return proc.returncode, stdout.decode(), stderr.decode()


def build_cmd(text: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
    cmd = [CLAUDE_BIN]
    if session_id:
        cmd += ["--resume", session_id]
    if model in VALID_MODELS:
        cmd += ["--model", model]
    if effort in VALID_EFFORTS:
        cmd += ["--effort", effort]
    cmd += ["-p", text, "--output-format", "json", "--dangerously-skip-permissions"]
    return cmd


def build_stream_cmd(session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
    cmd = [CLAUDE_BIN]
    if session_id:
        cmd += ["--resume", session_id]
    if model in VALID_MODELS:
        cmd += ["--model", model]
    if effort in VALID_EFFORTS:
        cmd += ["--effort", effort]
    cmd += ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    return cmd


def parse_stream_output(stdout: str) -> tuple:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "result":
                return event.get("result", ""), event.get("session_id")
        except json.JSONDecodeError:
            pass
    return "", None


def session_summary(s: dict) -> dict:
    return {k: v for k, v in s.items() if k != "messages"}


@app.get("/api/usage")
async def get_usage():
    import re as _re

    # /usage 슬래시 커맨드 실행 (토큰 0 소비)
    try:
        rc, stdout, stderr = await run_claude(
            [CLAUDE_BIN, "-p", "--output-format", "json", "--dangerously-skip-permissions"],
            cwd=str(BASE),
            stdin_data=b"/usage\n",
        )
        raw = json.loads(stdout).get("result", "")
    except Exception:
        raw = ""

    def parse_pct(text: str, pattern: str) -> Optional[int]:
        m = _re.search(pattern, text)
        return int(m.group(1)) if m else None

    def parse_reset(text: str, pattern: str) -> Optional[str]:
        m = _re.search(pattern, text)
        return m.group(1).strip() if m else None

    session_pct   = parse_pct(raw,   r"Current session:\s*(\d+)%")
    session_reset = parse_reset(raw, r"Current session:.*?resets\s+(.+?)(?:\n|$)")
    week_pct      = parse_pct(raw,   r"Current week \(all models\):\s*(\d+)%")
    week_reset    = parse_reset(raw, r"Current week \(all models\):.*?resets\s+(.+?)(?:\n|$)")
    fable_pct     = parse_pct(raw,   r"Current week \(Fable\):\s*(\d+)%")
    req_24h       = parse_pct(raw,   r"Last 24h\s*·\s*(\d+) requests")
    req_7d        = parse_pct(raw,   r"Last 7d\s*·\s*(\d+) requests")

    return {
        "session":      {"pct": session_pct, "resets": session_reset},
        "week":         {"pct": week_pct,    "resets": week_reset},
        "week_fable":   {"pct": fable_pct},
        "requests_24h": req_24h,
        "requests_7d":  req_7d,
        "raw":          raw,
    }


@app.get("/api/processes")
def list_processes():
    import subprocess, re

    SKIP_CMDS = {
        "postgres", "redis-ser", "mysqld", "rapportd", "identitys",
        "sharingd", "ControlCe", "CrossEXSe", "replicato",
    }

    try:
        ts = subprocess.run(["/usr/local/bin/tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        tailscale_ip = ts.stdout.strip() if ts.returncode == 0 else None
    except Exception:
        tailscale_ip = None

    try:
        lo = subprocess.run(
            ["/usr/sbin/lsof", "-i", "-P", "-n", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        lines = lo.stdout.splitlines()[1:]
    except Exception:
        lines = []

    def get_cwd(pid: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["/usr/sbin/lsof", "-p", pid, "-a", "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=3,
            )
            for l in r.stdout.splitlines():
                if l.startswith("n"):
                    return l[1:].strip()
        except Exception:
            pass
        return None

    seen: set = set()
    servers = []
    for line in lines:
        if "(LISTEN)" not in line:
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
        cmd = parts[0]
        pid = parts[1]
        if cmd in SKIP_CMDS:
            continue
        # addr is second-to-last: NAME col is "addr (LISTEN)"
        addr = parts[-2]
        # localhost-only 포트 제외
        if addr.startswith("127.") or addr.startswith("[::1]") or addr.startswith("localhost"):
            continue
        m = re.search(r":(\d+)$", addr)
        if not m:
            continue
        port = int(m.group(1))
        if port < 1024 or port in seen:
            continue
        seen.add(port)
        cwd = get_cwd(pid)
        project = cwd.split("/")[-1] if cwd else cmd
        servers.append({
            "port": port,
            "name": cmd,
            "project": project,
            "cwd": cwd,
            "url": f"http://{tailscale_ip}:{port}" if tailscale_ip else f"http://localhost:{port}",
        })

    servers.sort(key=lambda x: x["port"])
    return {"tailscale_ip": tailscale_ip, "servers": servers}


@app.get("/api/projects")
def list_projects():
    return scan_projects()


@app.post("/api/projects")
def create_project(req: ProjectCreate):
    name = req.name.strip()
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(400, "잘못된 프로젝트 이름")
    path = JKPROJECT / name
    if path.exists():
        raise HTTPException(409, f"이미 존재함: {name}")
    path.mkdir(parents=False)
    return {"name": name, "path": str(path)}


@app.get("/api/projects/{name}/sessions")
def list_sessions(name: str):
    data = load_data()
    sessions = [
        session_summary(s)
        for s in data["sessions"].values()
        if s["project"] == name
    ]
    sessions.sort(key=lambda s: s.get("last_used", s["created_at"]), reverse=True)
    return sessions


@app.post("/api/projects/{name}/sessions")
def create_session(name: str):
    if not (JKPROJECT / name).is_dir():
        raise HTTPException(404, f"프로젝트 없음: {name}")
    data = load_data()
    count = sum(1 for s in data["sessions"].values() if s["project"] == name)
    sid = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()
    session = {
        "id": sid,
        "project": name,
        "name": f"세션 {count + 1}",
        "claude_session_id": None,
        "created_at": now,
        "last_used": now,
        "messages": [],
    }
    data["sessions"][sid] = session
    save_data(data)
    return session_summary(session)


@app.get("/api/sessions/{sid}/messages")
def get_messages(sid: str):
    data = load_data()
    if sid not in data["sessions"]:
        raise HTTPException(404, "Not found")
    return data["sessions"][sid]["messages"]


@app.post("/api/sessions/{sid}/chat")
async def chat(sid: str, req: Message):
    if not req.text.strip() and not req.image_data:
        raise HTTPException(400, "메시지를 입력해주세요")
    data = load_data()
    if sid not in data["sessions"]:
        raise HTTPException(404, "Not found")

    session = data["sessions"][sid]
    claude_sid = session.get("claude_session_id")
    working_dir = str(JKPROJECT / session["project"])

    user_text = req.text or ""
    now = datetime.utcnow().isoformat()
    msg_entry: dict = {"role": "user", "content": user_text}
    if req.image_data:
        msg_entry["image_mime"] = req.image_mime or "image/png"
        if len(req.image_data) < 3_000_000:
            msg_entry["image_data"] = req.image_data
        else:
            msg_entry["image_large"] = True
    session["messages"].append(msg_entry)
    session["last_used"] = now
    display_text = user_text or "이미지 첨부"
    if len(session["messages"]) == 1:
        session["name"] = display_text[:22] + ("…" if len(display_text) > 22 else "")
    save_data(data)

    sel_model = req.model if req.model in VALID_MODELS else None
    sel_effort = req.effort if req.effort in VALID_EFFORTS else None
    try:
        if req.image_data and req.image_mime:
            content = [{"type": "image", "source": {"type": "base64", "media_type": req.image_mime, "data": req.image_data}}]
            if req.text.strip():
                content.append({"type": "text", "text": req.text})
            stdin_msg = json.dumps({"type": "user", "message": {"role": "user", "content": content}}).encode()
            rc, stdout, stderr = await run_claude(build_stream_cmd(claude_sid, sel_model, sel_effort), working_dir, stdin_msg)
            if rc != 0 and claude_sid:
                rc, stdout, stderr = await run_claude(build_stream_cmd(None, sel_model, sel_effort), working_dir, stdin_msg)
            if rc != 0:
                raise HTTPException(500, f"Claude 오류: {stderr[:500]}")
            reply, new_claude_sid = parse_stream_output(stdout)
            if not new_claude_sid:
                new_claude_sid = claude_sid
        else:
            rc, stdout, stderr = await run_claude(build_cmd(req.text, claude_sid, sel_model, sel_effort), working_dir)
            if rc != 0 and claude_sid:
                rc, stdout, stderr = await run_claude(build_cmd(req.text, None, sel_model, sel_effort), working_dir)
            if rc != 0:
                raise HTTPException(500, f"Claude 오류: {stderr[:500]}")
            try:
                output = json.loads(stdout)
            except json.JSONDecodeError:
                raise HTTPException(500, f"응답 파싱 실패: {stdout[:300]}")
            reply = output.get("result", "")
            new_claude_sid = output.get("session_id", claude_sid)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(504, "실행 오류: Claude 응답 시간 초과 (1시간)")
    except Exception as e:
        raise HTTPException(500, f"실행 오류: [{type(e).__name__}] {e}")
    now = datetime.utcnow().isoformat()

    data = load_data()
    s = data["sessions"][sid]
    s["claude_session_id"] = new_claude_sid
    s["last_used"] = now
    s["messages"].append({"role": "assistant", "content": reply})
    save_data(data)
    return {"reply": reply, "session_name": s["name"]}


@app.post("/api/sessions/{sid}/reset")
def reset_session(sid: str):
    data = load_data()
    if sid not in data["sessions"]:
        raise HTTPException(404, "Not found")
    data["sessions"][sid]["claude_session_id"] = None
    data["sessions"][sid]["messages"] = []
    save_data(data)
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    data = load_data()
    if sid not in data["sessions"]:
        raise HTTPException(404, "Not found")
    del data["sessions"][sid]
    save_data(data)
    return {"ok": True}


@app.get("/api/projects/{name}/tree")
def get_tree(name: str, path: str = ""):
    base = (JKPROJECT / name).resolve()
    target = (base / path).resolve() if path else base
    if not str(target).startswith(str(base)):
        raise HTTPException(403, "Access denied")
    if not target.is_dir():
        raise HTTPException(400, "Not a directory")
    items = []
    try:
        for p in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if p.name in IGNORED_FILES or p.name.startswith("."):
                continue
            rel = str(p.relative_to(base))
            items.append({"name": p.name, "path": rel, "type": "dir" if p.is_dir() else "file"})
    except PermissionError:
        pass
    return items


@app.get("/api/projects/{name}/file")
def get_file_content(name: str, path: str):
    base = (JKPROJECT / name).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(403, "Access denied")
    if not target.is_file():
        raise HTTPException(404, "Not found")
    mime, _ = mimetypes.guess_type(str(target))
    if mime and mime.startswith("image/"):
        data = base64.b64encode(target.read_bytes()).decode()
        return {"type": "image", "mime": mime, "data": data}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        if len(content) > 300_000:
            content = content[:300_000] + "\n\n... (파일이 너무 큽니다)"
        return {"type": "text", "content": content}
    except Exception:
        return {"type": "binary"}


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
