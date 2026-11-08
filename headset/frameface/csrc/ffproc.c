/* SPDX-License-Identifier: MIT */
/*
 * FrameFace pixel helper (libffproc.so), spec section 4.15: the Baballonia-exact preprocessing
 * of one VIVE Facial Tracker frame.
 *
 *   frame 320000 B (400 x 400 "YUYV", 800 B per row) -> byte plane of parity `phase`
 *   (1 = odd bytes) -> columns [col0, col0 + 200) -> OpenCV INTER_LINEAR stretch 200 -> 400
 *   -> 5x5 binomial [1 4 6 4 1] blur, BORDER_REFLECT_101, (S + 128) >> 8 -> lut -> 160000 B.
 *
 * Byte-identical to preprocess_swar() in frameface/preprocess.py; the service uses this library
 * only after parity_ok() has compared the two. No heap, no globals, no threads.
 *
 * Build: gcc -std=c99 -O2 -Wall -Wextra -fPIC -shared -o lib/libffproc.so csrc/ffproc.c
 * Test only: -DFF_CORRUPT_FOR_TEST adds 1 to out[12345], so tests can prove the parity gate.
 */
#include <stdint.h>
#include <string.h>

#define FF_ABI 1

#define FF_ROW_BYTES 800      /* bytes per input row */
#define FF_H 400              /* rows */
#define FF_COLS 200           /* columns of one camera */
#define FF_W 400              /* output width */
#define FF_IN_LEN 320000u
#define FF_OUT_LEN 160000u

int ff_abi(void);
int ff_preprocess(const uint8_t *yuyv, uint32_t len, int phase, int col0,
                  const uint8_t lut[256], uint8_t *out, uint32_t out_len);

int ff_abi(void)
{
    return FF_ABI;
}

/* BORDER_REFLECT_101 for i in -2..n+1: -1 -> 1, -2 -> 2, n -> n-2, n+1 -> n-3. */
static int reflect101(int i, int n)
{
    if (i < 0)
        return -i;
    if (i >= n)
        return 2 * n - 2 - i;
    return i;
}

/*
 * One source row y: the 2x INTER_LINEAR stretch in integer form (out[0] = s[0];
 * out[2k] = (s[k-1] + 3 s[k] + 2) >> 2; out[2k+1] = (3 s[k] + s[k+1] + 2) >> 2; out[399] = s[199]),
 * then the horizontal [1 4 6 4 1] pass with reflect-101 into h (sums <= 16 * 255 = 4080).
 */
static void row_pass(const uint8_t *yuyv, int y, int phase, int col0, uint16_t *h)
{
    uint8_t st[FF_W];
    const uint8_t *s = yuyv + (uint32_t)y * FF_ROW_BYTES + (uint32_t)(2 * col0 + phase);
    int k, x;

    /* sample k of this camera row is s[2 * k] */
    st[0] = s[0];
    for (k = 1; k < FF_COLS; k++)
        st[2 * k] = (uint8_t)((s[2 * k - 2] + 3u * s[2 * k] + 2u) >> 2);
    for (k = 0; k < FF_COLS - 1; k++)
        st[2 * k + 1] = (uint8_t)((3u * s[2 * k] + s[2 * k + 2] + 2u) >> 2);
    st[FF_W - 1] = s[2 * (FF_COLS - 1)];

    h[0] = (uint16_t)(2u * st[2] + 8u * st[1] + 6u * st[0]);
    h[1] = (uint16_t)(st[1] + 4u * st[0] + 6u * st[1] + 4u * st[2] + st[3]);
    for (x = 2; x < FF_W - 2; x++)
        h[x] = (uint16_t)(st[x - 2] + 4u * st[x - 1] + 6u * st[x] + 4u * st[x + 1] + st[x + 2]);
    h[FF_W - 2] = (uint16_t)(st[FF_W - 4] + 4u * st[FF_W - 3] + 6u * st[FF_W - 2]
                             + 4u * st[FF_W - 1] + st[FF_W - 2]);
    h[FF_W - 1] = (uint16_t)(2u * st[FF_W - 3] + 8u * st[FF_W - 2] + 6u * st[FF_W - 1]);
}

/*
 * yuyv: 320000-byte frame; phase 0 or 1; col0 0 or 200; lut: 256 entries; out: 160000 bytes.
 * Returns 0, or -1 on bad arguments (nothing written then).
 */
int ff_preprocess(const uint8_t *yuyv, uint32_t len, int phase, int col0,
                  const uint8_t lut[256], uint8_t *out, uint32_t out_len)
{
    uint16_t ring[5][FF_W];    /* horizontal sums of source rows, row r in ring[r % 5] */
    int y, x, next = 0;

    if (yuyv == NULL || lut == NULL || out == NULL)
        return -1;
    if (len != FF_IN_LEN || out_len != FF_OUT_LEN)
        return -1;
    if ((phase != 0 && phase != 1) || (col0 != 0 && col0 != FF_COLS))
        return -1;

    for (y = 0; y < FF_H; y++) {
        const int last = y + 2 < FF_H ? y + 2 : FF_H - 1;
        const uint16_t *r0, *r1, *r2, *r3, *r4;
        uint8_t *o = out + (uint32_t)y * FF_W;

        /* rows y-2..y+2 (reflected) are needed; they have distinct residues mod 5 */
        while (next <= last) {
            row_pass(yuyv, next, phase, col0, ring[next % 5]);
            next++;
        }
        r0 = ring[reflect101(y - 2, FF_H) % 5];
        r1 = ring[reflect101(y - 1, FF_H) % 5];
        r2 = ring[y % 5];
        r3 = ring[reflect101(y + 1, FF_H) % 5];
        r4 = ring[reflect101(y + 2, FF_H) % 5];
        for (x = 0; x < FF_W; x++) {
            const uint32_t sum = (uint32_t)r0[x] + 4u * r1[x] + 6u * r2[x] + 4u * r3[x] + r4[x];
            o[x] = lut[(sum + 128u) >> 8];      /* sum <= 65280, so the index is <= 255 */
        }
    }
#ifdef FF_CORRUPT_FOR_TEST
    out[12345] = (uint8_t)(out[12345] + 1u);
#endif
    return 0;
}
