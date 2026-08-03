# Pytest 项目目录

将 Pytest 测试文件、公共 fixture 和项目配置放在此目录。

建议结构：

```text
test/
├── conftest.py
├── test_camera_link.py
└── test_video_stream.py
```

测试中心会执行下面的命令收集用例：

```bash
python -m pytest --collect-only -q test
```

选择用例后，工具在项目根目录运行对应的 Pytest node ID。Pytest 始终在运行 Camera Debug Studio 的电脑上执行，不通过 SSH Transport 执行。

可以把虚拟环境放在 `test/.venv` 或项目根目录 `.venv`。也可通过 `CAMERA_DEBUG_PYTEST_PYTHON` 环境变量指定执行 Pytest 的 Python。
