# 音乐灯算法来源

`core/services/spectrum_effects.py` 移植自 eastwoodnet 的
[xiaomi-sound-spectrum](https://github.com/eastwoodnet/xiaomi-sound-spectrum)。
原项目 README 标明 MIT License。

算法及 OH2P 映射的对照版本为
[neqq3/xiaomi-sound-spectrum@33d5d233](https://github.com/neqq3/xiaomi-sound-spectrum/tree/33d5d233e5edc711d88ab9e1240c3269ca52a6f3)：
`led_music.c`、`oh2p_output.h`。

保留了 1024 点 Q14 FFT、双声道八频段 AGC、调色板、两种渲染模式、白色高亮、峰值保持、
2800 块轮换和 OH2P 面积重采样。Python 使用 NumPy 并行执行整数蝶形计算；查表常数按数学
定义生成，已与原表逐项核对。测试用纯人工 PCM 对照独立 C 参考程序的逐帧输出。

Bridge 的 HTTP 传输、对话优先级、进程生命周期和设备状态检查独立实现。

## MIT License

Attribution: eastwoodnet — xiaomi-sound-spectrum; OH2P adaptation: neqq3.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
