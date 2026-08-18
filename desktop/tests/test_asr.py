"""asr 模块测试（不依赖 funasr 实际安装）。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recorder import asr
class TestAsr(unittest.TestCase):
    def test_language_codes(self):
        self.assertIn("auto", asr.LANGUAGES)
        self.assertIn("zh", asr.LANGUAGES)

    def test_models_registered(self):
        self.assertIn("sensevoice", asr.MODELS)
        self.assertIn("paraformer", asr.MODELS)
        self.assertIn("zh", asr.MODELS["sensevoice"]["languages"])
        self.assertIn("yue", asr.MODELS["sensevoice"]["languages"])
        # Paraformer 不支持粤语，只保留 auto/zh/en
        self.assertEqual(asr.MODELS["paraformer"]["languages"], ("auto", "zh", "en"))

    def test_invalid_model_raises(self):
        with self.assertRaises(ValueError):
            asr.transcribe_file_sync(Path("fake.wav"), model="nope")

    def test_missing_dependency_raises_clear_error(self):
        """未安装 funasr 时应抛出带安装指引的 AsrNotAvailable。"""
        try:
            import funasr  # noqa: F401
            self.skipTest("funasr 已安装，跳过缺依赖分支")
        except ImportError:
            pass
        self.assertFalse(asr.is_loaded())
        # 不指定参数，使用默认 sensevoice
        with self.assertRaises(asr.AsrNotAvailable) as ctx:
            asr._get_model("sensevoice", "onnx", None)
        self.assertIn("requirements-asr.txt", str(ctx.exception))
if __name__ == "__main__":
    unittest.main(verbosity=2)