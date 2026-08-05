import codecs
import os
import time
import unittest
import warnings

from camera_debug_studio.terminal import TerminalSession

warnings.filterwarnings("ignore", message=r"unclosed <socket\.socket.*", category=ResourceWarning)


class TerminalIncrementalDecoderTests(unittest.TestCase):
    def test_utf8_chinese_split_across_chunks(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        encoded = "终端中文正常".encode("utf-8")

        output = decoder.decode(encoded[:2])
        output += decoder.decode(encoded[2:7])
        output += decoder.decode(encoded[7:], final=True)

        self.assertEqual(output, "终端中文正常")
        self.assertNotIn("\ufffd", output)

    @unittest.skipUnless(os.name == "nt", "ConPTY is only available on Windows")
    def test_windows_conpty_emits_chinese_and_ansi(self):
        try:
            import winpty  # noqa: F401
        except ImportError:
            self.skipTest("pywinpty is not installed")

        class Runtime:
            config = {"target": {"transport": "local", "terminalEncoding": "utf-8"}}

            @staticmethod
            def terminal_command():
                script = "Write-Output '终端中文正常'; Write-Output \"$([char]27)[32mGREEN$([char]27)[0m\""
                return ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script]

            @staticmethod
            def command_environment():
                return dict(os.environ)

        messages = []
        session = TerminalSession(Runtime(), messages.append)
        session.resize(120, 40)
        self.assertTrue(session.closed.wait(10), "ConPTY process did not exit")
        output = "".join(item.get("data", "") for item in messages)

        self.assertEqual(session.backend, "conpty")
        self.assertTrue(session.winpty_process.closed)
        self.assertIn("终端中文正常", output)
        self.assertIn("\x1b[32mGREEN\x1b[0m", output)


if __name__ == "__main__":
    unittest.main()
