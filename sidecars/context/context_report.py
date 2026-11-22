#!/usr/bin/env python3
"""Context window usage report for Antigravity 2.0 conversations.

A port of the agy CLI `/context` command (cli/model.ContextModel in agy 1.4.4).
The CLI reads the live trajectory from memory; this script reads the same
protobufs from the conversation database (`steps` and `gen_metadata` tables).
"""

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import unquote, urlparse

# --- protobuf ----------------------------------------------------------------


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise ValueError("Truncated varint")


class Msg:
    """Lazily decoded protobuf message: field number -> list of raw values."""

    def __init__(self, buf: bytes | None):
        self.fields: dict[int, list] = {}
        pos, n = 0, len(buf or b"")
        while pos < n:
            try:
                tag, pos = read_varint(buf, pos)
                fn, wt = tag >> 3, tag & 7
                if wt == 0:
                    val, pos = read_varint(buf, pos)
                elif wt == 2:
                    ln, pos = read_varint(buf, pos)
                    val = buf[pos : pos + ln]
                    pos += ln
                elif wt == 1:
                    val, pos = buf[pos : pos + 8], pos + 8
                elif wt == 5:
                    val, pos = buf[pos : pos + 4], pos + 4
                else:
                    break
            except ValueError:
                break
            self.fields.setdefault(fn, []).append(val)

    def has(self, fn: int) -> bool:
        return fn in self.fields

    def int(self, fn: int, signed32: bool = False) -> int:
        v = self.fields.get(fn, [0])[-1]
        v = v if isinstance(v, int) else 0
        if signed32 and v >= 1 << 63:
            v -= 1 << 64
        return v

    def bytes(self, fn: int) -> bytes:
        v = self.fields.get(fn, [b""])[-1]
        return v if isinstance(v, bytes) else b""

    def str(self, fn: int) -> str:
        return self.bytes(fn).decode("utf-8", errors="replace")

    def msg(self, fn: int) -> "Msg | None":
        return Msg(self.bytes(fn)) if self.has(fn) else None

    def msgs(self, fn: int) -> list["Msg"]:
        return [Msg(v) for v in self.fields.get(fn, []) if isinstance(v, bytes)]


# CortexStep oneof payload field numbers.
STEP_VIEW_FILE = 14
STEP_USER_INPUT = 19
STEP_PLANNER_RESPONSE = 20
STEP_CHECKPOINT = 30
STEP_CODE_ACTION = 10

STATUS_DONE = 3

# TokenType values of TopLevelTokenGroup.type.
TOKEN_SYSTEM_PROMPT, TOKEN_TOOLS, TOKEN_RULES, TOKEN_MCP_TOOLS, TOKEN_SKILLS = 1, 2, 3, 4, 5
TOKEN_SUBAGENTS, TOKEN_CHAT_MESSAGES, TOKEN_WORKFLOWS, TOKEN_PLUGINS, TOKEN_FLOWS = 6, 7, 8, 9, 10

# CortexStepType names (exa.cortex_pb), used to parse "N: TYPE" chat item names.
STEP_TYPES = {
    "UNSPECIFIED": 0, "DUMMY": 1, "FINISH": 2, "PLAN_INPUT": 3, "MQUERY": 4, "CODE_ACTION": 5,
    "GIT_COMMIT": 6, "GREP_SEARCH": 7, "VIEW_FILE": 8, "LIST_DIRECTORY": 9, "COMPILE": 10,
    "VIEW_CODE_ITEM": 13, "USER_INPUT": 14, "PLANNER_RESPONSE": 15, "ERROR_MESSAGE": 17,
    "RUN_COMMAND": 21, "CHECKPOINT": 23, "PROPOSE_CODE": 24, "FIND": 25, "SUGGESTED_RESPONSES": 27,
    "COMMAND_STATUS": 28, "MEMORY": 29, "READ_URL_CONTENT": 31, "VIEW_CONTENT_CHUNK": 32,
    "SEARCH_WEB": 33, "RETRIEVE_MEMORY": 34, "MCP_TOOL": 38, "MANAGER_FEEDBACK": 39,
    "TOOL_CALL_PROPOSAL": 40, "TOOL_CALL_CHOICE": 41, "TRAJECTORY_CHOICE": 42, "CLIPBOARD": 45,
    "VIEW_FILE_OUTLINE": 47, "POST_PR_REVIEW": 49, "LIST_RESOURCES": 51, "READ_RESOURCE": 52,
    "LINT_DIFF": 53, "FIND_ALL_REFERENCES": 54, "BRAIN_UPDATE": 55, "OPEN_BROWSER_URL": 56,
    "RUN_EXTENSION_CODE": 57, "PROPOSAL_FEEDBACK": 59, "TRAJECTORY_SEARCH": 60,
    "EXECUTE_BROWSER_JAVASCRIPT": 61, "LIST_BROWSER_PAGES": 62, "CAPTURE_BROWSER_SCREENSHOT": 63,
    "CLICK_BROWSER_PIXEL": 64, "READ_TERMINAL": 65, "CAPTURE_BROWSER_CONSOLE_LOGS": 66,
    "READ_BROWSER_PAGE": 67, "BROWSER_GET_DOM": 68, "CODE_SEARCH": 73, "BROWSER_INPUT": 74,
    "BROWSER_MOVE_MOUSE": 75, "BROWSER_SELECT_OPTION": 76, "BROWSER_SCROLL_UP": 77,
    "BROWSER_SCROLL_DOWN": 78, "BROWSER_CLICK_ELEMENT": 79, "BROWSER_PRESS_KEY": 80,
    "TASK_BOUNDARY": 81, "NOTIFY_USER": 82, "CODE_ACKNOWLEDGEMENT": 83, "INTERNAL_SEARCH": 84,
    "BROWSER_SUBAGENT": 85, "FILE_CHANGE": 86, "MOVE": 87, "BROWSER_SCROLL": 88,
    "KNOWLEDGE_GENERATION": 89, "EPHEMERAL_MESSAGE": 90, "GENERATE_IMAGE": 91,
    "DELETE_DIRECTORY": 92, "COMPILE_APPLET": 93, "INSTALL_APPLET_DEPENDENCIES": 94,
    "INSTALL_APPLET_PACKAGE": 95, "BROWSER_RESIZE_WINDOW": 96, "BROWSER_DRAG_PIXEL_TO_PIXEL": 97,
    "CONVERSATION_HISTORY": 98, "KNOWLEDGE_ARTIFACTS": 99, "SEND_COMMAND_INPUT": 100,
    "SYSTEM_MESSAGE": 101, "WAIT": 102, "AGENCY_TOOL_CALL": 103, "CIDER_AGENT_DUMMY": 104,
    "BUILD_CLEANER": 105, "BLAZE_BUILD_TARGETS": 106, "BLAZE_TEST_TARGETS": 107,
    "SET_UP_FIREBASE": 108, "MOMA": 109, "RESTART_DEV_SERVER": 110, "DEPLOY_FIREBASE": 111,
    "SHELL_EXEC": 112, "BROWSER_MOUSE_WHEEL": 113, "LINT_APPLET": 114, "KI_INSERTION": 116,
    "RETRIEVE_CONTENT": 117, "CRITIQUE": 118, "FINDINGS": 119, "BROWSER_MOUSE_UP": 120,
    "BROWSER_MOUSE_DOWN": 121, "WORKSPACE_API": 122, "BROWSER_LIST_NETWORK_REQUESTS": 123,
    "BROWSER_GET_NETWORK_REQUEST": 124, "BROWSER_REFRESH_PAGE": 125, "EDIT_NOTEBOOK": 126,
    "INVOKE_SUBAGENT": 127, "WRITE_BLOB": 128, "READ_NOTEBOOK": 129, "PROPOSE_AI_COMMENTS": 130,
    "START_CODE_REVIEW": 131, "GENERIC": 132, "SET_UP_CLOUD_SQL": 133, "EXECUTE_NOTEBOOK": 134,
    "CLOUD_SQL_UPDATE_SCHEMA": 135, "RPC_ACTION": 136, "CLOUD_SQL_EXECUTE_SQL": 137,
    "ASK_QUESTION": 138, "DIRECTORY_RULES": 139, "TOOL_SEARCH": 140,
}

CAT_USER, CAT_AGENT, CAT_TOOL, CAT_CHECKPOINT, CAT_SYSTEM = 0, 1, 2, 3, 4

# --- formatting --------------------------------------------------------------


def fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        v = n / 1_000_000.0
        return f"{v:.0f}M" if v.is_integer() else f"{v:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000.0:.1f}k"
    return str(n)


def fmt_ctx_limit(n: int) -> str:
    return fmt_tok(n)


def go_div4(n: int) -> int:
    """Go's int division by 4 (truncates toward zero)."""
    return int(n / 4)


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a local filesystem path."""
    p = uri
    if p.startswith("file:"):
        parsed = urlparse(p)
        p = unquote(parsed.path)
        if len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
    return p


def shorten_path(uri: str) -> str:
    p = uri_to_path(uri)
    home = str(Path.home())
    p = p.replace("\\", "/")
    home = home.replace("\\", "/")
    if p.startswith(home):
        return "~" + p[len(home) :]
    return p


# --- stats (model.ContextModel.collectStats) ---------------------------------


def step_category(step_type: int) -> int:
    if step_type == 14:
        return CAT_USER
    if step_type == 15:
        return CAT_AGENT
    if step_type == 23:
        return CAT_CHECKPOINT
    if step_type in (0, 2, 101):
        return CAT_SYSTEM
    return CAT_TOOL


STEP_GENERIC = 140
GENERIC_TYPE = 132
ORIGINAL_STEP_TYPE_URL = "/gemini_coder.Step"


def packed_original_step(generic: Msg) -> bytes | None:
    """The typed step a GENERIC step wraps (rehydration.originalStepAny)."""
    result = generic.msg(2)
    if result is None:
        return None
    payload = result.msg(6)
    if payload is not None and payload.str(1).endswith(ORIGINAL_STEP_TYPE_URL):
        return payload.bytes(2)
    for entry in result.msgs(5):
        if entry.str(1) == "__original_cortex_step":
            value = entry.msg(2)
            if value is not None and value.str(1).endswith(ORIGINAL_STEP_TYPE_URL):
                return value.bytes(2)
    return None


def is_skill_file(path: str) -> bool:
    return path.replace("\\", "/").endswith("/SKILL.md")


class Step:
    def __init__(
        self,
        pos: int,
        payload: bytes,
        step_type: int,
        status: int,
        metadata: bytes | None,
        idx: int | None = None,
        task_details: bytes | None = None,
        error_details: bytes | None = None,
    ):
        self.pos = pos
        self.idx = pos if idx is None else idx
        self.pb = Msg(payload)
        self.type = self.pb.int(1) if self.pb.has(1) else step_type
        self.status = self.pb.int(4) if self.pb.has(4) else status
        self.md = self.pb.msg(5) or Msg(metadata)
        self.task_details = Msg(task_details) if task_details else None
        self.error_details = error_details
        self.view_file_override: Msg | None = None
        if self.type == GENERIC_TYPE:
            self.rehydrate()

    def rehydrate(self):
        """Turn a GENERIC tool step back into its typed step, like the CLI's rehydration.RehydrateStep."""
        generic = self.pb.msg(STEP_GENERIC)
        if generic is None:
            return
        original = packed_original_step(generic)
        if original is not None:
            self.pb = Msg(original)
            self.type = self.pb.int(1)
            self.md = self.pb.msg(5) or Msg(b"")
            return
        tc = self.md.msg(4)
        if tc is None or tc.str(2) not in ("view_file", "read_file"):
            return
        try:
            args = json.loads(tc.str(3) or "{}")
        except json.JSONDecodeError:
            return
        path = next((args[k] for k in ("AbsolutePath", "Path", "FilePath") if isinstance(args.get(k), str) and args[k]), "")
        if path:
            self.type = 8  # VIEW_FILE
            self.view_file_override = Msg(
                b"\x0a" + _varint(len(path.encode())) + path.encode() + (b"\x88\x01\x01" if is_skill_file(path) else b"")
            )

    def payload(self, fn: int) -> Msg | None:
        if fn == STEP_VIEW_FILE and self.view_file_override is not None:
            return self.view_file_override
        return self.pb.msg(fn)


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def estimate_step_tokens(step: Step) -> int:
    ui = step.payload(STEP_USER_INPUT)
    pr = step.payload(STEP_PLANNER_RESPONSE)
    if ui is not None:
        n = len(ui.bytes(2))  # user_response
    elif pr is not None:
        n = len(pr.bytes(1))  # response
    else:
        tc = step.md.msg(4)  # tool_call
        n = len(tc.bytes(3)) if tc else 0  # arguments_json
        out = step.md.int(23, signed32=True)  # tool_call_output_tokens
        if out > 0:
            return out
    if n == 0:
        return 0
    return max(1, go_div4(n))


def step_tokens(step: Step) -> int:
    usage = step.md.msg(9)  # model_usage
    if usage is not None:
        return usage.int(3)  # output_tokens
    return estimate_step_tokens(step)


def compaction_tokens(step: Step) -> int:
    for ri in reversed(step.md.msgs(28)):  # retry_infos
        usage = ri.msg(2)
        if usage is None:
            continue
        resp = usage.int(10)
        if resp:
            return resp
        out, thinking = usage.int(3), usage.int(9)
        return out - thinking if thinking < out else 0
    return 0


def checkpoint_summary_tokens(step: Step) -> int:
    t = compaction_tokens(step)
    if t:
        return t
    ck = step.payload(STEP_CHECKPOINT) or Msg(b"")
    return go_div4(len(ck.bytes(4)) + len(ck.bytes(5)) + len(ck.bytes(6)))


def checkpoint_newer_than_generation(steps: list[Step], chat_start: Msg | None) -> tuple[int, Step | None]:
    i = len(steps) - 1
    while i >= 0:
        s = steps[i]
        if s.payload(STEP_CHECKPOINT) is not None and s.status == STATUS_DONE:
            break
        i -= 1
    if i < 0:
        return -1, None
    for s in steps[i + 1 :]:
        if s.payload(STEP_PLANNER_RESPONSE) is not None and s.status == STATUS_DONE:
            return -1, None
    gen_ck = chat_start.int(2, signed32=True) if chat_start is not None else -1
    ck_idx = steps[i].payload(STEP_CHECKPOINT).int(1)
    if gen_ck >= ck_idx:
        return -1, None
    return i, steps[i]


def chat_tokens_by_category(items: list[Msg]) -> tuple[bool, list[int]]:
    out = [0, 0, 0, 0]  # user, agent, tool, system
    if not items:
        return False, out
    for it in items:
        left, sep, right = it.str(1).partition(": ")
        if not sep or not re.fullmatch(r"[+-]?[0-9]+", left) or right not in STEP_TYPES:
            return False, [0, 0, 0, 0]
        cat = step_category(STEP_TYPES[right])
        tok = it.int(3, signed32=True)
        if cat == CAT_USER:
            out[0] += tok
        elif cat in (CAT_AGENT, CAT_CHECKPOINT):
            out[1] += tok
        elif cat == CAT_TOOL:
            out[2] += tok
        else:
            out[3] += tok
    return True, out


def detect_system_files(home: str, workspace: str | None, app_dir: str) -> list[tuple[str, int]]:
    if not home:
        return []
    gemini = Path(home) / ".gemini"
    disp = "~/" + gemini.name
    found = []
    f = gemini / "GEMINI.md"
    if f.exists() and not f.is_dir():
        found.append(disp + "/GEMINI.md")
    for sub in ("skills", "rules", "agents", "global_workflows"):
        if (gemini / sub).is_dir():
            found.append(f"{disp}/{sub}/")
    if app_dir:
        for sub in ("rules", "skills", "agents", "global_workflows"):
            if (gemini / app_dir / sub).is_dir():
                found.append(f"{disp}/{app_dir}/{sub}/")
    if workspace:
        for root in (".agents", "_agents"):
            for sub in ("rules", "skills", "agents", "workflows"):
                if (Path(workspace) / root / sub).is_dir():
                    found.append(f"{root}/{sub}/")
            hooks = Path(workspace) / root / "hooks.json"
            if hooks.exists() and not hooks.is_dir():
                found.append(f"{root}/hooks.json")
    return [(p, 0) for p in found]


def collect_stats(steps: list[Step], gen: Msg | None, limit: int) -> dict:
    st = {k: 0 for k in (
        "user", "agent", "tool", "checkpoint", "system_messages", "system_prompt", "system_tools",
        "system_overhead", "rules", "skills", "subagents", "legacy_workflows", "workflows",
        "plugins", "mcp_tools",
    )}
    st["has_breakdown"] = False
    st["active_checkpoint"] = -1
    checkpoints, knowledge, artifacts, skill_files = [], [], [], []
    seen_k, seen_a, seen_s = set(), set(), set()

    chat = gen.msg(1) if gen is not None else None
    chat_start = chat.msg(9) if chat is not None else None
    newer_pos, newer_step = checkpoint_newer_than_generation(steps, chat_start)
    has_newer = newer_step is not None
    st["compacted"] = has_newer

    for pos, step in enumerate(steps):
        cat = step_category(step.type)
        tok = step_tokens(step)
        counted = 0 if has_newer and newer_pos > pos else tok
        if cat == CAT_USER:
            st["user"] += counted
        elif cat == CAT_AGENT:
            st["agent"] += counted
        elif cat == CAT_TOOL:
            st["tool"] += counted
        elif cat == CAT_CHECKPOINT:
            st["checkpoint"] += tok
            ck = step.payload(STEP_CHECKPOINT)
            if ck is not None:
                checkpoints.append({
                    "index": ck.int(1),
                    "start": ck.int(11),
                    "end": ck.int(12),
                    "intent": ck.str(4),
                })
        else:
            st["system_messages"] += counted

        ca = step.payload(STEP_CODE_ACTION)
        result = ca.msg(2) if ca is not None else None
        edit = result.msg(1) if result is not None else None
        if edit is not None:
            uri = edit.str(8)  # absolute_uri
            short = shorten_path(uri)
            if ca.int(21) and uri not in seen_a:  # is_artifact_file
                seen_a.add(uri)
                artifacts.append((short, tok))
            if ca.int(24) and uri not in seen_k:  # is_knowledge_file
                seen_k.add(uri)
                knowledge.append((short, tok))

        vf = step.payload(STEP_VIEW_FILE)
        if vf is not None and vf.int(17):  # is_skill_file
            uri = vf.str(1)
            if uri not in seen_s:
                seen_s.add(uri)
                skill_files.append((shorten_path(uri), tok))

    cwm = chat_start.msg(10) if chat_start is not None else None
    breakdown = cwm.msg(3) if cwm is not None else None
    if breakdown is not None:
        st["has_breakdown"] = True
        seen_prompt = seen_tools = False
        chat_tok, chat_items = 0, []
        for g in breakdown.msgs(1):
            gtype, gtok = g.int(2), g.int(4, signed32=True)
            if gtype == TOKEN_SYSTEM_PROMPT:
                extracted = 0
                for child in g.msgs(5):
                    name, ctok = child.str(1), child.int(3, signed32=True)
                    if name == "flows":
                        st["workflows"] += ctok
                    elif name == "skills":
                        st["skills"] += ctok
                    elif name == "subagents":
                        st["subagents"] += ctok
                    else:
                        continue
                    extracted += ctok
                st["system_prompt"] = gtok - extracted
                seen_prompt = True
            elif gtype == TOKEN_TOOLS:
                st["system_tools"] = gtok
                seen_tools = True
            elif gtype == TOKEN_RULES:
                st["rules"] += gtok
            elif gtype == TOKEN_MCP_TOOLS:
                st["mcp_tools"] += gtok
            elif gtype == TOKEN_SKILLS:
                st["skills"] += gtok
            elif gtype == TOKEN_SUBAGENTS:
                st["subagents"] += gtok
            elif gtype == TOKEN_CHAT_MESSAGES:
                chat_tok, chat_items = gtok, g.msgs(5)
            elif gtype == TOKEN_WORKFLOWS:
                st["legacy_workflows"] += gtok
            elif gtype == TOKEN_PLUGINS:
                st["plugins"] += gtok
            elif gtype == TOKEN_FLOWS:
                st["workflows"] += gtok

        if not seen_prompt and not seen_tools and chat_tok:
            usage = chat.msg(4)
            api_input = usage.int(2) + usage.int(5) if usage is not None else 0
            if chat_tok < api_input:
                st["system_overhead"] = api_input - chat_tok

        if chat_tok and not has_newer:
            ok, cats = chat_tokens_by_category(chat_items)
            if ok:
                st["user"], st["agent"], st["tool"], st["system_messages"] = cats
            else:
                est = st["user"] + st["agent"] + st["tool"]
                if est == 0:
                    st["agent"] = chat_tok
                else:
                    f = chat_tok / est
                    st["user"] = int(st["user"] * f)
                    st["agent"] = int(st["agent"] * f)
                    st["tool"] = int(st["tool"] * f)

    if chat_start is not None:
        st["active_checkpoint"] = chat_start.int(2, signed32=True)
    if has_newer:
        st["agent"] += checkpoint_summary_tokens(newer_step)
        st["active_checkpoint"] = newer_step.payload(STEP_CHECKPOINT).int(1)

    st["total"] = sum(st[k] for k in (
        "user", "agent", "tool", "system_messages", "system_prompt", "system_tools", "system_overhead",
        "rules", "skills", "subagents", "legacy_workflows", "workflows", "plugins", "mcp_tools",
    ))
    st["limit"] = limit
    st["checkpoints"] = checkpoints
    st["knowledge_files"] = knowledge
    st["artifact_files"] = artifacts
    st["skill_files"] = skill_files
    return st


# --- rendering ---------------------------------------------------------------

CATEGORIES = [
    ("◉", "User messages", "user", True),
    ("◉", "Agent responses", "agent", True),
    ("◉", "Tool calls", "tool", True),
    ("⛁", "System prompt", "system_prompt", False),
    ("⛁", "System tools", "system_tools", False),
    ("⛁", "System overhead", "system_overhead", False),
    ("⛁", "Rules", "rules", False),
    ("⛁", "Skills", "skills", False),
    ("⛁", "Subagents", "subagents", False),
    ("⛁", "Legacy workflows", "legacy_workflows", False),
    ("⛁", "Workflows", "workflows", False),
    ("⛁", "Plugins", "plugins", False),
    ("⛁", "MCP tools", "mcp_tools", False),
    ("◉", "System messages", "system_messages", False),
]

GRID_COLORS = ["\x1b[90m", "\x1b[36m", "\x1b[35m", "\x1b[33m", "\x1b[34m", "\x1b[2m"]


def pct(n: int, limit: int) -> float:
    return n / limit * 100.0 if limit else 0.0


def stats_heading(st: dict) -> str:
    if st["compacted"]:
        return "Estimated after compaction · updates next turn"
    if st["has_breakdown"]:
        return "Token usage by category"
    return "Estimated usage (awaiting generation)"


def render_stats(st: dict, model: str) -> str:
    used, limit = st["total"], st["limit"]
    out = []
    if limit:
        out.append(f"{model} · {fmt_tok(used)}/{fmt_ctx_limit(limit)} tokens\n ({min(100.0, pct(used, limit)):.1f}%)\n")
    else:
        out.append(f"{model} · {fmt_tok(used)}/Unknown tokens\n")
    out.append(stats_heading(st) + "\n")
    for icon, name, key, always in CATEGORIES:
        tok = st[key]
        if always or tok:
            out.append(f"{icon} {name}: {fmt_tok(tok)} tokens ({pct(tok, limit):.1f}%)\n")
    free = limit - used if used < limit else 0
    out.append(f"□ Free space: {fmt_tok(free)} ({pct(free, limit):.1f}%)\n")
    if st["checkpoint"]:
        out.append(f"⊠ Checkpoint buffer: {fmt_tok(st['checkpoint'])} tokens (not counted in usage)\n")
    return "".join(out)


def grid_segments(st: dict) -> list[int]:
    """Token totals of the grid's five colored segments, in drawing order."""
    return [
        st["system_prompt"] + st["system_tools"] + st["system_overhead"],
        st["user"],
        st["agent"],
        st["tool"],
        sum(st[k] for k in ("rules", "skills", "subagents", "legacy_workflows", "workflows", "plugins", "mcp_tools")),
    ]


def render_grid(st: dict, width: int, rows: int, color: bool) -> list[str]:
    cols = max(10, width // 2)
    total_cells = max(1, rows) * cols
    limit = st["limit"]
    counts = [0 if not tok or not limit else max(1, int(tok / limit * total_cells)) for tok in grid_segments(st)]
    used = sum(counts)
    if used > total_cells:
        f = total_cells / used
        counts = [int(c * f) for c in counts]
    counts.append(total_cells - sum(counts))

    cells = []
    for i, n in enumerate(counts):
        glyph = "□" if i == 5 else "◉"
        cells.extend([f"{GRID_COLORS[i]}{glyph}\x1b[0m" if color else glyph] * n)
    return [" ".join(cells[r * cols : (r + 1) * cols]) for r in range(len(cells) // cols)]


def checkpoint_view(st: dict) -> dict | None:
    cks = st["checkpoints"]
    if not cks:
        return None
    active, historical = None, 0
    for ck in cks:
        if st["active_checkpoint"] >= 0 and ck["index"] == st["active_checkpoint"]:
            active = ck
        else:
            historical += 1
    if active is None:
        active, historical = cks[-1], len(cks) - 1
    text = active["intent"]
    if len(text.encode()) > 70:
        text = text.encode()[:67].decode("utf-8", errors="ignore") + "..."
    if not text:
        text = f"steps {active['start']}–{active['end']}"
    return {"count": len(cks), "active_number": active["index"] + 1, "active_text": text, "historical": historical}


def render_checkpoints(st: dict) -> str:
    view = checkpoint_view(st)
    if view is None:
        return ""
    out = [
        f"Checkpoints ({view['count']}) · /rewind\n",
        f"└ Checkpoint {view['active_number']} (active, in context): {view['active_text']}\n",
    ]
    if view["historical"]:
        out.append(f"  {view['historical']} historical checkpoint(s) (summarized, not in context)\n")
    return "".join(out)


def render_file_section(title: str, subtitle: str, items: list[tuple[str, int]]) -> str:
    if not items:
        return ""
    out = [f"\n{title} · {subtitle}\n"]
    for path, tok in items:
        out.append(f" └ {path}: {fmt_tok(tok)} tokens\n" if tok > 0 else f" └ {path}\n")
    return "".join(out)


def render(st: dict, model: str, system_files: list[tuple[str, int]], term_width: int, color: bool) -> str:
    stats = render_stats(st, model)
    grid_width = max(20, (2 * term_width) // 5)
    if term_width >= 70 and term_width - grid_width - 6 >= 40:
        grid = render_grid(st, grid_width, max(3, stats.count("\n")), color)
        stat_lines = stats.rstrip("\n").split("\n")
        pad = max(10, grid_width // 2) * 2 - 1
        lines = []
        for i in range(max(len(grid), len(stat_lines))):
            g = grid[i] if i < len(grid) else " " * pad
            s = stat_lines[i] if i < len(stat_lines) else ""
            lines.append(f"{g}   {s}".rstrip())
        body = "\n".join(lines) + "\n"
    else:
        grid = render_grid(st, term_width if term_width >= 20 else 40, 3, color)
        body = stats + "\n" + "\n".join(grid) + "\n"

    parts = ["└ Context Usage\n", "\n", body]
    ck = render_checkpoints(st)
    if ck:
        parts += ["\n", ck]
    parts.append(render_file_section("Knowledge files", "/rules", st["knowledge_files"]))
    parts.append(render_file_section("Artifact files", "/artifact", st["artifact_files"]))
    parts.append(render_file_section("Skill files", "/skill", st["skill_files"]))
    parts.append(render_file_section("System files", "auto-loaded", system_files))
    parts.append("\nRelated: /artifact · /skill · /rewind")
    return "".join(parts)


# --- background tasks --------------------------------------------------------

TASK_STATUS_NAMES = {
    2: "running",
    3: "done",
    4: "error",
    6: "cancelled",
}


def _parse_json_dict(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fallback_command_from_tool_call(tc: Msg | None) -> str:
    if tc is None:
        return ""
    args = _parse_json_dict(tc.str(3))
    if not args:
        return ""
    if isinstance(args.get("CommandLine"), str) and args["CommandLine"]:
        return args["CommandLine"]
    if "DurationSeconds" in args or "CronExpression" in args:
        parts = []
        if args.get("DurationSeconds") is not None:
            parts.append(f"Timer: {args['DurationSeconds']}s")
        if args.get("CronExpression"):
            parts.append(f"Cron: {args['CronExpression']}")
        if args.get("Prompt"):
            parts.append(f"Prompt: {args['Prompt']}")
        return ", ".join(parts)
    return ""


def _subagent_db_status(db_path: Path | None, sub_conv_id: str) -> str:
    if db_path is None or not sub_conv_id or not re.fullmatch(r"[0-9a-fA-F-]+", sub_conv_id):
        return "done"
    sub_db = db_path.parent / f"{sub_conv_id}.db"
    if not sub_db.is_file():
        return "done"
    try:
        conn = sqlite3.connect("file:" + sub_db.as_posix() + "?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT status FROM steps ORDER BY idx DESC LIMIT 5").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return "done"
    if not rows:
        return "running"
    statuses = [r[0] for r in rows]
    if any(s == 2 for s in statuses):
        return "running"
    latest = statuses[0]
    if latest == 4:
        return "error"
    if latest == 6:
        return "cancelled"
    return "done"


def extract_background_tasks(steps: list[Step], db_path: Path | None = None) -> list[dict]:
    """Extract background tasks (commands, timers) and subagents from steps."""
    killed_subagents: dict[str, int] = {}
    latest_kill_all_idx = -1
    for step in steps:
        tc = step.md.msg(4)
        if tc is None or tc.str(2) != "manage_subagents":
            continue
        args = _parse_json_dict(tc.str(3))
        action = args.get("Action")
        if action == "kill_all":
            latest_kill_all_idx = max(latest_kill_all_idx, step.idx)
        elif action == "kill":
            for cid in args.get("ConversationIds") or []:
                if isinstance(cid, str) and cid:
                    killed_subagents[cid] = max(killed_subagents.get(cid, -1), step.idx)

    tasks: list[dict] = []
    for step in steps:
        td = step.task_details
        tc = step.md.msg(4)
        tool_name = tc.str(2) if tc is not None else ""

        if td is not None and td.fields:
            command = td.str(4) or _fallback_command_from_tool_call(tc)
            log_uri = td.str(2)
            log_path = uri_to_path(log_uri) if log_uri else ""
            has_log = False
            log_mtime = 0
            log_size = 0
            if log_path:
                try:
                    if os.path.isfile(log_path):
                        st_res = os.stat(log_path)
                        has_log = True
                        log_mtime = int(st_res.st_mtime)
                        log_size = int(st_res.st_size)
                except OSError:
                    pass
            ts = td.msg(8)
            started_at = ts.int(1) if ts is not None else 0
            status_code = step.status
            status = TASK_STATUS_NAMES.get(status_code, "unknown")
            tasks.append({
                "step_idx": step.idx,
                "task_id": td.str(1),
                "title": td.str(7),
                "command": command,
                "output_preview": td.str(3),
                "status": status,
                "status_code": status_code,
                "is_daemon": bool(td.int(6)),
                "tool": tool_name,
                "started_at": started_at,
                "has_log": has_log,
                "log_mtime": log_mtime,
                "log_size": log_size,
            })
        elif step.type == 127 or tool_name == "invoke_subagent":
            args = _parse_json_dict(tc.str(3) if tc is not None else "")
            role = args.get("Role") if isinstance(args.get("Role"), str) else ""
            type_name = args.get("TypeName") if isinstance(args.get("TypeName"), str) else ""
            prompt = args.get("Prompt") if isinstance(args.get("Prompt"), str) else ""

            gen_step = step.pb.msg(STEP_GENERIC)
            res = gen_step.msg(2) if gen_step is not None else None
            res_text = res.str(1) if res is not None else ""
            m_cid = re.search(r'"conversationId"\s*:\s*"([0-9a-fA-F-]+)"', res_text)
            sub_conv_id = m_cid.group(1) if m_cid else ""

            if step.status == 2:
                status = "running"
            elif step.status == 4:
                status = "error"
            elif step.status == 6:
                status = "cancelled"
            elif latest_kill_all_idx > step.idx or (sub_conv_id and killed_subagents.get(sub_conv_id, -1) > step.idx):
                status = "cancelled"
            else:
                status = _subagent_db_status(db_path, sub_conv_id)

            ts = step.md.msg(1)
            started_at = ts.int(1) if ts is not None else 0
            tasks.append({
                "step_idx": step.idx,
                "task_id": sub_conv_id or f"subagent-{step.idx}",
                "title": role or type_name or "Subagent",
                "command": prompt,
                "output_preview": "",
                "status": status,
                "status_code": step.status,
                "is_daemon": False,
                "tool": "invoke_subagent",
                "subagent_type": type_name,
                "started_at": started_at,
                "has_log": False,
                "log_mtime": 0,
                "log_size": 0,
            })

    tasks.sort(key=lambda t: (t["status"] == "running", t["step_idx"]), reverse=True)
    return tasks


# --- cache & output stats ----------------------------------------------------


def collect_cache_stats(gens: list[Msg]) -> dict:
    """Compute latest-turn and session-cumulative prompt cache hit rates from gen_metadata."""
    generations = 0
    session_input = 0
    session_cache_read = 0
    last_input = 0
    last_cache_read = 0

    for g in gens:
        chat = g.msg(1)
        u = chat.msg(4) if chat is not None else None
        if u is None:
            continue
        inp = u.int(2)
        cache_read = u.int(5)
        if inp + cache_read <= 0:
            continue
        generations += 1
        session_input += inp
        session_cache_read += cache_read
        last_input = inp
        last_cache_read = cache_read

    last_total_input = last_input + last_cache_read
    session_total_input = session_input + session_cache_read
    return {
        "generations": generations,
        "last_hit_rate": round(last_cache_read / last_total_input * 100.0, 1) if last_total_input > 0 else None,
        "last_cache_read_tokens": last_cache_read,
        "last_input_tokens": last_input,
        "last_total_input_tokens": last_total_input,
        "session_hit_rate": round(session_cache_read / session_total_input * 100.0, 1) if session_total_input > 0 else None,
        "session_cache_read_tokens": session_cache_read,
        "session_input_tokens": session_input,
        "session_total_input_tokens": session_total_input,
    }


def collect_output_stats(gens: list[Msg]) -> dict:
    """Compute session-cumulative and latest-turn output & thinking tokens from gen_metadata."""
    session_output = 0
    session_thinking = 0
    session_response = 0
    last_output = 0
    last_thinking = 0

    for g in gens:
        chat = g.msg(1)
        u = chat.msg(4) if chat is not None else None
        if u is None:
            continue
        out = u.int(3)
        if out <= 0:
            continue
        thinking = u.int(9)
        resp = u.int(10)
        session_output += out
        session_thinking += thinking
        session_response += resp if resp > 0 else max(0, out - thinking)
        last_output = out
        last_thinking = thinking

    return {
        "session_output_tokens": session_output,
        "session_thinking_tokens": session_thinking,
        "session_response_tokens": session_response,
        "thinking_pct": round(session_thinking / session_output * 100.0, 1) if session_output > 0 else None,
        "last_output_tokens": last_output,
        "last_thinking_tokens": last_thinking,
    }


# --- main --------------------------------------------------------------------


def load_db(db_path: Path) -> tuple[list[Step], Msg | None, dict, dict]:
    uri = "file:" + db_path.as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT idx, step_type, status, metadata, step_payload, task_details, error_details FROM steps ORDER BY idx"
        ).fetchall()
        steps = [
            Step(pos, r[4] or b"", r[1], r[2], r[3], idx=r[0], task_details=r[5], error_details=r[6])
            for pos, r in enumerate(rows)
        ]
        gen_rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx").fetchall()
    finally:
        conn.close()
    gens = [Msg(r[0]) for r in gen_rows]
    return steps, (gens[-1] if gens else None), collect_cache_stats(gens), collect_output_stats(gens)


def load(db_path: Path) -> tuple[list[Step], Msg | None]:
    steps, latest_gen, _, _ = load_db(db_path)
    return steps, latest_gen


# Native context windows from the model catalog, which the CLI fetches from the server
# (ModelInfo.max_tokens) and never writes to disk. Matched by model ID prefix; the first
# match wins. Add a row when a new model family ships.
NATIVE_CONTEXT_WINDOWS = [
    ("claude-opus-4-6", 250_000),
    ("claude-sonnet-4-6", 250_000),
    ("claude-", 1_000_000),
    ("gpt-oss-", 131_072),
    ("gemini-", 1_048_576),
]

EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
WORD_LABELS = {"gpt": "GPT", "oss": "OSS"}


def model_id(gen: Msg | None) -> str:
    planner = gen.msg(3) if gen is not None else None
    return planner.str(28) if planner is not None else ""  # planner_config.model_name


def model_label(slug: str) -> str:
    """Catalog-style label from a model ID: gemini-3.8-flash-medium -> Gemini 3.8 Flash (Medium)."""
    parts = slug.split("-")
    effort = parts.pop().capitalize() if len(parts) > 1 and parts[-1] in EFFORT_LEVELS else ""
    words: list[str] = []
    for p in parts:
        if p.isdigit() and words and words[-1][-1:].isdigit():
            words[-1] += "." + p  # claude-opus-5-5 -> 5.5
        elif p in WORD_LABELS:
            words.append(WORD_LABELS[p])
        elif p[:1].isdigit():
            words.append(p.upper())  # 120b -> 120B
        else:
            words.append(p.capitalize())
    label = " ".join(words).replace("GPT OSS", "GPT-OSS")
    return f"{label} ({effort})" if effort else label


def model_name(gen: Msg | None) -> str:
    chat = gen.msg(1) if gen is not None else None
    if chat is not None and chat.str(21):  # model_display_name
        return chat.str(21)
    if model_id(gen):
        return model_label(model_id(gen))
    if chat is not None and chat.str(19):  # response_model
        return chat.str(19)
    return "Unknown Model"


def context_limit(gen: Msg | None) -> int:
    """The model's native window, like the CLI; else the request budget (max_context_tokens)."""
    slug = model_id(gen)
    for prefix, tokens in NATIVE_CONTEXT_WINDOWS:
        if slug.startswith(prefix):
            return tokens
    return request_budget(gen)


def request_budget(gen: Msg | None) -> int:
    chat = gen.msg(1) if gen is not None else None
    start = chat.msg(9) if chat is not None else None
    cwm = start.msg(10) if start is not None else None
    return cwm.int(4, signed32=True) if cwm is not None else 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Analyze Antigravity 2.0 conversation context usage.")
    parser.add_argument("--conversation-id", help="Conversation UUID (auto-detected if omitted)")
    parser.add_argument("--db", help="Path to a conversation .db file (e.g. one from ~/.gemini/antigravity-cli/conversations)")
    parser.add_argument("--workspace", help="Active workspace path", default=None)
    parser.add_argument("--limit", type=int, default=0, help="Context window token limit override")
    parser.add_argument("--app-dir", default="antigravity", help="App data folder under ~/.gemini (default: antigravity)")
    parser.add_argument("--width", type=int, default=100, help="Terminal width used for the grid layout")
    parser.add_argument("--color", action="store_true", help="Color the grid segments with ANSI escapes")
    parser.add_argument("--json", action="store_true", help="Output JSON data")
    parser.add_argument("--model-catalog", help='JSON file of {"<model id>": {"maxTokens": n, "displayName": "..."}} '
                        "from the app's live model catalog; overrides the built-in limits and labels")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db).expanduser()
        conv_id = db_path.stem
    else:
        conv_dir = Path.home() / ".gemini" / "antigravity" / "conversations"
        conv_id = args.conversation_id
        if not conv_id:
            dbs = sorted(conv_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not dbs:
                raise SystemExit("No conversation databases found.")
            conv_id = dbs[0].stem
        db_path = conv_dir / f"{conv_id}.db"
    if not db_path.is_file():
        raise SystemExit(f"Conversation database not found: {db_path}")

    steps, gen, cache_stats, output_stats = load_db(db_path)
    catalog_entry = {}
    if args.model_catalog:
        with open(args.model_catalog, encoding="utf-8") as fh:
            catalog_entry = json.load(fh).get(model_id(gen)) or {}
    limit = args.limit if args.limit > 0 else int(catalog_entry.get("maxTokens") or 0) or context_limit(gen)
    st = collect_stats(steps, gen, limit)
    model = catalog_entry.get("displayName") or model_name(gen)
    system_files = detect_system_files(os.path.expanduser("~"), args.workspace, args.app_dir)

    if args.json:
        print(json.dumps({
            "conversation_id": conv_id,
            "model": model,
            "heading": stats_heading(st),
            "grid_segments": grid_segments(st),
            "checkpoint_view": checkpoint_view(st),
            "text": render(st, model, system_files, args.width, False),
            "model_id": model_id(gen),
            "request_budget_tokens": request_budget(gen),
            "used_tokens": st["total"],
            "limit_tokens": limit,
            "used_pct": round(min(100.0, pct(st["total"], limit)), 1),
            "free_tokens": limit - st["total"] if st["total"] < limit else 0,
            "has_breakdown": st["has_breakdown"],
            "compacted": st["compacted"],
            "checkpoint_buffer": st["checkpoint"],
            "categories": [
                {"icon": icon, "name": name, "key": key, "tokens": st[key], "pct": round(pct(st[key], limit), 1)}
                for icon, name, key, always in CATEGORIES
                if always or st[key]
            ],
            "active_checkpoint": st["active_checkpoint"],
            "checkpoints": st["checkpoints"],
            "knowledge_files": [{"path": p, "tokens": t} for p, t in st["knowledge_files"]],
            "artifact_files": [{"path": p, "tokens": t} for p, t in st["artifact_files"]],
            "skill_files": [{"path": p, "tokens": t} for p, t in st["skill_files"]],
            "system_files": [{"path": p} for p, _ in system_files],
            "background_tasks": extract_background_tasks(steps, db_path=db_path),
            "cache_stats": cache_stats,
            "output_stats": output_stats,
        }, indent=2))
        return

    print(render(st, model, system_files, args.width, args.color))


if __name__ == "__main__":
    main()
