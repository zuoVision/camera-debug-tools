import unittest

from camera_debug import ApiError, Runtime


class SftpTests(unittest.TestCase):
    def test_parses_directories_files_spaces_and_links(self):
        output = """total 12
drwxr-xr-x 2 0 0 4096 Aug  5 10:00 logs
-rw-r--r-- 1 0 0 1536 Aug  5 10:01 camera report.txt
lrwxrwxrwx 1 0 0    8 Aug  5 10:02 current -> logs/run.log
"""

        entries = Runtime.parse_sftp_listing(output, "/tmp")

        self.assertEqual([entry["name"] for entry in entries], ["logs", "camera report.txt", "current"])
        self.assertEqual(entries[0]["type"], "directory")
        self.assertEqual(entries[1]["size"], 1536)
        self.assertEqual(entries[1]["path"], "/tmp/camera report.txt")

    def test_sftp_rejects_local_transport(self):
        runtime = object.__new__(Runtime)
        runtime.config = {"target": {"transport": "local"}}

        with self.assertRaisesRegex(ApiError, "仅适用于 SSH"):
            runtime.sftp_list("/")
        with self.assertRaisesRegex(ApiError, "仅适用于 SSH"):
            runtime.sftp_upload("/tmp", "a.txt", "YQ==")


if __name__ == "__main__":
    unittest.main()
