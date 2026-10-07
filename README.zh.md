# pico-tx（RP2040 通用 RF 发射机）

**版本 v0.1.0** —— RP2040 通用 RF 发射机

中文 · [English](README.md) · [项目主页](https://git.sr.ht/~bytewolf/pico-tx) · 基于 [rp2040-fm-transmitter](https://git.sr.ht/~bytewolf/rp2040-fm-transmitter)

把一片 Raspberry Pi Pico（RP2040）变成 **通用 RF 发射机**，基于
[pico-fractional-pll](https://github.com/kaduhi/pico-fractional-pll) 技术
（GPIO21 输出 RF 方波，无 DAC / 模拟级）：

- **所有调制都在固件里**（48kHz PWM 中断），MicroPython 只做控制层，不参与实时通路；
- **USB 声卡**（UAC1，48kHz/16bit/立体声）只是多种信号源之一：电脑播放的声音 FM 调制到载波；
- `tx>` 控制台可切换更多发射方式：内部 DDS 音调、FSK、OOK、CW（摩斯）、线性 chirp、BPSK/QPSK；
- 载波覆盖广播 88-108M、2m 业余 144-148M、以及经 ≤150MHz 基频 3/5 次谐波输出的 UHF 409/433/440M。

| 模式 | 信号源 | 调制 | 典型用途 |
|------|--------|------|----------|
| `fm` | USB 音频 | FM | 电脑声音发到 FM 收音机 |
| `tone` | 内部 DDS | FM | 测试音 / CTCSS / DTMF |
| `fsk` | 符号 FIFO | 2-FSK | RTTY / 数传 / 寻呼 |
| `ook` | 符号 FIFO | 开关键控 | 遥控 |
| `cw` | 键控 | 摩斯 | 信标 / 键控载波 |
| `chirp` | 内部 | 线性扫频 | FMCW / 扫频测试 |
| `psk` | 符号 FIFO | BPSK/QPSK | 恒包络数据 |

> **⚠️ 法律警告**：GPIO21 输出强 RF 方波。**严禁接天线**。多数国家未经许可辐射即违法。测试时让 FM 收音机贴近 Pico（几厘米内）即可。UHF 谐波模式下**基频也会辐射**：409MHz 的基频在民航频段（118-137MHz）内——在 409 段发射前必须加带通滤波器抑制基频。FSK/OOK/PSK 等数字方式的频谱比窄带 FM 宽，辐射使用前务必检查谐波与杂散。

---

## 快速开始

1. 烧录 `firmware/picotx_firmware.uf2`（BOOTSEL 拖拽，或 `tools/flash.sh`）；
2. 上传控制台脚本：
   ```
   tools/upload.sh
   ```
   （控制台会抢占 REPL，直接 `mpremote cp` 会报 "could not enter raw repl"；
   该脚本自动让控制台 `exit` 后上传并重启。手动等效步骤：串口终端在 `tx>`
   输入 `exit`，再执行 `mpremote resume fs cp python/main.py :main.py`。）
3. 拔插（或按 RESET），等 2~3 秒，打开串口终端（115200）→ 自动进入 `tx>` 控制台；
4. 电脑上把音频输出选为 **"pico-tx"**，播放音乐；
5. FM 收音机调到 **87.9MHz** 收听。

不接电脑也能发射（`tone`/`fsk`/`cw`/`chirp`/`psk`）：

```
mode tone            # 切到内部信号源
tone 1000            # 1kHz FM 测试音
mode cw              # 摩斯
cw CQ CQ DE PICO TX
```

一键频段预设（保存并重启）：

```
band fm    -> 98.0 MHz 广播 FM（载波停靠式静音）
band 2m    -> 145.0 MHz，手台 VHF WIDE 模式
band 433   -> 433.92 MHz（3 次谐波，UHF WIDE 模式）
band 409   -> 409.75 MHz 免证公众对讲机（3 次谐波）
```

手台请用 **WIDE（25kHz）** 模式、频偏保持 12kHz 左右；凡是在无声时关断 RF 的
频段，控制台会自动设 `refdiv 2`（PDM 步长减半是谐波链路音质干净的关键）；
广播 FM 保持 `refdiv 1`，因为停靠载波在 refdiv 2 下会有可听嗡声。无声时窄带频段会把 RF
输出键控关断（类 PTT），手台静噪闭合而不再听到失谐停靠载波的单音；需要恢复
广播行为用 `silence park`。串口会话可用 `tools/serial.sh`（tio 自动重连；
udev 规则把设备固定为 `/dev/pico`）。

详细见 [docs/zh/usage.md](docs/zh/usage.md) 与 [docs/zh/commands.md](docs/zh/commands.md)。

## 仓库结构

```
release/
├── README.md / README.zh.md   # 英文（默认）+ 中文说明
├── LICENSE                    # MIT + 第三方许可说明
├── build.sh                   # 一键构建脚本（克隆 MicroPython + 打补丁 + 编译）
├── patches/
│   └── micropython-tx.patch   # 对 MicroPython v1.29.0 的全部改动
├── firmware/
│   ├── picotx_firmware.uf2   # 预编译固件
│   └── sha256.txt
├── python/
│   └── main.py                # FM 控制台脚本（复制到板子）
├── tools/
│   ├── flash.sh               # picotool 烧录辅助
│   ├── upload.sh              # 上传 python/main.py（自动退回控制台）
│   ├── serial.sh              # 串口终端（自动重连）
│   ├── pico_port.sh           # 解析开发板的 USB CDC 端口
│   ├── pll_range.py           # PLL 可达范围 / PDM 步进枚举
│   ├── audio_quality.py       # 主机端音频链定点模型
│   ├── design_filters.py      # Chebyshev 设计与 Q15 验证
│   └── make_test_audio.py     # 测试信号生成
└── docs/
    ├── README.md              # 文档索引（中英双语）
    ├── en/                    # English docs
    │   ├── usage.md
    │   ├── commands.md
    │   ├── technical.md
    │   ├── audio-quality.md
    │   └── troubleshooting.md
    └── zh/                    # 中文文档
        ├── usage.md           # 使用说明
        ├── commands.md        # 指令使用指南
        ├── technical.md       # 技术文档
        ├── audio-quality.md   # 音质分层审计
        └── troubleshooting.md # 故障排查
```

## 从源码构建

```bash
git clone git@git.sr.ht:~bytewolf/pico-tx
cd pico-tx
./build.sh            # 默认 MicroPython v1.29.0
# 或 ./build.sh <tag/commit>
```
脚本会克隆干净的 MicroPython、应用补丁、拉取子模块并编译，产物在 `firmware/`。
完全自动化：一条命令即可得到 UF2 及对应的 `sha256.txt`。详见
[docs/zh/technical.md](docs/zh/technical.md)。

**前置依赖（Ubuntu/Debian）**——运行 `build.sh` 前宿主机需先装好 RP2040
交叉工具链：

```bash
sudo apt install -y build-essential git cmake \
    gcc-arm-none-eabi libnewlib-arm-none-eabi \
    libstdc++-arm-none-eabi-newlib python3
```

`mpremote`（上传 `main.py`）与 `picotool`（`tools/flash.sh`）仅在上传/烧录时
需要，`build.sh` 本身不依赖。详见 [docs/zh/technical.md](docs/zh/technical.md)。

## 文档

| 文档 | 内容 |
|------|------|
| [docs/zh/usage.md](docs/zh/usage.md) | 烧录、连接、播放、LED/指示灯说明 |
| [docs/zh/commands.md](docs/zh/commands.md) | `fm>` 控制台全部指令与示例 |
| [docs/zh/technical.md](docs/zh/technical.md) | 架构：USB 音频→环形缓冲→48kHz 调制→PLL-PDM，构建原理 |
| [docs/zh/troubleshooting.md](docs/zh/troubleshooting.md) | 常见问题（Windows 驱动、无信号、shell 进不去等） |

英文版本在 [docs/en/](docs/en/)，也可从[文档索引](docs/README.md)进入。

## 致谢与许可

- [MicroPython](https://github.com/micropython/micropython)（MIT）——我们的改动以补丁形式提供；
- [pico-fractional-pll](https://github.com/kaduhi/pico-fractional-pll)（BSD-3-Clause，Kazuhisa Terasaki）——**其核心代码已并入本项目的补丁**（`ports/rp2/tx/pico_fractional_pll.c/.h`），需保留署名；不作为独立构建依赖；
- [kaduhi/pico-playground（fm_transmitter 分支）](https://github.com/kaduhi/pico-playground)——**仅作参考**（USB 声卡描述符与 FM 调制思路），未包含其代码；
- 本项目代码见 [LICENSE](LICENSE)。

## 依赖与构建顺序

构建只依赖 **MicroPython**（含其自带子模块 pico-sdk/tinyusb/mbedtls）。`build.sh` 依次尝试以下源（先取可用者）：
1. `https://git.sr.ht/~bytewolf/micropython`（维护者的 fork，保证可用性）；
2. `https://github.com/micropython/micropython`（官方上游）。

> 关于"依赖用 git submodule 是否更优雅"：对本项目，**补丁 + 克隆脚本更合适**——
> MicroPython 体积大且自带多层子模块，做成顶层 submodule 仍需递归初始化，省不了多少；
> 补丁让"我们改了什么"一目了然、可应用到任何版本/自己的 fork，且不把项目绑死在某个 fork
> 的维护节奏上。若你希望子模块方式，可在 fork 里直接应用补丁后用 `git clone --recurse-submodules`
> 引用该 fork——build.sh 的"已应用则跳过"逻辑已兼容这种用法。
