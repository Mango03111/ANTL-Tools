# 发布清单

本项目有意保持为本地单用户 LLM 聊天测速工具。发布工作应强化这一核心形态，
而非将其扩展为共享服务。

## 打标签前

快速路径：

```bash
./scripts/check-release.sh
```

仅元数据审计：

```bash
python3 scripts/audit-release-metadata.py --strict
```

当前维护者拥有的公开发布前阻塞项：

1. 在仓库根目录添加最终的 `LICENSE` 文件。
2. 在 `pyproject.toml` 的 `[project]` 下添加 `license = {file = "LICENSE"}`。
3. 在 `pyproject.toml` 中添加 `[project.urls]`，包含 `Homepage`、`Repository`
   和 `Issues`。

起始片段：

```toml
license = {file = "LICENSE"}

[project.urls]
Homepage = "https://example.com/llm-speed-chat"
Repository = "https://example.com/llm-speed-chat.git"
Issues = "https://example.com/llm-speed-chat/issues"
```

1. 确认用户范围仍与 README 匹配：
   - 本地、单用户
   - OpenAI 兼容上游
   - 多轮聊天测速
   - 仅核心吞吐指标
2. 为发布更新 `CHANGELOG.md`。
3. 运行 Python 测试套件：

```bash
python3 -m unittest discover -s tests -v
```

4. 运行浏览器 E2E 套件：

```bash
npm ci
npx playwright install --with-deps chromium
npm run test:e2e
```

## 构建产物

准备发布产物时使用隔离的虚拟环境：

```bash
python3 -m venv /tmp/llm-speed-chat-build
. /tmp/llm-speed-chat-build/bin/activate
python -m pip install --upgrade pip build twine
python -m build
python -m twine check dist/*
```

从仓库外部冒烟测试构建的 wheel：

```bash
python -m venv /tmp/llm-speed-chat-wheel
. /tmp/llm-speed-chat-wheel/bin/activate
python -m pip install --upgrade pip
python -m pip install dist/*.whl
cd /tmp
python /path/to/repo/scripts/smoke-installed.py
```

## 容器冒烟测试

构建并冒烟测试容器镜像：

```bash
docker build -t llm-speed-chat:test .
docker run -d --rm --name llm-speed-chat-smoke -p 18080:9000 llm-speed-chat:test
python3 scripts/smoke_http.py --base-url http://127.0.0.1:18080 --wait-seconds 30
docker stop llm-speed-chat-smoke
```

## 公开发布说明

在公开注册表发布前，显式验证以下非代码发布项：

1. 版本号和发布说明已最终确定。
2. 维护者已确认所选许可证和项目 URL。
3. README 安装命令与你发布的产物匹配。
4. 已安装的 `llm-speed-chat --help` 和 `llm-speed-chat --version` 输出仍与
   文档中的 CLI 匹配。
