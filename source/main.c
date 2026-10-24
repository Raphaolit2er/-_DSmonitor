#include <nds.h>
#include <dswifi9.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <sys/ioctl.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>

#ifndef TCP_NODELAY
#define TCP_NODELAY 1
#endif

// ----- Configuration -----
char serverIP[16] = "192.168.1.100";
int serverPort = 8888;

// ----- Screen & Networking States -----
u16 backBottom[256 * 192] __attribute__((aligned(32))); // 32-byte aligned for DMA + cache ops
u16* subVram = NULL;
int bgBottom = -1;
u16 palette[256];
volatile bool running = false;
volatile bool vblank_flag = false;
bool is_streaming = false;
static bool wifi_initialized = false;

static bool single_on_top = true;
static bool frame_single_on_top = true;

// Single UDP datagram buffers (always overwrite to keep only the newest packet)
#define UDP_MAX_PKT 2048
static u8 udp_rx_latest[UDP_MAX_PKT] __attribute__((aligned(32)));
static u8 udp_rx_scratch[UDP_MAX_PKT] __attribute__((aligned(32)));
static u16 frame_w = 256, frame_h = 192;

// ----- DMA Channel 3 Wait Helper (portable across libnds versions) -----
static inline void waitDma3(void) {
    while (dmaBusy(3)) ;
}

void onVBlank(void) {
    vblank_flag = true;
}

// ----- Perceptual 256-Color Palette (Matches PC Encoder) -----
void initPalette(void) {
    int idx = 0;
    for (int r = 0; r < 6; r++) {
        u8 r5 = (r * 31 + 2) / 5;
        for (int g = 0; g < 6; g++) {
            u8 g5 = (g * 31 + 2) / 5;
            for (int b = 0; b < 6; b++) {
                u8 b5 = (b * 31 + 2) / 5;
                palette[idx++] = RGB15(r5, g5, b5) | BIT(15);
            }
        }
    }
    for (int k = 0; k < 32; k++) {
        palette[idx++] = RGB15(k, k, k) | BIT(15);
    }
    static const u8 extra[8][3] = {
        {28, 22, 18}, {24, 17, 13}, {19, 13, 9}, {14, 9, 6},
        {3, 5, 8},    {5, 9, 14},   {8, 14, 20}, {16, 22, 28}
    };
    for (int i = 0; i < 8; i++) {
        palette[idx++] = RGB15(extra[i][0], extra[i][1], extra[i][2]) | BIT(15);
    }
}

// ----- Custom Native Keyboard (For Startup IP/Port Menu Only) -----
#define KB_ROWS 4
#define KB_COLS 3
const char kb_keys[KB_ROWS][KB_COLS] = {
    {'7', '8', '9'}, {'4', '5', '6'},
    {'1', '2', '3'}, {'0', '\b', '\n'}
};
int kb_cx = 0, kb_cy = 0;
bool kb_visible = false;

const u8* getGlyph(char ch) {
    static const u8 glyph_0[] = {0x3E,0x51,0x49,0x45,0x3E};
    static const u8 glyph_1[] = {0x00,0x42,0x7F,0x40,0x00};
    static const u8 glyph_2[] = {0x42,0x61,0x51,0x49,0x46};
    static const u8 glyph_3[] = {0x21,0x41,0x45,0x4B,0x31};
    static const u8 glyph_4[] = {0x18,0x14,0x12,0x7F,0x10};
    static const u8 glyph_5[] = {0x27,0x45,0x45,0x45,0x39};
    static const u8 glyph_6[] = {0x3C,0x4A,0x49,0x49,0x30};
    static const u8 glyph_7[] = {0x01,0x71,0x09,0x05,0x03};
    static const u8 glyph_8[] = {0x36,0x49,0x49,0x49,0x36};
    static const u8 glyph_9[] = {0x06,0x49,0x49,0x29,0x1E};
    static const u8 glyph_less[]  = {0x00,0x08,0x14,0x22,0x41};
    static const u8 glyph_minus[] = {0x08,0x08,0x08,0x08,0x08};
    static const u8 glyph_E[] = {0x7F,0x49,0x49,0x49,0x41};
    static const u8 glyph_N[] = {0x7F,0x04,0x08,0x10,0x7F};
    static const u8 glyph_T[] = {0x01,0x01,0x7F,0x01,0x01};
    static const u8 glyph_blank[] = {0,0,0,0,0};

    switch(ch) {
        case '0': return glyph_0; case '1': return glyph_1;
        case '2': return glyph_2; case '3': return glyph_3;
        case '4': return glyph_4; case '5': return glyph_5;
        case '6': return glyph_6; case '7': return glyph_7;
        case '8': return glyph_8; case '9': return glyph_9;
        case '<': return glyph_less; case '-': return glyph_minus;
        case 'E': return glyph_E; case 'N': return glyph_N;
        case 'T': return glyph_T; default:  return glyph_blank;
    }
}

void flushMenuSubScreen(void) {
    waitDma3();
    DC_FlushRange(backBottom, sizeof(backBottom));
    if (bgBottom != -1) {
        dmaCopyHalfWords(3, backBottom, bgGetGfxPtr(bgBottom), sizeof(backBottom));
    }
}

void drawKeyboardToBuffer(void) {
    if (!kb_visible) return;
    for (int i = 0; i < 256 * 192; i++) backBottom[i] = RGB15(3, 3, 3) | BIT(15);
    for (int row = 0; row < KB_ROWS; row++) {
        for (int col = 0; col < KB_COLS; col++) {
            int kx = 40 + col * 60, ky = 96 + row * 24, kw = 56, kh = 20;
            u16 bg = (kb_cy == row && kb_cx == col) ? (RGB15(31, 0, 0) | BIT(15)) : (RGB15(12, 12, 12) | BIT(15));
            for (int py = 0; py < kh; py++)
                for (int px = 0; px < kw; px++) backBottom[(ky + py) * 256 + (kx + px)] = bg;

            char label[4];
            char c = kb_keys[row][col];
            if (c == '\b') strcpy(label, "<-");
            else if (c == '\n') strcpy(label, "ENT");
            else { label[0] = c; label[1] = '\0'; }

            int textLen = strlen(label);
            int textPx = kx + (kw - textLen * 6) / 2 + 1;
            int textPy = ky + 6;
            for (int i = 0; i < textLen; i++) {
                const u8* glyph = getGlyph(label[i]);
                for (int gx = 0; gx < 5; gx++) {
                    u8 colBits = glyph[gx];
                    for (int gy = 0; gy < 7; gy++)
                        if ((colBits >> gy) & 1)
                            backBottom[(textPy + gy) * 256 + (textPx + gx)] = RGB15(31, 31, 31) | BIT(15);
                }
                textPx += 6;
            }
        }
    }
}

void refreshKeyboard(void) { drawKeyboardToBuffer(); flushMenuSubScreen(); }
void showCustomKeyboard(void) { kb_cx = 0; kb_cy = 0; kb_visible = true; refreshKeyboard(); }
void hideCustomKeyboard(void) { kb_visible = false; memset(backBottom, 0, sizeof(backBottom)); flushMenuSubScreen(); }

char updateCustomKeyboard(u32 down, u32 repeat) {
    bool changed = false;
    if (repeat & KEY_UP)    { kb_cy--; if (kb_cy < 0) kb_cy = KB_ROWS - 1; changed = true; }
    if (repeat & KEY_DOWN)  { kb_cy++; if (kb_cy >= KB_ROWS) kb_cy = 0; changed = true; }
    if (repeat & KEY_LEFT)  { kb_cx--; if (kb_cx < 0) kb_cx = KB_COLS - 1; changed = true; }
    if (repeat & KEY_RIGHT) { kb_cx++; if (kb_cx >= KB_COLS) kb_cx = 0; changed = true; }
    if (down & KEY_TOUCH) {
        touchPosition touch;
        touchRead(&touch);
        if (touch.py >= 96 && touch.px >= 40) {
            int gx = (touch.px - 40) / 60;
            int gy = (touch.py - 96) / 24;
            if (gx >= 0 && gx < KB_COLS && gy >= 0 && gy < KB_ROWS) {
                kb_cx = gx; kb_cy = gy; down |= KEY_A; changed = true;
            }
        }
    }
    if (changed) refreshKeyboard();
    if (down & KEY_A) return kb_keys[kb_cy][kb_cx];
    return 0;
}

int getInputString(char *buf, int maxLen) {
    showCustomKeyboard();
    int len = 0;
    buf[0] = '\0';
    iprintf("\x1b[1;0H> %-7s   ", buf);

    while (1) {
        swiWaitForVBlank();
        scanKeys();
        char c = updateCustomKeyboard(keysDown(), keysDownRepeat());
        if (c > 0) {
            if (c == '\n') break;
            else if (c == '\b') { if (len > 0) buf[--len] = '\0'; }
            else if (len < maxLen - 1) { buf[len++] = c; buf[len] = '\0'; }
            iprintf("\x1b[1;0H> %-7s   ", buf);
        }
        if (keysDown() & KEY_SELECT) { buf[0] = '\0'; len = 0; break; }
    }
    hideCustomKeyboard();
    return len;
}

void editIP(void) {
    int octets[4] = {0};
    int cursor = 0, holdFrames = 0;
    const int HOLD_DELAY = 20, REPEAT_RATE = 3;
    sscanf(serverIP, "%d.%d.%d.%d", &octets[0], &octets[1], &octets[2], &octets[3]);
    consoleClear();
    bool draw = true;
    while (1) {
        if (draw) {
            printf("\x1b[0;0HSet Server IP (D-Pad, A=OK):\n");
            for (int i = 0; i < 4; i++) {
                if (i == cursor) printf("[%3d]", octets[i]);
                else printf(" %3d ", octets[i]);
                if (i < 3) printf(".");
            }
            printf("\n\nHold UP/DOWN to change\nA: confirm IP");
            draw = false;
        }

        swiWaitForVBlank();
        scanKeys();
        u32 held = keysHeld(), down = keysDown();
        if ((down & KEY_RIGHT) && cursor < 3) { cursor++; draw = true; }
        if ((down & KEY_LEFT)  && cursor > 0) { cursor--; draw = true; }

        if (held & KEY_UP) {
            if (down & KEY_UP) { octets[cursor] = (octets[cursor] + 1) & 255; holdFrames = 0; draw = true; }
            else {
                holdFrames++;
                if (holdFrames >= HOLD_DELAY && (holdFrames - HOLD_DELAY) % REPEAT_RATE == 0) {
                    octets[cursor] = (octets[cursor] + 1) & 255; draw = true;
                }
            }
        } else if (held & KEY_DOWN) {
            if (down & KEY_DOWN) { octets[cursor] = (octets[cursor] - 1) & 255; holdFrames = 0; draw = true; }
            else {
                holdFrames++;
                if (holdFrames >= HOLD_DELAY && (holdFrames - HOLD_DELAY) % REPEAT_RATE == 0) {
                    octets[cursor] = (octets[cursor] - 1) & 255; draw = true;
                }
            }
        } else holdFrames = 0;

        if (down & KEY_A) break;
    }
    sprintf(serverIP, "%d.%d.%d.%d", octets[0], octets[1], octets[2], octets[3]);
    consoleClear();
}

// ----- Direct-to-VRAM Line Pointer (Immediate Screen Tearing, Zero Latency) -----
static inline u16* get_vram_line(int py) {
    if (frame_h == 192) {
        return frame_single_on_top ? &VRAM_A[py * 256] : &subVram[py * 256];
    }
    return (py < 192) ? &VRAM_A[py * 256] : &subVram[(py - 192) * 256];
}

// ----- Fast Division-Free Quadtree Decoder (Writes Directly to VRAM) -----
void decode_node(u16 x, u16 y, u16 size, u8** stream, u8* stream_end) {
    if (x >= frame_w || y >= frame_h) return;
    if (*stream >= stream_end) return;

    u8 type = *(*stream)++;

    int draw_w = (x + size > frame_w) ? (frame_w - x) : size;
    int draw_h = (y + size > frame_h) ? (frame_h - y) : size;

    if (type == 0x00) { // S_SPLIT
        u16 half = size >> 1;
        decode_node(x, y, half, stream, stream_end);
        decode_node(x + half, y, half, stream, stream_end);
        decode_node(x, y + half, half, stream, stream_end);
        decode_node(x + half, y + half, half, stream, stream_end);
    }
    else if (type == 0x01) { // S_SOLID (2-byte RGB15)
        if (*stream + 2 > stream_end) return;
        u16 color = (u16)((*stream)[0] | ((*stream)[1] << 8)) | BIT(15);
        *stream += 2;
        for (int cy = 0; cy < draw_h; cy++) {
            u16* dst = get_vram_line(y + cy) + x;
            for (int cx = 0; cx < draw_w; cx++) dst[cx] = color;
        }
    }
    else if (type == 0x02) { // S_GRAD (Division-free bit-shift 15-bit gradient)
        if (*stream + 5 > stream_end) return;
        u16 c0 = (u16)((*stream)[0] | ((*stream)[1] << 8));
        u16 c1 = (u16)((*stream)[2] | ((*stream)[3] << 8));
        u8 dir = (*stream)[4] & 3;
        u8 dshift = ((*stream)[4] >> 2) & 7;
        *stream += 5;

        int r0 = c0 & 31, g0 = (c0 >> 5) & 31, b0 = (c0 >> 10) & 31;
        int dr = (int)(c1 & 31) - r0;
        int dg = (int)((c1 >> 5) & 31) - g0;
        int db = (int)((c1 >> 10) & 31) - b0;

        for (int cy = 0; cy < draw_h; cy++) {
            u16* dst = get_vram_line(y + cy) + x;
            for (int cx = 0; cx < draw_w; cx++) {
                int t = (dir == 0) ? cx : (dir == 1) ? cy : (dir == 2) ? (cx + cy) : (cx + (size - 1 - cy));
                int r = r0 + ((dr * t) >> dshift);
                int g = g0 + ((dg * t) >> dshift);
                int b = b0 + ((db * t) >> dshift);
                dst[cx] = RGB15(r, g, b) | BIT(15);
            }
        }
    }
    else if (type == 0x03) { // S_RLE (row pointer hoisted out of per-pixel loop)
        if (*stream + 1 > stream_end) return;
        u8 rle_sz = *(*stream)++;
        if (*stream + rle_sz > stream_end) return;
        u8* rle_end = *stream + rle_sz;
        int cx = 0, cy = 0;
        u16* row_ptr = NULL;
        int last_cy = -1;
        while (*stream < rle_end && cy < size) {
            u8 count = *(*stream)++;
            u8 idx = *(*stream)++;
            u16 color = palette[idx];
            for (int i = 0; i < count; i++) {
                if (cy < draw_h && cx < draw_w) {
                    if (cy != last_cy) {
                        row_ptr = get_vram_line(y + cy) + x;
                        last_cy = cy;
                    }
                    row_ptr[cx] = color;
                }
                if (++cx >= size) {
                    cx = 0;
                    if (++cy >= size) break;
                }
            }
        }
        *stream = rle_end;
    }
    else if (type == 0x04) { // S_RAW
        int total = size * size;
        if (*stream + total > stream_end) return;
        for (int cy = 0; cy < size; cy++) {
            if (cy < draw_h) {
                u16* dst = get_vram_line(y + cy) + x;
                for (int cx = 0; cx < size; cx++) {
                    u8 idx = *(*stream)++;
                    if (cx < draw_w) dst[cx] = palette[idx];
                }
            } else {
                *stream += size;
            }
        }
    }
    else if (type == 0x05) { // S_BTC (4-byte RGB15 endpoints + 1bpp bitmask)
        int mask_bytes = (size * size + 7) >> 3;
        if (*stream + 4 + mask_bytes > stream_end) return;
        u16 c0 = (u16)((*stream)[0] | ((*stream)[1] << 8)) | BIT(15);
        u16 c1 = (u16)((*stream)[2] | ((*stream)[3] << 8)) | BIT(15);
        *stream += 4;
        u8* mask = *stream;
        *stream += mask_bytes;

        for (int cy = 0; cy < draw_h; cy++) {
            u16* dst = get_vram_line(y + cy) + x;
            int row_base = cy * size;
            for (int cx = 0; cx < draw_w; cx++) {
                int idx = row_base + cx;
                u8 bit = (mask[idx >> 3] >> (idx & 7)) & 1;
                dst[cx] = bit ? c1 : c0;
            }
        }
    }
    else if (type == 0x06) { // S_PAL4 (4-color local palette + 2bpp indices)
        int data_bytes = (size * size + 3) >> 2;
        if (*stream + 4 + data_bytes > stream_end) return;
        u16 pal4[4];
        pal4[0] = palette[(*stream)[0]];
        pal4[1] = palette[(*stream)[1]];
        pal4[2] = palette[(*stream)[2]];
        pal4[3] = palette[(*stream)[3]];
        *stream += 4;
        u8* data = *stream;
        *stream += data_bytes;

        for (int cy = 0; cy < draw_h; cy++) {
            u16* dst = get_vram_line(y + cy) + x;
            int row_base = cy * size;
            for (int cx = 0; cx < draw_w; cx++) {
                int idx = row_base + cx;
                u8 pidx = (data[idx >> 2] >> ((idx & 3) << 1)) & 3;
                dst[cx] = pal4[pidx];
            }
        }
    }
    // 0xFF = S_UNCHANGED
}

static void flipVramSingleScreen(void) {
    waitDma3();
    if (frame_single_on_top) {
        dmaCopyHalfWords(3, subVram, VRAM_A, 256 * 192 * sizeof(u16));
        waitDma3();
        dmaFillHalfWords(0, subVram, 256 * 192 * sizeof(u16));
    } else {
        dmaCopyHalfWords(3, VRAM_A, subVram, 256 * 192 * sizeof(u16));
        waitDma3();
        dmaFillHalfWords(0, VRAM_A, 256 * 192 * sizeof(u16));
    }
    waitDma3();
}

// ----- UDP Receiver: Drains Socket Queue and Decodes ONLY the Last Frame Into VRAM -----
void process_udp_network(int udp_sock) {
    int latest_len = -1;
    struct sockaddr_in from_addr;

    // Drain all pending UDP datagrams; dswifi9 REQUIRES non-NULL from_addr & from_len!
    while (1) {
        socklen_t from_len = sizeof(from_addr);
        int r = recvfrom(udp_sock, udp_rx_scratch, sizeof(udp_rx_scratch), 0,
                         (struct sockaddr*)&from_addr, &from_len);
        if (r <= 0) break;

        // Ensure the CPU sees exactly what was written to udp_rx_scratch
        // (covers both CPU-written and DMA-written recvfrom implementations).
        DC_FlushRange(udp_rx_scratch, (u32)r);

        if (r >= 9 && udp_rx_scratch[0] == 0x03) {
            memcpy(udp_rx_latest, udp_rx_scratch, r);
            latest_len = r;
        }
    }

    if (latest_len < 9) return;

    u16 w = (udp_rx_latest[1] << 8) | udp_rx_latest[2];
    u16 h = (udp_rx_latest[3] << 8) | udp_rx_latest[4];
    u32 payload_sz = ((u32)udp_rx_latest[5] << 24) |
                     ((u32)udp_rx_latest[6] << 16) |
                     ((u32)udp_rx_latest[7] << 8)  |
                     ((u32)udp_rx_latest[8]);

    if (payload_sz == 0 || 9 + payload_sz > (u32)latest_len) return;

    frame_w = w;
    frame_h = h;

    if (frame_h == 192 && single_on_top != frame_single_on_top) {
        frame_single_on_top = single_on_top;
        flipVramSingleScreen();
    }

    u8* stream_ptr = udp_rx_latest + 9;
    u8* stream_end = stream_ptr + payload_sz;

    u16 root_size = (frame_h > 256) ? 512 : 256;
    decode_node(0, 0, root_size, &stream_ptr, stream_end);
}

// ----- Main Monitor Loop (TCP Handshake + UDP Streaming + Screen Swap Only) -----
void runMonitor(void) {
    if (!wifi_initialized) {
        iprintf("Connecting to AP...\n");
        if (!Wifi_InitDefault(WFC_CONNECT)) {
            iprintf("WiFi init failed!\n");
            while (1) swiWaitForVBlank();
        }
        wifi_initialized = true;
    }

    while (1) {
        int st = Wifi_AssocStatus();
        if (st == ASSOCSTATUS_ASSOCIATED) break;
        if (st == ASSOCSTATUS_CANNOTCONNECT) {
            iprintf("Cannot connect to AP.\n");
            while (1) swiWaitForVBlank();
        }
        swiWaitForVBlank();
    }

    while (1) {
        int tcp_sock = socket(AF_INET, SOCK_STREAM, 0);
        if (tcp_sock < 0) {
            iprintf("TCP Socket error\n");
            while (1) swiWaitForVBlank();
        }

        struct sockaddr_in saddr;
        memset(&saddr, 0, sizeof(saddr));
        saddr.sin_family = AF_INET;
        saddr.sin_port = htons(serverPort);
        saddr.sin_addr.s_addr = inet_addr(serverIP);

        iprintf("TCP Handshake with PC...\n");
        if (connect(tcp_sock, (struct sockaddr*)&saddr, sizeof(saddr)) < 0) {
            iprintf("Failed! Retrying...\n");
            closesocket(tcp_sock);
            for (int i = 0; i < 90; i++) swiWaitForVBlank();
            continue;
        }

        // Create UDP socket bound to serverPort + 1 (avoids port collision on emulators/LAN)
        int udp_sock = socket(AF_INET, SOCK_DGRAM, 0);
        if (udp_sock < 0) {
            closesocket(tcp_sock);
            for (int i = 0; i < 60; i++) swiWaitForVBlank();
            continue;
        }

        u16 ds_udp_port = (u16)(serverPort + 1);
        struct sockaddr_in local_udp;
        memset(&local_udp, 0, sizeof(local_udp));
        local_udp.sin_family = AF_INET;
        local_udp.sin_port = htons(ds_udp_port);
        local_udp.sin_addr.s_addr = INADDR_ANY;
        if (bind(udp_sock, (struct sockaddr*)&local_udp, sizeof(local_udp)) < 0) {
            ds_udp_port = (u16)serverPort;
            local_udp.sin_port = htons(ds_udp_port);
            bind(udp_sock, (struct sockaddr*)&local_udp, sizeof(local_udp));
        }

        // 1. Send 4-byte TCP handshake telling PC our UDP port
        u8 hs[4] = { 'D', 'S', (u8)((ds_udp_port >> 8) & 0xFF), (u8)(ds_udp_port & 0xFF) };
        send(tcp_sock, hs, 4, 0);

        // 2. Send UDP registration probe so PC also learns any NAT/emulator-mapped UDP port
        sendto(udp_sock, "DSUP", 4, 0, (struct sockaddr*)&saddr, sizeof(saddr));

        iprintf("Connected (UDP Stream)!\n");

        videoSetMode(MODE_FB0);
        vramSetBankA(VRAM_A_LCD);
        subVram = bgGetGfxPtr(bgBottom);
        is_streaming = true;

        waitDma3();
        dmaFillHalfWords(0, VRAM_A, 256 * 192 * sizeof(u16));
        waitDma3();
        dmaFillHalfWords(0, subVram, 256 * 192 * sizeof(u16));
        waitDma3();

        int one = 1;
        setsockopt(tcp_sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        ioctl(tcp_sock, FIONBIO, &one);
        ioctl(udp_sock, FIONBIO, &one);

        running = true;
        int heartbeat_ticks = 0;

        // Zero-wait loop: decodes latest UDP frame directly to VRAM with screen tearing
        while (running) {
            // Check TCP control socket for server disconnect
            u8 tcp_dummy;
            int tr = recv(tcp_sock, &tcp_dummy, 1, 0);
            if (tr == 0 || (tr < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
                running = false;
                break;
            }

            // Drain UDP socket and render newest frame directly to VRAM
            process_udp_network(udp_sock);

            if (vblank_flag) {
                vblank_flag = false;
                scanKeys();
                u32 pressed = keysDown();

                // Only allowed input: UP / DOWN to change single screen position
                if (pressed & (KEY_UP | KEY_DOWN)) {
                    single_on_top = !single_on_top;
                    if (frame_h == 192 && single_on_top != frame_single_on_top) {
                        frame_single_on_top = single_on_top;
                        flipVramSingleScreen();
                    }
                }

                // Send UDP probe every ~30 VBlanks so PC always has our active UDP port
                if (++heartbeat_ticks >= 30) {
                    heartbeat_ticks = 0;
                    sendto(udp_sock, "DSUP", 4, 0, (struct sockaddr*)&saddr, sizeof(saddr));
                }
            }
        }

        closesocket(udp_sock);
        closesocket(tcp_sock);

        is_streaming = false;
        videoSetMode(MODE_0_2D);
        vramSetBankA(VRAM_A_MAIN_BG);
        consoleInit(NULL, 0, BgType_Text4bpp, BgSize_T_256x256, 31, 0, true, true);

        iprintf("Disconnected. Reconnecting...\n");
        for (int i = 0; i < 60; i++) swiWaitForVBlank();
    }
}

void menuMode(void) {
    is_streaming = false;
    videoSetMode(MODE_0_2D);
    vramSetBankA(VRAM_A_MAIN_BG);
    videoSetModeSub(MODE_5_2D);
    vramSetBankC(VRAM_C_SUB_BG);
    bgBottom = bgInitSub(3, BgType_Bmp16, BgSize_B16_256x256, 2, 0);
    subVram = bgGetGfxPtr(bgBottom);
    REG_DISPCNT_SUB = MODE_5_2D | DISPLAY_BG3_ACTIVE;
    consoleInit(NULL, 0, BgType_Text4bpp, BgSize_T_256x256, 31, 0, true, true);
    memset(backBottom, 0, sizeof(backBottom));
    flushMenuSubScreen();
}

int main(void) {
    irqSet(IRQ_VBLANK, onVBlank);
    irqEnable(IRQ_VBLANK);            // <-- was missing; without this onVBlank() never fires
    initPalette();
    menuMode();

    while (1) {
        iprintf("\x1b[2JDS Monitor\n\n");
        iprintf("Target: %s:%d\n\n", serverIP, serverPort);
        iprintf("A: Start Monitor Mode\n");
        iprintf("Y: Config IP and Port\n");

        while (1) {
            swiWaitForVBlank();
            scanKeys();
            u32 down = keysDown();

            if (down & KEY_A) {
                iprintf("\x1b[2J");
                runMonitor();
                menuMode();
                break;
            }
            if (down & KEY_Y) {
                editIP();
                iprintf("\x1b[2JEnter new Port:\n\n> ");
                char newPortStr[8];
                if (getInputString(newPortStr, sizeof(newPortStr)) > 0) {
                    int p = atoi(newPortStr);
                    if (p > 0 && p <= 65535) serverPort = p;
                }
                menuMode();
                break;
            }
        }
    }
    return 0;
}