/* Host substitutes for the hardware touched by tx_modulator.c. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define MICROPY_HW_TX 1
#define CFG_TUD_AUDIO 1
#define CFG_TUD_AUDIO_FUNC_1_EP_OUT_SW_BUF_SZ 192
#define __not_in_flash_func(name) name
#define __time_critical_func(name) name
#define PWM_IRQ_WRAP 4
#define GPIO_FUNC_PWM 4
typedef unsigned int uint;
typedef struct { uint32_t top; } pwm_config;
static struct { struct { uint32_t top, div, csr, ctr; } slice[8]; } fake_pwm;
#define pwm_hw (&fake_pwm)
static uint32_t fake_time, written_freq, led_writes;
static bool rf_enabled;
static uint32_t time_us_32(void) { return fake_time++; }
static uint32_t save_and_disable_interrupts(void) { return 0; }
static void restore_interrupts(uint32_t state) { }
static void pwm_clear_irq(uint slice) { }
static pwm_config pwm_get_default_config(void) { return (pwm_config){0}; }
static void pwm_config_set_clkdiv(pwm_config *c, float div) { }
static void pwm_config_set_wrap(pwm_config *c, uint32_t wrap) { c->top = wrap; }
static void pwm_init(uint slice, pwm_config *c, bool enabled) { fake_pwm.slice[slice].top = c->top; }
static void pwm_set_irq_enabled(uint slice, bool enabled) { }
static void pwm_set_enabled(uint slice, bool enabled) { }
static void irq_set_exclusive_handler(uint irq, void (*handler)(void)) { }
static void irq_set_priority(uint irq, uint priority) { }
static void irq_set_enabled(uint irq, bool enabled) { }
static uint pwm_gpio_to_slice_num(uint gpio) { return (gpio >> 1) & 7; }
static void gpio_set_function(uint gpio, uint function) { }
static void pwm_set_gpio_level(uint gpio, uint32_t duty) { led_writes++; }
static void pico_fractional_pll_set_freq_u32(uint32_t hz) { written_freq = hz; }
static void pico_fractional_pll_enable_output(bool enable) { rf_enabled = enable; }

typedef struct { uint16_t wValue; } tusb_control_request_t;
static uint8_t tu_u16_low(uint16_t v) { return v; }
static uint8_t usb_data[192];
static uint16_t usb_len, usb_reads, usb_clears;
static uint16_t tud_audio_available(void) { return usb_len; }
static uint16_t tud_audio_read(void *buf, uint16_t len) {
    assert(len <= usb_len);
    memcpy(buf, usb_data, len);
    memmove(usb_data, usb_data + len, usb_len - len);
    usb_len -= len;
    usb_reads++;
    return len;
}
static void tud_audio_clear_ep_out_ff(void) { usb_len = 0; usb_clears++; }
