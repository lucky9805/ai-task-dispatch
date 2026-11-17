"""可插拔执行器 Gate：命令构造、占位符、dry-run、未安装工具友好报错。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import executors as E  # noqa: E402
import agent  # noqa: E402


TASK = {"id": 7, "title": "生成日报", "payload": {"command": "echo report-ok"}}


class ExecutorsTest(unittest.TestCase):
    def test_shell_keeps_command(self):
        self.assertEqual(E.build_command("shell", TASK), "echo report-ok")

    def test_claude_command(self):
        cmd = E.build_command("claude", TASK)
        self.assertIn("claude -p", cmd)
        self.assertIn("--allowedTools", cmd)
        self.assertIn("生成日报", cmd)
        self.assertIn("echo report-ok", cmd)

    def test_codex_command(self):
        cmd = E.build_command("codex", TASK)
        self.assertTrue(cmd.startswith("codex exec "))
        self.assertIn("--sandbox workspace-write", cmd)
        self.assertIn("生成日报", cmd)

    def test_workbuddy_command(self):
        cmd = E.build_command("workbuddy", TASK)
        self.assertTrue(cmd.startswith("codebuddy -p "))
        self.assertIn("-y", cmd)
        self.assertIn("生成日报", cmd)

    def test_chatgpt_command(self):
        cmd = E.build_command("chatgpt", TASK)
        self.assertTrue(cmd.startswith("python3 chatgpt_run.py "))
        self.assertIn("生成日报", cmd)

    def test_custom_template_placeholders(self):
        t = {"id": 9, "title": "T", "payload": {"command": "C"}}
        cmd = E.build_command('my-agent run --prompt "{task}" --id {id}', t)
        self.assertIn('--prompt "T：C"', cmd)
        self.assertIn("--id 9", cmd)

    def test_custom_all_placeholders(self):
        t = {"id": "1", "title": "标题", "payload": {"command": "命令", "prompt": "提示词"}}
        cmd = E.build_command("{title}|{command}|{task}|{prompt}|{id}|{executor}", t)
        self.assertIn("标题", cmd)
        self.assertIn("命令", cmd)
        self.assertIn("提示词", cmd)  # prompt 字段优先于 command 进入 task 描述

    def test_unknown_placeholder_preserved(self):
        cmd = E.build_command("tool --x {unknown}", TASK)
        self.assertIn("{unknown}", cmd)  # 不抛异常，原样保留便于排查

    def test_missing_binary_hint(self):
        self.assertIsNone(E.missing_binary("shell"))
        self.assertIsNone(E.missing_binary("echo"))
        hint = E.missing_binary("claude")
        if hint:  # 本机没装 claude 时应给出提示
            self.assertIn("claude", hint)
            self.assertIn("PATH", hint)

    def test_is_builtin_and_describe(self):
        self.assertTrue(E.is_builtin("claude"))
        self.assertTrue(E.is_builtin("workbuddy"))
        self.assertTrue(E.is_builtin("chatgpt"))
        self.assertFalse(E.is_builtin("unknown-tool"))
        self.assertIn("Claude", E.describe("claude"))
        self.assertIn("自定义", E.describe("my-tool run {task}"))

    def test_binary_of_workbuddy_is_codebuddy(self):
        self.assertEqual(E.binary_of("workbuddy"), "codebuddy")
        self.assertIsNone(E.binary_of("chatgpt"))  # 走脚本+API，不检查本地二进制


    def test_dry_run_previews_command(self):
        ok, out = agent.execute(TASK, 5, True, lambda: None, executor="claude")
        self.assertTrue(ok)
        self.assertIn("[dry-run]", out)
        self.assertIn("claude -p", out)

    def test_task_level_executor_overrides(self):
        """payload.executor 覆盖启动参数——中心可指定这条活该谁干。"""
        t = dict(TASK, payload={"command": "echo x", "executor": "echo"})
        ok, out = agent.execute(t, 5, True, lambda: None, executor="claude")
        self.assertTrue(ok)
        self.assertIn("[echo]", out)  # 用的是 payload 里的 echo，不是传入的 claude

    def test_shell_requires_command(self):
        ok, out = agent.execute({"title": "无命令", "payload": {}}, 5, True,
                                lambda: None, executor="shell")
        self.assertFalse(ok)
        self.assertIn("payload.command", out)

    def test_detect_auth_failure_even_with_zero_exit(self):
        """codebuddy 未登录时输出认证提示却返回 0，必须被识别为失败。"""
        out = "Authentication required. Please use /login command to sign in to your account"
        self.assertIsNotNone(E.detect_failure_signal(out))
        self.assertIn("Authentication required", E.detect_failure_signal(out))

    def test_detect_failure_does_not_false_positive(self):
        """任务本身讨论认证主题的长输出不应被误判为失败。"""
        self.assertIsNone(E.detect_failure_signal(
            "请分析 authentication 模块的实现：\n1.登录流程\n2.鉴权\n3.令牌刷新\n4.测试\n5.总结"))
        self.assertIsNone(E.detect_failure_signal("任务完成：已修复登录逻辑"))
        self.assertIsNone(E.detect_failure_signal(""))


if __name__ == "__main__":
    unittest.main()
