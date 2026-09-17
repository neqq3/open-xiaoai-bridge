# 原厂对话灯效（实验功能）

当前适配 OH2P 1.62.2，默认关闭。实现位于 Bridge，使用现有 Client 的录音流和
`run_shell`，不要求重新刷固件、更新 Client 或安装设备端二进制。

适用于共享 `ExternalConversationController` 的 `local_asr` 对话；OpenAI、OpenClaw、
QwenPaw 均通过同一阶段接口进入。原厂 `xiaoai_asr` 继续使用自身会话。
回答灯目前仅随 `tts_speaker=xiaoai` 的原厂 TTS 路径生效。

## 配置

```python
"native_visual": {
    "enabled": True,
    "public_url": "http://你的Bridge局域网地址:9093",
    "bind_host": "0.0.0.0",
    "port": 9093,
    "listening_gain": 0.25,
},
```

Docker 需要额外映射 `9093:9093`。这是独立于可选 REST API 的有限音频回送端口。
功能关闭时不启动监听；启动后也只有当前收音阶段持有的随机短期令牌能消费一次流。
等待阶段只发送租约心跳，不发送麦克风。请求访问日志关闭，语音不保存到文件。

`listening_gain` 仅调整白灯幅度，范围0.05～1.0，与 `audio_input.gain` 相互独立。
例如从0.25改为0.30会使可视化幅度增加20%，不改变识别或音量。

## 阶段与实现

- 收音：增益前的16kHz单声道PCM副本，在主机转换为48kHz双声道S16，按已校准的
  1/4增益回送。音箱预装 curl 写入已有 `mic_audio.fifo`，由原厂 vis/ledd 驱动白灯。
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
