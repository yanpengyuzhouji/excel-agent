"""Test ModelScope Qwen3-VL against the current workbook and save an HTML report.

The script deliberately keeps the ModelScope token outside the repository. It
reads MODELSCOPE_API_KEY from the process environment first, then from the
RongNengRAG .env used by the local Windows setup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.tools import html_interpreter


MODEL = "Qwen/Qwen3-VL-8B-Instruct"
BASE_URL = "https://api-inference.modelscope.cn/v1/chat/completions"
DEFAULT_WORKBOOK = "077b05356a8d40b195cfcd9043522514"


def _load_modelscope_key() -> str:
    key = os.environ.get("MODELSCOPE_API_KEY", "").strip()
    if key:
        return key
    candidates = [
        Path("/mnt/d/git/RongNengRAG/.env"),
        Path("D:/git/RongNengRAG/.env"),
    ]
    for path in candidates:
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                if raw.startswith("MODELSCOPE_API_KEY="):
                    return raw.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    raise RuntimeError("MODELSCOPE_API_KEY is not configured")


def _chat(api_key: str, messages: list[dict[str, str]], max_tokens: int) -> str:
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": max_tokens,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        BASE_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=300) as response:
        result = json.loads(response.read().decode("utf-8"))
    choice = result.get("choices", [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or choice.get("text") or ""
    return str(content).strip()


def _database_context(database: Path) -> dict:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(sheet1)")]
        rows = [
            dict(zip(columns, row))
            for row in connection.execute(
                "SELECT column_a, column_b, column_c FROM sheet1 ORDER BY _source_row"
            )
        ]
        total_rows = connection.execute("SELECT COUNT(*) FROM sheet1").fetchone()[0]
        units = {
            "进线单元": "column_a",
            "计量单元": "column_b",
            "出现单元": "column_c",
        }
        completeness = []
        frequencies = []
        for unit, column in units.items():
            total, non_empty, distinct = connection.execute(
                f"SELECT COUNT(*), COUNT({column}), COUNT(DISTINCT {column}) FROM sheet1"
            ).fetchone()
            completeness.append(
                {
                    "unit": unit,
                    "total_rows": total,
                    "non_empty_count": non_empty,
                    "null_count": total - non_empty,
                    "distinct_count": distinct,
                }
            )
            for material, count in connection.execute(
                f"SELECT {column}, COUNT(*) FROM sheet1 "
                f"WHERE {column} IS NOT NULL GROUP BY {column} "
                "ORDER BY COUNT(*) DESC, CAST(" + column + " AS TEXT)"
            ):
                frequencies.append(
                    {"unit": unit, "material": material, "occurrence_count": count}
                )
        return {
            "table": "sheet1",
            "column_mapping": units,
            "total_rows": total_rows,
            "completeness": completeness,
            "frequencies": frequencies,
            "rows": rows,
        }
    finally:
        connection.close()


def _extract_sql(text: str) -> str:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    candidate = fenced.group(1).strip() if fenced else text.strip()
    match = re.search(r"\b(WITH|SELECT)\b.*", candidate, re.IGNORECASE | re.DOTALL)
    if not match:
        return candidate
    candidate = match.group(0).strip()
    return candidate.split(";", 1)[0].strip() + ";"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook-id", default=DEFAULT_WORKBOOK)
    args = parser.parse_args()

    database = PROJECT_ROOT / "data" / "workbook_imports" / args.workbook_id / "v1" / "database.sqlite"
    if not database.exists():
        raise SystemExit(f"Workbook database not found: {database}")
    api_key = _load_modelscope_key()
    context = _database_context(database)

    sql_answer = _chat(
        api_key,
        [
            {
                "role": "system",
                "content": (
                    "你是只读SQLite SQL生成器。只输出一条可执行的SELECT或WITH SQL，"
                    "不要解释，不要Markdown代码围栏。必须保留每个单元的字段含义。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "当前表 sheet1 有字段 column_a=进线单元、column_b=计量单元、"
                    "column_c=出现单元。请生成SQL，统计总行数、每个单元的总数/非空数/空值数/"
                    "去重数，并统计每个单元的物料频次。"
                ),
            },
        ],
        2048,
    )

    report_prompt = (
        "请基于下面已经由只读SQL得到的真实结果，生成一份专业的中文物料分析报告。"
        "只输出完整、可直接渲染的HTML，不要Markdown代码围栏，不要解释文字。"
        "必须包含DOCTYPE、html、head、body，并在head中使用一个完整的<style>块。"
        "CSS使用自包含样式，不要url()、@import、外部资源或JavaScript。"
        "页面需要有标题、摘要卡片、各单元完整性统计表、物料频次表、数据质量结论。"
        "只能使用下方结果中的数字，不得臆造；注意源字段名‘出现单元’必须原样保留。\n\n"
        "真实SQL结果：\n" + json.dumps(context, ensure_ascii=False, indent=2)
    )
    started = time.perf_counter()
    html = _chat(
        api_key,
        [
            {
                "role": "system",
                "content": "你是严谨的HTML数据报告生成器，输出必须是完整HTML文档。",
            },
            {"role": "user", "content": report_prompt},
        ],
        16384,
    )
    elapsed = time.perf_counter() - started
    html = re.sub(r"^```(?:html)?\s*|\s*```$", "", html, flags=re.IGNORECASE | re.DOTALL).strip()

    storage = PROJECT_ROOT / "data" / "workbook_imports"
    service = SimpleNamespace(
        config=SimpleNamespace(resolved_storage_path=lambda: str(storage))
    )
    report = html_interpreter(
        service,
        args.workbook_id,
        html,
        "Qwen3-VL-8B-Instruct 物料分析报告",
    )
    if "error" in report:
        raise SystemExit(report["error"])

    print(json.dumps({
        "model": MODEL,
        "sql_probe": sql_answer,
        "total_rows": context["total_rows"],
        "report_url": report["url"],
        "report_path": report["path"],
        "report_chars": len(html),
        "css_generated": "<style" in html.lower(),
        "elapsed_seconds": round(elapsed, 2),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
