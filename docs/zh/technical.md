# 技术文档

> 中文 · [English](../en/technical.md)

## 系统架构

```
PC (48kHz 立体声 16-bit PCM)
  → USB UAC1 音频 RX 回调  (TinyUSB / tud_task 软中断)
  → 立体声混单 + 音量 + 静音
  → 15kHz 带限 + 75µs 预加重 + 限幅（定点整数，无 FPU）
  → SPSC 环形缓冲（4096 采样）
  → PWM slice7 wrap 中断 @ 精确 48kHz（48MHz/1000，最高 NVIC 优先级）
  → pico_fractional_pll_set_freq_u32(carrier + sample·dev/32768)
  → Core1 1MHz PDM 抖动 PLL_SYS fbdiv_int
  → CLK_GPOUT0 → GPIO21 输出 ~87.9MHz
```

- 硬实时音频路径**不经过 Python**；MicroPython 只做配置与控制。
- 系统时钟固定 **48MHz**（pico-fractional-pll 硬性要求，clk_sys 取自
  PLL_USB，PLL_SYS 专用于 RF）。

## 关键工程点

### 1. USB 音频（UAC1）

- 内置 TinyUSB 配置中启用 `CFG_TUD_AUDIO`，配置描述符加入 UAC1 音频块
  （IAD + AC 接口 + AS 接口 alt0/alt1，48kHz 立体声 16bit，ISO OUT 端点
  0x03，同步模式）；
- 音量/静音控制请求（GET/SET CUR/MIN/MAX/RES）在
  `tud_audio_*_req_entity_cb` 中处理，操作系统混音器可用；
- **音频回调必须编译进固件**：`#include "tusb.h"` 必须在 `#if CFG_TUD_AUDIO`
  之前，否则回调被裁剪、控制请求全部 STALL。

### 2. 48kHz 采样时钟

- 用 PWM slice7（TOP=999，分频 1.0）做**精确无抖 48kHz 时基**（定时器只能按
  整数 µs，无法精确 48k）；
- **必须把 PWM_IRQ_WRAP 提到最高 NVIC 优先级（0）**：否则会被同优先级 USB
  中断排队饿死（实测只有 ~26kHz 触发，声音变速+失真）；
- ISR 内**禁止 64 位除法**：`acc = (freq−low)·k >> 32`，`k=2⁶⁴/Δf` 在初始化时
  预计算（M0+ 无 FPU，除法极慢）。

### 3. Core1 PDM 与时钟

- `pico-fractional-pll` 的 Core1 循环以 1MHz 抖动 PLL 反馈分频（PDM），
  **必须 48MHz clk_sys**；
- MicroPython 的链接脚本把 SCRATCH_X 设为 0，库的 `multicore_launch_core1`
  依赖 `.stack1` 会 panic —— 改用 `multicore_launch_core1_with_stack` +
  自带 2KB 栈（放普通 RAM）；
- 不要与 `_thread` 同时使用（都要 Core1）。

## 手台窄带接收（对讲机）

分频器 PDM 以 1MHz 在两个相邻 fbdiv 值间抖动，PLL 模拟环路滤波器对抖动取
平均但不完美，残留的频率抖动决定了可用信噪比：

- 广播 FM（87.9MHz、75kHz 频偏、230kHz 中频）：残留抖动在中频通带内，
  且是 1MHz 的，被 15kHz 音频低通滤掉 → 声音干净。
- 残留抖动随 PDM 步长（基频处 12MHz/总分频）缩放。**REFDIV=2 使步长减半**
  （每 fbdiv LSB 从 12MHz 变 6MHz），同时 PLL 鉴相频率降到 6MHz、环路带宽
  更低 → 对 1MHz 抖动的平均更充分。这是窄带接收的关键。

**实测配方（手台 WIDE/25kHz 模式）：**

| 频段 | 配置 | 效果 |
|---|---|---|
| 2m 144-148MHz（基频） | `reinit 145000000 12000 21` | 清晰，~10dB SNR |
| 433.92MHz（144.64MHz 的 3 次谐波） | `reinit 433920000 12500 21` + `refdiv 2` | **清晰** |
| 409.75MHz（136.58MHz 的 3 次谐波） | `reinit 409750000 12500 21` + `refdiv 2` | 清晰，略噪 |

UHF 目标（高于约 150MHz 基频上限）由较低基频的 3 次奇次谐波承载：PLL 跑在
target/3，手台收 target（频偏同样 ×3）。REFDIV=2 把基频 PDM 步长减半到约
0.6MHz（refdiv 1 时为 1.2MHz）——这就是 UHF 谐波链路比 refdiv 1 的 2m
基频更干净的原因。

- `pdm >1MHz` 实测更差（M0+ systick 延迟与 PLL 写入时序失效）——保持 1。
- `refdiv 2` 在 2m 段中性，但对 UHF 谐波配方是必需的。
- 不重启的在线 PLL 重初始化（deinit/init）会死机——pdm/refdiv 持久化并在
  下次启动生效。
- 发射频率会比标称偏几 kHz（晶振误差 × 谐波次数）——手台调到实际信号即可。
- 法律/安全：409MHz 的基频（136.6MHz）落在民航频段（118-137MHz）内且功率
  比 409MHz 信号更强——任何持续使用都必须加带通滤波器抑制基频。433MHz
  的基频在 2m 业余段，无此问题。

### PLL 参考分频器（refdiv）选择策略

瞬时频率在 1 MHz 的 PDM 节拍上以 `step = ref/div` 在相邻两个反馈分频值
之间跳跃，这就是射频残余抖动的硬下限。`refdiv 2` 把参考从 12 MHz 降到
6 MHz，在**每个**频段把 `step` 减半（见 `tools/pll_range.py bands`）：

| 频段 | 频率 | refdiv 1 的 step | refdiv 2 的 step |
|---|---|---|---|
| 160m | 1.840 MHz | 不可达 | **7 kHz** |
| 80m | 3.573 MHz | 不可达 | **16 kHz** |
| 40m | 7.074 MHz | 80 kHz | **30 kHz** |
| 30m | 10.136 MHz | 80 kHz | **40 kHz** |
| 20m | 14.074 MHz | 120 kHz | **60 kHz** |
| 10m | 28.074 MHz | 240 kHz | **120 kHz** |
| 2m | 145.000 MHz | 1.2 MHz | **600 kHz** |
| 433.92 MHz（×3） | 144.640 MHz | 1.2 MHz | **600 kHz** |
| FM 广播 | 87.900 MHz | 750 kHz | 375 kHz（但见下） |

**但是 refdiv 2 不能用在载波需要“停波”的频段。** 广播 FM（87.5–108 MHz）
在静音时把未调制载波停在 fc，此时 PDM 图案会改变并在静音载波上产生可听
的嗡声（硬件实测，见 v0.22.x 的 FM 静音回归）。所以规则是：

```
refdiv_for(target) = 2 if (该频段静音时关断射频) else 1
```

即广播 FM 用 1，其余（2m、UHF 谐波、短波窄带）用 2。控制台新增
`refdiv <1|2|auto>`（默认 auto）；配置里已有显式的 1/2 时以配置为准。
refdiv 2 会把可达 PLL 窗口变窄，因此启动时如果 `init` 失败会自动回退到
refdiv 1（失败的 `init` 不会启动 core1，所以重试是安全的——这与「运行中
deinit/init 会死锁」是两回事），并把 1 写回配置避免每次开机重试。

`status` 现在会显示实际的 step（`pico_tx.range()` 返回的窗口宽度就是
一个反馈分频步）。另外 `tools/pll_range.py minstep` 验证了 C 的
`calculate_pll_divider()` 取到的解**已经是同 pass 内 step 最小的解**
（HF 559 个窗口 + UHF 129 个窗口，0 个反例），所以无需修改 PLL 搜索。

### 5. 诊断

- `diag`：1 秒内 ISR 触发数、RX 入队帧数，以及环形缓冲漂移计数
  （underflow = 主机时钟偏慢、空环时重复上一采样；drop = 主机时钟偏快、
  环满丢弃）；
- `pwm`：clk_sys、PWM 寄存器、ISR 单次耗时（2~6µs 正常）；
- `pll`：PLL ready / 实际输出范围 / 最后一次写入频率；
- `ring`：环形缓冲水位（平衡时应接近 0%，99% 说明消费端异常）。

## 构建原理

`build.sh` 克隆干净的 MicroPython（**依次尝试维护者 fork
`git.sr.ht/~bytewolf/micropython` 与官方 upstream**）→ 应用
`patches/micropython-tx.patch`（已应用则跳过）→ `make submodules` →
`make BOARD=RPI_PICO_TX`。补丁包含：
- `ports/rp2/boards/RPI_PICO_TX/`（新板：48MHz 时钟 + USB_AUDIO + FM 宏 + 独立
  VID/PID 0x1209:0xFA50 + USB 字符串）；
- `ports/rp2/tx/`（pico_tx 用户 C 模块：**内置 pico-fractional-pll
  库源码**（BSD-3-Clause，Kazuhisa Terasaki）+ 调制器 + MicroPython 模块）；
- `ports/rp2/main.c`（48MHz 启动）、`modmachine.c`（禁用 machine.freq 设置）；
- `shared/tinyusb/`（UAC1 描述符与配置）。

[kaduhi/pico-playground（fm_transmitter 分支）](https://github.com/kaduhi/pico-playground)
仅作参考（UAC1 描述符结构与 FM 调制思路），未包含其代码。

### 构建前置依赖（Ubuntu/Debian）

`build.sh` 是单条自动命令，但宿主机必须先装好 RP2040 交叉工具链。
Ubuntu/Debian 示例：

```bash
sudo apt update
sudo apt install -y \
    build-essential git \
    cmake \
    gcc-arm-none-eabi \
    libnewlib-arm-none-eabi \
    libstdc++-arm-none-eabi-newlib \
    python3
```

- `build-essential` —— `make`、`gcc` 等（驱动整个构建）；
- `git` —— `build.sh` 要克隆 MicroPython（必需，没有离线 tar 包回退）；
- `cmake` —— pico-sdk 的构建系统；
- `gcc-arm-none-eabi` —— ARM Cortex-M0+ 交叉编译器（10.x 及以上均可）；
- `libnewlib-arm-none-eabi` + `libstdc++-arm-none-eabi-newlib` —— 目标机的
  newlib C 库 / C++ 运行库，pico-sdk 必需；
- `python3` —— MicroPython 构建脚本使用。

以下仅在上传/烧录时需要（`build.sh` 本身不需要）：`mpremote`
（`pip install mpremote`，用于上传 `main.py`）与 `picotool`
（`tools/flash.sh` 使用）。可用 `arm-none-eabi-gcc --version` 检查工具链
（应为 10.3.x 或更新）。

## 更新补丁

```bash
cd micropython
git add -A
git diff --cached > ../release/patches/micropython-tx.patch
git reset -q
```
