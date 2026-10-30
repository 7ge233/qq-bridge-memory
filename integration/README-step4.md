# Step 4：把 qq-bridge-memory 接到 qq-bridge（补丁包）

> **本文讲「改了什么、为什么」。要装/要退，请看同目录 [INSTALL.md](INSTALL.md)**——
> 那里按设备分了 Windows PowerShell、Windows cmd、Linux/服务器三套命令。

这个目录是**一份可以在任何 qq-bridge 仓库上重复应用的补丁**，不是一份已经改好的代码快照。
桥接是别人的上游仓库（`github.com/Derpyu520/qq-bridge`），直接改工作区的话，下次 `git pull`
要么冲突、要么被覆盖；所以改动统一收敛成补丁，升级流程变成「回滚 → 拉取 → 重打」。

补丁基线：**`a49877f`**（qq-bridge `0.2.1-r1`，2026-10-22 的上游 HEAD）。基线之后的 commit、或上游动过同一处，
`apply.ps1` 会在 `git apply --check` 阶段就报错，不会留下半成品。

## 1. 包里有四个文件

| 文件 | 作用 |
| --- | --- |
| `bridge-memory.patch` | 真正的改动（722 行）：`src/bridge.js` 打钩子、新增 `src/qq-memory-client.js`、`start.bat` 顶部环境变量区、新增 `ENV.md` |
| `apply.ps1` | 安装：校验 → `git apply` → `node --check`，失败自动回滚 |
| `revert.ps1` | 回滚：`git apply -R`，只撤本补丁，不碰你的其它改动 |
| `pack.ps1` | 只有当你又改过 bridge 的那几个文件时才需要跑：重新生成补丁（无 BOM）并同步客户端副本 |
| `qq-memory-client.js` | 补丁里那个新文件的副本，方便阅读/比对（装的时候由补丁写入，不用手动拷） |

## 1.5 什么时候需要重新打包？

**日常不需要**——补丁已经生成好并提交进 git，你只要 `apply.ps1` / `revert.ps1`。

只有这两种情况才要跑 `pack.ps1`：

- 你自己又改了 bridge 里的 `src/bridge.js` / `src/qq-memory-client.js` / `start.bat` / `ENV.md`（新增钩子、修 bug）；
- 你想换基线（比如上游发版后决定以新版本为基准）。

```powershell
.\pack.ps1                                  # 默认基线 a49877f
.\pack.ps1 -Base <新的上游 commit>          # 换基线
.\apply.ps1 -Check                          # 再验证能否打到干净基线上
```

`pack.ps1` 用 `git diff <基线> -- 4个文件` 取差异，**包含未提交改动**，并写 UTF-8 无 BOM
（带 BOM 的补丁在 Linux 上 `git apply` 会失败）。

> 下文命令里的 `<qq-bridge 路径>` 是示例路径，换成你的克隆位置。

## 2. 安装

```powershell
cd <qq-bridge-memory 路径>\integration

# 先干跑一遍，确认能打上（不改任何文件）
.\apply.ps1 -BridgeDir <qq-bridge 路径> -Check

# 真装
.\apply.ps1 -BridgeDir <qq-bridge 路径>
```

`-BridgeDir` 不写时取环境变量 `QQ_BRIDGE_DIR`。

装完会打印下一步；重复执行是安全的（检测到已装就退出，不会打两遍）。

## 3. 验证（装完之后必须做的三件事）

1. **语法**：`apply.ps1` 已自动跑 `node --check src/bridge.js`，失败会自己回滚。
2. **侧车在线**：`curl http://127.0.0.1:8787/healthz` 应返回 `{"ok":true,...}`。
3. **开关打开**：`start.bat` 顶部设 `QQ_MEMORY=1`，重启桥接后：

   - 日志出现 `DSH 地址：…（来源：config.json / DSH_BASE_URL）`；
   - 发一条 QQ 消息，日志出现 `[memory]` 写入成功；
   - 库里多一条：`sqlite3 data/qq-memory.db "select count(*) from conversation_turns"`。

**没看到 `[memory]` 就是没接上**。最常见的两个原因：`QQ_MEMORY` 没设成 1（默认是 0，全空操作），
或者桥接拿不到机器人 QQ 号（扫码登录后 selfUserId 为空时，日志会明确警告）。

## 4. 回滚

```powershell
.\revert.ps1 -BridgeDir <qq-bridge 路径>
```

回滚后 `src/bridge.js` 回到装之前的样子，`src/qq-memory-client.js` 与 `ENV.md` 被删除，
`git status` 应为空（除了你原本就有的改动）。

如果脚本说「反向打补丁失败」，说明装完之后你又手改过这些文件。它不会强行覆盖，会给你手动命令：

```powershell
git checkout -- src/bridge.js start.bat
Remove-Item src/qq-memory-client.js, ENV.md
```

**更省事的回滚**：把 `QQ_MEMORY` 设成 `0` 重启桥接。客户端所有方法立刻变成空操作，
代码留着也不影响主链路——排查问题时先用这招。

## 5. 升级 qq-bridge 的正确顺序

```powershell
.\revert.ps1                       # 1. 先撤掉补丁
cd <qq-bridge 路径>
git pull                           # 2. 拉上游
cd <integration 目录>
.\apply.ps1 -Check                # 3. 先干跑：冲突在这一步暴露
.\apply.ps1                        # 4. 真装
```

**顺序反了会很麻烦**：先 `git pull` 再回滚，补丁上下文早已漂移，`-R` 会失败。

如果第 3 步报「补丁与当前代码不匹配」，说明上游改了同一处，需要人工对照下一节的钩子表合并。
把报错贴出来即可，钩子只有 8 处，改动量很小。

## 6. 部署到服务器

服务器上通常端口固定、没有 Windows 批处理，做法是：

```bash
# 1. 拉 bridge，打补丁（Linux 上用 git apply，脚本逻辑一样）
git apply --check integration/bridge-memory.patch && git apply integration/bridge-memory.patch

# 2. 环境变量写进服务单元，不要写进代码
#    /etc/systemd/system/qq-bridge.service
Environment=QQ_MEMORY=1
Environment=QQ_MEMORY_BASE=http://127.0.0.1:8787
Environment=DSH_BASE_URL=http://127.0.0.1:3080     # 端口固定，直接写死
```

`start.bat` 与 `ENV.md` 的改动是为 Windows 本机准备的，服务器上留着无害（不会被调用）。
环境变量全表见仓库里的 `ENV.md`，侧车侧的开关见 `qq-bridge-memory/config.py` 顶部注释。

## 7. 补丁到底改了什么（8 处钩子）

行号会漂移，按函数/位置记：

| # | 位置 | 改动 |
| --- | --- | --- |
| 1 | `bridge.js` 顶部 import | 引入 `createMemoryClient` |
| 2 | `selfUserId` 声明之后 | 创建客户端（日志、`state/memory-client.json`、account 取自 `selfUserId`，取不到回退持久化账号） |
| 3 | `reserved2` 分支收到用户消息处 | `memoryClient.writeIncoming`（异步，失败丢弃） |
| 4 | 记录已发送消息处 | `memoryClient.writeAssistant` |
| 5 | `deliverPromptNow` / 拼 prompt 处 | `buildWakeExtras` → 续接包 + 召回块插在 base 之后、【本次消息快照】之前 |
| 6 | 会话用量回调 | `observeEvent` 记录每轮 inputTokens |
| 7 | `turn/end` 处理 | `noteTurnEnd` 判定轮换（4 道安全阀） |
| 8 | `/reset` 与控制台 reset | `memoryClient.forget` 清掉待注入续接包与计数 |

另外 `start.bat` 顶部集中了环境变量并回显生效值；`ENV.md` 是变量总表。
（`start.bat` 特意写成纯 ASCII：cmd.exe 用 GBK 解析含中文的批处理会错行。）

## 8. 已知限制（Step 5 验收前不打算修的）

- 会话轮换的 60% 阈值依赖 `QQ_MEMORY_CONTEXT_TOKENS`，当前默认 131072 是估值，**没按你的实际模型窗口校准**；
- SIM_GATE `0.55` 只有片段复述的代理标定（`docs/SIM_GATE标定-20261029.md`：相关最低 0.6329、误召 3.47%），真实同义改写问句未覆盖；
- 真实 QQ 全天 soak、轮换接续率抽检未做（Step 5 的活）；
- 群聊只在被唤醒时写「被唤醒消息及其上下文 3 条」，未做全量写入（与设计稿一致，但要注意覆盖率）。