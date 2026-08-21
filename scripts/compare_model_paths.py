"""Run the same Excel questions through multiple configured models.

This intentionally exercises the public SSE endpoint instead of calling the
LLM clients directly, so prompt assembly, ReAct parsing, SQL tools, report
rendering, and termination are all measured together.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List
from urllib.request import Request, urlopen


DEFAULT_QUESTIONS = [
    "请查询当前Excel共有多少条记录，并直接回答数量，不生成报告。",
    "请对当前Excel生成一份完整的物料分析报告，统计各单元数量、主要物料和数据完整性。",
]


def run_once(
    base_url: str,
    workbook_id: str,
    model: str,
    question: str,
    analysis_route: str | None = None,
) -> Dict[str, Any]:
    request_payload = {"question": question, "model": model, "max_rows": 500}
    if analysis_route:
        request_payload["analysis_route"] = analysis_route
    payload = json.dumps(
        request_payload,
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}/workbooks/{workbook_id}/ask",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    started = time.perf_counter()
    actions: List[str] = []
    sqls: List[str] = []
    reports: List[Dict[str, Any]] = []
    done: Dict[str, Any] = {}
    errors: List[str] = []
    status = None
    try:
        with urlopen(request, timeout=360) as response:
            status = response.status
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "step.start":
                    actions.append(str(event.get("title", "")))
                elif event_type == "sql":
                    sqls.append(str(event.get("sql", "")))
                elif event_type == "html":
                    reports.append(
                        {
                            "title": event.get("title"),
                            "report_id": event.get("report_id"),
                            "url": event.get("url"),
                        }
                    )
                elif event_type == "done":
                    done = {
                        "answer": event.get("full_answer", ""),
                        "report_url": event.get("report_url"),
                        "report_title": event.get("report_title"),
                    }
                elif event_type == "error":
                    errors.append(str(event.get("error", "")))
    except Exception as exc:  # pragma: no cover - exercised against live service
        errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "model": model,
        "question": question,
        "http_status": status,
        "seconds": round(time.perf_counter() - started, 3),
        "actions": actions,
        "sql_count": len(sqls),
        "sql": sqls,
        "report_count": len(reports),
        "reports": reports,
        "done": done,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://192.168.0.7:8090/api/v1")
    parser.add_argument("--workbook-id", default="077b05356a8d40b195cfcd9043522514")
    parser.add_argument(
        "--models",
        nargs="+",
        default=[
            "deepseek-v4-flash",
            "Qwen3-4B",
            "Qwen3-14B-AWQ",
            "Qwen3-VL-8B-Instruct",
            "Qwen3-14B",
            "Qwen3-30B-A3B-Instruct-2507",
            "Qwen3-Coder-30B-A3B-Instruct",
        ],
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--analysis-route",
        choices=("large", "small"),
        help="Force the same analysis route for every selected model.",
    )
    parser.add_argument(
        "--question",
        action="append",
        help="Run only this question; may be supplied more than once.",
    )
    args = parser.parse_args()

    questions = args.question or DEFAULT_QUESTIONS
    for question in questions:
        for repeat in range(1, max(1, args.repeat) + 1):
            for model in args.models:
                result = run_once(
                    args.base_url,
                    args.workbook_id,
                    model,
                    question,
                    analysis_route=args.analysis_route,
                )
                result["repeat"] = repeat
                print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
