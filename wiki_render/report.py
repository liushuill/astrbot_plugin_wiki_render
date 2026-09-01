"""渲染日志/报告（JSONL）+ 统计（对应需求 §10.3）。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_LINES = 2000


class RenderReport:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, **fields) -> None:
        fields.setdefault("ts", time.time())
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(fields, ensure_ascii=False) + "\n")
                if self.path.stat().st_size > _MAX_FILE_BYTES:
                    self._trim()
            except OSError:
                pass

    def _trim(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            with self.path.open("w", encoding="utf-8") as f:
                f.write("\n".join(lines[-_MAX_LINES:]) + "\n")
        except OSError:
            pass

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            out = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
            return out
        except OSError:
            return []

    def stats(self) -> dict:
        # 只统计渲染记录（kind != audit），审计记录不污染渲染统计
        records = [r for r in self.read() if r.get("kind") != "audit"]
        n = len(records)
        ok = [r for r in records if r.get("ok")]
        fails = n - len(ok)
        avg = round(sum(r.get("duration", 0) for r in ok) / len(ok), 2) if ok else 0.0
        slowest = sorted(ok, key=lambda r: r.get("duration", 0), reverse=True)[:5]
        return {
            "total": n,
            "ok": len(ok),
            "failed": fails,
            "avg_duration": avg,
            "slowest": [
                {"page": r.get("page", ""), "duration": r.get("duration", 0)}
                for r in slowest
            ],
            "last": records[-5:],
        }
