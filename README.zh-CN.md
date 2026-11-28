# STS2 Harness

[English](README.md)

**面向《杀戮尖塔 2》的原生 AI 对局引擎与评测框架** —— 不靠截图/OCR，不依赖外挂攻略库，不做任何 S/L。接入主流大模型 API，点击游戏内头像即可看 AI 自己规划路线、构筑牌组、完整打完标准模式各进阶与恐惧模式对局，也能在决策间隙一键暂停接管、实时给 AI 发建议互动，或离线复盘每一手决策。

## 为什么不一样

- **直读游戏状态与机制，不在黑箱描述里盲打。** `sts2agent` Mod 直接从运行中的游戏读取公开状态和真实机制（敌人招式规律、Boss/精英阵容、事件选项、单卡实时伤害/升级预览与问号房概率），让模型清楚掌握局内规则而不是靠截图猜测或反复踩坑，无需外挂攻略库即可应对新版本与全角色。
- **严格公平：和人类玩家站在同一条信息起跑线上。** 抽牌堆、弃牌堆、消耗堆全部按引用排序不偷看顺序，隐藏或未知敌人意图统一脱敏为 `unknown`，不读取不可见能力、不推进游戏 RNG、无任何存档/读档（No Save/Load）；开发者调试端点在普通游玩下物理关闭。
- **专为“围观打牌 + 实时互动”打磨的游戏内原生体验。** 顶部角色头像直接变为一键 Start/Pause 开关，随时交出或收回控制权；AI 规划路线时会用游戏原生羽毛笔在地图上逐笔绘制并自动居中跟随；选牌思考时原生展开待选牌面，卡组选牌自动滚进视野高亮；画面左上角实时浮动游戏当前语言的简短决策理由，遇商店或全屏选牌自动避让不遮挡。还可以开启本机 HTTP 输入队列（`STS2_USER_INPUT=true`），对局途中随时发送意见或弹幕，让 AI 在后续决策里阅读并互动。
- **高缓存、无黑盒的三角色架构。** 零第三方 Agent 框架，基于 OpenAI / Anthropic 官方 SDK 自研调度（支持 Responses、Chat Completions、Messages 三协议并原样保留推理/Thinking 续传）。按生命周期拆分为 **Run**（单幕宏观）、**Deck**（多组卡牌奖励联合规划与重随 + 牌库循环统计）和 **Combat**（单场战斗批量出牌与预判选择）；无分支机械操作（含遗物防呆）零模型调用自动推进，配合 Append-Only 上下文与零额外调用的确定性容量压缩，长局前缀缓存命中率可达 **86%**。
- **从开箱挂机到科研评测全链路可扩展。** 配好 API 即可用默认精简模式开玩；也可自写 Markdown 攻略增强局内表现，把后端切成本机工具 API 接给 Claude Code / Codex 等外部 Agent，替换任意角色为固定代码策略，或基于单局 SQLite 全量归档做离线回放、变体对照与断点接管评测。

## 实测成绩

**全六角色轮换，进阶 10，连续 36 局（每角色 6 局）：21 胜 15 负（58.3%；Wilson 95% 区间 42%–73%），最高 9 连胜。**

全 36 局均为**纯核心运行（无外部攻略或知识库、无观众输入）**，使用默认精简模式，每局从开局一直打到游戏自行判定胜负。

| 项目 | 配置 |
| --- | --- |
| 游戏 | 《杀戮尖塔 2》v0.114.0（标准模式，进阶 10） |
| 模型 | `gpt-6.2-astra`，Responses 协议，reasoning effort `xhigh` |
| 模式 | 精简模式（默认），无外部知识库，无观众输入 |

| 角色 | 战绩 | 胜率 |
| --- | --- | ---: |
| 故障机器人（Defect） | 5 胜 1 负 | 83.3% |
| 静默猎手（Silent） | 4 胜 2 负 | 66.7% |
| 储君（Regent） | 4 胜 2 负 | 66.7% |
| 亡灵契约师（Necrobinder） | 4 胜 2 负 | 66.7% |
| 铁甲战士（Ironclad） | 3 胜 3 负 | 50.0% |
| 愚者（Fool） | 1 胜 5 负 | 16.7% |

- **耗时与开销（中位数）**：全部 36 局 **50.0 分钟**，输入 6.16M token（缓存命中 5.26M，总缓存率 86.0%），输出 43.1K token；21 局胜局 **56.3 分钟**，输入 7.17M token（缓存命中 6.15M），输出 45.3K token。
- **公开记录与核验**：逐局种子、终局层数、击杀遭遇、token 明细与版本说明见 [docs/results.md](docs/results.md)；全部 36 局完整 SQLite 归档与游戏原生 `.run` 记录已公开在 [sts2-harness-runs](https://github.com/langwhat/sts2-harness-runs/tree/main/2026-11-26-rotation-a10)。

## 快速开始（推荐：接入模型 API + 默认精简模式）

### 环境要求

- Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。
- Steam `public-beta` 分支上的 **《杀戮尖塔 2》v0.114.0**，并安装 [`sts2agent` Mod](mod/README.md)（仓库不含游戏程序集；从源码编译 Mod 需 **.NET 9 SDK**，支持 Windows / macOS / Linux，Windows 可直接运行 `dotnet build mod/src/Sts2Mod/Sts2Mod.csproj -c Release`）。
- 一个支持 **OpenAI Responses**、**Chat Completions** 或 **Anthropic Messages** 协议的模型 API（官方接口或兼容中转均可）。仅使用外部 Agent 驱动时不需要配置模型。
- 开发者并发 headless 模式仅支持 macOS（普通 GUI 游玩三平台均支持）。

### 安装与开玩

```sh
uv sync
cp .env.example .env    # PowerShell：Copy-Item .env.example .env
# 在 .env 中填入你的 API 配置（LLM_PROTOCOL / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY），注意保密
uv run sts2 check-config
```

启动装好 Mod 的游戏，选好角色与模式进入对局，然后启动后端：

```console
uv run sts2 start
```

后端连上游戏时默认暂停；**点击游戏顶部栏的角色头像**即可把对局交给模型，再次点击随时收回控制权（保留当前上下文与状态）。后端常驻运行，开新局直接再点头像即可接手。

### 精简模式（默认）vs 完整模式

内置驱动**默认使用精简模式**，在保留核心决策能力的条件下大幅减少等待停顿，更适合日常游玩与观看（上方 36 局进阶 10 轮换即在精简模式下跑出）。**完整模式**加入了战后复盘与跨节点策略笔记闭环，**可能进一步提升胜率**，但会在战后与切换节点时增加额外模型调用，拖慢单局节奏并影响观战体验：

| 对比项 | 精简模式（默认，`STS2_MINIMAL=true`） | 完整模式（`STS2_MINIMAL=false`） |
| --- | --- | --- |
| 核心玩法、三角色分工、联合选牌与构筑指导 | 完整保留 | 完整保留 |
| 原生机制提取、牌组循环统计、自动推进、攻略自动注入 | 完整保留 | 完整保留 |
| 原生图鉴查询（`run_knowledge` / `combat_knowledge`） | 保留原生图鉴 | 保留，并附带扩展补充文档 |
| 战后复盘、跨战交接与策略笔记（`core`/`plan`/`other`） | 跳过（更快、观感更流畅） | 开启（可能提升胜率，但耗时更长、等待更多） |
| 额外检视与技能工具（`*_inspect` / `*_skill` / `run_notes`） | 关闭 | 开启 |

如需切到完整模式，可在 `.env` 中设 `STS2_MINIMAL=false`，或启动时临时覆盖：

```console
uv run sts2 start --set STS2_MINIMAL=false
```

全部环境变量、多协议 `.env` 模板、认证方式与上下文容量控制见 [docs/configuration.md](docs/configuration.md)；常见问题（版本、代理拦截 `127.0.0.1`、端口占用等）见 [docs/running.md](docs/running.md#troubleshooting)。

## 编写自己的攻略知识增强表现

核心本身不内置任何攻略库，但提供完整的文档扩展机制。你可以把自己的**单卡评价、遗物联动、敌人机制补充、Boss 对策或角色构筑思路**写成 Markdown 文档接入核心：

- **自动按需注入**：进幕时自动匹配当前幕与地图上的 Boss；开战时自动匹配当前牌组、遗物、药水、敌人与能力（**精简模式和完整模式均生效**）；
- **按单局上下文动态裁剪**：可根据进阶等级与运行模式渲染不同建议；
- **可量化验证**：配合离线回放评测直接对比加攻略前后的决策差异。

具体能写哪些内容、局内何时生效以及最小可运行接入代码见 [自定义攻略指南](docs/custom-knowledge.md)，底层接口规范见 [EXTENSIONS.md](EXTENSIONS.md)。

## 更多玩法

- **接入外部 Agent（本机工具 API）。** 无需在后端配置模型 Key，一条命令把游戏暴露为单步同步的本机 HTTP 工具服务（默认端口 `17878`），配合仓库自带低 token 增量 CLI（`sts2_cli.py`）的 [play-sts2](skills/play-sts2/SKILL.md) skill，直接让 Claude Code、Codex 或你自己的 Agent 上手打牌：
  ```console
  uv run sts2 start --set STS2_DRIVER=external
  ```
- **对局实时意见互动（HTTP 输入队列）。** 启用 `STS2_USER_INPUT=true` 后，可通过本机 HTTP 接口（`POST http://127.0.0.1:17879/inputs`）随时向当前决策角色发送你的建议或直播弹幕，按顺序排队注入下一次模型请求（详见 [docs/running.md](docs/running.md#live-user-input)）。
- **自定义策略 / 把核心当库用。** 实现统一的 `DecisionSource` 协议即可把 Run、Deck 或 Combat 任意替换成固定代码策略，同时复用全部执行、结算、暂停与录档能力（见 [docs/library.md](docs/library.md)）。
- **多实例并发 Headless 评测（仅 macOS）。** 每个实例拥有独立存档、端口与录档，且自动旁路所有 GUI 视觉节奏以全速跑局：
  ```console
  uv run sts2 headless --game /absolute/Game.app/Contents/MacOS/Game \
    --template /absolute/template.manifest.json --character IRONCLAD --ascension 0
  ```

启动方式、观众输入、外部工具 API 与 headless 细节见 [docs/running.md](docs/running.md)；全部命令见 `uv run sts2 --help`。

## 录档、复盘与离线评测

内置驱动会自动把每局的每一次游戏操作、模型输入输出、Token 缓存统计与材料读取写入 `runs/<run-directory>/archive.sqlite`（外部驱动仅写诊断日志）。无需启动游戏或联网即可离线检视：

```sh
uv run sts2 journal --archive runs/<run-directory>/archive.sqlite
```

配合 `sts2_harness_eval`，还可以对历史对局做软件级离线回放、固定决策点基准评测、声明式攻略变体对照以及任意节点接管重跑，详见 [docs/evaluation.md](docs/evaluation.md) 与 [docs/gameplay-retrospectives.md](docs/gameplay-retrospectives.md)。

## 开发与校验

```sh
./scripts/check      # Ruff、严格模式 Pyright、pytest（无需游戏与模型）
./scripts/check-mod  # Mod 离线测试；检测到本机游戏程序集时编译 Release Mod（Windows 可用 dotnet build）
```

## 文档导航

以下文档均为英文。

- [实测成绩](docs/results.md)：36 局轮换明细、设置、版本记录与归档索引。
- [自定义攻略指南](docs/custom-knowledge.md)：自己写攻略知识提升局内表现的完整指南。
- [架构设计](docs/architecture.md)：核心模块、三角色分工、上下文投影与工具协议。
- [术语表](GLOSSARY.md)：领域术语。
- [配置说明](docs/configuration.md)：API 协议、认证方式与精简/完整模式。
- [运行指南](docs/running.md)：普通启动、观众输入、外部工具 API 与 headless。
- [作为库使用](docs/library.md)：模型客户端、SQLite 录档、会话与自定义角色。
- [离线评测](docs/evaluation.md)：离线回放、接管与固定决策评测。
- [对局复盘](docs/gameplay-retrospectives.md)：如何系统复盘已结束的对局。
- [扩展接口](EXTENSIONS.md)：文档提供者协议与交付语义。
- [每局文档](docs/materials.md)：按单局上下文动态构建文档。
- [Mod 说明](mod/README.md)：C# 游戏 Mod、安装方式与原生模板工具。
- [贡献指南](CONTRIBUTING.md)。

## 免责声明

这是非官方的粉丝项目，与 Mega Crit 无关，也未经其认可。Slay the Spire 是 Mega Crit 的商标。本仓库不含任何游戏代码或资源，你需要自己拥有一份《杀戮尖塔 2》。

## 许可

[MIT](LICENSE)。
