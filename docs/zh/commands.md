# 指令使用指南

> 中文 · [English](../en/commands.md)

`fm>` 控制台指令（值默认单位 Hz）。控制台可通过 `help` 随时查看。

## 常用指令

| 指令 | 说明 | 示例 |
|---|---|---|
| `help` | 显示全部指令帮助 | `help` |
| `ver` | 显示版本号、固件 sha256 与项目链接 | `ver` |
| `status` / `s` | 显示全部音频/FM 参数 | `status` |
| `freq <Hz>` | 设置调制中心（有效频率；当前 PLL 频带内实时生效；UHF 目标自动走 3/5 次谐波；频带外则重启） | `freq 433920000` |
| `dev <Hz>` | 设置满幅频偏（有效值；1000..PLL 半范围 x 谐波次数） | `dev 12000` |
| `reinit <载波> <频偏> [引脚]` | 保存新载波/频偏/引脚并重启（UHF 409/433/440M 目标自动换算为 3/5 次谐波基频） | `reinit 433920000 12000 21` |
| `pdm <1\|2\|3\|4>` | PDM 抖动率（MHz；1 = 默认；实测 >1 更差；保存并重启） | `pdm 1` |
| `refdiv <1\|2\|auto>` | PLL 参考分频（2 = PDM 步长减半；auto 为默认，停波频段自动保持 1） | `refdiv auto` |
| `pin <21\|23\|24\|25>` | 切换 RF 输出引脚（保存并重启） | `pin 21` |
| `pwr <2\|4\|8\|12>` | RF 驱动强度 mA（发射功率）；8/12 偶次谐波抑制更好 | `pwr 12` |
| `audio on\|off` | USB 音频→FM 路由开关 | `audio on` |
| `vol <0-100>` | 音量百分比 | `vol 70` |
| `mute on\|off` | 静音（≈vol 0；audio off ≠ mute） | `mute off` |
| `pre on\|off\|50\|75` | 15kHz 带限 + 预加重（50 或 75µs）+ 限幅；on = 75µs（默认），off = 不处理 | `pre 50` |
| `sq <0-100>` | 静音低于该满幅百分比的弱样本，避免微弱底噪被调制；暂停/静音时载波自动停在 fc（无调制），收音机自然静音；0 = 关（默认） | `sq 5` |
| `led <0\|1\|2\|3>` | LED：0关 1常亮 2发射时亮 3随音频VU | `led 3` |
| `ledpin <0-29>` | 设置普通 LED 引脚（保存并重启） | `ledpin 25` |
| `ledpin ws2812 [引脚]` | 使用 WS2812 彩灯（默认 GPIO16） | `ledpin ws2812` |
| `vbar` | 音频电平表（~50fps）：峰值条 + 静噪阈值（T 标记）+ 削波速率；输入安静时显示 "silent"（慢刷新）；任意键退出 | `vbar` |
| `ring` | 环形缓冲水位 % | `ring` |
| `diag` | 1 秒内实测 ISR/RX 速率（应各 ~48000）与环形缓冲漂移计数（underflow/drop） | `diag` |
| `pwm` | PWM/ISR 诊断 | `pwm` |
| `pll` | PLL 诊断（ready/范围/最后写入频率） | `pll` |
| `sweep [低 高 步长]` | 暂停音频扫频（PLL 自检） | `sweep 87000000 88500000 500000` |
| `cls` | 清空终端屏幕（ANSI） | `cls` |
| `reset` | 删除保存的配置并重启到默认 | `reset` |
| `reboot` | 保存当前设置并重启（等同重新插拔，保留配置） | `reboot` |
| `exit` | 退出控制台回到 REPL | `exit` |

## 发射模式

所有调制都在固件中完成；控制台只负责选择方式与喂数据。切到非 `fm` 模式会自动启动调制引擎。

| 命令 | 说明 | 示例 |
|---|---|---|
| `mode [名称]` | 查看/选择发射方式：`fm` `tone` `fsk` `ook` `cw` `chirp` `psk` | `mode tone` |
| `tx on\|off\|pause` | `on` 启动/继续；`pause` 保留位置，再次 `tx on` 从原处继续；`off` 回退到开头，再次 `tx on` 重新发射。FM 模式下 `off` 还会关断 RF，`pause` 只停靠载波 | `tx pause` |
| `tone <hz> [hz2] [level%]` | 内部 DDS 音调 FM 调制到载波；`tone off` 停止 | `tone 1000` |
| `fsk <baud> <shift_hz> <hex> [repeat [n]] [gap ms]` | 排队 2-FSK 符号（`0`→-shift，`1`→+shift） | `fsk 1200 4500 55aa0f repeat 0 gap 500` |
| `ook <baud> <hex> [repeat [n]] [gap ms]` | 排队开关键控符号（`0`→RF 关） | `ook 2000 aaaa repeat 0` |
| `psk <baud> <2\|4> <hex> [repeat [n]] [gap ms]` | 排队 BPSK（`2`）/ QPSK（`4`）符号 | `psk 2400 2 abcd` |
| `cw <文本> [repeat [n]] [gap ms]` | 在载波上发摩斯（20 wpm） | `cw CQ CQ DE PICO TX repeat 0 gap 800` |
| `chirp <f0> <f1> <ms> [gap] [repeat]` | 线性扫频；`chirp off` 停止 | `chirp 87900000 88100000 100 20 1` |
| `service [命令...]` | 开机自动执行的一条控制台命令（无头运行）；`service off` 清除 | `service tone 1000` |
| `log` | 实时发射日志（每秒一行；Ctrl-C 停，`log` 恢复）。`cw`/`fsk`/`ook`/`psk`/`tone`/`chirp` 发完后自动打开 | `log` |

发送命令会自动启动调制引擎，然后打开 `log`；按 Ctrl-C 回到提示符（发射仍在固件里继续）。

`repeat [n]`：不加 = 发一次；`repeat` 或 `repeat 0` = 无限循环；`repeat n` = 共发 n 遍。
循环在固件里跑，控制台不阻塞——`tx pause` 暂停（`tx on` 继续），`tx off` 回退（`tx on` 重新发射）；
新的发送命令或 `mode` 会替换图案。

`gap <ms>`：两次 repeat 之间插入的静默间隔（这段时间 RF 键控关断），方便收信机分辨。
仅在重复时生效；不写时按模式取默认值：`cw`/`ook` 为一个词间隔（约 7 个码元），
`fsk`/`psk` 为 250ms。`gap 0` 关闭间隔。

说明：
- `fsk`/`ook`/`psk`/`cw` 共用固件内 1024 符号 FIFO，超出部分会被拒绝（`send` 返回实际接受的数量）。`baud` 要配合 PLL 环路带宽（宽带 FM 下几 kbaud 量级）。
- `shift_hz` 必须落在 `init(carrier ± deviation)` 设定的 PLL 窗口内。
- `chirp` 频率为绝对 Hz，同样必须在窗口内。
- `cw` 是把摩斯编码成 OOK（点/划 = 固定时长通断）；接收机调到该载波并用 CW/AM 模式才能听到。
- `service` 开机后执行一条命令，例如 `service cw CQ DE PICO repeat 0` 或 `service tone 1000`；开机服务不会打开实时 `log`，适合无头信标。

## 说明

- **`freq` 与 PLL 范围**：`freq` 只能在当前 `init` 锁定的 PLL 范围内实时微调
  （`status` 显示实际范围，如 87.750..88.500 MHz）；换频段用 `reinit`。
- **`power`（发射功率）**：RP2040 GPIO 是轨到轨推挽，用高阻探头测幅度看不出
  区别；接 50Ω 真实负载（频谱仪 50Ω 输入 + 衰减器）才见 10dB+ 差异。
- **`audio off` 与 `mute`**：两者都会立即静音收音机（载波停在 fc、无调制）。
  `audio off` 停止 USB 音频→FM 路由，并向主机报告为"静音"（Windows 显示静音状态）；
  `mute` 保持路由但静音音频流。`vol 0` 是另一种安静载波的方式。
- **`pre`**：on = 75µs（美/欧广播标准），`50` = 50µs（中/日）。若收音机去加重
  是 50µs，用 `pre 50` 可减少高频嘶嘶声。
- **UHF 目标（409/433/440MHz）**：RP2040 PLL 基频上限约 150MHz，因此 UHF
  目标用较低基频的 3 次（或 5 次）奇次谐波发射——`reinit 433920000 12000 21`
  实际让 PLL 跑在 144.640MHz，手台在 433.920MHz 接收（频偏同样 xN）。
  `status` 会同时显示基频与有效频率。**法律/安全**：409MHz 段的基频落在
  136.6MHz（民航频段 118-137MHz 内）——控制台会警告，必须加带通滤波器
  抑制基频后再发射。
- **`pdm` / `refdiv`**：持久化并在下次重启时生效（实测：不重启的在线 PLL
  重初始化会死机）。实测结论：pdm >1MHz 反而更差（systick 延迟 / PLL 写入
  时序）。`refdiv 2` 在每个频段都把 PDM 抖动步长（`ref/div`）减半，但载波
  需要停波的频段（广播 FM）不能用它——停波时的 PDM 图案会变，静音载波上
  会听到嗡声。所以默认 `refdiv auto`：关断 RF 的频段用 2，停波频段用 1。
  配合 `tools/serial.sh`（tio 自动重连）使用，重启不再打断串口会话。
- **上位机仍在播放时的 FM 静音**：`mute on` / `audio off` 会把未调制载波停在
  fc，但 USB 包接收仍在进行，其片上数字活动会向停靠载波耦合微弱噪声（只在
  收音机静噪态下可闻；音乐以 75kHz 频偏调制时会掩盖）。这是方波 PDM 发射器
  的固有属性，原版固件完全相同——RX 路径已丢弃帧（不做 DSP），但 USB 硬件
  必须继续接收。要完全静音请暂停上位机播放（或 `rf off`）。窄带频段不受此
  影响（静音键控直接关断 RF）。
- **`sq`（弱样本静音）**：RX 路径中低于阈值的样本被置零，微弱流噪声不再被调制。
  注意：暂停/静音本身已把载波停在精确 fc（无调制），调谐到 fc 的收音机自然静音；
  早期版本的"失谐静音门"已移除——中频通带边缘的偏调 CW 可能让收音机 AGC/静噪
  周期性动作产生可闻噪声。观察 `vbar` 中的 T 标记。
- **`vbar` 的 clips**：每秒削波次数表示限幅器被触发的频率；数值高说明 PC 音量
  过大（调低音量或 `vol`）。
- **`reset`**：删除 `/tx_cfg.json`（载波/频偏/引脚/功率/LED/预加重/静噪）并重启
  到默认（75µs 预加重、静噪关）。**`reboot`**：先把当前设置持久化再重启、
  不删除配置——用于"某些改动需要重新插拔才生效"的场景（例如已经保存过的
  `ledpin`/`pin` 类重配置）。
- 配置持久化在板内文件系统 `/tx_cfg.json`；开机自动恢复。
