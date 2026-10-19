"""Run the existing suite and persist an honest, machine-readable acceptance."""
import io
import json
import logging
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

logging.getLogger().handlers=[logging.StreamHandler(io.StringIO())]
logging.getLogger().setLevel(logging.CRITICAL)
suite=unittest.defaultTestLoader.discover('tests')
stream=io.StringIO()
result=unittest.TextTestRunner(stream=stream,verbosity=1).run(suite)
report={'tests_run':result.testsRun,'passed':result.wasSuccessful(),
        'failures':[{'id':t.id(),'trace':v} for t,v in result.failures],
        'errors':[{'id':t.id(),'trace':v} for t,v in result.errors],
        'skipped':[{'id':t.id(),'reason':v} for t,v in result.skipped]}
target=Path(sys.argv[1]);target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(stream.getvalue())
raise SystemExit(0 if result.wasSuccessful() else 1)
