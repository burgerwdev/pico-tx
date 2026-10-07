# FM 发射音质分层审计

> [English](../en/audio-quality.md) · 中文

本文逐层审计本仓库中影响**接收端声音质量**的代码，给出可量化的限值与影响排序。
所有数据来自 host 端脚本 `tools/audio_quality.py`（逐行复刻
`fm_modulator.c: fm_audio_process()` 的定点语义与系数）。

> **本分析不刷写开发板、不做硬件实测。** 文中每个数字都可用下列命令在主机上
> 复现：
> ```bash
> python3 tools/audio_quality.py resp
> python3 tools/audio_quality.py thd
> python3 tools/audio_quality.py limiter
> python3 tools/audio_quality.py drift
> ```

---

## 1. 分层与影响排序

| 层 | 位置 | 限值 | 对听感的影响 | 优先级 |
|---|---|---|---|---|
| **L3 音频 DSP 链** | `fm_modulator.c: fm_audio_process()` | 预加重曲线非标准：2 kHz 处多 +8.6 dB、15 kHz 处少 −6.3 dB；一阶低通 ×2 在 10 kHz 掉 −2.2 dB；限幅器无前瞻 | **音色（中频隆起、高频发闷）+ 大声压时严重失真（THD 18–26%）** | **P0** |
| **L1 USB 音频与异步采样时钟** | `shared/tinyusb/*`、`tud_audio_rx_done_isr()` | 主机 48 kHz 与 PWM 48 kHz 是两颗独立晶振，无重采样、无反馈端点、无漂移计数 | 长时间播放后周期性 click（>14 分钟后开始，持续存在） | **P1** |
| **L5 PLL/PDM 射频层** | `pico_fractional_pll.c` | 瞬时频率按 `ref/div` 步进在 1 MHz 上抖动，PLL 环路滤波只部分平均 | 射频相噪/接收端噪底（窄带与弱信号尤其明显） | **P1** |
| **L2 单声道混音与环形缓冲** | `tud_audio_rx_done_isr()`、`fm_pwm_wrap_handler()` | 混音本身无缺陷（int32 不溢出、反相抵消为单声道固有）；环形缓冲无丢样/欠载计数器，欠载时清零而非保持 | 混音无可听影响（−91.8 dBFS 截断）；缓冲问题掩盖 L1 并把丢样放大为 dropout | **P2** |
| **L6 USB 控制面与默认值** | `fm_modulator.c: fm_db256_to_gain()`、`main.py` | UAC1 音量按 **dB→线性增益**线性映射，不是 dB→dB | 音量滑条标定错误（请求 −30 dB 实得 −6 dB） | **P2** |
| **L4 调制映射** | `fm_pwm_wrap_handler()` | `freq = carrier + (sample*dev)>>15` | 无（0.99997× 满偏离，可忽略） | — |

优先级判据：L3 直接决定音色与失真且可 100% 在 host 端验证；L1/L5 决定"长听"与
"弱信号"的可用性；L2/L6 是正确性与可观测性。

---

## 2. 逐层审计

### L3 音频 DSP 链（P0）

处理链（`preemph` 打开时）：`2 Hz 直流阻断 → 一阶 15 kHz 低通 → 预加重 → 一阶
15 kHz 低通 → 2:1 软拐点限幅器`。

**缺陷 1：预加重不是标准曲线。** 实现是 `y = lp15 + (lp15 - lp75)*5`，即一个
**上限 +14 dB 的搁架**（20·log10(5)），转折在 ~1 kHz 就开始。标准 75 µs
预加重是 +6 dB/oct 的斜升，2.12 kHz 处为 0 dB 参考、15 kHz 处 +17.0 dB。

```
  f (Hz)  75us     （发射端实测响应，dB）
  1000    7.75
  2000    11.33
  5000    13.20
  15000   10.80

  净接收响应 = 发射响应 − 标准接收端去加重（即听众听到的）
  f (Hz)     75us net
  1000           6.88
  2000           8.57
  5000           5.04
  10000         -1.44
  15000         -6.27
```

**结论：接收端听到的是「2 kHz 附近 +8.6 dB 的中频隆起 + 15 kHz 处 −6.3 dB 的
高频塌陷」。** 这不是"没开预加重"，而是曲线形状错了——关掉预加重只会让高频
更闷（接收机去加重是固定的 −6 dB/oct）。

**缺陷 2：预加重吃掉了 7–8 dB 动态余量。** 因为 1 kHz 就被抬了 7.75 dB：

```
  tone    level      preemph 75   preemph off  clips(75us)
  1000    loud          17.859 %      0.002 %        0
  5000    loud          25.945 %      0.000 %        0
  1000    linear         0.033 %      0.013 %        0
```

`loud` = 幅值 20000（约 −4 dBFS）的正弦。标准 75 µs 曲线在 1 kHz 只有
+0.87 dB，同样的输入不会进入限幅器。**所以这是预加重曲线错误直接导致的失真，
而不是限幅器本身的问题。**

**缺陷 3：带限只有一阶 ×2。**

```
  f (Hz)  lp2only   （仅两级带限低通，dB）
  5000     -0.66
  10000    -2.16
  15000    -3.67
  23000    -4.89
```

通带在 10 kHz 已掉 2.2 dB、15 kHz 掉 3.7 dB（对 15 kHz 广播音频带宽而言偏大）；
阻带在 23 kHz 仅 −4.9 dB，对 48 kHz 采样镜像几乎没有抑制。

**缺陷 4：预加重关闭时连带限一起没了。** `if (!s_preemphasis_enabled)` 分支
在宽带模式下直接 `return x`（不经过任何低通），只有窄带模式才保留了 3 kHz
低通。见上表 `off` 列全程 0.00 dB。

**缺陷 5：限幅器无前瞻。** 阶跃输入下 200 个采样点处于软拐点之上：

```
  step 0->30000 [+pre]   overshoot=200 samples above knee, clips=0
```

（软拐点本身是刻意的节目处理，不一定要改；但与缺陷 2 叠加时失真被放大。）

### L1 USB 音频与异步采样时钟（P1）

- 描述符：UAC1、48 kHz、16-bit、立体声、`wMaxPacketSize=192`、`bInterval=1`；
- `CFG_TUD_AUDIO_ENABLE_FEEDBACK_EP = 0`：**没有反馈端点**，主机按自己的晶振
  自由发送 48000 样本/秒；
- 消费端是 PWM slice 7，`48 MHz / 1000`，由 RP2040 的 12 MHz 晶振决定；
- 两端之间**没有异步采样率转换（ASRC）**；漂移计数已由改动 4 补上（§4）。

```
  ppm    time to hit    glitch rate      glitches in 600s
         the limit      (samples/s)
  -100   427 s          4.80             832
  -50    853 s          2.40             0
  -25    1707 s         1.20             0
  100    427 s          4.80             832

  Ring depth = 4096 samples = 85.3 ms of buffering
```

典型晶振误差 10–50 ppm → **缓冲约 14–28 分钟后饱和，之后以 0.5–2.4 次/秒
持续丢样/欠载**（每次都是一个采样级不连续 = 一声轻 click）。两颗自由振荡的
晶振永远不会自己重新对齐，所以这是"听久了必现"的问题。

### L5 PLL/PDM 射频层（P1）

音频 DSP 决定"送进去什么"，这一层决定"发出去多干净"。瞬时频率在 1 MHz 的
PDM 节拍上以 `step = ref/div` 抖动，PLL 环路滤波器只部分平均：

```
$ python3 tools/pll_range.py check 87900000 75000 1
... div=16 ... PDM step=750.000 kHz
$ python3 tools/pll_range.py check 87900000 75000 2
... div=16 ... PDM step=375.000 kHz
$ python3 tools/pll_range.py check 7074000 3000 1
... div=150 ... PDM step=80.000 kHz
$ python3 tools/pll_range.py check 7074000 3000 2
... div=200 ... PDM step=30.000 kHz
```

`refdiv 2` 在**所有**频段把步进减半，但 `main.py` 原规则只在目标 >150 MHz
（UHF 谐波）时设置 `refdiv 2`。注意 host 端无法算出环路滤波后的**残余**抖动
（需要环路带宽与相噪实测），所以这里只能给出可计算的步进，残余量留给硬件实测。

→ **已由改动 5 修复（§4）**：改为数据驱动规则并新增 `refdiv auto`。

### L2 单声道混音与环形缓冲（P2，缓冲部分已由 §4 改动 4 修复）

**混音部分：审计结论是无缺陷，不需要修改。** `tud_audio_rx_done_isr()` 先做
`mono = ((int32_t)frame[0] + frame[1]) >> 1`，再 `mono = (mono * volume) >> 15`：

```
$ python3 tools/audio_quality.py mono
    L=R=30000  -> 29999  (相关内容为 1:1)
    L=+30000 R=-30000 -> 0  (反相完全抵消：单声道下混的固有性质)
  volume     mean err (LSB) rms err (LSB)  equiv. noise floor
  0 dB       -0.750         0.841          -91.8 dBFS
  -6 dB      -0.376         0.469          -96.9 dBFS
  -40 dB     -0.502         0.579          -95.1 dBFS
```

- int32 求和后右移，不可能溢出；相关内容 1:1，不引入增益错误；
- 唯一 artefact 是「截断而非四舍五入」：满音量下混音取整（−0.25 LSB）与音量
  取整叠加为 −0.75 LSB 均值 / 0.84 LSB RMS = **−91.8 dBFS**，比理想四舍五入
  （0.289 LSB RMS = −101.1 dBFS）差约 9 dB，远低于任何可听阈值；其 DC 分量由
  2 Hz 直流阻断移除。所以不为它增加取整逻辑。

**环形缓冲部分：改前状态如下（已修复）。**

- `fm_pwm_wrap_handler()` 在 ring 空时执行 `s_cur_sample = 0` → 该拍载波停在
  fc（一次 dropout），而不是保持上一个采样；
- ring 满时静默丢样（注释写了 "drop sample"）；
- 两者**都不计数**，`diag` 只暴露 `ISR ticks` 和 `RX frames`，无法发现 L1 的
  漂移。

### L6 USB 控制面与默认值（P2）

`fm_db256_to_gain()` 把 UAC1 的 dB 值**线性**映射到线性增益：

```c
return (uint16_t)(((clamped - FM_VOL_MIN) * 32767) / (FM_VOL_MAX - FM_VOL_MIN));
```

于是 `gain_lin = (db + 60) / 60`，实际衰减 = `20·log10((db+60)/60)`：

| 请求 (dB) | 实际 (dB) | 误差 |
|---|---|---|
| −6 | −0.92 | +5.1 |
| −12 | −1.94 | +10.1 |
| −20 | −3.52 | +16.5 |
| −30 | −6.02 | +24.0 |
| −40 | −9.54 | +30.5 |
| −50 | −15.56 | +34.4 |
| −60 | −∞ | — |

即音量滑条只有顶部约 10 dB 是准的。

→ **已由改动 6 修复（§4）**。

其余默认值（`DEFAULT_PREEMPH="on"(75us)`、`DEFAULT_SQUELCH=0`、
`DEFAULT_CARRIER=87.9 MHz`）本身合理；`refdiv` 选择规则见 L5。

### L4 调制映射（可忽略）

`freq = s_carrier_hz + ((int64_t)sample * deviation) >> 15`：满度 ±32767 对应
0.99997× 偏离，0 对应精确停载波——都是刻意且正确的。FM 不需要对音频做预积分。
48 kHz 采样对 15 kHz 音频带宽有 3.2× 过采样，够用。

---

## 3. 完整基线输出

<details>
<summary>python3 tools/audio_quality.py resp</summary>

```
频率响应（dB 相对输入；小信号，未触发限幅器）
  f (Hz)  75us      50us      300us     off       nfm300    lp2only
  20      0.04      0.01      0.35      -0.00     -20.41    -0.00
  50      0.10      0.05      1.25      -0.00     -14.63    -0.00
  100     0.29      0.12      3.34      -0.00     -6.81     -0.00
  300     1.86      0.85      9.53      -0.00     6.28      -0.01
  1000    7.75      5.01      14.24     -0.00     12.82     -0.03
  2000    11.33     8.87      14.91     -0.00     11.62     -0.11
  3000    12.62     10.71     14.94     -0.00     9.05      -0.25
  5000    13.20     11.99     14.61     -0.00     3.84      -0.66
  8000    12.76     11.93     13.79     -0.00     -2.28     -1.51
  10000   12.21     11.49     13.14     -0.00     -5.29     -2.16
  12000   11.63     10.96     12.51     -0.00     -7.67     -2.80
  14000   11.06     10.44     11.91     -0.00     -9.54     -3.39
  15000   10.80     10.18     11.64     -0.00     -10.31    -3.67
  16000   10.56     9.96      11.39     -0.00     -10.98    -3.92
  18000   10.14     9.56      10.97     -0.00     -12.05    -4.35
  20000   9.84      9.27      10.65     -0.00     -12.78    -4.65
  22000   9.65      9.08      10.46     -0.00     -13.22    -4.85
  23000   9.61      9.04      10.41     0.00      -13.32    -4.89

  净接收响应 = 发射响应 − 标准接收端去加重
  f (Hz)     75us net   50us net  300us net
  20             0.04       0.01       0.35
  50             0.10       0.04       1.21
  100            0.28       0.12       3.19
  300            1.78       0.81       8.33
  1000           6.88       4.61       7.66
  2000           8.57       7.42       3.09
  3000           7.85       7.95      -0.25
  5000           5.04       6.59      -4.93
  8000           0.94       3.29     -9.80
  10000         -1.44       1.13    -12.37
  12000         -3.56      -0.87    -14.59
  14000         -5.43      -2.65    -16.52
  15000         -6.27      -3.47    -17.40
  16000         -7.07      -4.24    -18.20
  18000         -8.49      -5.62    -19.65
  20000         -9.69      -6.81    -20.88
  22000        -10.70      -7.80    -21.90
  23000        -11.13      -8.22    -22.33
```
</details>

<details>
<summary>python3 tools/audio_quality.py thd / limiter / drift</summary>

```
谐波失真 (THD, %)，1 s 窗口，2..10 次谐波
  tone       level        preemph 75   preemph off  clips(75us)
  1000       linear       0.033        0.013        0
  1000       loud         17.859       0.002        0
  1000       over         28.778       0.822        0
  5000       linear       0.005        0.004        0
  5000       loud         25.945       0.000        0
  5000       over         28.374       0.545        0
  10000      linear       0.006        0.000        0
  10000      loud         1.946        0.000        0
  10000      over         0.758        0.268        0

限幅器 / 峰值偏离
  signal                       peak_out   peak/dev      clips
  full-scale 1kHz sine [+pre]      29491     0.9000          0
  full-scale 1kHz sine [flat]      32768     1.0000          0
  full-scale 6kHz sine [+pre]      29479     0.8996          0
  full-scale 6kHz sine [flat]      32768     1.0000          0
  full-scale 12kHz sine [+pre]     27336     0.8342          0
  full-scale 12kHz sine [flat]     32768     1.0000          0
  step 0->30000 [+pre]       overshoot=200 samples above knee, clips=0
  step 0->30000 [flat]       overshoot=200 samples above knee, clips=0

异步采样时钟漂移（见 L1 表）
```
</details>

---

## 4. 已落地的改动与改前改后数据（host 端）

三项改动均在 `fm_modulator.c` 的定点路径内，无除法、无浮点；
`python3 tools/audio_quality.py compare` 可完整复现下表。补丁已重新生成
（`patches/micropython-fm.patch`）并**编译通过**（`make BOARD=RPI_PICO_FM`，
仅编译，未刷写）。

### 改动 1：标准预加重曲线

- 旧：`y = lp15 + (lp15-lp75)*5` —— 固定 +14 dB 搁架
- 新：一阶差分 `y = x + G*(x-x[n-1])`，`G = tau*fs`（Q10）：
  75 µs → 3686、50 µs → 2458、300 µs → 14746
- 语义变化：预加重关闭时不再丢失带限（旧代码在宽带 + 关闭时整条链无低通）

```
净接收响应（听众听到的；0 dB = 正确）
  f (Hz)     old 75us   new 75us
  1000           6.88       0.22
  5000           5.04       0.91
  8000           0.94       0.82
  10000         -1.44       0.56
  14000         -5.43      -0.03
  15000         -6.27      -0.37
  0.3-15 kHz 内最大偏差: 6.88 dB -> 0.91 dB

THD (%)
  tone   level      old 75us   new 75us
  1000   loud        17.859%     0.008%
  5000   loud        25.945%    13.180%
  10000  over         0.758%     0.000%

预加重关闭时的带限
  f (Hz)      old off    new off
  20000         -0.00     -34.72
  23000         -0.00     -79.25
```

### 改动 2：四阶 Chebyshev 带限（15 kHz）

系数由 `tools/design_filters.py` 生成（Chebyshev I、0.2 dB 纹波、fc=15 kHz，
双二阶、Q15），该脚本同时验证量化后的响应误差（< 0.05 dB）与满度下的
内部状态峰值（~3.3e4，远低于 int32）。窄带模式同步改为 2 阶 3.4 kHz。

```
带限响应（关闭预加重；dB 相对输入）
  f (Hz)    old 1p x2  new cheby
  5000          -0.66       0.12
  10000         -2.16       0.14
  15000         -3.67      -0.00
  18000         -4.35     -17.33
  20000         -4.65     -34.72
  23000         -4.89     -79.25
```

### 改动 3：定点溢出安全

双二阶的输入与反馈项被限制在 int16 范围，使最大乘积
`|b1|*32767 = 1.32e9 < 2^31`。（未加限制时，全幅宽带噪声会把某一节的
状态推到 ~1e5，`a1*y = 2.5e9` 溢出 int32 = 未定义行为。）饱和只在信号
已经是满度数倍、即将被限幅器硬剪的位置发生；返回给限幅器的是未饱和的 y，
所以 `clips` 计数仍有效。

### 改动 4：欠载保持 + 丢样/欠载计数器（L1/L2）

- `fm_modulator.c`：ring 空时不再无条件清零。正在流式（`s_audio_active`）
  且连续空拍 < 480（10 ms）时重复上一采样（hold-last），并计入
  `s_ring_underflows`；连续空拍超过 10 ms 说明主机真的停了，仍停载波
  （保持原有"主机暂停 = 精确停在 fc"的行为）。
- ring 满时计入新增的 `s_ring_drops`（改前是静默丢弃）。
- 通过 `pico_fm.ring_stats() -> (underflows, drops)` 暴露；`diag` 与
  `status` 均已输出（旧固件上 `diag` 会提示无此接口）。

```
异步采样时钟漂移（同一组计数器；与策略无关）
  ppm    time to hit    glitch rate      counters: underflows / drops
  -100   427 s          4.80             833 / 0
  -50    853 s          2.40             0 / 0
  50     853 s          2.40             0 / 0
  100    427 s          4.80             0 / 881

漂移事件的听感（1 kHz 正弦、-6 dBFS、经当前 DSP 链）
  ppm      worst |dy| hold  worst |dy| zero  THD zero     THD hold
  10       5898             22993                0.016%    0.007%
  50       7053             27591                0.035%    0.008%
  100      7053             27591                0.064%    0.012%
  clean    5898             -                        -    0.007%
```

即：事件次数一样（计数器可证明漂移在发生），但改后每个事件不再产生
4–5 倍的阶跃，THD 回到无漂移水平。

注意：计数器只能"看见"漂移，不能**纠正**它；hold-last 去掉的是 click，
不是时间误差。真正的修复是 ASRC 或 UAC1 反馈端点（见下面的升级路径）。

### 改动 5：refdiv 策略与控制台默认值（L5）

- 新增 `refdiv_for(target)`：**载波需要停波的频段用 1，其余用 2**。
  广播 FM（87.5–108 MHz）静音时停载波，而 refdiv 2 会改变停波时的 PDM
  图案并在静音载波上产生可听嗡声（硬件实测，即 v0.22.x 的 FM 静音回归），
  所以它必须保持 1；2m/UHF/窄带短波静音时关断射频，没有停载波，所以用 2。
- `band_apply()`、`band` 命令与启动时的 `apply_config()` 都改用该函数
  （原来只看 `target > 150 MHz`）；控制台新增 `refdiv <1|2|auto>`，
  默认 auto；配置里的显式 1/2 优先。
- 启动时若 refdiv 2 的窗口求解失败，自动回退到 refdiv 1（失败的 `init`
  不会启动 core1，重试安全），并把 1 写回配置。
- `status` 新增实际 PDM step（`pico_fm.range()` 的窗口宽度就是一个
  反馈分频步）。

```
$ python3 tools/pll_range.py minstep 1800000 30000000 50000 3000 --refdiv 2
  windows with a solution: 559, without: 6
  first-found is NOT the minimum step in 0 window(s)
$ python3 tools/pll_range.py minstep 80000000 500000000 500000 5000 --refdiv 2
  windows with a solution: 129, without: 712
  first-found is NOT the minimum step in 0 window(s)
```

即 C 的 `calculate_pll_divider()` 取到的已经是同 pass 内 step 最小的解，
无需改动搜索逻辑（验证代替猜测）。

| 频段 | 改前 step | 改后 step |
|---|---|---|
| FM 98.0 MHz（停波） | 750 kHz（refdiv 1） | 750 kHz（保持 1，避免静音嗡声） |
| 2m 145.0 MHz | 1.2 MHz（refdiv 1） | **600 kHz**（refdiv 2） |
| 433.92 MHz（×3） | 600 kHz（refdiv 2） | 600 kHz（不变） |
| 30m 10.136 MHz | 80 kHz（refdiv 1） | **40 kHz**（refdiv 2） |
| 20m 14.074 MHz | 120 kHz | **60 kHz** |
| 10m 28.074 MHz | 240 kHz | **120 kHz** |

### 改动 6：UAC1 音量真正的 dB 映射（L6）

- 新增 `fm_vol_table[61]`（-0..-60 dB 每 dB 一个 `round(32767*10^(dB/20))`），
  `fm_db256_to_gain()` 按整数 dB + 1/256 dB 余数插值；
  `fm_gain_to_db256()` 为单调表的逆查（往返误差 0）。
- 只改两个定点辅助函数，不在 ISR 路径上，无浮点。

| 请求 (dB) | 改前实际 (dB) | 改前误差 | 改后实际 (dB) | 改后误差 |
|---|---|---|---|---|
| −6 | −0.92 | +5.08 | −6.00 | −0.00 |
| −12 | −1.94 | +10.06 | −12.00 | +0.00 |
| −20 | −3.52 | +16.48 | −20.00 | +0.00 |
| −30 | −6.02 | +23.98 | −30.00 | −0.00 |
| −40 | −9.54 | +30.46 | −39.99 | +0.01 |
| −60 | −∞ | — | −59.94 | +0.06 |

插值最差误差：0…−30 dB 内 0.015 dB，全程 0.26 dB（`-60 dB` 处，已是 60 dB 衰减）。

### 改动前后对比（`audio_quality.py compare`）

| 指标 | 改前 | 改后 |
|---|---|---|
| 净接收响应 1 kHz | +6.88 dB | **+0.22 dB** |
| 净接收响应 15 kHz | −6.27 dB | **−0.37 dB** |
| 0.3–15 kHz 最大偏差 | 6.88 dB | **0.91 dB** |
| 14 kHz 通带掉幅 | 3.39 dB | **0.15 dB** |
| 23 kHz 阻带 | −4.89 dB | **−79.25 dB** |
| 1 kHz 大声压 THD | 17.86 % | **0.008 %** |
| 10 kHz 满幅 THD | 0.76 % | **0.000 %** |
| 关闭预加重时的带限 | 无（0.00 dB） | 20 kHz −34.7 dB |
| 欠载（漂移）最大阶跃 | 27591 | **7053** |
| 欠载时的 THD | 0.035 % | **0.008 %** |
| 丢样/欠载可观测性 | 无计数器 | `pico_fm.ring_stats()` |
| 2m 的 PDM 抖动步进 | 1.2 MHz | **600 kHz** |
| 30m 的 PDM 抖动步进 | 80 kHz | **40 kHz** |
| refdiv 选择 | 仅看 >150 MHz | **`refdiv_for()` + `refdiv auto`** |
| 音量 −30 dB 请求的实际衰减 | −6.0 dB（误差 +24 dB） | **−30.0 dB（误差 0）** |

## 5. 后续项

| 项 | 状态 |
|---|---|
| L1/L2：欠载保持 + 丢样/欠载计数器 | **已完成（改动 4）**；残余：无 ASRC/反馈端点，漂移仍存在 |
| L5：refdiv 策略与 `refdiv auto` | **已完成（改动 5）**；残余：环路滤波后的**残余**抖动仍需硬件实测 |
| L6：UAC1 音量真正的 dB 映射 | **已完成（改动 6）**（最差插值误差 0.26 dB，0…−30 dB 内 0.015 dB） |
| 限幅器前瞻 | 未实施（收益取决于节目素材；改动 1 已消除主要的伪失真来源） |
| ASRC / UAC1 反馈端点 | 未实施（需要 ISR 预算或 TinyUSB 反馈支持，属于结构改动） |

---

## 6. 未纳入本次范围

- **立体声 MPX（19 kHz 导频 + 38 kHz DSB）**：48 kHz 采样下需要 4× 内插
  （192 kHz），而当前 ISR 已是 2–6 µs/样本，4 倍即 40–115% CPU。属于重构采样
  时钟的独立课题。
- **AM/SSB/DRM**：本链路是固定幅度方波 + 纯频率调制，需要外部线性调制级。
- **射频实测**（相噪、残余抖动、实际接收音质）：本目标明确不做硬件实测；
  上表第 5 项的"残余抖动"只能由实测确定。

---

## 7. 回归验证

**编译（`./build.sh`，干净克隆）**

```
$ ./build.sh
==> Building RPI_PICO_FM firmware, MicroPython ref: v1.29.0
    trying https://git.sr.ht/~bytewolf/micropython ...
    patch applied
    source: https://git.sr.ht/~bytewolf/micropython
[100%] Built target firmware
==> Done: firmware/rp2040pico_fm_firmware.uf2
   FLASH 350180 B / 640 KB (53.43%), RAM 36812 B / 256 KB (14.04%)
```

- `./build.sh` 在**干净克隆**上全流程成功（克隆 → 打补丁 → `make submodules`
  → 编译），产出 UF2 的 sha256 为
  `810e9e0bd8599ef11051de81aa9a65a28e35ca4b39bf30caf9983b11f6018ebb`；
  仓库中的 `firmware/rp2040pico_fm_firmware.uf2` 就是它
  （`sha256sum -c firmware/sha256.txt` 通过，`python/main.py` 的 `FW_SHA256`
  与其一致）。
- **补丁往返校验**：在干净 MicroPython 上应用
  `patches/micropython-fm.patch` 后重新生成的补丁与仓库中的补丁 **0 差异**，
  说明补丁自洽且能精确复现该构建。

**硬件验证（v0.24.0，98.0 MHz FM 广播，主机播放中）**

| 项 | 实测 | 对照 |
|---|---|---|
| `ver` | sha `810e9e0b…` | 与仓库固件一致 |
| `status` | `PLL range 97.500..98.250 MHz`、`step 750.0 kHz`、`refdiv 1` | 与 `pll_range.py check 98000000 75000 1 → div=16` 逐位一致 |
| `diag 30` | ISR +1440230、RX +1440192、Underflows +39、Drops 0 | 两个独立估计：事件率 27.1 ppm、计数差 26.4 ppm；理论事件率 1.28/s，实测 1.3/s |
| `Clips` | 0 | 预加重改造后余量充足 |

即：主机 48 kHz 时钟比板载 PWM 时钟慢约 27 ppm，环缓冲被抽干、
丢样数 = 计数数（无隐藏丢样）。旧固件在此条件下是 **1.3 次/秒的满度阶跃
（咔嗒）**，本版本变成 1 个采样重复（约 20 µs，不可闻）——
这是本次改动中靠仿真无法证明的那一项。

- **文档数字一致性**：本文件引用的 72 个数值全部在对应脚本输出中找到
  （`audio_quality.py resp/compare/drift/mono/volume`、
  `pll_range.py bands/check/minstep`）；未发现只存在于文档而不可复现的数字。
