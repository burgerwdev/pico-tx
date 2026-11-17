#include "modulator_under_test.h"

static void test_offset(void) {
    const uint32_t devs[] = {0, 1, 1000, 12500, 32767, 32768, 75000,
        500000, 1500000, 0x7fffffff, UINT32_MAX};
    for (unsigned d = 0; d < sizeof(devs) / sizeof(devs[0]); d++) {
        for (int32_t s = -32768; s <= 32767; s++) {
            assert(tx_sample_offset(s, devs[d]) == (uint32_t)(((int64_t)s * devs[d]) >> 15));
        }
    }
}

static void test_chirp(void) {
    const uint32_t endpoints[][2] = {{87900000, 87901237}, {87901237, 87900000},
        {87900000, 87900000}, {1, 150000000}, {150000000, 1}};
    const uint32_t ms[] = {1, 7, 101};
    for (unsigned e = 0; e < 5; e++) {
        for (unsigned d = 0; d < 3; d++) {
            for (uint32_t gap = 0; gap <= 2; gap += 2) {
                for (int repeat = 0; repeat < 2; repeat++) {
                    tx_modulator_init();
                    uint32_t f0 = endpoints[e][0], f1 = endpoints[e][1];
                    tx_modulator_chirp(f0, f1, ms[d], gap, repeat);
                    uint32_t dur = ms[d] * 48, period = dur + gap * 48, t = 0;
                    for (uint32_t tick = 0; tick < period * 3; tick++) {
                        uint32_t expected = t < dur
                            ? (uint32_t)((int64_t)f0 + ((int64_t)f1 - f0) * t / dur) : f0;
                        tx_pwm_wrap_handler();
                        assert(written_freq == expected);
                        t++;
                        if (t >= period) t = repeat ? 0 : period - 1;
                    }
                    tx_modulator_chirp_stop();
                    tx_pwm_wrap_handler();
                    assert(written_freq == f0);
                }
            }
        }
    }
    // Large denominators: the remainder addition must not overflow.
    tx_ramp_t ramp = tx_ramp_init(0, UINT32_MAX - 1, UINT32_MAX);
    for (uint32_t i = 0; i < 10000; ++i) {
        assert(ramp.freq == (uint32_t)((uint64_t)(UINT32_MAX - 1) * i / UINT32_MAX));
        tx_ramp_advance(&ramp, UINT32_MAX);
    }
}

static void test_psk(void) {
    const uint32_t bauds[] = {1, 17, 1200, 2400, 9600, 48000};
    for (uint8_t order = 2; order <= 4; order += 2) {
        for (unsigned b = 0; b < 6; b++) {
            tx_modulator_init();
            tx_modulator_psk_config(bauds[b], order);
            for (uint32_t prev = 0; prev < order; prev++) {
                for (uint32_t next = 0; next < 256; next++) {
                    tx_modulator_clear_symbols();
                    uint8_t symbol = next;
                    assert(tx_modulator_write_symbols(&symbol, 1) == 1);
                    s_psk_phase = (uint32_t)(((uint64_t)prev << 32) / order);
                    uint32_t target = (uint32_t)(((uint64_t)(next % order) << 32) / order);
                    int32_t delta = (int32_t)(target - s_psk_phase);
                    int32_t expected = (int32_t)(((int64_t)delta * 48000) / ((int64_t)s_sps * 4294967296LL));
                    tx_symbol_load();
                    assert(s_sym_freq == expected && s_psk_phase == target);
                }
            }
        }
    }
}

static void test_audio(void) {
    int16_t input[96], expected[48];
    for (unsigned i = 0; i < 96; i++) input[i] = (int16_t)(i * 14321u);
    for (int nfm = 0; nfm < 2; nfm++) {
        for (int tc = 0; tc < 4; tc++) {
            for (uint32_t volume = 0; volume <= 32767; volume += 32767) {
                tx_modulator_init();
                tx_modulator_set_nfm_audio(nfm);
                tx_modulator_set_preemphasis(tc != 0);
                tx_modulator_set_preemphasis_us(tc == 1 ? 50 : tc == 2 ? 75 : 300);
                for (unsigned i = 0; i < 48; i++) {
                    int32_t mono = ((int32_t)input[2 * i] + input[2 * i + 1]) >> 1;
                    expected[i] = tx_audio_process((int16_t)((mono * volume) >> 15));
                }
                tx_modulator_init();
                tx_modulator_set_nfm_audio(nfm);
                tx_modulator_set_preemphasis(tc != 0);
                tx_modulator_set_preemphasis_us(tc == 1 ? 50 : tc == 2 ? 75 : 300);
                tx_modulator_set_volume(volume);
                tx_modulator_set_led_mode(3);
                s_audio_route_on = true;
                memcpy(usb_data, input, sizeof(input)); usb_len = sizeof(input);
                usb_reads = led_writes = 0;
                tud_audio_rx_done_isr(0, sizeof(input), 0, 3, 1);
                assert(usb_reads == 1 && usb_len == 0 && led_writes == 1);
                assert(s_rx_frames == 48 && s_ring_head == 48 && s_ring_drops == 0);
                assert(memcmp(s_ring, expected, sizeof(expected)) == 0);
                int32_t peak = 0;
                uint16_t max = 0;
                for (unsigned i = 0; i < 48; i++) {
                    int32_t a = expected[i] < 0 ? -expected[i] : expected[i];
                    peak += (a - peak) >> (a > peak ? 2 : 6);
                    if (a > max) max = a;
                }
                assert(s_led_peak == peak && tx_modulator_sample_peak(256) == max);
                s_mute = true; usb_len = sizeof(input); usb_clears = 0;
                tud_audio_rx_done_isr(0, sizeof(input), 0, 3, 1);
                assert(usb_len == 0 && usb_clears == 1 && s_rx_frames == 48);
                s_mute = false; s_audio_route_on = false; usb_len = sizeof(input);
                tud_audio_rx_done_isr(0, sizeof(input), 0, 3, 1);
                assert(usb_len == 0 && usb_clears == 2 && s_rx_frames == 48);
            }
        }
    }
    tx_modulator_init(); s_audio_route_on = true;
    s_ring_head = TX_RING_SIZE - 4;
    memcpy(usb_data, input, sizeof(input)); usb_len = sizeof(input);
    tud_audio_rx_done_isr(0, sizeof(input), 0, 3, 1);
    assert(s_rx_frames == 3 && s_ring_drops == 45); // preserve ring-full accounting
    tx_modulator_init(); s_audio_route_on = true;
    s_ring_head = s_ring_tail = TX_RING_SIZE - 4;
    memcpy(usb_data, input, sizeof(input)); usb_len = sizeof(input);
    tud_audio_rx_done_isr(0, sizeof(input), 0, 3, 1);
    assert(s_rx_frames == 48 && s_ring_head == 44 && s_ring_drops == 0);
}

static void test_diagnostics_and_gaps(void) {
    for (uint8_t scheme = TX_MOD_FSK; scheme <= TX_MOD_PSK; scheme++) {
        if (scheme == TX_MOD_CHIRP) continue;
        tx_modulator_init(); tx_modulator_set_scheme(scheme);
        tx_modulator_set_symbol_rate(48000);
        const uint8_t symbols[] = {1, 1};
        tx_modulator_write_symbols(symbols, 2);
        tx_modulator_set_repeat(0); tx_modulator_set_repeat_gap(1);
        tx_modulator_start();
        tx_pwm_wrap_handler(); // initial idle tick
        tx_pwm_wrap_handler(); tx_pwm_wrap_handler();
        assert(rf_enabled);
        for (int i = 0; i < 48; i++) {
            tx_pwm_wrap_handler(); assert(!rf_enabled);
        }
        tx_pwm_wrap_handler(); assert(rf_enabled && !s_rf_gated);
        uint32_t ticks = s_isr_ticks, countdown = s_sym_countdown;
        uint16_t tail = s_sym_tail;
        uint32_t stats[6];
        for (int i = 0; i < 100; i++) {
            tx_modulator_get_timing(stats); tx_modulator_isr_cost_us();
        }
        assert(s_isr_ticks == ticks && s_sym_tail == tail && s_sym_countdown == countdown);
    }
}

int main(void) {
    test_offset(); test_chirp(); test_psk(); test_audio(); test_diagnostics_and_gaps();
    puts("C hot paths: exact FM scaling, chirp/PSK timing, batched PCM/LED, ring wrap/drop, gaps, passive diagnostics: OK");
}
