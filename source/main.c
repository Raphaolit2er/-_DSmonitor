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
u16 backTop[256 * 192] __attribute__((aligned(4)));
u16 backBottom[256 * 192] __attribute__((aligned(4)));
int bgTop = -1;
int bgBottom = -1;
u16 palette[256];
volatile bool running = false;
bool is_streaming = false; 

static bool single_on_top = true;         
static bool frame_single_on_top = true;   

// Quadtree Network State
static u8 rx_buf[200000] __attribute__((aligned(4)));
static u32 rx_got = 0;
static u32 rx_needed = 1;
static int rx_state = 0; // 0:CMD, 1:QUAD_HEAD, 2:QUAD_DATA
static u16 frame_w, frame_h;

// ----- Custom Native Keyboard -----
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

void swapScreens(void) {
    DC_FlushRange(backBottom, sizeof(backBottom));
    DC_FlushRange(backTop, sizeof(backTop));
    
    if (bgBottom != -1) {
        dmaCopyHalfWords(3, backBottom, bgGetGfxPtr(bgBottom), 256 * 192 * sizeof(u16));
    }
    if (is_streaming) {
        dmaCopyHalfWords(3, backTop, VRAM_A, 256 * 192 * sizeof(u16));
    }
}

void drawKeyboardToBuffer(void) {
    if (!kb_visible) return;
    for (int i = 0; i < 256*192; i++) backBottom[i] = RGB15(3,3,3)|BIT(15);
    for (int row = 0; row < KB_ROWS; row++) {
        for (int col = 0; col < KB_COLS; col++) {
            int kx = 40 + col*60, ky = 96 + row*24, kw = 56, kh = 20;
            u16 bg = (kb_cy==row && kb_cx==col) ? RGB15(31,0,0)|BIT(15) : RGB15(12,12,12)|BIT(15);
            for (int py=0; py<kh; py++)
                for (int px=0; px<kw; px++) backBottom[(ky+py)*256 + (kx+px)] = bg;
            
            char label[4]; char c = kb_keys[row][col];
            if (c == '\b') strcpy(label, "<-"); 
            else if (c == '\n') strcpy(label, "ENT"); 
            else { label[0]=c; label[1]='\0'; }
            
            int textLen = strlen(label); int textPx = kx + (kw - textLen*6)/2 + 1; int textPy = ky + 6;
            for (int i=0; i<textLen; i++) {
                const u8* glyph = getGlyph(label[i]);
                for (int gx=0; gx<5; gx++) {
                    u8 colBits = glyph[gx];
                    for (int gy=0; gy<7; gy++) if ((colBits >> gy) & 1) backBottom[(textPy+gy)*256 + (textPx+gx)] = RGB15(31,31,31)|BIT(15);
                }
                textPx += 6;
            }
        }
    }
}

void refreshKeyboard(void) { drawKeyboardToBuffer(); swapScreens(); }
void showCustomKeyboard(void) { kb_cx=0; kb_cy=0; kb_visible=true; refreshKeyboard(); }
void hideCustomKeyboard(void) { kb_visible=false; memset(backBottom, 0, sizeof(backBottom)); swapScreens(); }

char updateCustomKeyboard(u32 down, u32 repeat) {
    bool changed = false;
    if (repeat & KEY_UP)    { kb_cy--; if (kb_cy<0) kb_cy=KB_ROWS-1; changed=true; }
    if (repeat & KEY_DOWN)  { kb_cy++; if (kb_cy>=KB_ROWS) kb_cy=0; changed=true; }
    if (repeat & KEY_LEFT)  { kb_cx--; if (kb_cx<0) kb_cx=KB_COLS-1; changed=true; }
    if (repeat & KEY_RIGHT) { kb_cx++; if (kb_cx>=KB_COLS) kb_cx=0; changed=true; }
    if (down & KEY_TOUCH) {
        touchPosition touch; touchRead(&touch);
        if (touch.py >= 96 && touch.px >= 40) {
            int gx = (touch.px - 40) / 60; int gy = (touch.py - 96) / 24;
            if (gx>=0 && gx<KB_COLS && gy>=0 && gy<KB_ROWS) { kb_cx = gx; kb_cy = gy; down |= KEY_A; changed = true; }
        }
    }
    if (changed) refreshKeyboard();
    if (down & KEY_A) return kb_keys[kb_cy][kb_cx];
    return 0;
}

int getInputString(char *buf, int maxLen) {
    showCustomKeyboard();
    int len = 0; buf[0] = '\0';
    iprintf("\x1b[1;0H> %-7s   ", buf); 
    
    while (1) {
        swiWaitForVBlank(); scanKeys();
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
    int octets[4] = {0}; int cursor=0, holdFrames=0; const int HOLD_DELAY=20, REPEAT_RATE=3;
    sscanf(serverIP, "%d.%d.%d.%d", &octets[0], &octets[1], &octets[2], &octets[3]);
    consoleClear();
    bool draw = true;
    while (1) {
        if (draw) {
            printf("\x1b[0;0HSet Server IP (D-Pad, A=OK):\n");
            for (int i=0; i<4; i++) {
                if (i==cursor) printf("[%3d]", octets[i]); else printf(" %3d ", octets[i]);
                if (i<3) printf(".");
            }
            printf("\n\nHold UP/DOWN to change\nA: confirm IP");
            draw = false;
        }
        
        swiWaitForVBlank(); scanKeys(); u32 held = keysHeld(), down = keysDown();
        if (down & KEY_RIGHT && cursor<3) { cursor++; draw = true; }
        if (down & KEY_LEFT  && cursor>0) { cursor--; draw = true; }
        
        if (held & KEY_UP) {
            if (down & KEY_UP) { octets[cursor] = (octets[cursor]+1)%256; holdFrames=0; draw = true; }
            else { holdFrames++; if (holdFrames>=HOLD_DELAY && (holdFrames-HOLD_DELAY)%REPEAT_RATE==0) { octets[cursor] = (octets[cursor]+1)%256; draw = true; } }
        } else if (held & KEY_DOWN) {
            if (down & KEY_DOWN) { octets[cursor] = (octets[cursor]-1<0)?255:octets[cursor]-1; holdFrames=0; draw = true; }
            else { holdFrames++; if (holdFrames>=HOLD_DELAY && (holdFrames-HOLD_DELAY)%REPEAT_RATE==0) { octets[cursor] = (octets[cursor]-1<0)?255:octets[cursor]-1; draw = true; } }
        } else holdFrames=0;
        
        if (down & KEY_A) break;
    }
    sprintf(serverIP, "%d.%d.%d.%d", octets[0], octets[1], octets[2], octets[3]);
    consoleClear();
}

// ----- Recursive Quadtree Decoder (FIXED with horizontal clipping) -----
static inline u16* get_backbuffer(int py) {
    if (frame_h == 192) {
        return frame_single_on_top ? &backTop[py * 256] : &backBottom[py * 256];
    }
    return (py < 192) ? &backTop[py * 256] : &backBottom[(py - 192) * 256];
}

// ============ FIXED decode_node ============
void decode_node(u16 x, u16 y, u16 size, u8** stream, u8* stream_end) {
    if (*stream >= stream_end) return;
    u8 type = *(*stream)++;
    
    // If entirely outside the right edge, skip data without drawing
    if (x >= frame_w) {
        if (type == 0x00) {
            u16 half = size / 2;
            decode_node(x, y, half, stream, stream_end);
            decode_node(x + half, y, half, stream, stream_end);
            decode_node(x, y + half, half, stream, stream_end);
            decode_node(x + half, y + half, half, stream, stream_end);
        } else if (type == 0x01) {
            if (*stream + 3 > stream_end) return;
            *stream += 3;
        } else if (type == 0x02) {
            if (*stream + 6 > stream_end) return;
            *stream += 6;
        } else if (type == 0x03) {
            if (*stream + 1 > stream_end) return;
            u8 rle_sz = *(*stream)++;
            if (*stream + rle_sz > stream_end) return;
            *stream += rle_sz;
        } else if (type == 0x04) {
            if (*stream + size * size > stream_end) return;
            *stream += size * size;
        } else if (type == 0x05) {
            int mask_bytes = (size * size + 7) >> 3;
            if (*stream + 2 + mask_bytes > stream_end) return;
            *stream += 2 + mask_bytes;
        } else if (type == 0x06) {
            int data_bytes = (size * size + 3) >> 2;
            if (*stream + 4 + data_bytes > stream_end) return;
            *stream += 4 + data_bytes;
        }
        // 0xFF needs no data
        return;
    }
    
    int draw_w = size;
    if (x + draw_w > frame_w) draw_w = frame_w - x;
    
    if (type == 0x00) { 
        u16 half = size / 2;
        decode_node(x, y, half, stream, stream_end);
        decode_node(x + half, y, half, stream, stream_end);
        decode_node(x, y + half, half, stream, stream_end);
        decode_node(x + half, y + half, half, stream, stream_end);
    } 
    else if (type == 0x01) { 
        if (*stream + 3 > stream_end) return;
        u8 r = *(*stream)++; u8 g = *(*stream)++; u8 b = *(*stream)++;
        u16 color = RGB15(r >> 3, g >> 3, b >> 3) | BIT(15);
        for (int cy = 0; cy < size; cy++) {
            if (y + cy >= frame_h) continue;
            u16* dst = get_backbuffer(y + cy) + x;
            for (int cx = 0; cx < draw_w; cx++) dst[cx] = color;
        }
    } 
    else if (type == 0x02) { 
        if (*stream + 6 > stream_end) return;
        u8 base = *(*stream)++; s8 dx = (s8)*(*stream)++; s8 dz = (s8)*(*stream)++;
        u8 mR = *(*stream)++; u8 mG = *(*stream)++; u8 mB = *(*stream)++;
        for (int cy = 0; cy < size; cy++) {
            if (y + cy >= frame_h) continue;
            int y_row = base + dz * cy;
            u16* dst = get_backbuffer(y + cy) + x;
            for (int cx = 0; cx < draw_w; cx++) {
                int v = y_row + dx * cx;
                if (v < 0) v = 0; else if (v > 255) v = 255;
                dst[cx] = RGB15((v * mR) >> 11, (v * mG) >> 11, (v * mB) >> 11) | BIT(15);
            }
        }
    } 
    else if (type == 0x03) { 
        if (*stream + 1 > stream_end) return;
        u8 rle_sz = *(*stream)++;
        if (*stream + rle_sz > stream_end) return;
        u8* rle_end = *stream + rle_sz;
        int cx = 0, cy = 0;
        while (*stream < rle_end && cy < size) {
            u8 count = *(*stream)++;
            u8 idx = *(*stream)++;
            u16 color = palette[idx];
            for (int i = 0; i < count; i++) {
                if (y + cy < frame_h && cx < draw_w) {
                    get_backbuffer(y + cy)[x + cx] = color;
                }
                cx++;
                if (cx >= size) { cx = 0; cy++; if (cy >= size) break; }
            }
        }
    } 
    else if (type == 0x04) { 
        for (int cy = 0; cy < size; cy++) {
            if (y + cy < frame_h) {
                u16* dst = get_backbuffer(y + cy) + x;
                for (int cx = 0; cx < size; cx++) {
                    if (*stream >= stream_end) return;
                    if (cx < draw_w) dst[cx] = palette[*(*stream)++];
                    else (*stream)++;
                }
            } else {
                if (*stream + size > stream_end) return;
                *stream += size;
            }
        }
    } 
    else if (type == 0x05) {
        if (*stream + 2 > stream_end) return;
        u8 min = *(*stream)++;
        u8 max = *(*stream)++;
        int mask_bytes = (size * size + 7) >> 3;
        if (*stream + mask_bytes > stream_end) return;
        u8* mask = *stream;
        *stream += mask_bytes;
        for (int cy = 0; cy < size; cy++) {
            if (y + cy >= frame_h) continue;
            u16* dst = get_backbuffer(y + cy) + x;
            for (int cx = 0; cx < draw_w; cx++) {
                int idx = cy * size + cx;
                u8 bit = (mask[idx >> 3] >> (idx & 7)) & 1;
                dst[cx] = palette[bit ? max : min];
            }
        }
    }
    else if (type == 0x06) {
        if (*stream + 4 > stream_end) return;
        u8 pal[4];
        pal[0] = *(*stream)++; pal[1] = *(*stream)++;
        pal[2] = *(*stream)++; pal[3] = *(*stream)++;
        int data_bytes = (size * size + 3) >> 2;
        if (*stream + data_bytes > stream_end) return;
        for (int cy = 0; cy < size; cy++) {
            if (y + cy >= frame_h) continue;
            u16* dst = get_backbuffer(y + cy) + x;
            for (int cx = 0; cx < draw_w; cx++) {
                int idx = cy * size + cx;
                int byte_idx = idx >> 2;
                int shift = (idx & 3) * 2;
                u8 pidx = ((*stream)[byte_idx] >> shift) & 3;
                dst[cx] = palette[pal[pidx]];
            }
        }
        *stream += data_bytes;
    }
    else if (type == 0xFF) {
        // UNCHANGED
    }
}

// ----- Non-blocking state machine for receiving frames -----
void process_network(int sock) {
    while (running) {
        int r = recv(sock, rx_buf + rx_got, rx_needed - rx_got, 0);
        
        if (r == 0) {
            running = false; 
            return;
        }
        if (r < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) return; 
            running = false; 
            return;
        }

        rx_got += r;
        if (rx_got < rx_needed) continue; 

        if (rx_state == 0) { 
            u8 cmd = rx_buf[0];
            rx_got = 0;
            if (cmd == 0xFE) {
                swapScreens();
                rx_needed = 1;
            } 
            else if (cmd == 0x03) { // 0x03 indicates QUADTREE FRAME HEADER
                rx_state = 1;
                rx_needed = 8; // w(2), h(2), payload_size(4)
            } else {
                rx_needed = 1; 
            }
        } 
        else if (rx_state == 1) { 
            frame_w = (rx_buf[0] << 8) | rx_buf[1];
            frame_h = (rx_buf[2] << 8) | rx_buf[3];
            u32 payload_sz = (rx_buf[4] << 24) | (rx_buf[5] << 16) | (rx_buf[6] << 8) | rx_buf[7];
            
            if (frame_h == 192) {
                if (single_on_top != frame_single_on_top) {
                    frame_single_on_top = single_on_top;
                    if (frame_single_on_top)
                        memset(backBottom, 0, sizeof(backBottom));
                    else
                        memset(backTop, 0, sizeof(backTop));
                }
            }
            
            rx_got = 0;
            if (payload_sz > 0 && payload_sz <= sizeof(rx_buf)) {
                rx_state = 2;
                rx_needed = payload_sz;
            } else {
                rx_state = 0;
                rx_needed = 1;
            }
        } 
        else if (rx_state == 2) {
            u8* stream_ptr = rx_buf;
            u8* stream_end = rx_buf + rx_needed;
            
            // Skip encoder's internal width/height (first 2 bytes of payload)
            if (rx_needed >= 2) stream_ptr += 2;
            
            // Decode single tree rooted at 256x256 (or 512 for taller frames)
            u16 root_size = (frame_h > 256) ? 512 : 256;
            decode_node(0, 0, root_size, &stream_ptr, stream_end);
            
            rx_got = 0;
            rx_state = 0;
            rx_needed = 1;
        }
    }
}

// ----- Reliable send (retry until all bytes are queued) -----
int send_all(int sock, const uint8_t *data, int len) {
    int total = 0;
    while (total < len && running) {
        int n = send(sock, data + total, len - total, 0);
        if (n > 0) {
            total += n;
        } else if (n == 0) {
            swiWaitForVBlank();
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            swiWaitForVBlank();
        } else {
            return -1;
        }
    }
    return total;
}

// ----- Main monitor loop (buttons + touch lists) -----
void runMonitor(void) {
    iprintf("Connecting to AP...\n");
    if (!Wifi_InitDefault(WFC_CONNECT)) { iprintf("WiFi init failed!\n"); while(1) swiWaitForVBlank(); }
    while (1) {
        int st = Wifi_AssocStatus();
        if (st == ASSOCSTATUS_ASSOCIATED) break;
        if (st == ASSOCSTATUS_CANNOTCONNECT) { iprintf("Cannot connect to AP.\n"); while (1) swiWaitForVBlank(); }
        swiWaitForVBlank();
    }

    bool quit = false;
    while (!quit) {
        int sock = socket(AF_INET, SOCK_STREAM, 0);
        if (sock < 0) { iprintf("Socket error\n"); while(1) swiWaitForVBlank(); }

        struct sockaddr_in saddr;
        memset(&saddr, 0, sizeof(saddr));
        saddr.sin_family = AF_INET;
        saddr.sin_port = htons(serverPort);
        saddr.sin_addr.s_addr = inet_addr(serverIP);

        iprintf("Connecting to PC... (B=quit)\n");
        if (connect(sock, (struct sockaddr*)&saddr, sizeof(saddr)) < 0) {
            iprintf("Failed! Retrying...\n");
            closesocket(sock);
            for (int i = 0; i < 120 && !quit; i++) {
                swiWaitForVBlank(); scanKeys();
                if (keysDown() & KEY_B) quit = true;
            }
            continue;
        }

        iprintf("Connected!\n");
        
        videoSetMode(MODE_FB0);
        vramSetBankA(VRAM_A_LCD);
        is_streaming = true;
        
        memset(backTop, 0, sizeof(backTop));
        memset(backBottom, 0, sizeof(backBottom));
        swapScreens();

        int one = 1;
        setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        ioctl(sock, FIONBIO, &one);

        running = true;
        rx_state = 0; rx_got = 0; rx_needed = 1;

        touchPosition touch_buf[10];
        int touch_count = 0;
        bool touch_active = false;
        touchPosition last_touch = {0,0,0,0};

        int frameCounter = 0;

        while (running) {
            swiWaitForVBlank();
            scanKeys();
            frameCounter++;

            process_network(sock);
            if (!running) break;

            u32 held = keysHeld();
            u32 pressed = keysDown();
            u32 released = keysUp();
            
            if (pressed & KEY_UP || pressed & KEY_DOWN) {
                single_on_top = !single_on_top;
            }
            
            if (rx_state == 0 && frame_h == 192 && single_on_top != frame_single_on_top) {
                frame_single_on_top = single_on_top;
                if (frame_single_on_top) {
                    memcpy(backTop, backBottom, sizeof(backTop));
                    memset(backBottom, 0, sizeof(backBottom));
                } else {
                    memcpy(backBottom, backTop, sizeof(backBottom));
                    memset(backTop, 0, sizeof(backTop));
                }
            }

            for (int i = 0; i < 12; i++) {
                if (pressed & (1 << i)) {
                    uint8_t packet = i;
                    if (send_all(sock, &packet, 1) < 0) { running = false; break; }
                }
                if (released & (1 << i)) {
                    uint8_t packet = i | 0x80;
                    if (send_all(sock, &packet, 1) < 0) { running = false; break; }
                }
            }
            if (!running) break;

            bool now_touch = (held & KEY_TOUCH) ? true : false;
            if (now_touch) {
                touchPosition touch;
                touchRead(&touch);
                if (!touch_active || touch.px != last_touch.px || touch.py != last_touch.py) {
                    if (touch_count < 10) {
                        touch_buf[touch_count++] = touch;
                    }
                    last_touch = touch;
                }
                touch_active = true;
            } else {
                if (touch_active) {
                    uint8_t packet[3] = {0xFC, 0x00, 0xFD};
                    if (send_all(sock, packet, 3) < 0) running = false;
                    touch_active = false;
                    touch_count = 0;
                }
            }

            if ((frameCounter & 1) == 0 && touch_count > 0) {
                uint8_t packet[64];
                int idx = 0;
                packet[idx++] = 0xFC;
                packet[idx++] = touch_count;
                for (int i = 0; i < touch_count; i++) {
                    packet[idx++] = (touch_buf[i].px >> 8) & 0xFF;
                    packet[idx++] = touch_buf[i].px & 0xFF;
                    packet[idx++] = (touch_buf[i].py >> 8) & 0xFF;
                    packet[idx++] = touch_buf[i].py & 0xFF;
                }
                packet[idx++] = 0xFD;
                if (send_all(sock, packet, idx) < 0) running = false;
                touch_count = 0;
            }

            if (pressed & KEY_SELECT) {
                running = false;
            }
        }

        running = false;
        closesocket(sock);

        if (!quit) {
            is_streaming = false;
            videoSetMode(MODE_0_2D); 
            vramSetBankA(VRAM_A_MAIN_BG);
            consoleInit(NULL, 0, BgType_Text4bpp, BgSize_T_256x256, 31, 0, true, true);
            
            iprintf("Disconnected. Reconnecting... (B=quit)\n");
            for (int i = 0; i < 60 && !quit; i++) {
                swiWaitForVBlank(); scanKeys();
                if (keysDown() & KEY_B) quit = true;
            }
        }
    }
}

void menuMode(void) {
    is_streaming = false;
    videoSetMode(MODE_0_2D); vramSetBankA(VRAM_A_MAIN_BG);
    videoSetModeSub(MODE_5_2D); vramSetBankC(VRAM_C_SUB_BG);
    bgBottom = bgInitSub(3, BgType_Bmp16, BgSize_B16_256x256, 2, 0); 
    REG_DISPCNT_SUB = MODE_5_2D | DISPLAY_BG3_ACTIVE;
    consoleInit(NULL, 0, BgType_Text4bpp, BgSize_T_256x256, 31, 0, true, true);
    memset(backBottom, 0, sizeof(backBottom)); swapScreens();
}

int main(void) {
    for (int i = 0; i < 256; i++) {
        u8 r = (i>>5)&7, g = (i>>2)&7, b = i&3;
        palette[i] = RGB15((r*31)/7, (g*31)/7, (b*31)/3) | BIT(15);
    }
    menuMode();

    while (1) {
        iprintf("\x1b[2JDS Monitor\n\n");
        iprintf("Target: %s:%d\n\n", serverIP, serverPort);
        iprintf("A: Start Monitor Mode\n");
        iprintf("Y: Config IP and Port\n");

        while (1) {
            swiWaitForVBlank(); scanKeys();
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