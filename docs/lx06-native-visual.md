# LX06 1.94.13 灯效原型（未实机验证）

LX06 与 OH2P 在 `dev/native-conversation-visual` 中共用对话阶段接口，设备差异由 profile 选择。
不提供独立的 LX06 产品分支或专用镜像。依据是上游发布的 LX06 1.94.13 原版固件，
不是 LX06 实机回归结果。默认 `profile="auto"` 仍只选择已实测的 OH2P 1.62.2。
本原型不要求刷写新固件或更换 Client，不改设备启动项。

## 启用范围

仅供 **LX06 / 1.94.13** 设备的维护者实验。需明确选择 profile，仍会核对音箱真实型号和版本：

```python
"native_visual": {
    "enabled": True,
    "profile": "lx06_1.94.13_experimental",
    "public_url": "http://192.168.1.100:9093",
},
```

端口、Docker 和后端配置沿用 [原厂灯效部署说明](native-visual.md)。实验分支也可使用
`docker compose -f docker-compose.native-visual.yml up -d --build` 从当前源码构建。
`local_asr` 才进入本功能；原厂 TTS 路径要求后端 `tts_speaker="xiaoai"`。
未知型号、未知固件及 profile 与设备不符时跳过灯效，继续原有对话。

## 目前实现

- 收音阶段申请 LX06 原厂动画 `L=1, pos=0`；这表示阶段提示，不包含声源方向估计。
- 等待阶段申请原厂动画 `L=2, pos=0`。
- 回答沿用原生 `mibrain text_to_speech(save=0, play=1)`，观察播放器开始后回到闲置。
  固件已注册这组参数，但该调用能否自动产生回答灯、播放器观测是否可靠，尚待实机验证。
- 单独解析 LX06 `led status` 的嵌套 `effect` 数组；不使用 OH2P 的状态字符串。
- 9093 只发送短期控制心跳，不回送麦克风 PCM。因此 `listening_gain` 对 LX06 没有调灯效果。
- 正常结束发送清理标记；原厂唤醒回调发送保留标记。只在正常结束且灯态仍匹配时撤销本轮灯。
- 播放器忙、灯态未知、出现其他动画时退出；不调用全局 `shut_all`，不重启原厂服务。

## 已知限制与验证要求

固件中的 `ledserver`、PNS 动画字符串和 UBus 方法表支持上述接口推断，但不能替代实机观察。
还不能承诺具体颜色、动态效果、TTS 回答灯或长期共存正常。
没有发现可复用 OH2P `mic_audio.fifo` 的证据，所以没有实现“随收音音量变化”的灯效。

灯效归属是状态轮询，不是硬件提供的独占锁。原厂恰好使用相同动画和位置时，仅靠快照不能
识别；唤醒回调和控制标记降低误清理风险，但不能消除所有竞态。
如果断网或 Bridge 崩溃导致结束标记缺失，原型不猜测归属而关灯，可能留下阶段提示灯；
需要在 LX06 实机验证原厂唤醒能否恢复。远端 curl/脚本都有期限，不常驻执行。
这是独立实验分支，不建议在无人观察的设备上默认启用。

实机验收需要覆盖：收音/等待/回答、无语音超时、连续多轮、原厂唤醒抢占、播放音乐时进入、
静音/夜间模式、Bridge 退出及网络断开。每项记录灯态、播放器状态和恢复行为。
没有 LX06 设备时，只报告静态依据和离线测试结果，不将该型号列入已验证支持列表。

## 可复核依据与测试

- [上游原版固件](https://github.com/idootop/open-xiaoai/releases/tag/LX06_1.94.13)
- 镜像 SHA-256：`317a54909f12b63f22128965bf9005c06435d15f61c4202bb88c4524bf1e2ef4`
- `/etc/init.d/led` 启动 `/bin/ledserver`；`show_led` 使用 `L` / `pos` / `rgb`。
- `ledserver` ARM VA `0x1504c` 返回 `info`，`0x147c8` 构造 `effect` 数组。
- `mibrain_service` ARM VA `0x1d070` 的参数表包含 `text/caller/vendor/codec/volume/save/play`。

```bash
python -m unittest discover -s tests -p 'test*visual*.py' -v
```

Linux 测试使用隔离的 UBus/curl/jshn 替身执行完整 shell；状态 fixture 按反汇编构造，
不是实机采集数据。HTTP 测试覆盖正常清理与原厂接管标记，以及不传输麦克风的行为。
