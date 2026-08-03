# Camera Debug Studio

完整中文文档：[工具介绍、操作与配置手册](docs/用户手册.md)

面向不同芯片平台和项目的配置化相机调试工具。当前提供：

- SSH 密码/私钥认证与 Local 可替换传输层
- WebSocket 持久交互终端：macOS/Linux 使用 PTY，Windows 使用系统 OpenSSH/持久 Shell 管道
- 类终端命令执行与实时 stdout/stderr
- Link Lock、Video Lock、FPS 等配置化监控卡片
- 监控指标可视化编辑器，支持命令、解析、映射和展示规则配置
- 测试项选择、脚本预览、参数化运行、实时日志和停止
- 自动收集并运行 `test/` 目录中的 Pytest 用例
- 平台配置切换与网页 JSON 编辑器
- 命令模板 + 变量 + 正则解析器，不在代码中写死 `i2cdbgr` 或 `csi i2c`

## 启动

需要 Python 3.9+，以及 SSH 场景下系统可用的 OpenSSH 客户端。

```bash
python3 camera_debug.py
```

默认打开本机演示环境。使用平台示例：

```bash
python3 camera_debug.py --config configs/profiles/qualcomm
python3 camera_debug.py --config configs/profiles/bmc
```

服务默认仅监听 `127.0.0.1:8765`。如需让局域网访问，可显式传入 `--host 0.0.0.0`，并自行增加访问控制。

## Pytest 测试项目

将 Pytest 项目放入根目录的 `test/` 中，并在运行工具的同一个 Python 环境安装 Pytest：

```bash
python3 -m pip install pytest
```

进入“测试中心”后点击“刷新用例”，工具会通过 `python -m pytest --collect-only -q test` 收集 node ID。选择用例后可实时查看执行输出并停止测试。Pytest 在本机执行，不通过 SSH Transport。

工具会优先使用 `test/.venv` 或项目根目录 `.venv` 中的 Python。也可以通过环境变量指定解释器：`CAMERA_DEBUG_PYTEST_PYTHON=/path/to/python`。

## 配置模型

每个平台使用独立目录，各功能模块分别保存：

```text
configs/profiles/<平台>/
├── project.json
├── target.json
├── variables.json
├── monitoring.json
├── topology.json
└── tests.json
```

模块的核心层次：

```text
target       -> transport、主机、端口、用户、密钥、SSH 参数
variables    -> 总线、设备地址、寄存器等项目变量
monitoring   -> 指标的命令模板、轮询周期、解析规则、展示规则
tests        -> 测试脚本命令模板和用户参数
```

例如同一个 Link Lock 指标可以按平台替换命令：

```json
{"command": "i2cdbgr r {bus} {device} {link_reg}"}
```

或：

```json
{"command": "csi i2c {controller} {channel} {device} {link_reg}"}
```

后端只负责变量替换、执行和解析，因此添加平台通常只需复制并修改 JSON。解析器支持 `text`、`number` 和 `regex`，`regex` 可通过 `map` 把寄存器原始值映射为 `LOCKED/UNLOCKED`。

测试参数默认只接受字母、数字和常见路径字符；可用参数项的 `pattern` 自定义正则约束，或用 `choices` 限定可选值。数字类型会单独校验，避免参数直接注入 shell。

## 安全说明

这是开发调试工具，命令和测试脚本拥有目标板用户的权限。SSH 密码会以明文保存在被 Git 忽略的 `target.local.json`，并覆盖公共 `target.json`；仅在可信网络使用，限制文件权限，生产环境优先采用 SSH 密钥。
