# 原厂对话灯效（实验功能）

当前适配 OH2P 1.62.2，默认关闭。实现位于 Bridge，使用现有 Client 的录音流和
`run_shell`，不要求重新刷固件、更新 Client 或安装设备端二进制。

另可独立启用[音乐频谱灯效](music-visual.md)。两者共用 9093 端口，Bridge 对话优先于音乐。
下文 `enabled` 控制对话灯；音乐由 `music.enabled` 控制。

统一适配分支 `dev/native-conversation-visual` 同时包含 OH2P 和 [LX06 1.94.13 原型](lx06-native-visual.md)。
两款设备使用同一套上层接口和镜像，不需要按型号切换分支。LX06 必须显式选择实验 profile，
未经过实机验证。下面兼容表描述默认 `profile="auto"` 的行为。

适用于共享 `ExternalConversationController` 的 `local_asr` 对话；OpenAI、OpenClaw、
QwenPaw 均通过同一阶段接口进入。原厂 `xiaoai_asr` 继续使用自身会话。
回答灯目前仅随 `tts_speaker=xiaoai` 的原厂 TTS 路径生效。

## 兼容范围

| 设备 / 使用方式 | 当前行为 |
| --- | --- |
| OH2P / 1.62.2 / `local_asr` | 显式启用并通过检查后使用三阶段灯效；回答灯要求 `tts_speaker="xiaoai"` |
| LX06、其他型号或 OH2P 其他固件 | 未适配；跳过新增灯效，继续原有对话流程 |
| `xiaoai_asr` | 保持原厂会话，不接入本功能 |
| 没有 `native_visual` 配置或 `enabled=False` | 保持原有流程，不探测设备、不启动灯效端口 |

型号和版本检查是已验证范围的限制，不是通用能力认证。不能通过改型号名单来代替适配和实机测试。
本功能也不会改变原厂“小爱同学”的唤醒词或把其他后端自动切换到 `local_asr`。

## 配置与默认值

沿用项目的 `config.py` / `APP_CONFIG` 配置方式；灯效参数不需要额外的环境变量。
仓库默认配置如下，已有用户配置可以完全省略这一段：

```python
"native_visual": {
    "enabled": False,
    "profile": "auto",
    "public_url": "",
    "bind_host": "0.0.0.0",
    "port": 9093,
    "listening_gain": 0.25,
},
```

| 参数 | 默认值 | 用途 / 取值 |
| --- | --- | --- |
| `enabled` | `False` | Python 布尔值；`True` 请求启用，仍需通过设备检查 |
| `profile` | `"auto"` | 自动选择已验证型号；LX06 实验值及限制见专门说明，不能绕过型号/版本检查 |
| `public_url` | `""` | 启用时填写音箱可访问的 HTTP 地址，包含映射后的端口；不带路径、用户名、密码、查询串或片段 |
| `bind_host` | `"0.0.0.0"` | Bridge 进程的监听地址；Docker bridge 网络中通常保持默认 |
| `port` | `9093` | Bridge 内监听端口；选择未占用的整数端口 1～65535 |
| `listening_gain` | `0.25` | 白灯幅度系数，范围 0.05～1.0；数值越大，白灯越敏感 |

`listening_gain` 与 `audio_input.gain` 相互独立，不改变 ASR/KWS 输入或扬声器音量。
OH2P 1.62.2 实测中，测试者反馈 0.30 比 0.25 更接近原厂白灯灵敏度，可从 0.25 开始微调；
这不是其他设备的通用校准值。

### 最小启用配置

在原有 `APP_CONFIG` 中添加以下字段即可，其余字段使用上述默认值：

```python
"native_visual": {
    "enabled": True,
    "public_url": "http://192.168.1.100:9093",  # 改成运行 Bridge 的主机局域网地址
},
```

需要调白灯时再添加 `"listening_gain": 0.30`。使用的后端还应配置
`"input_mode": "local_asr"` 和 `"tts_speaker": "xiaoai"`，并通过原有唤醒路由进入对话。
修改配置后重启 Bridge；修改监听地址或端口也应重启，不依赖热重载重新绑定端口。

## 最小 Docker Compose 示例

仓库根目录的 [docker-compose.native-visual.yml](../docker-compose.native-visual.yml)
是可独立使用的示例，沿用主 Compose 的 OpenClaw 后端和配置挂载方式，仅发布 Client
连接端口 4399 和灯效回送端口 9093。灯效不依赖 REST API，因此这里不启用 9092。
示例从当前源码构建；不要假定上游 `latest` 已包含尚未合入的改动。

```yaml
services:
  open-xiaoai-bridge:
    build: .
    image: open-xiaoai-bridge:native-visual-local
    restart: unless-stopped
    ports:
      - "4399:4399"
      - "9093:9093"
    environment:
      - LOGLEVEL=INFO
      - OPENCLAW_ENABLE=1
    volumes:
      - ./config.py:/app/config.py:ro
      - ./openclaw:/app/openclaw
      - ./models:/app/core/models
```

1. 在包含本功能的仓库根目录操作，按 README 将 VAD / KWS / ASR 模型放进 `./models`。
2. 编辑现有 `config.py`：填写 OpenClaw 的 `url`、`token`，保持 `local_asr`、`tts_speaker="xiaoai"`，
   再添加上面的最小灯效配置。容器内的 `127.0.0.1` 指容器自身；后端地址应从容器可达。
3. 使用以下命令检查配置、构建并启动：

```bash
docker compose -f docker-compose.native-visual.yml config --quiet
docker compose -f docker-compose.native-visual.yml up -d --build
docker compose -f docker-compose.native-visual.yml logs -f
```

4. 将现有音箱 Client 连接到这台主机的 4399 端口，使用配置中的 OpenClaw 唤醒词测试。
   这份示例与原服务使用相同端口，切换前应停止占用端口的旧实例；不要并行启动争用端口。

已有部署只需在原 Compose 中增加 `9093:9093`，并使用包含灯效代码的镜像；保留原来的后端、
环境变量、配置和模型挂载即可。上面的示例不是要求已有用户改用 OpenClaw。
如使用 OpenAI-compatible 或 QwenPaw，按 README 换成对应的后端开关、连接配置及唤醒路由。

### 宿主机端口不同的情况

例如宿主机 9093 已被占用，可改映射为 `19093:9093`：

```yaml
ports:
  - "4399:4399"
  - "19093:9093"
```

此时 `public_url` 改为 `http://192.168.1.100:19093`，`native_visual.port` 仍为 `9093`。
`public_url` 不能填 `0.0.0.0`、`localhost` 或仅容器内部可解析的服务名；它是音箱访问 Bridge 的地址。
回送流供局域网音箱使用，不应把该端口映射到公网。

对话灯和音乐灯都关闭时不启动监听；启动后收音流使用随机短期令牌，限制为单消费者。
等待阶段只发送租约心跳，不发送麦克风。请求访问日志关闭，语音不保存到文件。

### 常见问题

- **能对话但没有灯效**：确认使用包含本功能的版本，检查设备 / 固件、`enabled`、后端输入模式和日志中的“原厂灯效未启用”原因。
- **等待或白灯无法正常工作**：核对音箱到 `public_url` 的路由、防火墙以及宿主机 / 容器端口映射。
- **白灯太敏感或不敏感**：只调 `listening_gain`，从 0.25 小幅调整；不要为调灯效修改 ASR 输入增益。
- **恢复原行为**：把 `enabled` 改回 `False` 并重启 Bridge；不需要重新刷固件。

## 阶段与实现

- 收音：增益前的16kHz单声道PCM副本，在主机转换为48kHz双声道S16，按已校准的
  默认1/4增益回送（可通过 `listening_gain` 调整）。音箱预装 curl 写入已有 `mic_audio.fifo`，由原厂 vis/ledd 驱动白灯。
  输入回调不等待网络，缓存只留最新块，按10ms包节奏输出。
- 等待：原厂蓝色移动短条，收到回复或会话结束时撤销。白灯和蓝灯的状态字符串不同，
  适配器分别处理。
- 回答：`mibrain text_to_speech(save=0, play=1)`，由原厂播放器产生随声音变化的灯效。
  UBus 返回后仍需等待观察到播放开始、再回到闲置。失败或结果不明不自动重复播报。

数字事件和设备命令集中在 `core/services/native_visual.py` / `oh2p_visual_phase.sh`，
业务层只调用 `phase`、`clear`、`speak`。没有新 Client RPC。

## 目前的边界

设备没有提供给本功能使用的独占资源锁，状态轮询也不是锁。观察到原厂麦克风写端、
播放器活动或灯态变化时，适配器退出并让出控制；原厂唤醒回调会撤销回送。
这不能证明任意并发情况下完全无干扰，因此本功能仍需实机共存回归，不能默认开启。

一次阶段最长60秒，录音断流约500ms就结束回送。设备脚本独立监护自己的 curl，覆盖
FIFO 打开和写入阻塞；仅设置 curl 网络超时不足以处理写满的 FIFO。RPC 取消并不等于
设备进程退出；清理未确认时禁用后续灯效尝试，直至应用重启。

播放完成目前是设备全局状态观测，没有带请求归属的完成凭据。无法观察到开始、遇到
暂停/中断或超时就退出本轮，不盲目回退重播，也不以全局暂停来取消未知归属的播放。

## 验证

```bash
python -m unittest discover -s tests -p 'test*visual*.py' -v
python -m unittest discover -s tests -p test_wakeup_keywords.py -v
python -m unittest discover -s tests -p test_openai.py -v
```

POSIX shell 格式回归需在 Linux 上运行。还需真实设备和观察者确认三阶段灯效、连续
多轮、无语音超时、原厂唤醒抢占、断网和进程退出；单元测试不替代这些观察。

测试 Hermes 可直接使用上游现有 OpenAI-compatible 后端，配置其 `base_url/api_key/model`，
并使用独立 `session_key`。不需要先合入 Hermes 专属后端 PR。
