"""Re-render saved report answers locally; no model calls or live data changes."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent.evidence_publication import render_html
from app.agent.html_sanitizer import sanitize_report_html


def main():
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    titles = {'R01': '接地电缆推荐表分析报告', 'R02': '损坏文件扫描报告汇总统计', 'T03': '市区与福清2022年计划投产投资对比'}
    for case, title in titles.items():
        result = json.loads((source / case / 'result.json').read_text(encoding='utf-8'))
        text = re.sub(r'\[(?:查看报告|下载 PDF)\]\(/api/v1/[^)]+\)', '', result['answer']).rstrip(' ·\n')
        target = output / (case + '.html')
        target.write_text(sanitize_report_html(render_html(text, title)), encoding='utf-8')
        print(str(target))


if __name__ == '__main__':
    main()
