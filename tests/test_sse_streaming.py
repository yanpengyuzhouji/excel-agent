import json
import unittest

from app.api.endpoints import _matches_model_config, _sse


class SseStreamingTests(unittest.TestCase):
    def test_sse_padding_keeps_event_json_parseable(self):
        payload = _sse({"type": "status", "message": "模型正在分析"})
        lines = payload.splitlines()

        self.assertTrue(lines[0].startswith(": stream-flush"))
        self.assertGreaterEqual(len(lines[0]), 2048)
        event = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        self.assertEqual("status", event["type"])

    def test_model_alias_matches_canonical_vllm_config(self):
        config = {
            "name": "Qwen3-14B-AWQ",
            "model": "/data/models/Qwen/Qwen3-14B-AWQ",
            "aliases": ["local-vllm-18002"],
        }

        self.assertTrue(_matches_model_config(config, "Qwen3-14B-AWQ"))
        self.assertTrue(_matches_model_config(config, "/data/models/Qwen/Qwen3-14B-AWQ"))
        self.assertTrue(_matches_model_config(config, "local-vllm-18002"))
        self.assertFalse(_matches_model_config(config, "Qwen3-14B"))


if __name__ == "__main__":
    unittest.main()
