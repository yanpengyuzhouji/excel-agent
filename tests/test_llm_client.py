import unittest
import os
from types import SimpleNamespace
from unittest.mock import patch

from app.service.llm import ExcelLLM


class ExcelLlmClientTests(unittest.TestCase):
    @patch("openai.OpenAI")
    def test_provider_timeout_and_retry_budget_are_forwarded(self, openai_client):
        ExcelLLM(
            {
                "base_url": "https://example.invalid/v1",
                "model": "deepseek-v4-flash",
                "api_key": "test-key",
                "timeout": 60,
                "max_retries": 0,
            }
        )

        openai_client.assert_called_once_with(
            base_url="https://example.invalid/v1",
            api_key="test-key",
            timeout=60.0,
            max_retries=0,
        )

    @patch("openai.OpenAI")
    def test_missing_retry_setting_defaults_to_fail_fast(self, openai_client):
        ExcelLLM({"api_key": "test-key"})

        self.assertEqual(0, openai_client.call_args.kwargs["max_retries"])
        self.assertEqual(90.0, openai_client.call_args.kwargs["timeout"])

    @patch("openai.OpenAI")
    @patch.dict(os.environ, {"BAILIAN_API_BASE": "https://bailian.example/v1"})
    def test_base_url_can_be_loaded_from_environment(self, openai_client):
        ExcelLLM(
            {
                "base_url": "https://fallback.example/v1",
                "base_url_env": "BAILIAN_API_BASE",
                "api_key": "test-key",
            }
        )

        self.assertEqual(
            "https://bailian.example/v1",
            openai_client.call_args.kwargs["base_url"],
        )

    @patch("openai.OpenAI")
    def test_bailian_model_keeps_exact_requested_model_id(self, openai_client):
        llm = ExcelLLM(
            {
                "base_url": "https://bailian.example/v1",
                "model": "qwen3.8-27b",
                "api_key": "test-key",
            }
        )

        self.assertEqual("qwen3.8-27b", llm.model_name)

    @patch("openai.OpenAI")
    def test_auto_model_uses_the_single_vllm_served_id(self, openai_client):
        openai_client.return_value.models.list.return_value = SimpleNamespace(
            data=[SimpleNamespace(id="/models/example")]
        )

        llm = ExcelLLM(
            {
                "base_url": "http://192.168.0.201:18002/v1",
                "model": "auto",
                "api_key": "",
            }
        )

        self.assertEqual("/models/example", llm.model_name)
        openai_client.return_value.models.list.assert_called_once_with()

    @patch("openai.OpenAI")
    def test_auto_model_rejects_multiple_served_ids(self, openai_client):
        openai_client.return_value.models.list.return_value = SimpleNamespace(
            data=[SimpleNamespace(id="model-a"), SimpleNamespace(id="model-b")]
        )

        with self.assertRaisesRegex(RuntimeError, "multiple models"):
            ExcelLLM({"model": "auto", "api_key": "test-key"})


if __name__ == "__main__":
    unittest.main()
