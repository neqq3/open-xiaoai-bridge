import ast
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class _Config:
    def __init__(self, values):
        self.values = values
        self.listeners = []

    def get_app_config(self, path, default=None):
        value = self.values
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    def add_reload_listener(self, listener):
        self.listeners.append(listener)


class HermesBackendTest(unittest.TestCase):
    def setUp(self):
        sys.modules["open_xiaoai_server"] = types.SimpleNamespace()
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        for name in ("core.openai", "core.hermes"):
            sys.modules.pop(name, None)
        self.openai_module = importlib.import_module("core.openai")
        self.openai = self.openai_module.OpenAIManager
        self.hermes_module = importlib.import_module("core.hermes")
        self.hermes = self.hermes_module.HermesManager

    def tearDown(self):
        self.openai._sessions.clear()
        self.hermes._sessions.clear()

    def test_openai_and_hermes_runtime_state_are_isolated(self):
        self.openai._session_key = "plain-openai"
        self.openai._session_header = "X-Hermes-Session-Key"
        self.openai._sessions["plain-openai"] = [
            {"role": "user", "content": "OpenAI history"}
        ]
        self.hermes._session_key = "agent:hermes:speaker"
        self.hermes._session_header = "X-Hermes-Session-Key"

        self.assertEqual(
            "plain-openai",
            self.openai._headers()["X-Hermes-Session-Key"],
        )
        self.assertEqual(
            "agent:hermes:speaker",
            self.hermes._headers()["X-Hermes-Session-Key"],
        )
        self.assertEqual({}, self.hermes._sessions)

    def test_hermes_reads_only_its_own_configuration(self):
        config = _Config(
            {
                "openai": {
                    "base_url": "http://openai.test/v1",
                    "model": "plain-model",
                },
                "hermes": {
                    "base_url": "http://hermes.test/v1",
                    "model": "hermes-model",
                    "session_key": "agent:hermes:test",
                },
            }
        )
        self.hermes._reload_listener_registered = False

        with (
            mock.patch.object(
                self.hermes_module.ConfigManager,
                "instance",
                return_value=config,
            ),
            mock.patch.object(
                self.hermes_module,
                "get_env",
                return_value="true",
            ),
        ):
            self.hermes.reload_from_config()

        self.assertTrue(self.hermes._enabled)
        self.assertEqual("http://hermes.test/v1", self.hermes._base_url)
        self.assertEqual("hermes-model", self.hermes._model)
        self.assertEqual("agent:hermes:test", self.hermes._session_key)
        self.assertEqual("X-Hermes-Session-Key", self.hermes._session_header)

    def test_main_app_adds_hermes_after_existing_backend_arguments(self):
        tree = ast.parse((ROOT / "core/app.py").read_text(encoding="utf-8"))
        main_app = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MainApp"
        )
        expected = [
            "enable_xiaozhi",
            "enable_openclaw",
            "enable_openai",
            "enable_qwenpaw",
            "enable_hermes",
        ]
        for method_name in ("instance", "__init__"):
            method = next(
                node
                for node in main_app.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            )
            argument_names = [argument.arg for argument in method.args.args]
            self.assertEqual(expected, argument_names[-len(expected):])

    def test_generic_openai_source_remains_upstream_compatible(self):
        source = (ROOT / "core/openai.py").read_text(encoding="utf-8")
        self.assertNotIn("hermes.tool.progress", source)
        self.assertNotIn("HERMES_ENABLE", source)
        self.assertNotIn("HermesManager", source)


if __name__ == "__main__":
    unittest.main()
