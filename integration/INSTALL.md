# 安装与运行说明书（不同设备 / 不同系统）

> **这是运行前提。** 不装这个补丁，侧车（`qq-bridge-memory`）再健康也不会被调用：
> 桥接照常收发消息，只是永远不写库、也永远不注入记忆，日志里连一句报错都没有。

本文只讲「怎么装、怎么验、怎么退」。补丁改了什么、为什么这么改，见同目录 `README-step4.md`。

> 下文命令里的 `<qq-bridge 路径>`、`<qq-bridge-memory 路径>` 都是占位符，
> 换成你自己的克隆位置即可。

## 0. 通用前提（所有系统都一样）

1. **顺序**：先起侧车，再起桥接。反了不会崩，只是第一条消息没有记忆可用。
2. **侧车在线**：`http://127.0.0.1:8787/healthz` 返回 `{"ok":true,...}`。
   起法见上级目录 `run.cmd`（Windows）或 `uvicorn app:app --host 127.0.0.1 --port 8787 --workers 1`（Linux）。
3. **桥接是 git 仓库**：`<bridge>/.git` 存在。补丁靠 `git apply`，不是拷文件。
4. **Node 可用**：`node --version` 需 ≥ 22.13（qq-bridge 自身要求）；脚本装完会跑 `node --check` 验证语法。
5. **适配版本**：补丁按 qq-bridge `0.2.1-r1`、基线 commit `a49877f`（2026-10-22）生成。上游改动过
   `src/bridge.js` 时，`-Check` 会在试打阶段报冲突并退出，不会留下半成品。

## 1. Windows（PowerShell，推荐）

```powershell
cd <qq-bridge-memory 路径>\integration

.\apply.ps1 -BridgeDir <qq-bridge 路径> -Check   # 干跑，不改文件
.\apply.ps1 -BridgeDir <qq-bridge 路径>          # 安装
```

`-BridgeDir` 不写时取环境变量 `QQ_BRIDGE_DIR`；两者都没有会直接报错，不会去猜路径。

**执行策略被拦**时（报 `无法加载文件…未数字签名`）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\apply.ps1 -BridgeDir <qq-bridge 路径>
```

## 2. Windows（没有 PowerShell / 只用 cmd 或 Git Bash）

脚本本质就是两条命令，可以直接手打：

```cmd
cd /d <qq-bridge 路径>
git apply --check --whitespace=nowarn D:\...\integration\bridge-memory.patch
git apply --whitespace=nowarn D:\...\integration\bridge-memory.patch
node --check src\bridge.js
```

回滚把 `git apply` 换成 `git apply -R`，其余相同。
Git Bash 里路径写 `/d/dsh/downloads/qq-bridge`。

## 3. Linux / 服务器

```bash
cd /opt/qq-bridge
git apply --check --whitespace=nowarn /opt/qq-bridge-memory/integration/bridge-memory.patch \
  && git apply --whitespace=nowarn /opt/qq-bridge-memory/integration/bridge-memory.patch
node --check src/bridge.js

# 回滚
git apply -R --whitespace=nowarn /opt/qq-bridge-memory/integration/bridge-memory.patch
```

补丁文件已确保 **UTF-8 无 BOM**（带 BOM 在 Linux 上会报 `unrecognized input`）。
不要用 `crlf` 转换后再打，会失败。

**服务器上环境变量写进 systemd，不要写进代码**：

```ini
# /etc/systemd/system/qq-bridge.service
[Service]
WorkingDirectory=/opt/qq-bridge
ExecStart=/usr/bin/node src/bridge.js
Environment=QQ_MEMORY=1
Environment=QQ_MEMORY_BASE=http://127.0.0.1:8787
Environment=DSH_BASE_URL=http://127.0.0.1:3080
Restart=always
```

服务器上的 DSH 端口一般固定，直接把 `DSH_BASE_URL` 写死即可，不需要每次改。

## 4. 环境变量（装完必须设）

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `QQ_MEMORY` | `1` | **总开关。不设就是 0，等于没装**（不报错、不写库） |
| `QQ_MEMORY_BASE` | `http://127.0.0.1:8787` | 侧车地址 |
| `QQ_MEMORY_TOKEN` | 侧车的 token | 侧车配了鉴权才需要 |
| `DSH_BASE_URL` | `http://127.0.0.1:<端口>` | DSH 端口变了才需要；覆盖 `config.json` 的 `dsh.baseUrl` |

- **Windows 本机**：改 `start.bat` 顶部那个方块（纯 ASCII，取消注释即可），启动时会回显生效值。
- **Linux**：`export` 或 systemd 的 `Environment=`。
- 全集见桥接仓库里的 `ENV.md`。

## 5. 验证（三步，缺一不可）

```powershell
# ① 桥接日志出现地址行
Get-Content <qq-bridge 路径>\state\bridge.log -Tail 20 | Select-String 'DSH 地址'

# ② 侧车健康
curl http://127.0.0.1:8787/healthz

# ③ 发一条 QQ 消息后：日志出现 [memory]，库里条数 +1
sqlite3 <qq-bridge-memory 路径>\data\qq-memory.db "select count(*) from conversation_turns"
```

**没有 `[memory]` 就是没接上**，按第 7 节排查。

## 6. 回滚

```powershell
.\revert.ps1 -BridgeDir <qq-bridge 路径>
```

只撤本补丁，不碰你的其它改动。撤完 `git status` 应干净（除你原本就有的改动）。

**不想改代码就先关开关**：`QQ_MEMORY=0` 重启桥接，客户端所有方法立刻变空操作，主链路完全不受影响。
排查问题时先试这一招，再决定要不要真回滚。

## 7. 故障速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 日志完全没有 `[memory]` | `QQ_MEMORY` 不是 `1` | 设成 1 重启；启动日志会打 `QQ_MEMORY=...` |
| 有 `[memory]` 但写库失败 | 侧车没起 / 端口不对 | `curl /healthz`；检查 `QQ_MEMORY_BASE` |
| 日志警告「拿不到机器人 QQ 号」 | 扫码登录后 `selfUserId` 为空 | 会回退到 `state/memory-client.json` 的历史账号；首次部署需成功登录一次 |
| `DSH 地址` 行显示 52011 之类旧端口 | 没设 `DSH_BASE_URL`，用的是 `config.json` | 改 `start.bat` 或 env |
| 回复正常但记忆是旧的 | 侧车重启过，桥接的连接池还活着 | 重启桥接（客户端已带一次重试） |
| `apply.ps1` 报「补丁与当前代码不匹配」 | 上游改过同一处 | 先 `git pull` 到最新再 `-Check`；仍冲突则按 `README-step4.md` 第 7 节钩子表手工合并 |
| `revert.ps1` 报「反向打补丁失败」 | 装完后你又手改过这些文件 | 脚本不会强覆盖；按它给的手动命令处理 |
| 控制台端口 3100 被占用 | 已有桥接实例 | 关掉旧实例；或改 `config.json` 的 console 端口 |

## 8. 升级桥接

```powershell
.\revert.ps1                       # 1. 先撤
cd <qq-bridge 路径>
git pull                           # 2. 拉上游
cd <integration>
.\apply.ps1 -Check                 # 3. 干跑暴露冲突
.\apply.ps1                        # 4. 重装
```

**顺序不能反**：先 `pull` 再 `revert`，补丁上下文已经漂移，回滚会失败。

## 9. 完全卸载

1. `revert.ps1` 撤补丁；
2. 删掉桥接的 `state/memory-client.json`（客户端状态，可选）；
3. 侧车那边的数据库与程序与桥接无关，按需自行保留/清理。