# STS2 Harness

[English](README.md)

由模型驱动的《杀戮尖塔 2》玩家，一次打一局，不存档也不读档。

- **Mod。** 游戏 Mod `sts2agent` 通过本机 HTTP 提供游戏的公开状态和可执行操作。
- **后端。** Python 后端把决策分给三个模型角色：
  - Run 负责路线、商店、事件和营火；
  - Deck 负责选牌；
  - Combat 负责每场战斗。
- **录档。** 内置驱动会录下游戏操作和模型交互，可以离线回放。外部驱动只写诊断日志。

也可以接入你自己写的固定代码策略，或者让外部 Agent 通过本机工具 API 玩游戏。

## 环境要求

- Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。
- Steam `public-beta` 分支上的杀戮尖塔 2 v0.113.1，并装好 [`sts2agent` Mod](mod/README.md)。构建 Mod 需要 .NET 9 SDK 和本机的游戏程序集。
- 一个使用 OpenAI Responses、Chat Completions 或 Anthropic Messages 协议的模型接口。外部驱动不需要模型。
- 开发用的 headless 模式只支持 macOS。

## 安装

```sh
uv sync
cp .env.example .env    # PowerShell：Copy-Item .env.example .env
# 在 .env 里填上模型和凭据，不要外传
uv run sts2 check-config
```

各项设置、优先级和精简模式见 [docs/configuration.md](docs/configuration.md)。

## 开始游戏

启动装了 Mod 的游戏，选好角色，然后运行：

```console
uv run sts2 start
```

后端连上游戏时处于暂停状态；按游戏里的 Start 控件，就把这一局交给模型。后端会一直运行，之后你开的每一局都由它接手。如果要用自己的 Agent 来玩，运行 `uv run sts2 start --set STS2_DRIVER=external`，再配合 [play-sts2](skills/play-sts2/SKILL.md) skill 使用本机工具 API。

要配合知识文档一起玩，把 [sts2-knowledge](https://github.com/langwhat/sts2-knowledge) clone 到本仓库旁边，然后在知识库目录里运行 `uv run sts2-knowledge start`。直接运行 `uv run sts2 start` 只用核心，不带这些知识文档。

开发者可以启动相互隔离的游戏实例，每个实例有自己的存档、端口和录档：

```console
uv run sts2 headless --game /absolute/Game.app/Contents/MacOS/Game \
  --template /absolute/template.manifest.json --character IRONCLAD --ascension 0
```

启动方式、直播观众输入、外部工具和 headless 的细节见 [docs/running.md](docs/running.md)。`uv run sts2 --help` 列出全部命令和选项。

## 查看录档

使用内置驱动时，每局会在 `runs/` 下各自的目录里写一个 `archive.sqlite`。外部驱动只写诊断日志。离线查看内置驱动的录档：

```sh
uv run sts2 journal --archive runs/<run-directory>/archive.sqlite
```

更多筛选和视图见 [docs/library.md](docs/library.md#recordings)。

## 开发

```sh
./scripts/check      # Ruff、严格模式 Pyright、pytest（不需要游戏和模型）
./scripts/check-mod  # Mod 离线测试；找到游戏程序集时构建 Mod
```

## 文档

以下文档都是英文。

- [架构](docs/architecture.md)：核心的组成、角色、上下文和工具。
- [术语表](GLOSSARY.md)：领域术语。
- [配置](docs/configuration.md)：设置、认证、精简模式。
- [运行](docs/running.md)：普通、外部驱动和 headless 三种启动方式。
- [把核心当库用](docs/library.md)：模型客户端、录档、会话、角色。
- [评测](docs/evaluation.md)：离线回放和固定决策评测。
- [对局复盘](docs/gameplay-retrospectives.md)：复盘打完的对局。
- [扩展文档](EXTENSIONS.md)：提供额外的知识和指导。
- [每局文档](docs/materials.md)：为每一局生成文档。
- [Mod](mod/README.md)：C# 游戏 Mod 和原生模板工具。
- [贡献指南](CONTRIBUTING.md)。

## 免责声明

这是非官方的粉丝项目，与 Mega Crit 无关，也未经其认可。Slay the Spire 是 Mega Crit 的商标。本仓库不含任何游戏代码或资源，你需要自己拥有一份《杀戮尖塔 2》。

## 许可

[MIT](LICENSE)。
