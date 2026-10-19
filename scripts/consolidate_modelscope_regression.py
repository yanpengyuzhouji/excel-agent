"""Consolidate saved ModelScope results, replacing only rate-limited attempts."""

from collections import Counter
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/business_acceptance"
RUNS = {
    "numeric": [
        "numeric-gate-v1/runs/Qwen38-27B-ModelScope-20261017-195148",
        "numeric-gate-v1/runs/Qwen38-27B-ModelScope-20261018-130924",
    ],
    "business": [
        "gold-v2/runs/Qwen38-27B-ModelScope-20261017-195216",
        "gold-v2/runs/Qwen38-27B-ModelScope-20261018-131010",
        "gold-v2/runs/Qwen38-27B-ModelScope-20261019-085456",
    ],
}
REVIEWS = {
    ("numeric", "C04"): ("execution_failure", "执行失败", "已查询元合计，但连续把/10000写进结果引用；纠正三次后引用校验失败，未完成万元换算回答。"),
    ("numeric", "A05"): ("pass_with_unit_risk", "初筛通过／单位风险", "核心加权计算正确；自行补充了来源未证明的“件、元/件”单位。"),
    ("business", "G009-Q2"): ("precision_deviation", "精度偏差", "原值12678.81142被自行舍入为12678.8114万元，未保留题目要求的原始结果精度。"),
    ("business", "C22"): ("incomplete_answer", "内容遗漏", "只列分区域比较，遗漏13区域整体两期总额、整体差额与增长率，原始表金额也未统一4位。"),
    ("business", "T03"): ("manual_pass", "人工复核通过", "数值、2022两区域口径、HTML及PDF下载正确；原题未强制表格，评分器额外要求表格造成误报。"),
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    records = []
    images = set()
    for suite, paths in RUNS.items():
        selected = {}
        for relative in paths:
            folder = DATA / relative
            report = read(folder / "acceptance.json")
            assert report["model"] == "Qwen3.8-27B-ModelScope"
            assert report["image_before"] == report["image_after"]
            images.add(report["image_before"])
            for question in report["questions"]:
                key = question["id"]
                if key in selected:
                    previous = selected[key][1]
                    assert (previous.get("error") or {}).get("error_code") == "rate_limited", key
                selected[key] = (folder, question)
        assert len(selected) == (36 if suite == "numeric" else 64)
        for key, (folder, question) in selected.items():
            assert (question.get("error") or {}).get("error_code") != "rate_limited", key
            review = REVIEWS.get((suite, key), ("auto_pass", "自动初筛通过", ""))
            if review[0] == "auto_pass":
                assert question["status"].startswith("auto_pass"), key
            artifacts = {
                name: str((folder / key / name).resolve())
                for name in ("result.json", "events.json", "report.html", "report.pdf", "preview.png")
                if (folder / key / name).is_file()
            }
            records.append({
                "suite": suite, "id": key, "question": question["question"],
                "original_status": question["status"],
                "review_status": review[0], "review_label": review[1], "review_note": review[2],
                "source_run": str(folder.resolve()), "artifacts": artifacts,
                "final_result": question,
            })
    assert len(images) == 1
    assert len({(r["suite"], r["id"]) for r in records}) == 100
    output = DATA / "modelscope-qwen38-27b-consolidated-20261019"
    output.mkdir(exist_ok=True)
    user_retests = output / "user-retests.json"
    overrides = read(user_retests)["results"]
    index = {(r["suite"], r["id"]): r for r in records}
    assert len({(r["suite"], r["id"]) for r in overrides}) == len(overrides)
    for item in overrides:
        record = index[(item["suite"], item["id"])]
        record.update({k: item[k] for k in ("review_status", "review_label", "review_note")})
        record["result_origin"] = "user_reported_manual_retest"
        record["user_retest_evidence"] = str(user_retests.resolve())
        record["final_result"] = {
            "id": item["id"], "question": record["question"], "answer": item["answer"],
            "status": "user_reported_answer", "error": None,
            "evidence_scope": "Final answer provided by user; no execution trace supplied.",
        }
    statuses = dict(Counter(r["review_status"] for r in records))
    original = dict(Counter(r["original_status"] for r in records))
    assert statuses == {"auto_pass": 95, "user_retest_pass": 3,
                        "precision_deviation": 1, "manual_pass": 1}
    manifest = {
        "model": "Qwen/Qwen3.8-27B", "selector": "Qwen3.8-27B-ModelScope",
        "service_version": "1.2.0-rc.39", "image_id": next(iter(images)),
        "coverage": {"numeric": 36, "business": 64, "total": 100},
        "method": "Use designated follow-ups for rate-limited attempts, then replace four current answers with user-authorized manual retest reports. Manual decisions assess the supplied final answer only. Historical raw evidence is retained separately; this is consolidated coverage across dates and evidence types.",
        "user_retest_source": str(user_retests.resolve()),
        "original_status_summary": original, "review_status_summary": statuses,
        "records": records,
    }
    (output / "results.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 魔搭 Qwen3.8-27B 完整测试记录", "",
        "整理日期：2026-10-19；服务RC39；共100题（数字36、业务64）。采用后续有效复测替换调用受限结果，原始执行文件保留。这是跨日合并记录。", "",
        "## 结果概览", "",
        "| 结果 | 数量 |", "|---|---:|", "| 自动初筛通过、无已记录附加问题 | 95 |",
        "| 用户手工复测通过 | 3 |", "| 人工复核接受 | 1 |", "| 精度丢失 | 1 |", "",
        "当前99题通过或接受、1题精度问题。其中C04、A05、C22采用用户提供的最新正确回答；手工判定仅核对最终回答，未收到相应工具轨迹。历史自动结果保留于原执行文件。", "",
        "## 问题简述", "",
    ]
    for r in records:
        if r["review_status"] == "precision_deviation":
            lines.append(f"- 业务 {r['id']}（{r['review_label']}）：{r['review_note']}")
    lines += ["", "C04、A05、C22已按最新手工回答更新为通过，当前问题清单仅保留上述精度丢失。", "",
              "## 全部题目索引", "", "| 类别 | 题号 | 结果 | 执行批次 |", "|---|---|---|---|"]
    for r in records:
        origin = "用户提供的手工复测" if r.get("result_origin") else Path(r["source_run"]).name
        lines.append(f"| {'数字' if r['suite'] == 'numeric' else '业务'} | {r['id']} | {r['review_label']} | {origin} |")
    lines += ["", "## 逐题执行记录", ""]
    for r in records:
        lines += [f"### {'数字' if r['suite'] == 'numeric' else '业务'} · {r['id']} · {r['review_label']}", "",
                  "问题：" + r["question"], ""]
        answer = r["final_result"].get("answer") or "（未生成最终回答）"
        # Test conversations are cleaned up; link retained report files instead.
        for title, name in (("查看报告", "report.html"), ("下载 PDF", "report.pdf")):
            pattern = r"\[" + re.escape(title) + r"\]\(/api/v1/agent/conversations/[^)]+\)"
            artifact = r["artifacts"].get(name)
            answer = re.sub(pattern, lambda _: f"[{title}]({Path(artifact).as_posix()})" if artifact else "", answer)
        lines += ["回答：", "", answer, ""]
        error = r["final_result"].get("error")
        if error:
            lines += [f"错误：`{error.get('error_code', 'unknown')}` — {error.get('error', '')}", ""]
        if r["review_note"]:
            lines += ["复核：" + r["review_note"], ""]
        result_path = r["artifacts"].get("result.json")
        if r.get("user_retest_evidence"):
            lines += [f"[本次手工复测记录]({Path(r['user_retest_evidence']).as_posix()})", ""]
        if result_path:
            label = "历史执行证据" if r.get("result_origin") else "执行证据"
            lines += [f"[{label}]({Path(result_path).as_posix()})", ""]
    (output / "完整测试记录.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"output": str(output), "coverage": manifest["coverage"], "summary": statuses}, ensure_ascii=False))


if __name__ == "__main__":
    main()
