import asyncio
import json
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
IGNORED = {"wssh", ".git", "__pycache__", "node_modules", ".DS_Store"}


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


class Message(BaseModel):
    text: str


async def run_claude(cmd, cwd: str):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    return proc.returncode, stdout.decode(), stderr.decode()


def build_cmd(text: str, session_id: Optional[str]):
    cmd = [CLAUDE_BIN]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += ["-p", text, "--output-format", "json", "--dangerously-skip-permissions"]
    return cmd


def session_summary(s: dict) -> dict:
    return {k: v for k, v in s.items() if k != "messages"}


@app.get("/api/projects")
def list_projects():
    return scan_projects()


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
    if not req.text.strip():
        raise HTTPException(400, "메시지를 입력해주세요")
    data = load_data()
    if sid not in data["sessions"]:
        raise HTTPException(404, "Not found")

    session = data["sessions"][sid]
    claude_sid = session.get("claude_session_id")
    working_dir = str(JKPROJECT / session["project"])

    try:
        rc, stdout, stderr = await run_claude(build_cmd(req.text, claude_sid), working_dir)
        if rc != 0 and claude_sid:
            rc, stdout, stderr = await run_claude(build_cmd(req.text, None), working_dir)
    except Exception as e:
        raise HTTPException(500, f"실행 오류: {e}")

    if rc != 0:
        raise HTTPException(500, f"Claude 오류: {stderr[:500]}")

    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        raise HTTPException(500, f"응답 파싱 실패: {stdout[:300]}")

    reply = output.get("result", "")
    new_claude_sid = output.get("session_id", claude_sid)
    now = datetime.utcnow().isoformat()

    data = load_data()
    s = data["sessions"][sid]
    s["claude_session_id"] = new_claude_sid
    s["last_used"] = now
    s["messages"].append({"role": "user", "content": req.text})
    s["messages"].append({"role": "assistant", "content": reply})
    if len(s["messages"]) == 2:
        s["name"] = req.text[:22] + ("…" if len(req.text) > 22 else "")
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


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
