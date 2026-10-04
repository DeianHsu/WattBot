"""离线检查独立命令行入口；使用模拟函数，不建库、不预测、不调用 API。"""

import runpy
import sys
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def run_entry(filename, *arguments):
    """隔离命令行与模块路径，执行指定入口脚本。"""
    # 使用真实脚本解析参数，同时恢复测试前的 argv 和导入路径。
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    with patch.object(sys, "argv", [str(path), *arguments]), patch.object(sys, "path", sys.path.copy()):
        runpy.run_path(str(path), run_name="__main__")


class ScriptEntryTests(unittest.TestCase):
    """验证参数转发、完成提示、缺题退出及无模型帮助。"""

    def test_build_default_and_pdf(self):
        """默认处理全部论文，指定 PDF 时原样传入核心函数。"""
        # 替换业务模块，避免解析论文或访问真实索引。
        for arguments, expected in (((), None), (("--pdf", "papers/example.pdf"), "papers/example.pdf")):
            with self.subTest(arguments=arguments):
                build = Mock()
                with patch.dict(sys.modules, {"wattbot.index": SimpleNamespace(build_index=build)}):
                    run_entry("build_index.py", *arguments)
                build.assert_called_once_with(expected)

    def test_predict_default(self):
        """默认输入输出与原入口一致，完成后显示结果位置。"""
        # 替换预测函数，只检查入口的参数与输出。
        predict = Mock(return_value=Path("submissions/test_submission.csv"))
        with patch.dict(sys.modules, {"wattbot.generate": SimpleNamespace(predict_all=predict)}), \
             patch("sys.stdout", new_callable=StringIO) as output:
            run_entry("predict.py")
        predict.assert_called_once_with(None, None, restart=False)
        self.assertIn("提交文件已保存", output.getvalue())

    def test_predict_custom_arguments(self):
        """自定义路径与重跑标记原样传给核心预测函数。"""
        # 参数只控制现有管线，不引入额外任务或配置。
        predict = Mock(return_value=Path("submissions/custom.csv"))
        with patch.dict(sys.modules, {"wattbot.generate": SimpleNamespace(predict_all=predict)}), \
             patch("sys.stdout", new_callable=StringIO):
            run_entry("predict.py", "--input", "input/questions.csv", "--output", "submissions/custom.csv", "--restart")
        predict.assert_called_once_with("input/questions.csv", "submissions/custom.csv", restart=True)

    def test_predict_incomplete(self):
        """未完成时退出为 1，保留进度提示且不宣告成功。"""
        # 模拟持续过滤或截断造成的缺题，不执行真实请求。
        predict = Mock(return_value=None)
        with patch.dict(sys.modules, {"wattbot.generate": SimpleNamespace(predict_all=predict)}), \
             patch("sys.stderr", new_callable=StringIO) as error, \
             patch("sys.stdout", new_callable=StringIO) as output:
            with self.assertRaises(SystemExit) as result:
                run_entry("predict.py", "--restart")
        predict.assert_called_once_with(None, None, restart=True)
        self.assertEqual(result.exception.code, 1)
        self.assertIn("已保存成功结果", error.getvalue())
        self.assertNotIn("提交文件已保存", output.getvalue())

    def test_help_without_business_imports(self):
        """业务模块不可导入时，两个入口仍能正常展示帮助。"""
        # 阻止业务模块导入，确认 --help 不触发模型加载或外部请求。
        for filename in ("build_index.py", "predict.py"):
            with self.subTest(filename=filename), \
                 patch.dict(sys.modules, {"wattbot.index": None, "wattbot.generate": None}), \
                 patch("sys.stdout", new_callable=StringIO) as output:
                with self.assertRaises(SystemExit) as result:
                    run_entry(filename, "--help")
                self.assertEqual(result.exception.code, 0)
                self.assertIn("--help", output.getvalue())


# 直接运行时执行离线入口测试，导入时不启动检查。
if __name__ == "__main__":
    unittest.main()
