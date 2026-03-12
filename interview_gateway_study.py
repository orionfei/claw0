"""
Interview Study Gateway (single-file learning scaffold)

This file condenses the core ideas from claw0 s01-s10 into one runnable script:
1) Agent loop + stop_reason
2) Tool schema + dispatch table
3) Session persistence + context guard
4) Multi-channel normalization
5) Gateway routing + session isolation
6) Prompt assembly + skills + memory recall
7) Heartbeat + cron proactive tasks
8) Reliable delivery queue (write-ahead + backoff)
9) Resilience retry onion (profile rotation + fallback model)
10) Named-lane concurrency

It is intentionally simplified for interview learning:
- Uses a local MockLLM (no external API dependency).
- Preserves architecture patterns and failure handling decisions.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import random
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / ".interview_data"
SESSIONS_DIR = DATA_DIR / "sessions"
DELIVERY_DIR = DATA_DIR / "delivery-queue"
FAILED_DIR = DELIVERY_DIR / "failed"
STATE_DIR = DATA_DIR / "state"
MEMORY_DAILY_DIR = DATA_DIR / "memory" / "daily"
CRON_RUN_LOG = DATA_DIR / "cron" / "cron-runs.jsonl"

BOOTSTRAP_FILES = [
    "SOUL.md",
    "IDENTITY.md",
    "TOOLS.md",
    "USER.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
    "AGENTS.md",
    "MEMORY.md",
]

MAX_FILE_CHARS = 20_000
MAX_TOTAL_BOOTSTRAP_CHARS = 120_000
MAX_TOOL_OUTPUT = 40_000
CONTEXT_SAFE_TOKENS = 12_000
BACKOFF_MS = [5_000, 25_000, 120_000, 600_000]
MAX_RETRIES = 5

for folder in [DATA_DIR, SESSIONS_DIR, DELIVERY_DIR, FAILED_DIR, STATE_DIR, MEMORY_DAILY_DIR, CRON_RUN_LOG.parent]:
    folder.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Pretty output helpers
# ---------------------------------------------------------------------------

CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"


def info(msg: str) -> None:
    print(f"{DIM}{msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}{msg}{RESET}")


def err(msg: str) -> None:
    print(f"{RED}{msg}{RESET}")


def show_agent(msg: str) -> None:
    print(f"\n{GREEN}{BOLD}Assistant:{RESET} {msg}\n")


def show_lane(lane: str, msg: str) -> None:
    color = {"main": CYAN, "cron": MAGENTA, "heartbeat": BLUE}.get(lane, YELLOW)
    print(f"{color}{BOLD}[{lane}]{RESET} {msg}")


def show_delivery(msg: str) -> None:
    print(f"{BLUE}[delivery]{RESET} {msg}")


# ---------------------------------------------------------------------------
# Common data models
# ---------------------------------------------------------------------------


@dataclass
class InboundMessage:
    text: str
    channel: str
    account_id: str
    peer_id: str
    sender_id: str


@dataclass
class LLMResponse:
    stop_reason: str
    content: list[dict[str, Any]]


def extract_text(blocks: list[dict[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in blocks if block.get("type") == "text")


# ---------------------------------------------------------------------------
# s02: Tool registry + dispatch
# ---------------------------------------------------------------------------


class ToolRegistry:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self.schemas: list[dict[str, Any]] = []
        self.handlers: dict[str, Callable[..., str]] = {}
        self._install_builtin_tools()

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[..., str],
    ) -> None:
        self.schemas.append(
            {
                "name": name,
                "description": description,
                "input_schema": input_schema,
            }
        )
        self.handlers[name] = handler

    def call(self, name: str, tool_input: dict[str, Any]) -> str:
        handler = self.handlers.get(name)
        if handler is None:
            return f"Error: Unknown tool '{name}'"
        try:
            return handler(**tool_input)
        except TypeError as exc:
            return f"Error: Invalid arguments for {name}: {exc}"
        except Exception as exc:
            return f"Error: {name} failed: {exc}"

    def _safe_path(self, raw: str) -> Path:
        target = (self.workspace_root / raw).resolve()
        if not str(target).startswith(str(self.workspace_root.resolve())):
            raise ValueError(f"Path traversal blocked: {raw}")
        return target

    def _truncate(self, text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n... [truncated, {len(text)} total chars]"

    def _install_builtin_tools(self) -> None:
        def tool_read_file(file_path: str) -> str:
            target = self._safe_path(file_path)
            if not target.exists():
                return f"Error: File not found: {file_path}"
            if not target.is_file():
                return f"Error: Not a file: {file_path}"
            return self._truncate(target.read_text(encoding="utf-8"))

        def tool_list_directory(directory: str = ".") -> str:
            target = self._safe_path(directory)
            if not target.exists():
                return f"Error: Directory not found: {directory}"
            if not target.is_dir():
                return f"Error: Not a directory: {directory}"
            entries = sorted(target.iterdir())
            lines = [("[dir]  " if e.is_dir() else "[file] ") + e.name for e in entries]
            return "\n".join(lines) if lines else "[empty directory]"

        def tool_get_current_time() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        self.register(
            name="read_file",
            description="Read a file under workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                },
                "required": ["file_path"],
            },
            handler=tool_read_file,
        )
        self.register(
            name="list_directory",
            description="List files/dirs under workspace.",
            input_schema={
                "type": "object",
                "properties": {"directory": {"type": "string"}},
            },
            handler=tool_list_directory,
        )
        self.register(
            name="get_current_time",
            description="Get current UTC time.",
            input_schema={"type": "object", "properties": {}},
            handler=tool_get_current_time,
        )


# ---------------------------------------------------------------------------
# s03: Session persistence + context guard
# ---------------------------------------------------------------------------


class SessionStore:
    """Append-only JSONL persistence with session_key -> session_id mapping."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_dir / "session_map.json"
        self.meta_path = self.base_dir / "sessions.json"
        self._session_map = self._load_json(self.index_path, default={})
        self._meta = self._load_json(self.meta_path, default={})

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _session_path(self, session_id: str) -> Path:
        return self.base_dir / f"{session_id}.jsonl"

    def _create_session(self, label: str = "") -> str:
        sid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        self._meta[sid] = {
            "label": label,
            "created_at": now,
            "last_active": now,
            "message_count": 0,
        }
        self._save_json(self.meta_path, self._meta)
        self._session_path(sid).touch()
        return sid

    def get_or_create_by_key(self, session_key: str) -> str:
        sid = self._session_map.get(session_key, "")
        if sid:
            return sid
        sid = self._create_session(label=session_key)
        self._session_map[session_key] = sid
        self._save_json(self.index_path, self._session_map)
        return sid

    def append(self, session_key: str, record: dict[str, Any]) -> None:
        sid = self.get_or_create_by_key(session_key)
        path = self._session_path(sid)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        meta = self._meta.get(sid)
        if meta is not None:
            meta["message_count"] = meta.get("message_count", 0) + 1
            meta["last_active"] = datetime.now(timezone.utc).isoformat()
            self._save_json(self.meta_path, self._meta)

    def load_messages(self, session_key: str) -> list[dict[str, Any]]:
        sid = self.get_or_create_by_key(session_key)
        path = self._session_path(sid)
        if not path.exists():
            return []
        return self._rebuild_history(path)

    def _rebuild_history(self, path: Path) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = record.get("type")
            if rtype == "user":
                messages.append({"role": "user", "content": record.get("content", "")})
            elif rtype == "assistant":
                content = record.get("content", [])
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                messages.append({"role": "assistant", "content": content})
            elif rtype == "tool_use":
                block = {
                    "type": "tool_use",
                    "id": record.get("tool_use_id", ""),
                    "name": record.get("name", ""),
                    "input": record.get("input", {}),
                }
                if messages and messages[-1]["role"] == "assistant":
                    last = messages[-1]["content"]
                    if isinstance(last, list):
                        last.append(block)
                    else:
                        messages[-1]["content"] = [{"type": "text", "text": str(last)}, block]
                else:
                    messages.append({"role": "assistant", "content": [block]})
            elif rtype == "tool_result":
                block = {
                    "type": "tool_result",
                    "tool_use_id": record.get("tool_use_id", ""),
                    "content": record.get("content", ""),
                }
                if (
                    messages
                    and messages[-1]["role"] == "user"
                    and isinstance(messages[-1]["content"], list)
                    and messages[-1]["content"]
                    and isinstance(messages[-1]["content"][0], dict)
                    and messages[-1]["content"][0].get("type") == "tool_result"
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
        return messages


class ContextGuard:
    """Three-stage overflow defense: estimate -> truncate -> compact."""

    def __init__(self, max_tokens: int = CONTEXT_SAFE_TOKENS) -> None:
        self.max_tokens = max_tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return len(text) // 4

    def estimate_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.estimate_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        total += self.estimate_tokens(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        total += self.estimate_tokens(json.dumps(block.get("input", {})))
                    elif block.get("type") == "tool_result":
                        total += self.estimate_tokens(str(block.get("content", "")))
        return total

    def truncate_tool_results(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        max_chars = int(self.max_tokens * 4 * 0.25)
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                result.append(msg)
                continue
            blocks: list[Any] = []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and isinstance(block.get("content"), str)
                    and len(block["content"]) > max_chars
                ):
                    b = dict(block)
                    original = len(b["content"])
                    b["content"] = b["content"][:max_chars] + (
                        f"\n\n[... truncated ({original} chars total, showing first {max_chars}) ...]"
                    )
                    blocks.append(b)
                else:
                    blocks.append(block)
            result.append({"role": msg["role"], "content": blocks})
        return result

    def compact_history(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(messages) <= 6:
            return messages
        compress_count = max(3, len(messages) // 2)
        keep = messages[compress_count:]
        summary_lines: list[str] = []
        for msg in messages[:compress_count]:
            role = msg["role"]
            content = msg.get("content", "")
            if isinstance(content, str):
                snippet = content[:120]
                summary_lines.append(f"[{role}] {snippet}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            summary_lines.append(f"[{role}] {block.get('text', '')[:120]}")
                        elif block.get("type") == "tool_use":
                            summary_lines.append(
                                f"[{role} called {block.get('name')}] {json.dumps(block.get('input', {}), ensure_ascii=False)[:120]}"
                            )
                        elif block.get("type") == "tool_result":
                            summary_lines.append(f"[tool_result] {str(block.get('content', ''))[:120]}")
        summary = "\n".join(summary_lines[:60])
        compacted = [
            {"role": "user", "content": "[Previous summary]\n" + summary},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Summary loaded. Continuing with recent context."}],
            },
        ]
        compacted.extend(keep)
        return compacted


# ---------------------------------------------------------------------------
# s05: Routing + agent/session isolation
# ---------------------------------------------------------------------------


@dataclass
class Binding:
    agent_id: str
    tier: int
    match_key: str
    match_value: str
    priority: int = 0


class BindingTable:
    def __init__(self) -> None:
        self._items: list[Binding] = []

    def add(self, binding: Binding) -> None:
        self._items.append(binding)
        self._items.sort(key=lambda b: (b.tier, -b.priority))

    def list_all(self) -> list[Binding]:
        return list(self._items)

    def resolve(
        self,
        channel: str,
        account_id: str,
        peer_id: str,
        guild_id: str = "",
    ) -> tuple[str | None, Binding | None]:
        for b in self._items:
            if b.tier == 1 and b.match_key == "peer_id":
                if ":" in b.match_value:
                    if b.match_value == f"{channel}:{peer_id}":
                        return b.agent_id, b
                elif b.match_value == peer_id:
                    return b.agent_id, b
            elif b.tier == 2 and b.match_key == "guild_id" and b.match_value == guild_id:
                return b.agent_id, b
            elif b.tier == 3 and b.match_key == "account_id" and b.match_value == account_id:
                return b.agent_id, b
            elif b.tier == 4 and b.match_key == "channel" and b.match_value == channel:
                return b.agent_id, b
            elif b.tier == 5 and b.match_key == "default":
                return b.agent_id, b
        return None, None


def build_session_key(
    agent_id: str,
    channel: str,
    account_id: str,
    peer_id: str,
    dm_scope: str = "per-channel-peer",
) -> str:
    aid = agent_id.strip().lower() or "main"
    ch = channel.strip().lower() or "unknown"
    acc = account_id.strip().lower() or "default"
    pid = peer_id.strip().lower()
    if dm_scope == "per-account-channel-peer":
        return f"agent:{aid}:{ch}:{acc}:direct:{pid}"
    if dm_scope == "per-channel-peer":
        return f"agent:{aid}:{ch}:direct:{pid}"
    if dm_scope == "per-peer":
        return f"agent:{aid}:direct:{pid}"
    return f"agent:{aid}:main"


# ---------------------------------------------------------------------------
# s06: Memory + skills + prompt assembly
# ---------------------------------------------------------------------------


class MemoryStore:
    """Evergreen MEMORY.md + daily JSONL with TF-IDF retrieval."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.evergreen_path = root / "MEMORY.md"
        self.daily_dir = MEMORY_DAILY_DIR
        self.daily_dir.mkdir(parents=True, exist_ok=True)

    def write_memory(self, content: str, category: str = "general") -> str:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.daily_dir / f"{day}.jsonl"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "content": content,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return f"Memory saved to {day}.jsonl ({category})"

    def load_evergreen(self) -> str:
        if not self.evergreen_path.exists():
            return ""
        return self.evergreen_path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        raw = re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())
        return [t for t in raw if len(t) > 1 or "\u4e00" <= t <= "\u9fff"]

    def _chunks(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        evergreen = self.load_evergreen()
        if evergreen:
            for para in evergreen.split("\n\n"):
                p = para.strip()
                if p:
                    result.append({"path": "MEMORY.md", "text": p})
        for file in sorted(self.daily_dir.glob("*.jsonl")):
            for line in file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = item.get("content", "")
                if text:
                    result.append({"path": file.name, "text": text})
        return result

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        chunks = self._chunks()
        if not chunks:
            return []
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []
        tokenized = [self._tokenize(c["text"]) for c in chunks]
        n = len(chunks)
        df: dict[str, int] = {}
        for tokens in tokenized:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1

        def tfidf(tokens: list[str]) -> dict[str, float]:
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            return {t: c * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1) for t, c in tf.items()}

        def cosine(a: dict[str, float], b: dict[str, float]) -> float:
            common = set(a) & set(b)
            if not common:
                return 0.0
            dot = sum(a[k] * b[k] for k in common)
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            return dot / (na * nb) if na and nb else 0.0

        qvec = tfidf(q_tokens)
        scored: list[dict[str, Any]] = []
        for i, tokens in enumerate(tokenized):
            if not tokens:
                continue
            score = cosine(qvec, tfidf(tokens))
            if score > 0:
                snippet = chunks[i]["text"]
                if len(snippet) > 180:
                    snippet = snippet[:180] + "..."
                scored.append({"path": chunks[i]["path"], "score": round(score, 4), "snippet": snippet})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def recall_block(self, query: str, top_k: int = 3) -> str:
        hits = self.search(query, top_k=top_k)
        if not hits:
            return ""
        lines = [f"- [{h['path']}] {h['snippet']}" for h in hits]
        return "\n".join(lines)


class SkillsManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.skills: list[dict[str, str]] = []

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, str]:
        if not text.startswith("---"):
            return {}
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}
        meta: dict[str, str] = {}
        for line in parts[1].splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        return meta

    def discover(self) -> None:
        scan_dirs = [self.root / "skills", self.root / ".skills"]
        seen: dict[str, dict[str, str]] = {}
        for d in scan_dirs:
            if not d.is_dir():
                continue
            for child in sorted(d.iterdir()):
                skill_md = child / "SKILL.md"
                if not skill_md.is_file():
                    continue
                content = skill_md.read_text(encoding="utf-8")
                meta = self._parse_frontmatter(content)
                name = meta.get("name", child.name)
                body = content.split("---", 2)[-1].strip() if content.startswith("---") else content
                seen[name] = {
                    "name": name,
                    "description": meta.get("description", ""),
                    "invocation": meta.get("invocation", ""),
                    "body": body[:1200],
                }
        self.skills = list(seen.values())[:80]

    def format_prompt_block(self) -> str:
        if not self.skills:
            return ""
        lines = ["## Available Skills", ""]
        for s in self.skills:
            lines.append(f"### {s['name']}")
            lines.append(f"Description: {s['description']}")
            if s["invocation"]:
                lines.append(f"Invocation: {s['invocation']}")
            if s["body"]:
                lines.append(s["body"])
            lines.append("")
        block = "\n".join(lines)
        return block[:20_000]


class PromptBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load_bootstrap(self, mode: str = "full") -> dict[str, str]:
        if mode == "none":
            return {}
        names = ["AGENTS.md", "TOOLS.md"] if mode == "minimal" else BOOTSTRAP_FILES
        total = 0
        result: dict[str, str] = {}
        for name in names:
            path = self.root / name
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            if len(content) > MAX_FILE_CHARS:
                content = content[:MAX_FILE_CHARS] + f"\n\n[... truncated to {MAX_FILE_CHARS} chars ...]"
            remaining = MAX_TOTAL_BOOTSTRAP_CHARS - total
            if remaining <= 0:
                break
            content = content[:remaining]
            result[name] = content
            total += len(content)
        return result

    def build(
        self,
        bootstrap: dict[str, str],
        skills_block: str,
        memory_recall: str,
        agent_id: str,
        channel: str,
        mode: str = "full",
    ) -> str:
        sections: list[str] = []
        identity = bootstrap.get("IDENTITY.md", "").strip()
        sections.append(identity if identity else "You are a helpful AI assistant.")
        if mode == "full":
            soul = bootstrap.get("SOUL.md", "").strip()
            if soul:
                sections.append("## Personality\n\n" + soul)
        tools_md = bootstrap.get("TOOLS.md", "").strip()
        if tools_md:
            sections.append("## Tool Usage Guidelines\n\n" + tools_md)
        if mode == "full" and skills_block:
            sections.append(skills_block)
        if mode == "full":
            mem_md = bootstrap.get("MEMORY.md", "").strip()
            mem_parts: list[str] = []
            if mem_md:
                mem_parts.append("### Evergreen Memory\n\n" + mem_md)
            if memory_recall:
                mem_parts.append("### Auto Recall\n\n" + memory_recall)
            if mem_parts:
                sections.append("## Memory\n\n" + "\n\n".join(mem_parts))
        for name in ["HEARTBEAT.md", "BOOTSTRAP.md", "AGENTS.md", "USER.md"]:
            content = bootstrap.get(name, "").strip()
            if content:
                sections.append(f"## {name.replace('.md', '')}\n\n{content}")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sections.append(
            "## Runtime Context\n\n"
            f"- Agent ID: {agent_id}\n"
            f"- Channel: {channel}\n"
            f"- Current time: {now}\n"
            f"- Prompt mode: {mode}"
        )
        channel_hint = {
            "cli": "Terminal output is fine.",
            "telegram": "Keep replies concise.",
            "discord": "Keep replies under 2000 chars.",
        }.get(channel, f"Respond for channel: {channel}")
        sections.append("## Channel\n\n" + channel_hint)
        return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# s08: Reliable delivery queue
# ---------------------------------------------------------------------------


@dataclass
class QueuedDelivery:
    id: str
    channel: str
    to: str
    text: str
    retry_count: int = 0
    last_error: str | None = None
    enqueued_at: float = field(default_factory=time.time)
    next_retry_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "to": self.to,
            "text": self.text,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "enqueued_at": self.enqueued_at,
            "next_retry_at": self.next_retry_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "QueuedDelivery":
        return QueuedDelivery(
            id=data["id"],
            channel=data["channel"],
            to=data["to"],
            text=data["text"],
            retry_count=data.get("retry_count", 0),
            last_error=data.get("last_error"),
            enqueued_at=data.get("enqueued_at", 0.0),
            next_retry_at=data.get("next_retry_at", 0.0),
        )


def compute_backoff_ms(retry_count: int) -> int:
    if retry_count <= 0:
        return 0
    idx = min(retry_count - 1, len(BACKOFF_MS) - 1)
    base = BACKOFF_MS[idx]
    jitter = random.randint(-base // 5, base // 5)
    return max(0, base + jitter)


class DeliveryQueue:
    def __init__(self, queue_dir: Path) -> None:
        self.queue_dir = queue_dir
        self.failed_dir = queue_dir / "failed"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(self, channel: str, to: str, text: str) -> str:
        did = uuid.uuid4().hex[:12]
        entry = QueuedDelivery(id=did, channel=channel, to=to, text=text)
        self._write_atomic(entry)
        return did

    def _write_atomic(self, entry: QueuedDelivery) -> None:
        final_path = self.queue_dir / f"{entry.id}.json"
        tmp_path = self.queue_dir / f".tmp.{os.getpid()}.{entry.id}.json"
        raw = json.dumps(entry.to_dict(), indent=2, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(final_path))

    def _read(self, delivery_id: str) -> QueuedDelivery | None:
        path = self.queue_dir / f"{delivery_id}.json"
        if not path.exists():
            return None
        try:
            return QueuedDelivery.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def ack(self, delivery_id: str) -> None:
        path = self.queue_dir / f"{delivery_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def fail(self, delivery_id: str, error: str) -> None:
        entry = self._read(delivery_id)
        if entry is None:
            return
        entry.retry_count += 1
        entry.last_error = error
        if entry.retry_count >= MAX_RETRIES:
            self.move_to_failed(delivery_id)
            return
        entry.next_retry_at = time.time() + compute_backoff_ms(entry.retry_count) / 1000.0
        self._write_atomic(entry)

    def move_to_failed(self, delivery_id: str) -> None:
        src = self.queue_dir / f"{delivery_id}.json"
        dst = self.failed_dir / f"{delivery_id}.json"
        try:
            os.replace(str(src), str(dst))
        except FileNotFoundError:
            pass

    def load_pending(self) -> list[QueuedDelivery]:
        items: list[QueuedDelivery] = []
        for path in self.queue_dir.glob("*.json"):
            if not path.is_file():
                continue
            try:
                items.append(QueuedDelivery.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        items.sort(key=lambda e: e.enqueued_at)
        return items

    def load_failed(self) -> list[QueuedDelivery]:
        items: list[QueuedDelivery] = []
        for path in self.failed_dir.glob("*.json"):
            if not path.is_file():
                continue
            try:
                items.append(QueuedDelivery.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        items.sort(key=lambda e: e.enqueued_at)
        return items

    def retry_failed(self) -> int:
        moved = 0
        for path in self.failed_dir.glob("*.json"):
            if not path.is_file():
                continue
            try:
                entry = QueuedDelivery.from_dict(json.loads(path.read_text(encoding="utf-8")))
                entry.retry_count = 0
                entry.last_error = None
                entry.next_retry_at = 0.0
                self._write_atomic(entry)
                path.unlink()
                moved += 1
            except Exception:
                continue
        return moved


def chunk_message(text: str, channel: str) -> list[str]:
    limits = {"telegram": 4096, "discord": 2000, "cli": 4000}
    limit = limits.get(channel, 4000)
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    for para in text.split("\n\n"):
        if chunks and len(chunks[-1]) + len(para) + 2 <= limit:
            chunks[-1] += "\n\n" + para
        else:
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            if para:
                chunks.append(para)
    return chunks


class DeliveryRunner:
    def __init__(self, queue: DeliveryQueue, deliver_fn: Callable[[str, str, str], None]) -> None:
        self.queue = queue
        self.deliver_fn = deliver_fn
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.total_attempted = 0
        self.total_succeeded = 0
        self.total_failed = 0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True, name="delivery-runner")
        self.thread.start()
        pending = len(self.queue.load_pending())
        failed = len(self.queue.load_failed())
        if pending or failed:
            show_delivery(f"Recovery scan: pending={pending}, failed={failed}")

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            now = time.time()
            for entry in self.queue.load_pending():
                if self.stop_event.is_set():
                    break
                if entry.next_retry_at > now:
                    continue
                self.total_attempted += 1
                try:
                    self.deliver_fn(entry.channel, entry.to, entry.text)
                    self.queue.ack(entry.id)
                    self.total_succeeded += 1
                except Exception as exc:
                    self.total_failed += 1
                    self.queue.fail(entry.id, str(exc))
            self.stop_event.wait(timeout=1.0)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)


# ---------------------------------------------------------------------------
# s09: Resilience retry onion
# ---------------------------------------------------------------------------


class FailoverReason(Enum):
    rate_limit = "rate_limit"
    auth = "auth"
    timeout = "timeout"
    billing = "billing"
    overflow = "overflow"
    unknown = "unknown"


def classify_failure(exc: Exception) -> FailoverReason:
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg:
        return FailoverReason.rate_limit
    if "401" in msg or "auth" in msg or "key" in msg:
        return FailoverReason.auth
    if "timeout" in msg or "timed out" in msg:
        return FailoverReason.timeout
    if "402" in msg or "billing" in msg or "quota" in msg:
        return FailoverReason.billing
    if "overflow" in msg or "context" in msg or "token" in msg:
        return FailoverReason.overflow
    return FailoverReason.unknown


@dataclass
class AuthProfile:
    name: str
    api_key: str
    cooldown_until: float = 0.0
    failure_reason: str | None = None
    last_good_at: float = 0.0


class ProfileManager:
    def __init__(self, profiles: list[AuthProfile]) -> None:
        self.profiles = profiles

    def select_available(self) -> AuthProfile | None:
        now = time.time()
        for p in self.profiles:
            if now >= p.cooldown_until:
                return p
        return None

    def mark_failure(self, profile: AuthProfile, reason: FailoverReason, cooldown: float) -> None:
        profile.cooldown_until = time.time() + cooldown
        profile.failure_reason = reason.value

    def mark_success(self, profile: AuthProfile) -> None:
        profile.cooldown_until = 0.0
        profile.failure_reason = None
        profile.last_good_at = time.time()

    def status_rows(self) -> list[str]:
        rows: list[str] = []
        now = time.time()
        for p in self.profiles:
            left = max(0, p.cooldown_until - now)
            status = "available" if left == 0 else f"cooldown {left:.0f}s"
            rows.append(
                f"{p.name:12s} | {status:16s} | last_good="
                f"{time.strftime('%H:%M:%S', time.localtime(p.last_good_at)) if p.last_good_at else 'never'}"
                + (f" | reason={p.failure_reason}" if p.failure_reason else "")
            )
        return rows


class SimulatedFailure:
    TEMPLATES = {
        "rate_limit": "Error code: 429 -- rate limit exceeded",
        "auth": "Error code: 401 -- invalid API key",
        "timeout": "Request timed out",
        "billing": "Error code: 402 -- quota exceeded",
        "overflow": "Context window overflow",
        "unknown": "Unexpected internal error",
    }

    def __init__(self) -> None:
        self.pending: str | None = None

    def arm(self, reason: str) -> str:
        if reason not in self.TEMPLATES:
            valid = ", ".join(self.TEMPLATES)
            return f"Unknown reason '{reason}'. Valid: {valid}"
        self.pending = reason
        return f"Armed simulated failure: {reason}"

    def fire_if_armed(self) -> None:
        if self.pending is None:
            return
        reason = self.pending
        self.pending = None
        raise RuntimeError(self.TEMPLATES[reason])


class ResilienceRunner:
    """Layer1 profile rotation + Layer2 overflow compaction + fallback model chain."""

    def __init__(
        self,
        profile_manager: ProfileManager,
        context_guard: ContextGuard,
        simulated_failure: SimulatedFailure,
        primary_model: str = "mock-sonnet",
        fallback_models: list[str] | None = None,
        max_overflow_compaction: int = 3,
    ) -> None:
        self.profile_manager = profile_manager
        self.guard = context_guard
        self.sim_fail = simulated_failure
        self.primary_model = primary_model
        self.fallback_models = fallback_models or ["mock-haiku"]
        self.max_overflow_compaction = max_overflow_compaction
        self.stats = {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "rotations": 0,
            "compactions": 0,
        }

    def call(
        self,
        messages: list[dict[str, Any]],
        invoke: Callable[[str, AuthProfile, list[dict[str, Any]]], LLMResponse],
    ) -> tuple[LLMResponse, list[dict[str, Any]], str]:
        tried: set[str] = set()
        for _ in range(len(self.profile_manager.profiles)):
            profile = self.profile_manager.select_available()
            if profile is None:
                break
            if profile.name in tried:
                break
            if tried:
                self.stats["rotations"] += 1
            tried.add(profile.name)

            local_messages = list(messages)
            for compact_attempt in range(self.max_overflow_compaction):
                try:
                    self.stats["attempts"] += 1
                    self.sim_fail.fire_if_armed()
                    response = invoke(self.primary_model, profile, local_messages)
                    self.profile_manager.mark_success(profile)
                    self.stats["successes"] += 1
                    return response, local_messages, profile.name
                except Exception as exc:
                    self.stats["failures"] += 1
                    reason = classify_failure(exc)
                    if reason == FailoverReason.overflow and compact_attempt < self.max_overflow_compaction - 1:
                        local_messages = self.guard.truncate_tool_results(local_messages)
                        local_messages = self.guard.compact_history(local_messages)
                        self.stats["compactions"] += 1
                        continue
                    cooldown = {
                        FailoverReason.timeout: 60.0,
                        FailoverReason.rate_limit: 120.0,
                        FailoverReason.auth: 300.0,
                        FailoverReason.billing: 300.0,
                        FailoverReason.overflow: 180.0,
                    }.get(reason, 90.0)
                    self.profile_manager.mark_failure(profile, reason, cooldown)
                    break

        for fallback in self.fallback_models:
            profile = self.profile_manager.select_available()
            if profile is None:
                break
            try:
                self.stats["attempts"] += 1
                self.sim_fail.fire_if_armed()
                response = invoke(fallback, profile, list(messages))
                self.profile_manager.mark_success(profile)
                self.stats["successes"] += 1
                return response, list(messages), profile.name
            except Exception:
                self.stats["failures"] += 1
                continue

        raise RuntimeError("All profiles and fallback models exhausted.")


# ---------------------------------------------------------------------------
# s10: Named-lane concurrency
# ---------------------------------------------------------------------------


class LaneQueue:
    def __init__(self, name: str, max_concurrency: int = 1) -> None:
        self.name = name
        self.max_concurrency = max(1, max_concurrency)
        self._queue: deque[tuple[Callable[[], Any], concurrent.futures.Future, int]] = deque()
        self._condition = threading.Condition()
        self._active = 0
        self._generation = 0

    def enqueue(self, fn: Callable[[], Any], generation: int | None = None) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._condition:
            gen = self._generation if generation is None else generation
            self._queue.append((fn, future, gen))
            self._pump()
        return future

    def _pump(self) -> None:
        while self._active < self.max_concurrency and self._queue:
            fn, future, gen = self._queue.popleft()
            self._active += 1
            t = threading.Thread(target=self._run_task, args=(fn, future, gen), daemon=True, name=f"lane-{self.name}")
            t.start()

    def _run_task(self, fn: Callable[[], Any], future: concurrent.futures.Future, gen: int) -> None:
        try:
            future.set_result(fn())
        except Exception as exc:
            future.set_exception(exc)
        finally:
            self._task_done(gen)

    def _task_done(self, gen: int) -> None:
        with self._condition:
            self._active -= 1
            if gen == self._generation:
                self._pump()
            self._condition.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            while self._active > 0 or self._queue:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                self._condition.wait(remaining)
            return True

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                "name": self.name,
                "active": self._active,
                "queued": len(self._queue),
                "max_concurrency": self.max_concurrency,
                "generation": self._generation,
            }

    def bump_generation(self) -> int:
        with self._condition:
            self._generation += 1
            return self._generation


class CommandQueue:
    def __init__(self) -> None:
        self._lanes: dict[str, LaneQueue] = {}
        self._lock = threading.Lock()

    def lane(self, name: str, max_concurrency: int = 1) -> LaneQueue:
        with self._lock:
            if name not in self._lanes:
                self._lanes[name] = LaneQueue(name, max_concurrency=max_concurrency)
            return self._lanes[name]

    def enqueue(self, lane_name: str, fn: Callable[[], Any]) -> concurrent.futures.Future:
        return self.lane(lane_name).enqueue(fn)

    def reset_all(self) -> dict[str, int]:
        result: dict[str, int] = {}
        with self._lock:
            for name, lane in self._lanes.items():
                result[name] = lane.bump_generation()
        return result

    def stats(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: lane.stats() for name, lane in self._lanes.items()}

    def wait_all_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            lanes = list(self._lanes.values())
        for lane in lanes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if not lane.wait_idle(remaining):
                return False
        return True


# ---------------------------------------------------------------------------
# Mock LLM (offline) + tool-use loop behavior
# ---------------------------------------------------------------------------


class MockLLM:
    """
    Deterministic model stub:
    - If user says "read <path>" -> tool_use(read_file)
    - "list <dir>" -> tool_use(list_directory)
    - "time" -> tool_use(get_current_time)
    - "remember <text>" -> tool_use(memory_write)
    - "recall <query>" -> tool_use(memory_search)
    - Tool results -> summarize and end_turn
    """

    def __init__(self) -> None:
        self._counter = 0

    def _next_tool_id(self) -> str:
        self._counter += 1
        return f"toolu_{self._counter:06d}"

    @staticmethod
    def _last_user_content(messages: list[dict[str, Any]]) -> Any:
        for msg in reversed(messages):
            if msg["role"] == "user":
                return msg.get("content", "")
        return ""

    def create(
        self,
        model: str,
        profile: AuthProfile,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        _ = (model, profile, system_prompt, tools)
        last = self._last_user_content(messages)
        char_count = sum(len(str(m.get("content", ""))) for m in messages)
        if char_count > 120_000:
            raise RuntimeError("Context window overflow")
        if isinstance(last, list) and last and isinstance(last[0], dict) and last[0].get("type") == "tool_result":
            previews: list[str] = []
            for block in last:
                previews.append(str(block.get("content", ""))[:240])
            text = "Tool results received:\n" + "\n---\n".join(previews)
            return LLMResponse(stop_reason="end_turn", content=[{"type": "text", "text": text}])
        if not isinstance(last, str):
            return LLMResponse(stop_reason="end_turn", content=[{"type": "text", "text": "I can continue."}])
        text = last.strip()
        if text.lower().startswith("read "):
            path = text[5:].strip() or "README.md"
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": self._next_tool_id(),
                        "name": "read_file",
                        "input": {"file_path": path},
                    }
                ],
            )
        if text.lower().startswith("list "):
            directory = text[5:].strip() or "."
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": self._next_tool_id(),
                        "name": "list_directory",
                        "input": {"directory": directory},
                    }
                ],
            )
        if text.lower().startswith("time"):
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": self._next_tool_id(),
                        "name": "get_current_time",
                        "input": {},
                    }
                ],
            )
        if text.lower().startswith("remember "):
            fact = text[9:].strip()
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": self._next_tool_id(),
                        "name": "memory_write",
                        "input": {"content": fact, "category": "user"},
                    }
                ],
            )
        if text.lower().startswith("recall "):
            query = text[7:].strip()
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": self._next_tool_id(),
                        "name": "memory_search",
                        "input": {"query": query, "top_k": 5},
                    }
                ],
            )
        reply = (
            "I am the interview-study gateway. "
            "Try: 'read README.md', 'list .', 'time', 'remember ...', 'recall ...'. "
            "I can also explain routing/retries/lanes with /status."
        )
        return LLMResponse(stop_reason="end_turn", content=[{"type": "text", "text": reply}])


# ---------------------------------------------------------------------------
# s07: Heartbeat + cron services (running through lanes)
# ---------------------------------------------------------------------------


class HeartbeatRunner:
    def __init__(self, root: Path, command_queue: CommandQueue, interval: float = 120.0, active_hours: tuple[int, int] = (8, 23)) -> None:
        self.root = root
        self.heartbeat_path = root / "HEARTBEAT.md"
        self.command_queue = command_queue
        self.interval = interval
        self.active_hours = active_hours
        self.last_run_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._output: list[str] = []
        self._lock = threading.Lock()
        self._last = ""

    def should_run(self) -> tuple[bool, str]:
        if not self.heartbeat_path.exists():
            return False, "HEARTBEAT.md missing"
        if not self.heartbeat_path.read_text(encoding="utf-8").strip():
            return False, "HEARTBEAT.md empty"
        elapsed = time.time() - self.last_run_at
        if elapsed < self.interval:
            return False, f"interval not elapsed ({self.interval - elapsed:.0f}s)"
        hour = datetime.now().hour
        s, e = self.active_hours
        in_hours = (s <= hour < e) if s <= e else not (e <= hour < s)
        if not in_hours:
            return False, "outside active hours"
        return True, "ready"

    def heartbeat_tick(self) -> None:
        ok, _ = self.should_run()
        if not ok:
            return
        lane_stat = self.command_queue.lane("heartbeat").stats()
        if lane_stat["active"] > 0:
            return

        def task() -> str:
            ins = self.heartbeat_path.read_text(encoding="utf-8").strip()[:500]
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return f"{ins}\n\nHeartbeat check at {now}. HEARTBEAT_OK if no urgent issue."

        future = self.command_queue.enqueue("heartbeat", task)

        def done(f: concurrent.futures.Future) -> None:
            self.last_run_at = time.time()
            try:
                out = f.result()
                out = out.replace("HEARTBEAT_OK", "").strip()
                if not out or out == self._last:
                    return
                self._last = out
                with self._lock:
                    self._output.append(out)
            except Exception as exc:
                with self._lock:
                    self._output.append(f"[heartbeat error] {exc}")

        future.add_done_callback(done)

    def start(self) -> None:
        if self._thread:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.heartbeat_tick()
                except Exception:
                    pass
                self._stop.wait(timeout=1.0)

        self._thread = threading.Thread(target=loop, daemon=True, name="heartbeat-loop")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def drain_output(self) -> list[str]:
        with self._lock:
            out = list(self._output)
            self._output.clear()
            return out


class CronService:
    def __init__(self, cron_file: Path, command_queue: CommandQueue) -> None:
        self.cron_file = cron_file
        self.command_queue = command_queue
        self.jobs: list[dict[str, Any]] = []
        self._output: list[str] = []
        self._lock = threading.Lock()
        self.load_jobs()

    def load_jobs(self) -> None:
        self.jobs.clear()
        if not self.cron_file.exists():
            return
        try:
            raw = json.loads(self.cron_file.read_text(encoding="utf-8"))
        except Exception:
            return
        now = time.time()
        for j in raw.get("jobs", []):
            sched = j.get("schedule", {})
            kind = sched.get("kind", "")
            if kind != "every":
                continue
            every = int(sched.get("every_seconds", 0))
            if every <= 0:
                continue
            self.jobs.append(
                {
                    "id": j.get("id", uuid.uuid4().hex[:8]),
                    "name": j.get("name", "cron-job"),
                    "enabled": j.get("enabled", True),
                    "every_seconds": every,
                    "payload": j.get("payload", {}),
                    "next_run_at": now + every,
                    "last_run_at": 0.0,
                    "errors": 0,
                }
            )

    def tick(self) -> None:
        now = time.time()
        for job in self.jobs:
            if not job["enabled"]:
                continue
            if now < job["next_run_at"]:
                continue
            self._enqueue_job(job, now)

    def _enqueue_job(self, job: dict[str, Any], now: float) -> None:
        payload = job.get("payload", {})
        message = payload.get("message", "") or payload.get("text", "")
        if not message:
            job["next_run_at"] = now + job["every_seconds"]
            return

        def task() -> str:
            current = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return f"[scheduled @ {current}] {message}"

        future = self.command_queue.enqueue("cron", task)

        def done(f: concurrent.futures.Future) -> None:
            job["last_run_at"] = time.time()
            job["next_run_at"] = time.time() + job["every_seconds"]
            status = "ok"
            output_preview = ""
            error_msg = ""
            try:
                result = f.result()
                output_preview = str(result)[:160]
                with self._lock:
                    self._output.append(f"[{job['name']}] {result}")
                job["errors"] = 0
            except Exception as exc:
                status = "error"
                error_msg = str(exc)
                job["errors"] += 1
                with self._lock:
                    self._output.append(f"[{job['name']}] error: {exc}")
                if job["errors"] >= 5:
                    job["enabled"] = False
            entry = {
                "job_id": job["id"],
                "run_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "output_preview": output_preview,
            }
            if error_msg:
                entry["error"] = error_msg
            with open(CRON_RUN_LOG, "a", encoding="utf-8") as f2:
                f2.write(json.dumps(entry, ensure_ascii=False) + "\n")

        future.add_done_callback(done)
        job["next_run_at"] = now + job["every_seconds"]

    def drain_output(self) -> list[str]:
        with self._lock:
            out = list(self._output)
            self._output.clear()
            return out


# ---------------------------------------------------------------------------
# Gateway core
# ---------------------------------------------------------------------------


class InterviewGateway:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.command_queue = CommandQueue()
        self.command_queue.lane("main", 1)
        self.command_queue.lane("heartbeat", 1)
        self.command_queue.lane("cron", 1)

        self.tool_registry = ToolRegistry(root)
        self.session_store = SessionStore(SESSIONS_DIR)
        self.context_guard = ContextGuard()
        self.memory = MemoryStore(root)
        self.prompt_builder = PromptBuilder(root)
        self.skills = SkillsManager(root)
        self.skills.discover()

        self._install_memory_tools()

        self.bindings = BindingTable()
        self._install_demo_bindings()
        self.dm_scopes = {"main": "per-channel-peer", "sage": "per-peer"}

        self.profiles = ProfileManager(
            [
                AuthProfile("main-key", os.getenv("ANTHROPIC_API_KEY", "demo-key")),
                AuthProfile("backup-key", os.getenv("ANTHROPIC_API_KEY", "demo-key")),
                AuthProfile("emergency-key", os.getenv("ANTHROPIC_API_KEY", "demo-key")),
            ]
        )
        self.sim_failure = SimulatedFailure()
        self.resilience = ResilienceRunner(
            profile_manager=self.profiles,
            context_guard=self.context_guard,
            simulated_failure=self.sim_failure,
        )

        self.model = MockLLM()
        self.messages_cache: dict[str, list[dict[str, Any]]] = {}

        self.delivery_queue = DeliveryQueue(DELIVERY_DIR)
        self.delivery_runner = DeliveryRunner(self.delivery_queue, self._deliver_mock)
        self.delivery_runner.start()

        self.heartbeat = HeartbeatRunner(root, self.command_queue, interval=120.0)
        self.heartbeat.start()

        self.cron = CronService(root / "CRON.json", self.command_queue)
        self._cron_stop = threading.Event()
        self._cron_thread = threading.Thread(target=self._cron_loop, daemon=True, name="cron-tick")
        self._cron_thread.start()

    def _install_memory_tools(self) -> None:
        self.tool_registry.register(
            name="memory_write",
            description="Save memory entry.",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["content"],
            },
            handler=lambda content, category="general": self.memory.write_memory(content, category),
        )
        self.tool_registry.register(
            name="memory_search",
            description="Search memory entries.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=lambda query, top_k=5: self._memory_search_tool(query, top_k),
        )

    def _memory_search_tool(self, query: str, top_k: int = 5) -> str:
        hits = self.memory.search(query, top_k=top_k)
        if not hits:
            return "No relevant memories."
        return "\n".join(f"[{h['path']}] ({h['score']}) {h['snippet']}" for h in hits)

    def _install_demo_bindings(self) -> None:
        self.bindings.add(Binding(agent_id="main", tier=5, match_key="default", match_value="*"))
        self.bindings.add(Binding(agent_id="sage", tier=4, match_key="channel", match_value="telegram", priority=1))
        self.bindings.add(Binding(agent_id="sage", tier=1, match_key="peer_id", match_value="discord:admin-001", priority=10))

    def _cron_loop(self) -> None:
        while not self._cron_stop.is_set():
            try:
                self.cron.tick()
            except Exception:
                pass
            self._cron_stop.wait(timeout=1.0)

    def shutdown(self) -> None:
        self.heartbeat.stop()
        self._cron_stop.set()
        if self._cron_thread.is_alive():
            self._cron_thread.join(timeout=2.0)
        self.delivery_runner.stop()
        self.command_queue.wait_all_idle(timeout=3.0)

    def _deliver_mock(self, channel: str, to: str, text: str) -> None:
        preview = text.replace("\n", " ")[:120]
        show_delivery(f"{channel}->{to}: {preview}")

    def _resolve(self, inbound: InboundMessage) -> tuple[str, str]:
        agent_id, matched = self.bindings.resolve(
            channel=inbound.channel,
            account_id=inbound.account_id,
            peer_id=inbound.peer_id,
        )
        if not agent_id:
            agent_id = "main"
        if matched:
            info(f"[route] tier={matched.tier} {matched.match_key}={matched.match_value} -> {matched.agent_id}")
        dm_scope = self.dm_scopes.get(agent_id, "per-channel-peer")
        sk = build_session_key(
            agent_id=agent_id,
            channel=inbound.channel,
            account_id=inbound.account_id,
            peer_id=inbound.peer_id,
            dm_scope=dm_scope,
        )
        return agent_id, sk

    def _get_messages(self, session_key: str) -> list[dict[str, Any]]:
        if session_key not in self.messages_cache:
            self.messages_cache[session_key] = self.session_store.load_messages(session_key)
        return self.messages_cache[session_key]

    def _save_user(self, session_key: str, text: str) -> None:
        self.session_store.append(
            session_key,
            {"type": "user", "content": text, "ts": time.time()},
        )

    def _save_assistant(self, session_key: str, content: list[dict[str, Any]]) -> None:
        self.session_store.append(
            session_key,
            {"type": "assistant", "content": content, "ts": time.time()},
        )

    def _save_tool_exchange(self, session_key: str, block: dict[str, Any], result: str) -> None:
        ts = time.time()
        self.session_store.append(
            session_key,
            {
                "type": "tool_use",
                "tool_use_id": block["id"],
                "name": block["name"],
                "input": block["input"],
                "ts": ts,
            },
        )
        self.session_store.append(
            session_key,
            {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": result,
                "ts": ts,
            },
        )

    def _invoke_model(
        self,
        model_name: str,
        profile: AuthProfile,
        system_prompt: str,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        return self.model.create(
            model=model_name,
            profile=profile,
            system_prompt=system_prompt,
            messages=messages,
            tools=self.tool_registry.schemas,
        )

    def run_turn(self, inbound: InboundMessage) -> tuple[str, dict[str, Any]]:
        agent_id, session_key = self._resolve(inbound)
        messages = self._get_messages(session_key)
        messages.append({"role": "user", "content": inbound.text})
        self._save_user(session_key, inbound.text)

        token_est = self.context_guard.estimate_messages_tokens(messages)
        if token_est > self.context_guard.max_tokens:
            messages[:] = self.context_guard.truncate_tool_results(messages)
            messages[:] = self.context_guard.compact_history(messages)

        bootstrap = self.prompt_builder.load_bootstrap(mode="full")
        recall = self.memory.recall_block(inbound.text, top_k=3)
        skills_block = self.skills.format_prompt_block()
        system_prompt = self.prompt_builder.build(
            bootstrap=bootstrap,
            skills_block=skills_block,
            memory_recall=recall,
            agent_id=agent_id,
            channel=inbound.channel,
            mode="full",
        )

        final_text = ""
        profile_used = ""
        for _ in range(12):
            response, updated_messages, profile_used = self.resilience.call(
                messages=messages,
                invoke=lambda model_name, profile, msgs: self._invoke_model(model_name, profile, system_prompt, msgs),
            )
            messages[:] = updated_messages
            messages.append({"role": "assistant", "content": response.content})
            self._save_assistant(session_key, response.content)

            if response.stop_reason == "end_turn":
                final_text = extract_text(response.content)
                break
            if response.stop_reason != "tool_use":
                final_text = extract_text(response.content) or f"[stop_reason={response.stop_reason}]"
                break

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.get("type") != "tool_use":
                    continue
                result = self.tool_registry.call(block["name"], block.get("input", {}))
                self._save_tool_exchange(session_key, block, result)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            final_text = "[max tool iterations reached]"

        for chunk in chunk_message(final_text, inbound.channel):
            self.delivery_queue.enqueue(inbound.channel, inbound.peer_id, chunk)

        meta = {
            "agent_id": agent_id,
            "session_key": session_key,
            "profile": profile_used,
            "token_estimate": token_est,
            "memory_recall": bool(recall),
        }
        return final_text, meta

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "lanes": self.command_queue.stats(),
            "profiles": self.profiles.status_rows(),
            "resilience": dict(self.resilience.stats),
            "delivery_pending": len(self.delivery_queue.load_pending()),
            "delivery_failed": len(self.delivery_queue.load_failed()),
            "sessions_cached": len(self.messages_cache),
            "skills": len(self.skills.skills),
            "heartbeat": self.heartbeat.should_run()[1],
            "cron_jobs": len(self.cron.jobs),
        }


# ---------------------------------------------------------------------------
# REPL commands
# ---------------------------------------------------------------------------


def print_help() -> None:
    info("Commands:")
    info("  /help                         Show commands")
    info("  /send <channel> <peer> <txt>  Send message via channel for routing demo")
    info("  /bindings                     Show routing bindings")
    info("  /profiles                     Show profile pool + cooldown")
    info("  /simulate-failure <reason>    Arm next API call failure")
    info("  /lanes                        Show named lane stats")
    info("  /reset-lanes                  Bump lane generations")
    info("  /queue                        Show pending deliveries")
    info("  /failed                       Show failed deliveries")
    info("  /retry-failed                 Move failed -> pending")
    info("  /status                       Show full system snapshot")
    info("  /heartbeat                    Show heartbeat status")
    info("  /trigger-heartbeat            Force heartbeat tick")
    info("  /cron                         List cron jobs")
    info("  quit / exit                   Exit")
    info("")
    info("Try these messages:")
    info("  read README.md")
    info("  list .")
    info("  time")
    info("  remember I prefer concise answers")
    info("  recall concise")


def print_bindings(gw: InterviewGateway) -> None:
    for b in gw.bindings.list_all():
        info(f"tier={b.tier} {b.match_key}={b.match_value} -> {b.agent_id} (pri={b.priority})")


def print_queue(gw: InterviewGateway) -> None:
    pending = gw.delivery_queue.load_pending()
    if not pending:
        info("No pending deliveries.")
        return
    now = time.time()
    for e in pending:
        wait_seconds = max(0.0, e.next_retry_at - now)
        wait_part = f" wait={wait_seconds:.0f}s" if wait_seconds > 0 else ""
        preview = e.text[:50].replace("\n", " ")
        info(f"{e.id[:8]}... ch={e.channel} to={e.to} retry={e.retry_count}{wait_part} text={preview}")


def print_failed(gw: InterviewGateway) -> None:
    failed = gw.delivery_queue.load_failed()
    if not failed:
        info("No failed deliveries.")
        return
    for e in failed:
        info(f"{e.id[:8]}... retries={e.retry_count} err={e.last_error or '-'}")


def print_lanes(gw: InterviewGateway) -> None:
    stats = gw.command_queue.stats()
    if not stats:
        info("No lanes.")
        return
    for name, st in stats.items():
        info(f"{name:10s} active={st['active']} queued={st['queued']} max={st['max_concurrency']} gen={st['generation']}")


def print_cron_jobs(gw: InterviewGateway) -> None:
    if not gw.cron.jobs:
        info("No cron jobs loaded.")
        return
    now = time.time()
    for j in gw.cron.jobs:
        nxt = max(0.0, j["next_run_at"] - now)
        info(f"[{'ON' if j['enabled'] else 'OFF'}] {j['id']} {j['name']} every={j['every_seconds']}s next_in={nxt:.0f}s errors={j['errors']}")


def drain_background_outputs(gw: InterviewGateway) -> None:
    for msg in gw.heartbeat.drain_output():
        show_lane("heartbeat", msg)
    for msg in gw.cron.drain_output():
        show_lane("cron", msg)


def repl() -> None:
    gw = InterviewGateway(ROOT)
    info("=" * 72)
    info("Interview Study Gateway")
    info("A compact, runnable architecture scaffold for agent gateway interviews.")
    info("Model: MockLLM (offline).")
    info("Type /help for commands.")
    info("=" * 72)

    default_channel = "cli"
    default_account = "cli-local"
    default_peer = "cli-user"
    default_sender = "cli-user"

    try:
        while True:
            drain_background_outputs(gw)
            try:
                user = input(f"{CYAN}{BOLD}You > {RESET}").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if not user:
                continue
            if user.lower() in ("quit", "exit"):
                break

            if user.startswith("/"):
                parts = user.split(maxsplit=3)
                cmd = parts[0].lower()
                if cmd == "/help":
                    print_help()
                elif cmd == "/bindings":
                    print_bindings(gw)
                elif cmd == "/profiles":
                    for row in gw.profiles.status_rows():
                        info(row)
                elif cmd == "/simulate-failure":
                    if len(parts) < 2:
                        info("Usage: /simulate-failure <reason>")
                    else:
                        info(gw.sim_failure.arm(parts[1]))
                elif cmd == "/lanes":
                    print_lanes(gw)
                elif cmd == "/reset-lanes":
                    result = gw.command_queue.reset_all()
                    for name, gen in result.items():
                        info(f"{name} -> generation {gen}")
                elif cmd == "/queue":
                    print_queue(gw)
                elif cmd == "/failed":
                    print_failed(gw)
                elif cmd == "/retry-failed":
                    moved = gw.delivery_queue.retry_failed()
                    info(f"Moved {moved} failed entries back to queue.")
                elif cmd == "/status":
                    snap = gw.status_snapshot()
                    info(json.dumps(snap, indent=2, ensure_ascii=False))
                elif cmd == "/heartbeat":
                    ok, reason = gw.heartbeat.should_run()
                    info(f"heartbeat should_run={ok}, reason={reason}")
                elif cmd == "/trigger-heartbeat":
                    gw.heartbeat.heartbeat_tick()
                    time.sleep(0.2)
                    drain_background_outputs(gw)
                elif cmd == "/cron":
                    print_cron_jobs(gw)
                elif cmd == "/send":
                    if len(parts) < 4:
                        info("Usage: /send <channel> <peer> <text>")
                        continue
                    ch = parts[1]
                    peer = parts[2]
                    text = parts[3]
                    inbound = InboundMessage(
                        text=text,
                        channel=ch,
                        account_id=default_account,
                        peer_id=peer,
                        sender_id=default_sender,
                    )
                    show_lane("main", "processing...")
                    fut = gw.command_queue.enqueue("main", lambda i=inbound: gw.run_turn(i))
                    reply, meta = fut.result(timeout=60)
                    show_agent(reply)
                    info(f"meta: {meta}")
                else:
                    info(f"Unknown command: {cmd}")
                continue

            inbound = InboundMessage(
                text=user,
                channel=default_channel,
                account_id=default_account,
                peer_id=default_peer,
                sender_id=default_sender,
            )
            show_lane("main", "processing...")
            future = gw.command_queue.enqueue("main", lambda i=inbound: gw.run_turn(i))
            try:
                reply, meta = future.result(timeout=60)
                if reply:
                    show_agent(reply)
                info(f"meta: {meta}")
            except concurrent.futures.TimeoutError:
                warn("Request timed out.")
            except Exception as exc:
                err(f"Error: {exc}")
    finally:
        gw.shutdown()
        info("Goodbye.")


if __name__ == "__main__":
    repl()
