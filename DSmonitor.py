#!/usr/bin/env python3
import sys
import os
import socket
import struct
import threading
import time
import subprocess
import urllib.request
import zipfile
import shutil
import ctypes
import platform
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageDraw
import mss

# ── 1. OS & Server Detection ──
OS_TYPE = platform.system()
DISPLAY_SERVER = "Unknown"

if OS_TYPE == "Windows":
    import winreg

if OS_TYPE == "Linux":
    if os.environ.get('WAYLAND_DISPLAY') or os.environ.get('XDG_SESSION_TYPE') == 'wayland':
        DISPLAY_SERVER = "Wayland"
    else:
        DISPLAY_SERVER = "X11"

# ── Defaults ──────────────────────────────────────────────────
DEFAULT_PORT = 8888
DEFAULT_BUDGET = 1350
DEFAULT_LUMINOSITY = 1.0

current_luminosity = DEFAULT_LUMINOSITY
current_budget = DEFAULT_BUDGET

server_running = False
server_socket = None
udp_socket = None
server_socket_lock = threading.Lock()
current_port = DEFAULT_PORT
accept_thread = None
current_client = None
current_client_lock = threading.Lock()

stream_mode = "1 Screen (256x192) - DS Up/Down to swap"
virtual_monitor_active = False
created_virtual_monitors = False
swapping_monitors = False
selected_indices = []
created_monitor_indices = []
app_gui = None

# ── 2. Compiler Download Management (Windows Only) ────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMPILER_URL = "https://github.com/skeeto/w64devkit/releases/download/v1.20.0/w64devkit-1.20.0.zip"
COMPILER_DIR = os.path.join(SCRIPT_DIR, "w64devkit")

def find_local_gcc():
    candidates = [
        os.path.join(COMPILER_DIR, "bin", "gcc.exe"),
        os.path.join(COMPILER_DIR, "w64devkit", "bin", "gcc.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    if os.path.exists(COMPILER_DIR):
        for root_dir, _, files in os.walk(COMPILER_DIR):
            if "gcc.exe" in files:
                return os.path.join(root_dir, "gcc.exe")
    return None

def prepare_compiler():
    if OS_TYPE != "Windows":
        return "gcc"

    try:
        subprocess.run(["gcc", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return "gcc"
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    existing_gcc = find_local_gcc()
    if existing_gcc:
        return existing_gcc

    temp_root = tk.Tk()
    temp_root.withdraw()
    ans = messagebox.askyesno(
        "Compiler Required",
        "To ensure zero-lag streaming performance, this script requires a portable C-Compiler (~80MB).\n\nDownload it now?"
    )
    temp_root.destroy()
    if not ans:
        sys.exit(0)

    zip_path = os.path.join(SCRIPT_DIR, "compiler.zip")
    try:
        os.makedirs(COMPILER_DIR, exist_ok=True)
        print("Downloading portable GCC compiler...")
        req = urllib.request.Request(COMPILER_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
            out_file.write(response.read())

        print("Extracting compiler...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(COMPILER_DIR)

        found_gcc = find_local_gcc()
        if not found_gcc:
            raise FileNotFoundError("Extracted archive did not contain gcc.exe")
        return found_gcc
    except Exception as e:
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror("Download Error", f"Failed to download/extract compiler: {e}")
        temp_root.destroy()
        sys.exit(1)
    finally:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass

# ── 3. Low-Bandwidth UDP Quadtree C-Encoder (pure ASCII source) ──
C_CODE = """
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MAX_STREAM_SIZE 65536
#define MAX_NODES 24000

#define S_SPLIT     0x00
#define S_SOLID     0x01
#define S_GRAD      0x02
#define S_RLE       0x03
#define S_RAW       0x04
#define S_BTC       0x05
#define S_PAL4      0x06
#define S_UNCHANGED 0xFF

#define M_UNCHANGED 0
#define M_SOLID     1
#define M_GRAD      2
#define M_BTC       3
#define M_PAL4      4
#define M_RLE       5
#define M_RAW       6
#define M_COUNT     7

static const int bayer[4][4] = {
    { 0,  8,  2, 10},
    {12,  4, 14,  6},
    { 3, 11,  1,  9},
    {15,  7, 13,  5}
};

static uint16_t palette_rgb15[256];
static uint8_t  rgb555_to_pal[32768];
static int      tables_initialized = 0;

static inline uint16_t pack_rgb15(int r5, int g5, int b5) {
    return (uint16_t)((r5 & 31) | ((g5 & 31) << 5) | ((b5 & 31) << 10) | 0x8000);
}

static inline uint16_t rgb24_to_rgb15(uint8_t r, uint8_t g, uint8_t b) {
    int r5 = (r + 4) >> 3; if (r5 > 31) r5 = 31;
    int g5 = (g + 4) >> 3; if (g5 > 31) g5 = 31;
    int b5 = (b + 4) >> 3; if (b5 > 31) b5 = 31;
    return pack_rgb15(r5, g5, b5);
}

static inline int color_err_rgb15(uint16_t c1, uint16_t c2) {
    int dr = (int)(c1 & 31) - (int)(c2 & 31);
    int dg = (int)((c1 >> 5) & 31) - (int)((c2 >> 5) & 31);
    int db = (int)((c1 >> 10) & 31) - (int)((c2 >> 10) & 31);
    return dr * dr * 3 + dg * dg * 4 + db * db * 2;
}

static inline int ilog2_pow2(int sz) {
    return (sz == 4) ? 2 : (sz == 8) ? 3 : (sz == 16) ? 4 : (sz == 32) ? 5 : 6;
}

static void init_tables(void) {
    if (tables_initialized) return;
    int idx = 0;
    for (int r = 0; r < 6; r++) {
        uint8_t r5 = (r * 31 + 2) / 5;
        for (int g = 0; g < 6; g++) {
            uint8_t g5 = (g * 31 + 2) / 5;
            for (int b = 0; b < 6; b++) {
                uint8_t b5 = (b * 31 + 2) / 5;
                palette_rgb15[idx++] = pack_rgb15(r5, g5, b5);
            }
        }
    }
    for (int k = 0; k < 32; k++) {
        palette_rgb15[idx++] = pack_rgb15(k, k, k);
    }
    static const uint8_t extra[8][3] = {
        {28, 22, 18}, {24, 17, 13}, {19, 13, 9}, {14, 9, 6},
        {3, 5, 8},    {5, 9, 14},   {8, 14, 20}, {16, 22, 28}
    };
    for (int i = 0; i < 8; i++) {
        palette_rgb15[idx++] = pack_rgb15(extra[i][0], extra[i][1], extra[i][2]);
    }

    for (int c = 0; c < 32768; c++) {
        uint16_t target = (uint16_t)(c | 0x8000);
        int best_err = 0x7FFFFFFF;
        uint8_t best_idx = 0;
        for (int p = 0; p < 256; p++) {
            int err = color_err_rgb15(target, palette_rgb15[p]);
            if (err < best_err) {
                best_err = err;
                best_idx = (uint8_t)p;
                if (err == 0) break;
            }
        }
        rgb555_to_pal[c] = best_idx;
    }
    tables_initialized = 1;
}

static inline uint8_t rgb15_to_index(uint16_t c) {
    return rgb555_to_pal[c & 0x7FFF];
}

typedef struct {
    int x, y, size;
    int children[4];
    int force_split;
    int rate[M_COUNT];
    int64_t dist[M_COUNT];

    uint16_t solid_c;
    uint16_t grad_c0, grad_c1;
    uint8_t  grad_meta;
    uint16_t btc_c0, btc_c1;
    uint8_t  btc_thresh;
    uint8_t  pal4[4];

    int chosen_mode;
    int total_rate;
    int64_t total_dist;
} Node;

static Node tree[MAX_NODES];
static int node_count = 0;
static uint8_t global_stream[MAX_STREAM_SIZE];
static uint32_t stream_pos = 0;
static int safety_abort = 0;
static uint32_t g_frame_seq = 0;

static int eval_rle(const uint8_t* idx, int size, uint8_t* out) {
    int pos = 0;
    for (int row = 0; row < size; row++) {
        const uint8_t* rp = idx + row * size;
        uint8_t prev = rp[0];
        int run = 1;
        for (int col = 1; col < size; col++) {
            if (rp[col] == prev && run < 255) {
                run++;
            } else {
                if (pos + 2 > 254) return 0;
                if (out) { out[pos] = (uint8_t)run; out[pos + 1] = prev; }
                pos += 2;
                prev = rp[col];
                run = 1;
            }
        }
        if (pos + 2 > 254) return 0;
        if (out) { out[pos] = (uint8_t)run; out[pos + 1] = prev; }
        pos += 2;
    }
    return pos;
}

static void eval_grad(const uint16_t* src15, int size, Node* n) {
    int half = size >> 1;
    int q_r[4] = {0}, q_g[4] = {0}, q_b[4] = {0}, q_cnt = half * half;
    if (q_cnt < 1) return;

    for (int y = 0; y < size; y++) {
        int qy = (y >= half) ? 2 : 0;
        for (int x = 0; x < size; x++) {
            int q = qy + ((x >= half) ? 1 : 0);
            uint16_t c = src15[y * size + x];
            q_r[q] += c & 31;
            q_g[q] += (c >> 5) & 31;
            q_b[q] += (c >> 10) & 31;
        }
    }
    for (int q = 0; q < 4; q++) {
        q_r[q] /= q_cnt; q_g[q] /= q_cnt; q_b[q] /= q_cnt;
    }

    uint16_t cand_c0[4] = {
        pack_rgb15((q_r[0] + q_r[2]) >> 1, (q_g[0] + q_g[2]) >> 1, (q_b[0] + q_b[2]) >> 1),
        pack_rgb15((q_r[0] + q_r[1]) >> 1, (q_g[0] + q_g[1]) >> 1, (q_b[0] + q_b[1]) >> 1),
        pack_rgb15(q_r[0], q_g[0], q_b[0]),
        pack_rgb15(q_r[1], q_g[1], q_b[1])
    };
    uint16_t cand_c1[4] = {
        pack_rgb15((q_r[1] + q_r[3]) >> 1, (q_g[1] + q_g[3]) >> 1, (q_b[1] + q_b[3]) >> 1),
        pack_rgb15((q_r[2] + q_r[3]) >> 1, (q_g[2] + q_g[3]) >> 1, (q_b[2] + q_b[3]) >> 1),
        pack_rgb15(q_r[3], q_g[3], q_b[3]),
        pack_rgb15(q_r[2], q_g[2], q_b[2])
    };

    int base_shift = ilog2_pow2(size);
    int64_t best_d = 0x7FFFFFFFFFFFFFFFLL;
    int best_dir = 0;
    int best_shift = base_shift;

    for (int dir = 0; dir < 4; dir++) {
        if (cand_c0[dir] == cand_c1[dir]) continue;
        int r0 = cand_c0[dir] & 31, g0 = (cand_c0[dir] >> 5) & 31, b0 = (cand_c0[dir] >> 10) & 31;
        int dr = (int)(cand_c1[dir] & 31) - r0;
        int dg = (int)((cand_c1[dir] >> 5) & 31) - g0;
        int db = (int)((cand_c1[dir] >> 10) & 31) - b0;
        int dshift = (dir >= 2) ? (base_shift + 1) : base_shift;

        int64_t d = 0;
        for (int cy = 0; cy < size; cy++) {
            for (int cx = 0; cx < size; cx++) {
                int t = (dir == 0) ? cx : (dir == 1) ? cy : (dir == 2) ? (cx + cy) : (cx + (size - 1 - cy));
                int r = r0 + ((dr * t) >> dshift);
                int g = g0 + ((dg * t) >> dshift);
                int b = b0 + ((db * t) >> dshift);
                d += color_err_rgb15(src15[cy * size + cx], pack_rgb15(r, g, b));
            }
        }
        if (d < best_d) {
            best_d = d;
            best_dir = dir;
            best_shift = dshift;
        }
    }

    if (best_d < 0x7FFFFFFFFFFFFFFFLL) {
        n->rate[M_GRAD] = 6;
        n->dist[M_GRAD] = best_d;
        n->grad_c0 = cand_c0[best_dir];
        n->grad_c1 = cand_c1[best_dir];
        n->grad_meta = (uint8_t)((best_dir & 3) | ((best_shift & 7) << 2));
    }
}

static int build_tree(int x, int y, int size, const uint8_t* raw, const uint16_t* server_state, int frame_w, int frame_h) {
    if (x >= frame_w || y >= frame_h) return -1;
    if (node_count >= MAX_NODES - 5) {
        safety_abort = 1;
        return -1;
    }

    int n_idx = node_count++;
    Node* n = &tree[n_idx];
    n->x = x; n->y = y; n->size = size;
    n->children[0] = n->children[1] = n->children[2] = n->children[3] = -1;
    n->force_split = (x + size > frame_w || y + size > frame_h) ? 1 : 0;

    for (int m = 0; m < M_COUNT; m++) {
        n->rate[m] = 999999;
        n->dist[m] = 0x3FFFFFFFFFFFFFFFLL;
    }

    if (n->force_split) {
        int half = size >> 1;
        n->children[0] = build_tree(x, y, half, raw, server_state, frame_w, frame_h);
        n->children[1] = build_tree(x + half, y, half, raw, server_state, frame_w, frame_h);
        n->children[2] = build_tree(x, y + half, half, raw, server_state, frame_w, frame_h);
        n->children[3] = build_tree(x + half, y + half, half, raw, server_state, frame_w, frame_h);
        return n_idx;
    }

    int count = size * size;
    uint32_t sum_r5 = 0, sum_g5 = 0, sum_b5 = 0;
    int64_t unchanged_dist = 0;
    int max_px_err = 0;
    int state_valid = 1;

    for (int row = 0; row < size; row++) {
        int pix_idx = (y + row) * frame_w + x;
        const uint8_t* rp = raw + pix_idx * 3;
        const uint16_t* sp = server_state + pix_idx;
        for (int col = 0; col < size; col++) {
            int r5 = rp[col * 3] >> 3;
            int g5 = rp[col * 3 + 1] >> 3;
            int b5 = rp[col * 3 + 2] >> 3;
            sum_r5 += r5; sum_g5 += g5; sum_b5 += b5;
            uint16_t sc = sp[col];
            if (!(sc & 0x8000)) {
                state_valid = 0;
            } else {
                uint16_t c15 = pack_rgb15(r5, g5, b5);
                int pe = color_err_rgb15(c15, sc);
                if (pe > 6) unchanged_dist += pe;
                if (pe > max_px_err) max_px_err = pe;
            }
        }
    }

    int in_refresh_band = ((((x >> 5) + (y >> 5) * 3 + g_frame_seq) & 31) == 0);

    if (state_valid && unchanged_dist == 0 && max_px_err <= 9 && !in_refresh_band) {
        n->rate[M_UNCHANGED] = 1;
        n->dist[M_UNCHANGED] = 0;
        return n_idx;
    }

    uint16_t avg_c = pack_rgb15((sum_r5 + (count >> 1)) / count,
                                (sum_g5 + (count >> 1)) / count,
                                (sum_b5 + (count >> 1)) / count);
    n->solid_c = avg_c;
    n->rate[M_SOLID] = 3;

    int64_t solid_dist = 0;
    for (int row = 0; row < size; row++) {
        const uint8_t* rp = raw + ((y + row) * frame_w + x) * 3;
        for (int col = 0; col < size; col++) {
            uint16_t c15 = rgb24_to_rgb15(rp[col * 3], rp[col * 3 + 1], rp[col * 3 + 2]);
            solid_dist += color_err_rgb15(c15, avg_c);
        }
    }
    n->dist[M_SOLID] = solid_dist;

    if (state_valid && !in_refresh_band && unchanged_dist <= solid_dist) {
        n->rate[M_UNCHANGED] = 1;
        n->dist[M_UNCHANGED] = unchanged_dist;
    }

    if (size <= 32) {
        uint16_t src15[1024];
        uint8_t  indices[1024];
        uint8_t  Y[1024];
        int idx = 0;
        int min_y = 255, max_y = 0;
        int64_t pal_dist = 0;

        for (int row = 0; row < size; row++) {
            const uint8_t* rp = raw + ((y + row) * frame_w + x) * 3;
            for (int col = 0; col < size; col++) {
                uint8_t r = rp[col * 3], g = rp[col * 3 + 1], b = rp[col * 3 + 2];
                uint16_t c15 = rgb24_to_rgb15(r, g, b);
                src15[idx] = c15;
                uint8_t p_idx = rgb15_to_index(c15);
                indices[idx] = p_idx;
                pal_dist += color_err_rgb15(c15, palette_rgb15[p_idx]);

                int yv = (r * 77 + g * 150 + b * 29) >> 8;
                Y[idx] = (uint8_t)yv;
                if (yv < min_y) min_y = yv;
                if (yv > max_y) max_y = yv;
                idx++;
            }
        }

        if (size >= 8) {
            eval_grad(src15, size, n);
        }

        int rle_sz = eval_rle(indices, size, NULL);
        if (rle_sz > 0) {
            n->rate[M_RLE] = 2 + rle_sz;
            n->dist[M_RLE] = pal_dist;
        }

        if (size <= 8) {
            n->rate[M_RAW] = 1 + count;
            n->dist[M_RAW] = pal_dist;
        }

        if (max_y - min_y >= 4) {
            int sum_y = 0;
            for (int i = 0; i < count; i++) sum_y += Y[i];
            int mean_y = sum_y / count;

            int thresh_cand[2] = { (min_y + max_y) >> 1, mean_y };
            int64_t best_btc_d = 0x3FFFFFFFFFFFFFFFLL;
            uint16_t best_c0 = 0, best_c1 = 0;
            int best_thresh = thresh_cand[0];

            for (int t = 0; t < 2; t++) {
                int thresh = thresh_cand[t];
                if (t == 1 && thresh == thresh_cand[0]) continue;

                int r0 = 0, g0 = 0, b0 = 0, n0 = 0;
                int r1 = 0, g1 = 0, b1 = 0, n1 = 0;
                for (int i = 0; i < count; i++) {
                    uint16_t c = src15[i];
                    if (Y[i] >= thresh) {
                        r1 += c & 31; g1 += (c >> 5) & 31; b1 += (c >> 10) & 31; n1++;
                    } else {
                        r0 += c & 31; g0 += (c >> 5) & 31; b0 += (c >> 10) & 31; n0++;
                    }
                }
                if (n0 > 0 && n1 > 0) {
                    uint16_t c0 = pack_rgb15((r0 + (n0 >> 1)) / n0, (g0 + (n0 >> 1)) / n0, (b0 + (n0 >> 1)) / n0);
                    uint16_t c1 = pack_rgb15((r1 + (n1 >> 1)) / n1, (g1 + (n1 >> 1)) / n1, (b1 + (n1 >> 1)) / n1);
                    int64_t btc_d = 0;
                    for (int i = 0; i < count; i++) {
                        btc_d += color_err_rgb15(src15[i], (Y[i] >= thresh) ? c1 : c0);
                    }
                    if (btc_d < best_btc_d) {
                        best_btc_d = btc_d;
                        best_c0 = c0;
                        best_c1 = c1;
                        best_thresh = thresh;
                    }
                }
            }
            if (best_btc_d < 0x3FFFFFFFFFFFFFFFLL) {
                n->btc_c0 = best_c0;
                n->btc_c1 = best_c1;
                n->btc_thresh = (uint8_t)best_thresh;
                n->rate[M_BTC] = 5 + ((count + 7) >> 3);
                n->dist[M_BTC] = best_btc_d;
            }
        }

        if (size <= 16) {
            int freq[256] = {0};
            for (int i = 0; i < count; i++) freq[indices[i]]++;
            uint8_t pal[4] = {0, 0, 0, 0};
            int p_cnt = 0;
            for (int it = 0; it < 4; it++) {
                int best_score = -1, best_c = 0;
                for (int c = 0; c < 256; c++) {
                    if (freq[c] <= 0) continue;
                    int score = freq[c];
                    if (p_cnt > 0) {
                        int min_d = 999999;
                        for (int k = 0; k < p_cnt; k++) {
                            int d = color_err_rgb15(palette_rgb15[c], palette_rgb15[pal[k]]);
                            if (d < min_d) min_d = d;
                        }
                        if (min_d < 36) score >>= 3;
                    }
                    if (score > best_score) { best_score = score; best_c = c; }
                }
                if (best_score <= 0) break;
                pal[p_cnt++] = (uint8_t)best_c;
                freq[best_c] = 0;
            }
            if (p_cnt > 0) {
                while (p_cnt < 4) pal[p_cnt++] = pal[0];

                for (int iter = 0; iter < 2; iter++) {
                    int32_t r_sum[4] = {0}, g_sum[4] = {0}, b_sum[4] = {0};
                    int cnt_assigned[4] = {0};
                    for (int i = 0; i < count; i++) {
                        uint16_t c = src15[i];
                        int best_k = 0, best_e = 0x7FFFFFFF;
                        for (int k = 0; k < 4; k++) {
                            int e = color_err_rgb15(c, palette_rgb15[pal[k]]);
                            if (e < best_e) { best_e = e; best_k = k; }
                        }
                        r_sum[best_k] += c & 31;
                        g_sum[best_k] += (c >> 5) & 31;
                        b_sum[best_k] += (c >> 10) & 31;
                        cnt_assigned[best_k]++;
                    }
                    for (int k = 0; k < 4; k++) {
                        if (cnt_assigned[k] == 0) continue;
                        int r5 = (r_sum[k] + (cnt_assigned[k] >> 1)) / cnt_assigned[k];
                        int g5 = (g_sum[k] + (cnt_assigned[k] >> 1)) / cnt_assigned[k];
                        int b5 = (b_sum[k] + (cnt_assigned[k] >> 1)) / cnt_assigned[k];
                        pal[k] = rgb15_to_index(pack_rgb15(r5, g5, b5));
                    }
                }

                int64_t p4_d = 0;
                for (int i = 0; i < count; i++) {
                    int best_e = 0x7FFFFFFF;
                    for (int k = 0; k < 4; k++) {
                        int e = color_err_rgb15(src15[i], palette_rgb15[pal[k]]);
                        if (e < best_e) best_e = e;
                    }
                    p4_d += best_e;
                }
                memcpy(n->pal4, pal, 4);
                n->rate[M_PAL4] = 5 + ((count + 3) >> 2);
                n->dist[M_PAL4] = p4_d;
            }
        }
    }

    if (size > 4 && solid_dist > count * 2) {
        int half = size >> 1;
        n->children[0] = build_tree(x, y, half, raw, server_state, frame_w, frame_h);
        n->children[1] = build_tree(x + half, y, half, raw, server_state, frame_w, frame_h);
        n->children[2] = build_tree(x, y + half, half, raw, server_state, frame_w, frame_h);
        n->children[3] = build_tree(x + half, y + half, half, raw, server_state, frame_w, frame_h);
    }

    return n_idx;
}

static void optimize_rdo(int n_idx, int64_t lambda) {
    if (n_idx < 0) return;
    Node* n = &tree[n_idx];

    int has_children = 0;
    int split_rate = 1;
    int64_t split_dist = 0;

    for (int i = 0; i < 4; i++) {
        if (n->children[i] >= 0) {
            has_children = 1;
            optimize_rdo(n->children[i], lambda);
            split_rate += tree[n->children[i]].total_rate;
            split_dist += tree[n->children[i]].total_dist;
        }
    }

    if (n->force_split) {
        n->chosen_mode = -1;
        n->total_rate = split_rate;
        n->total_dist = split_dist;
        return;
    }

    int best_m = M_SOLID;
    int64_t best_j = 0x7FFFFFFFFFFFFFFFLL;

    for (int m = 0; m < M_COUNT; m++) {
        if (n->rate[m] >= 999999) continue;
        int64_t j = (n->dist[m] << 8) + lambda * n->rate[m];
        if (j < best_j) {
            best_j = j;
            best_m = m;
        }
    }

    if (has_children) {
        int64_t split_j = (split_dist << 8) + lambda * split_rate;
        if (split_j < best_j) {
            n->chosen_mode = -1;
            n->total_rate = split_rate;
            n->total_dist = split_dist;
            return;
        }
    }

    n->chosen_mode = best_m;
    n->total_rate = n->rate[best_m];
    n->total_dist = n->dist[best_m];
}

static void emit_tree(int n_idx, const uint8_t* raw, uint16_t* server_state, int frame_w) {
    if (n_idx < 0 || safety_abort) return;
    if (stream_pos >= MAX_STREAM_SIZE - 2048) {
        safety_abort = 1;
        return;
    }

    Node* n = &tree[n_idx];
    int x = n->x, y = n->y, size = n->size;

    if (n->chosen_mode == -1) {
        global_stream[stream_pos++] = S_SPLIT;
        for (int i = 0; i < 4; i++) {
            emit_tree(n->children[i], raw, server_state, frame_w);
        }
        return;
    }

    if (n->chosen_mode == M_UNCHANGED) {
        global_stream[stream_pos++] = S_UNCHANGED;
        return;
    }

    if (n->chosen_mode == M_SOLID) {
        uint16_t c = n->solid_c;
        global_stream[stream_pos++] = S_SOLID;
        global_stream[stream_pos++] = (uint8_t)(c & 0xFF);
        global_stream[stream_pos++] = (uint8_t)(c >> 8);
        for (int cy = 0; cy < size; cy++) {
            uint16_t* sp = server_state + (y + cy) * frame_w + x;
            for (int cx = 0; cx < size; cx++) sp[cx] = c;
        }
        return;
    }

    if (n->chosen_mode == M_GRAD) {
        uint16_t c0 = n->grad_c0, c1 = n->grad_c1;
        uint8_t meta = n->grad_meta;
        int dir = meta & 3;
        int dshift = (meta >> 2) & 7;

        global_stream[stream_pos++] = S_GRAD;
        global_stream[stream_pos++] = (uint8_t)(c0 & 0xFF);
        global_stream[stream_pos++] = (uint8_t)(c0 >> 8);
        global_stream[stream_pos++] = (uint8_t)(c1 & 0xFF);
        global_stream[stream_pos++] = (uint8_t)(c1 >> 8);
        global_stream[stream_pos++] = meta;

        int r0 = c0 & 31, g0 = (c0 >> 5) & 31, b0 = (c0 >> 10) & 31;
        int dr = (int)(c1 & 31) - r0;
        int dg = (int)((c1 >> 5) & 31) - g0;
        int db = (int)((c1 >> 10) & 31) - b0;

        for (int cy = 0; cy < size; cy++) {
            uint16_t* sp = server_state + (y + cy) * frame_w + x;
            for (int cx = 0; cx < size; cx++) {
                int t = (dir == 0) ? cx : (dir == 1) ? cy : (dir == 2) ? (cx + cy) : (cx + (size - 1 - cy));
                int r = r0 + ((dr * t) >> dshift);
                int g = g0 + ((dg * t) >> dshift);
                int b = b0 + ((db * t) >> dshift);
                sp[cx] = pack_rgb15(r, g, b);
            }
        }
        return;
    }

    int count = size * size;
    uint16_t src15[1024];
    uint8_t  indices[1024];
    uint8_t  Y[1024];
    int idx = 0;

    for (int row = 0; row < size; row++) {
        const uint8_t* rp = raw + ((y + row) * frame_w + x) * 3;
        for (int col = 0; col < size; col++) {
            uint8_t r = rp[col * 3], g = rp[col * 3 + 1], b = rp[col * 3 + 2];
            uint16_t c15 = rgb24_to_rgb15(r, g, b);
            src15[idx] = c15;
            indices[idx] = rgb15_to_index(c15);
            int yv = (r * 77 + g * 150 + b * 29) >> 8;
            Y[idx] = (uint8_t)yv;
            idx++;
        }
    }

    if (n->chosen_mode == M_RLE) {
        uint8_t buf[512];
        int sz = eval_rle(indices, size, buf);
        global_stream[stream_pos++] = S_RLE;
        global_stream[stream_pos++] = (uint8_t)sz;
        memcpy(&global_stream[stream_pos], buf, sz);
        stream_pos += sz;
        for (int cy = 0; cy < size; cy++) {
            uint16_t* sp = server_state + (y + cy) * frame_w + x;
            for (int cx = 0; cx < size; cx++) {
                sp[cx] = palette_rgb15[indices[cy * size + cx]];
            }
        }
    }
    else if (n->chosen_mode == M_BTC) {
        uint16_t c0 = n->btc_c0, c1 = n->btc_c1;
        int thresh = n->btc_thresh;
        int mask_bytes = (count + 7) >> 3;
        global_stream[stream_pos++] = S_BTC;
        global_stream[stream_pos++] = (uint8_t)(c0 & 0xFF);
        global_stream[stream_pos++] = (uint8_t)(c0 >> 8);
        global_stream[stream_pos++] = (uint8_t)(c1 & 0xFF);
        global_stream[stream_pos++] = (uint8_t)(c1 >> 8);
        uint8_t* mask = &global_stream[stream_pos];
        memset(mask, 0, mask_bytes);
        stream_pos += mask_bytes;

        for (int cy = 0; cy < size; cy++) {
            uint16_t* sp = server_state + (y + cy) * frame_w + x;
            for (int cx = 0; cx < size; cx++) {
                int i = cy * size + cx;
                if (Y[i] >= thresh) {
                    mask[i >> 3] |= (uint8_t)(1 << (i & 7));
                    sp[cx] = c1;
                } else {
                    sp[cx] = c0;
                }
            }
        }
    }
    else if (n->chosen_mode == M_PAL4) {
        global_stream[stream_pos++] = S_PAL4;
        memcpy(&global_stream[stream_pos], n->pal4, 4);
        stream_pos += 4;
        for (int i = 0; i < count; i += 4) {
            uint8_t b = 0;
            for (int j = 0; j < 4 && (i + j) < count; j++) {
                int best = 0, best_e = 0x7FFFFFFF;
                for (int k = 0; k < 4; k++) {
                    int e = color_err_rgb15(src15[i + j], palette_rgb15[n->pal4[k]]);
                    if (e < best_e) { best_e = e; best = k; }
                }
                b |= (uint8_t)(best << (j * 2));
                int py = (i + j) / size, px = (i + j) % size;
                server_state[(y + py) * frame_w + (x + px)] = palette_rgb15[n->pal4[best]];
            }
            global_stream[stream_pos++] = b;
        }
    }
    else if (n->chosen_mode == M_RAW) {
        global_stream[stream_pos++] = S_RAW;
        for (int cy = 0; cy < size; cy++) {
            const uint8_t* rp = raw + ((y + cy) * frame_w + x) * 3;
            uint16_t* sp = server_state + (y + cy) * frame_w + x;
            for (int cx = 0; cx < size; cx++) {
                int d = bayer[(y + cy) & 3][(x + cx) & 3] - 8;
                int r = (int)rp[cx * 3]     + (d >> 1);
                int g = (int)rp[cx * 3 + 1] + (d >> 1);
                int b = (int)rp[cx * 3 + 2] + (d >> 1);
                if (r < 0) r = 0; else if (r > 255) r = 255;
                if (g < 0) g = 0; else if (g > 255) g = 255;
                if (b < 0) b = 0; else if (b > 255) b = 255;
                uint16_t c15 = rgb24_to_rgb15((uint8_t)r, (uint8_t)g, (uint8_t)b);
                uint8_t p_idx = rgb15_to_index(c15);
                global_stream[stream_pos++] = p_idx;
                sp[cx] = palette_rgb15[p_idx];
            }
        }
    }
}

#ifdef _WIN32
__declspec(dllexport)
#endif
int encode_quadtree_fast(const uint8_t* raw, uint16_t* server_state, int frame_w, int frame_h, uint8_t* out_stream, int budget) {
    init_tables();
    node_count = 0;
    stream_pos = 0;
    safety_abort = 0;
    g_frame_seq++;

    if (budget < 300) budget = 300;
    if (budget > 1430) budget = 1430;
    const int BUDGET = budget;

    int root_size = (frame_h > 256) ? 512 : 256;
    int root = build_tree(0, 0, root_size, raw, server_state, frame_w, frame_h);
    if (safety_abort || root < 0) return 0;

    int64_t lo = 64 + (int64_t)(1430 - BUDGET) * 32;
    optimize_rdo(root, lo);

    if (tree[root].total_rate > BUDGET) {
        int64_t hi = 1 << 24;
        for (int iter = 0; iter < 12; iter++) {
            int64_t mid = (lo + hi) >> 1;
            optimize_rdo(root, mid);
            if (tree[root].total_rate > BUDGET) {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        optimize_rdo(root, hi);
    }

    emit_tree(root, raw, server_state, frame_w);
    if (safety_abort) return 0;

    memcpy(out_stream, global_stream, stream_pos);
    return (int)stream_pos;
}
"""

def setup_fast_encoder():
    gcc_cmd = prepare_compiler()
    dll_name = "encoder.dll" if OS_TYPE == "Windows" else "encoder.dylib" if OS_TYPE == "Darwin" else "encoder.so"

    dll_path = os.path.abspath(os.path.join(SCRIPT_DIR, dll_name))
    c_path = os.path.abspath(os.path.join(SCRIPT_DIR, "encoder_temp.c"))
    gcc_abs = os.path.abspath(gcc_cmd) if OS_TYPE == "Windows" and gcc_cmd != "gcc" else gcc_cmd

    env = os.environ.copy()
    if OS_TYPE == "Windows":
        if gcc_abs != "gcc":
            compiler_bin_dir = os.path.dirname(gcc_abs)
            env["PATH"] = compiler_bin_dir + os.pathsep + env.get("PATH", "")
            compile_cmd = [
                gcc_abs, f"-B{compiler_bin_dir}{os.sep}", "-shared", "-O3", "-o", dll_path, c_path, "-static", "-static-libgcc"
            ]
        else:
            compile_cmd = [gcc_abs, "-shared", "-O3", "-o", dll_path, c_path, "-static", "-static-libgcc"]
    elif OS_TYPE == "Darwin":
        compile_cmd = ["clang", "-dynamiclib", "-O3", "-o", dll_path, c_path]
    else:
        compile_cmd = ["gcc", "-shared", "-O3", "-fPIC", "-o", dll_path, c_path]

    if os.path.exists(dll_path):
        try:
            os.remove(dll_path)
        except OSError:
            pass

    print(f"Auto-compiling high-speed C-Extension for {OS_TYPE}...")
    with open(c_path, "w", encoding="utf-8") as f:
        f.write(C_CODE)
    try:
        subprocess.run(compile_cmd, capture_output=True, text=True, check=True, env=env)
    except subprocess.CalledProcessError as e:
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror("GCC Compiler Error", f"Failed to compile C-Extension!\n\nOutput:\n{e.stderr}")
        temp_root.destroy()
        sys.exit(1)
    except Exception as e:
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror("Compiler Execution Error", f"Failed to run compiler:\n{e}")
        temp_root.destroy()
        sys.exit(1)
    finally:
        if os.path.exists(c_path):
            try:
                os.remove(c_path)
            except OSError:
                pass

    try:
        encoder = ctypes.CDLL(dll_path)
        encoder.encode_quadtree_fast.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
        ]
        encoder.encode_quadtree_fast.restype = ctypes.c_int
        return encoder
    except OSError as e:
        temp_root = tk.Tk()
        temp_root.withdraw()
        messagebox.showerror("DLL Load Error", f"Failed to load compiled DLL.\n{e}")
        temp_root.destroy()
        sys.exit(1)

fast_encoder = setup_fast_encoder()

def encode_quadtree_fast_wrapper(img, server_frame_state, out_stream_buf, budget=DEFAULT_BUDGET):
    orig_w, orig_h = img.size
    raw_bytes = img.tobytes()
    pixel_count = orig_w * orig_h

    if server_frame_state is None or len(server_frame_state) != pixel_count:
        server_frame_state = (ctypes.c_uint16 * pixel_count)()
        ctypes.memset(server_frame_state, 0, pixel_count * 2)

    stream_len = fast_encoder.encode_quadtree_fast(
        raw_bytes, server_frame_state, orig_w, orig_h, out_stream_buf, int(budget)
    )

    if stream_len <= 0:
        return b"", server_frame_state, orig_w, orig_h

    return bytes(memoryview(out_stream_buf)[:stream_len]), server_frame_state, orig_w, orig_h

def build_black_quadtree_payload(height):
    """
    Builds a thread-safe, valid quadtree bitstream for a solid black frame
    without touching the non-reentrant C encoder globals.
    - For 256x192: root is 256 (force_split=1 -> 4x 128 -> each 128 has top 2 children inside 192, bottom 2 outside).
    - For 256x384: root is 512 (force_split=1 -> 4x 256 -> left 2 children inside 256, right 2 outside).
    """
    solid_black = b"\x01\x00\x80"  # S_SOLID (0x01) + pack_rgb15(0,0,0) (0x8000 LE)
    split = b"\x00"                # S_SPLIT (0x00)

    # A 256x192 region inside a 256x256 quadtree node:
    # 256x256 splits into four 128x128 quadrants (0: TL, 1: TR, 2: BL, 3: BR).
    # Top two 128x128 quadrants (y=0..128) are fully inside 256x192 -> S_SOLID.
    # Bottom two 128x128 quadrants (y=128..256) exceed h=192 -> force_split into four 64x64 quadrants:
    #   Their top two 64x64 children (y=128..192) are inside -> S_SOLID.
    #   Their bottom two 64x64 children (y=192..256) are outside (y >= 192) -> omitted!
    sub_128_bottom = split + solid_black + solid_black
    node_256x192 = split + solid_black + solid_black + sub_128_bottom + sub_128_bottom

    if height <= 192:
        return node_256x192
    else:
        # For 256x384, root_size is 512:
        # Quadrant 0 (x=0, y=0, 256x256): fully inside 256x384 -> S_SOLID
        # Quadrant 1 (x=256, y=0, 256x256): x >= 256 -> omitted!
        # Quadrant 2 (x=0, y=256, 256x256): y=256..512 (128px inside 384) -> splits like top half of 256x192
        # Quadrant 3 (x=256, y=256, 256x256): x >= 256 -> omitted!
        q2_256 = split + solid_black + solid_black  # TL(128x128) and TR(128x128) at y=256..384 are inside; BL/BR at y>=384 omitted
        return split + solid_black + q2_256

def send_black_frame_for_mode(mode_str):
    """Sends a black frame matching the given mode_str to the connected DS client."""
    with current_client_lock:
        client = current_client
    with server_socket_lock:
        usock = udp_socket

    if not client or not usock or "udp_target" not in client:
        return

    target_ip, target_port = client["udp_target"]
    orig_w = 256
    orig_h = 384 if ("Giant" in mode_str or "2 Screens" in mode_str) else 192
    payload = build_black_quadtree_payload(orig_h)
    pkt = struct.pack('>BHHI', 0x03, orig_w, orig_h, len(payload)) + payload

    for _ in range(3):
        try:
            usock.sendto(pkt, (target_ip, target_port))
        except Exception:
            pass
        time.sleep(0.015)

# ── 3b. Luminosity (gamma) LUT cache ──────────────────────────
_LUM_LUT_CACHE = {}

def get_luminosity_lut(factor):
    key = round(float(factor), 2)
    if key in _LUM_LUT_CACHE:
        return _LUM_LUT_CACHE[key]
    single = [min(255, int(pow(i / 255.0, 1.0 / key) * 255.0 + 0.5)) for i in range(256)]
    lut = single * 3
    _LUM_LUT_CACHE[key] = lut
    if len(_LUM_LUT_CACHE) > 96:
        _LUM_LUT_CACHE.clear()
        _LUM_LUT_CACHE[key] = lut
    return lut

# ── 4. Multi-OS Hardware Mouse Cursor Overlay ─────────────────
if OS_TYPE == "Windows":
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint), ("hCursor", ctypes.c_void_p), ("ptScreenPos", POINT)]
    user32 = ctypes.windll.user32
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    try:
        H_IBEAM, H_HAND = user32.LoadCursorW(None, 32513), user32.LoadCursorW(None, 32649)
    except Exception:
        H_IBEAM = H_HAND = None

def draw_cursor(img, mon):
    if OS_TYPE == "Windows":
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & 1):
            mx, my = ci.ptScreenPos.x, ci.ptScreenPos.y
            if mon["left"] <= mx < mon["left"] + mon["width"] and mon["top"] <= my < mon["top"] + mon["height"]:
                rel_x, rel_y = mx - mon["left"], my - mon["top"]
                draw = ImageDraw.Draw(img)
                if ci.hCursor == H_IBEAM:
                    draw.line([(rel_x, rel_y - 7), (rel_x, rel_y + 7)], fill="black", width=2)
                    draw.line([(rel_x, rel_y - 6), (rel_x, rel_y + 6)], fill="white", width=1)
                elif ci.hCursor == H_HAND:
                    draw.polygon([(rel_x, rel_y), (rel_x + 5, rel_y + 12), (rel_x + 8, rel_y + 10), (rel_x + 12, rel_y + 14), (rel_x + 14, rel_y + 12), (rel_x + 10, rel_y + 8), (rel_x + 13, rel_y + 5)], fill="white", outline="black")
                else:
                    draw.polygon([(rel_x, rel_y), (rel_x + 12, rel_y + 12), (rel_x + 4, rel_y + 12), (rel_x, rel_y + 18)], fill="white", outline="black")
    return img

# ── 5. Cross-Platform Virtual Monitors ────────────────────────
def run_elevated_windows(bat_path, cwd):
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", bat_path, "", cwd, 0)
    return ret > 32

def prepare_driver_files():
    if OS_TYPE != "Windows":
        return True
    DRIVER_URL = "https://www.amyuni.com/downloads/usbmmidd_v2.zip"
    DRIVER_DIR = os.path.join(SCRIPT_DIR, "usbmmidd_v2")
    os.makedirs(DRIVER_DIR, exist_ok=True)
    exe_name = "deviceinstaller64.exe" if platform.machine().endswith('64') else "deviceinstaller.exe"
    exe_path = os.path.join(DRIVER_DIR, exe_name)

    if not os.path.exists(exe_path):
        temp_root = tk.Tk()
        temp_root.withdraw()
        ans = messagebox.askyesno("Driver Download", "The required Virtual Monitor files were not found locally.\n\nDo you want to download them now? (approx. 250KB)")
        temp_root.destroy()
        if not ans:
            return False
        zip_path = os.path.join(DRIVER_DIR, "usbmmidd.zip")
        try:
            req = urllib.request.Request(DRIVER_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
                out_file.write(response.read())
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(DRIVER_DIR)

            if not os.path.exists(exe_path):
                for item in os.listdir(DRIVER_DIR):
                    item_path = os.path.join(DRIVER_DIR, item)
                    if os.path.isdir(item_path):
                        for subitem in os.listdir(item_path):
                            shutil.move(os.path.join(item_path, subitem), os.path.join(DRIVER_DIR, subitem))
                        os.rmdir(item_path)
                        break
        except Exception as e:
            temp_root = tk.Tk()
            temp_root.withdraw()
            messagebox.showerror("Download Error", f"Failed to download files: {e}")
            temp_root.destroy()
            return False
        finally:
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
    return True

def get_active_windows_displays():
    if OS_TYPE != "Windows":
        return set()
    try:
        from ctypes import wintypes
        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]
        dev = DISPLAY_DEVICEW()
        dev.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        displays = set()
        idx = 0
        while user32.EnumDisplayDevicesW(None, idx, ctypes.byref(dev), 0):
            if dev.StateFlags & 1:
                displays.add(dev.DeviceName)
            idx += 1
        return displays
    except Exception:
        return set()

def force_windows_resolution(target_dev_name, target_w, target_h):
    if OS_TYPE != "Windows":
        return
    try:
        from ctypes import wintypes
        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", wintypes.WCHAR * 32),
                ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD),
                ("dmPositionX", wintypes.LONG),
                ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short),
                ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD),
                ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD),
                ("dmPanningHeight", wintypes.DWORD),
            ]
        for _ in range(5):
            current_mode = DEVMODEW()
            current_mode.dmSize = ctypes.sizeof(DEVMODEW)
            if user32.EnumDisplaySettingsW(target_dev_name, -1, ctypes.byref(current_mode)):
                if current_mode.dmPelsWidth == target_w and current_mode.dmPelsHeight == target_h:
                    break
                current_mode.dmPelsWidth = target_w
                current_mode.dmPelsHeight = target_h
                current_mode.dmFields = 0x00080000 | 0x00100000
                if user32.ChangeDisplaySettingsExW(target_dev_name, ctypes.byref(current_mode), None, 1, None) == 0:
                    break
            time.sleep(1.0)
    except Exception:
        pass

def rename_virtual_monitors():
    if OS_TYPE != "Windows":
        return
    try:
        base_path = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as display_key:
            subkeys_count, _, _ = winreg.QueryInfoKey(display_key)
            for i in range(subkeys_count):
                mon_id = winreg.EnumKey(display_key, i)
                if "USBMON" in mon_id.upper() or "AMYUNI" in mon_id.upper() or "VIRTUAL" in mon_id.upper():
                    mon_path = rf"{base_path}\{mon_id}"
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, mon_path) as instance_key:
                        instances_count, _, _ = winreg.QueryInfoKey(instance_key)
                        for j in range(instances_count):
                            inst_id = winreg.EnumKey(instance_key, j)
                            param_path = rf"{mon_path}\{inst_id}\Device Parameters"
                            try:
                                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, param_path, 0, winreg.KEY_SET_VALUE) as param_key:
                                    winreg.SetValueEx(param_key, "FriendlyName", 0, winreg.REG_SZ, "Nintendo DS Display")
                            except Exception:
                                pass
    except Exception:
        pass

def create_virtual_monitor(mode):
    w, h, count = (256, 384, 1) if "Giant" in mode else (256, 192, 2) if "2 Screens" in mode else (256, 192, 1)

    if OS_TYPE == "Windows":
        DRIVER_DIR = os.path.join(SCRIPT_DIR, "usbmmidd_v2")
        exe_name = "deviceinstaller64.exe" if platform.machine().endswith('64') else "deviceinstaller.exe"
        commands = [
            'cd /d "%~dp0"',
            f'{exe_name} enableidd 0',
            f'{exe_name} enableidd 0',
            f'{exe_name} remove usbmmidd',
            'timeout /t 1 /nobreak > NUL',
            f'{exe_name} install usbmmidd.inf usbmmidd',
            'timeout /t 2 /nobreak > NUL'
        ]
        reg_path = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\usbmmIdd\Parameters\Monitors"
        commands.extend([
            f'reg delete "{reg_path}" /f > NUL 2>&1',
            f'reg add "{reg_path}" /v 0 /t REG_SZ /d "{w},{h}" /f'
        ])
        if count == 2:
            commands.append(f'reg add "{reg_path}" /v 1 /t REG_SZ /d "{w},{h}" /f')
        commands.append(f'reg add "{reg_path}" /ve /t REG_SZ /d "0" /f')
        for _ in range(count):
            commands.append(f'{exe_name} enableidd 1')
            if count > 1:
                commands.append('timeout /t 2 /nobreak > NUL')
        bat_path = os.path.join(DRIVER_DIR, "run_action.bat")
        with open(bat_path, "w") as f:
            f.write("\n".join(commands))
        return run_elevated_windows(bat_path, DRIVER_DIR)

    elif OS_TYPE == "Linux" and DISPLAY_SERVER == "X11":
        try:
            mode_name = "Nintendo_DS_Top" if count == 2 else "Nintendo_DS"
            cvt_out = subprocess.check_output(["cvt", str(w), str(h), "60"]).decode()
            modeline = [line for line in cvt_out.split('\n') if "Modeline" in line][0].split('"')[1:]
            mode_params = modeline[1].strip()
            xrandr_out = subprocess.check_output(["xrandr"]).decode()
            virtual_port = "VIRTUAL1"
            for line in xrandr_out.split('\n'):
                if "disconnected" in line and ("VIRTUAL" in line or "DP-" in line):
                    virtual_port = line.split()[0]
                    break
            subprocess.run(["xrandr", "--newmode", mode_name] + mode_params.split())
            subprocess.run(["xrandr", "--addmode", virtual_port, mode_name])
            subprocess.run(["xrandr", "--output", virtual_port, "--mode", mode_name, "--right-of", "eDP1"])
            return True
        except Exception:
            return False

    elif OS_TYPE == "Darwin":
        bd_path = "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
        ds_name = "Nintendo_DS_Top" if count == 2 else "Nintendo_DS"
        if os.path.exists(bd_path):
            subprocess.run([bd_path, "set", f"-namematch={ds_name}", f"-resolution={w}x{h}"])
            return True
        else:
            messagebox.showinfo("macOS Requirement", "Virtual monitors on Mac require the free 'BetterDisplay' app.\nFalling back to Primary Screen.")
            return False
    return False

def remove_virtual_monitor():
    if OS_TYPE == "Windows":
        DRIVER_DIR = os.path.join(SCRIPT_DIR, "usbmmidd_v2")
        exe_name = "deviceinstaller64.exe" if platform.machine().endswith('64') else "deviceinstaller.exe"
        bat_path = os.path.join(DRIVER_DIR, "run_action.bat")
        with open(bat_path, "w") as f:
            f.write(f'cd /d "%~dp0"\n{exe_name} enableidd 0\n{exe_name} enableidd 0')
        run_elevated_windows(bat_path, DRIVER_DIR)
    elif OS_TYPE == "Linux" and DISPLAY_SERVER == "X11":
        try:
            subprocess.run(["xrandr", "--output", "VIRTUAL1", "--off"])
        except Exception:
            pass

def get_local_ips():
    ips = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips["Primary Network"] = s.getsockname()[0]
    except Exception:
        pass
    return ips

def get_sorted_ips():
    return list(get_local_ips().items())

def auto_add_monitors():
    global stream_mode, virtual_monitor_active, selected_indices, created_virtual_monitors, created_monitor_indices

    if "Primary Screen" in stream_mode or "Stream Monitor" in stream_mode:
        if "Stream Monitor" in stream_mode:
            try:
                idx = int(stream_mode.split("Stream Monitor ")[1].split(" ")[0])
                selected_indices = [idx]
            except Exception:
                selected_indices = [1]
        else:
            selected_indices = [1]

        created_virtual_monitors = False
        virtual_monitor_active = True
        return True

    displays_before = get_active_windows_displays()

    if not create_virtual_monitor(stream_mode):
        return False

    expected_new = 2 if "2 Screens" in stream_mode else 1

    if OS_TYPE == "Windows":
        w = 256
        h = 384 if "Giant" in stream_mode else 192

        new_displays = []
        for _ in range(20):
            displays_after = get_active_windows_displays()
            new_displays = [d for d in displays_after if d not in displays_before]
            if len(new_displays) >= expected_new:
                break
            time.sleep(0.5)

        rename_virtual_monitors()

        for dev_name in new_displays:
            force_windows_resolution(dev_name, w, h)

        found = False
        new_mss_indices = []
        for _ in range(20):
            with mss.MSS() as temp_sct:
                matched_indices = [
                    idx for idx, m in enumerate(temp_sct.monitors)
                    if idx != 0 and m['width'] == w and m['height'] == h
                ]
                if len(matched_indices) >= expected_new:
                    new_mss_indices = matched_indices[-expected_new:]
                    found = True
                    break
            time.sleep(0.5)

        if not found:
            with mss.MSS() as temp_sct:
                new_mss_indices = [len(temp_sct.monitors) - 1]

        selected_indices = new_mss_indices
        created_monitor_indices = new_mss_indices.copy()
    else:
        with mss.MSS() as sct:
            monitors_before = len(sct.monitors)
        found = False
        for _ in range(60):
            with mss.MSS() as temp_sct:
                if len(temp_sct.monitors) >= monitors_before + expected_new:
                    selected_indices = list(range(len(temp_sct.monitors) - expected_new, len(temp_sct.monitors)))
                    created_monitor_indices = selected_indices.copy()
                    found = True
                    break
            time.sleep(0.5)
        if not found:
            auto_remove_monitors()
            return False

    created_virtual_monitors = True
    virtual_monitor_active = True
    return True

def auto_remove_monitors():
    global virtual_monitor_active, selected_indices, created_virtual_monitors, created_monitor_indices
    if created_virtual_monitors:
        remove_virtual_monitor()
        time.sleep(2.0)
        created_virtual_monitors = False

    virtual_monitor_active = False
    selected_indices = []
    created_monitor_indices = []

# ── 6. TCP Handshake + UDP Streaming Handler ──────────────────
def capture_single_monitor(sct, mon, target_size):
    shot = sct.grab(mon)
    raw_img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    del shot
    draw_cursor(raw_img, mon)
    if raw_img.size != target_size:
        resized = raw_img.resize(target_size, Image.Resampling.BILINEAR)
        raw_img.close()
        raw_img = resized

    lum = current_luminosity
    if abs(lum - 1.0) > 0.01:
        lut = get_luminosity_lut(lum)
        adjusted = raw_img.point(lut)
        raw_img.close()
        raw_img = adjusted
    return raw_img

def capture_dual_monitors(sct, mon1, mon2):
    left = min(mon1["left"], mon2["left"])
    top = min(mon1["top"], mon2["top"])
    right = max(mon1["left"] + mon1["width"], mon2["left"] + mon2["width"])
    bottom = max(mon1["top"] + mon1["height"], mon2["top"] + mon2["height"])
    total_w = right - left
    total_h = bottom - top

    if total_w <= 1024 and total_h <= 1024:
        combined_mon = {"left": left, "top": top, "width": total_w, "height": total_h}
        shot = sct.grab(combined_mon)
        full_img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        del shot
        draw_cursor(full_img, combined_mon)

        m1_x, m1_y = mon1["left"] - left, mon1["top"] - top
        m2_x, m2_y = mon2["left"] - left, mon2["top"] - top

        img1 = full_img.crop((m1_x, m1_y, m1_x + mon1["width"], m1_y + mon1["height"]))
        img2 = full_img.crop((m2_x, m2_y, m2_x + mon2["width"], m2_y + mon2["height"]))
        full_img.close()

        if img1.size != (256, 192):
            r1 = img1.resize((256, 192), Image.Resampling.BILINEAR)
            img1.close()
            img1 = r1
        if img2.size != (256, 192):
            r2 = img2.resize((256, 192), Image.Resampling.BILINEAR)
            img2.close()
            img2 = r2

        out_img = Image.new("RGB", (256, 384))
        out_img.paste(img1, (0, 0))
        out_img.paste(img2, (0, 192))
        img1.close()
        img2.close()

        lum = current_luminosity
        if abs(lum - 1.0) > 0.01:
            lut = get_luminosity_lut(lum)
            adjusted = out_img.point(lut)
            out_img.close()
            out_img = adjusted

        return out_img
    else:
        img1 = capture_single_monitor(sct, mon1, (256, 192))
        img2 = capture_single_monitor(sct, mon2, (256, 192))
        out_img = Image.new("RGB", (256, 384))
        out_img.paste(img1, (0, 0))
        out_img.paste(img2, (0, 192))
        img1.close()
        img2.close()
        return out_img

def handle_client(conn, addr):
    global current_client, stream_mode, virtual_monitor_active, selected_indices, swapping_monitors, udp_socket

    ds_udp_port = current_port + 1
    try:
        conn.settimeout(2.0)
        hs = conn.recv(4)
        if len(hs) == 4 and hs[0:2] == b"DS":
            port_val = (hs[2] << 8) | hs[3]
            if 0 < port_val <= 65535:
                ds_udp_port = port_val
    except Exception:
        pass
    finally:
        try:
            conn.settimeout(None)
        except Exception:
            pass

    udp_target = [addr[0], ds_udp_port]
    client_info = {"conn": conn, "running": True, "udp_target": udp_target}
    with current_client_lock:
        current_client = client_info

    def tcp_disconnect_watcher():
        try:
            while client_info["running"]:
                data = conn.recv(64)
                if not data:
                    break
        except Exception:
            pass
        finally:
            client_info["running"] = False

    threading.Thread(target=tcp_disconnect_watcher, daemon=True).start()

    if app_gui:
        app_gui.log_message(f"DS Handshake OK ({udp_target[0]}:{udp_target[1]})! Starting UDP...")

    swapping_monitors = True

    def spawn_monitors_task():
        global swapping_monitors
        if not auto_add_monitors():
            if app_gui:
                app_gui.log_message("Monitor spawn failed. Disconnecting.")
            client_info["running"] = False
        else:
            if app_gui:
                app_gui.log_message("UDP Stream Active.")
        swapping_monitors = False

    threading.Thread(target=spawn_monitors_task, daemon=True).start()

    latest_frame_lock = threading.Lock()
    latest_frame = [None]
    frame_event = threading.Event()

    def capture_worker():
        sct = mss.MSS()
        last_swap_state = False
        try:
            while client_info["running"]:
                if swapping_monitors or not selected_indices:
                    last_swap_state = True
                    time.sleep(0.01)
                    continue

                if last_swap_state:
                    sct.close()
                    sct = mss.MSS()
                    last_swap_state = False

                try:
                    if "Primary Screen" in stream_mode or "Stream Monitor" in stream_mode:
                        idx = min(selected_indices[0], len(sct.monitors) - 1)
                        img = capture_single_monitor(sct, sct.monitors[idx], (256, 192))
                    elif "2 Screens" in stream_mode:
                        if len(sct.monitors) <= max(selected_indices):
                            time.sleep(0.005)
                            continue
                        idx1 = min(selected_indices[0], len(sct.monitors) - 1)
                        idx2 = min(selected_indices[1] if len(selected_indices) > 1 else idx1, len(sct.monitors) - 1)
                        img = capture_dual_monitors(sct, sct.monitors[idx1], sct.monitors[idx2])
                    else:
                        if len(sct.monitors) <= max(selected_indices):
                            time.sleep(0.005)
                            continue
                        idx = min(selected_indices[0], len(sct.monitors) - 1)
                        target_sz = (256, 384) if "Giant" in stream_mode else (256, 192)
                        img = capture_single_monitor(sct, sct.monitors[idx], target_sz)

                    with latest_frame_lock:
                        old = latest_frame[0]
                        latest_frame[0] = img
                        if old is not None:
                            old.close()
                    frame_event.set()
                except Exception:
                    time.sleep(0.005)
        finally:
            sct.close()
            with latest_frame_lock:
                if latest_frame[0] is not None:
                    latest_frame[0].close()
                    latest_frame[0] = None

    threading.Thread(target=capture_worker, daemon=True).start()

    out_stream_buf = (ctypes.c_uint8 * 65536)()
    server_frame_state = None
    was_swapping = False
    last_lum = current_luminosity
    last_budget = current_budget

    def poll_udp_probe():
        with server_socket_lock:
            usock = udp_socket
        if usock is None:
            return
        while True:
            try:
                data, uaddr = usock.recvfrom(64)
                if data == b"DSUP" and uaddr[0] == addr[0]:
                    udp_target[1] = uaddr[1]
            except (BlockingIOError, socket.error):
                break

    def send_udp_frame(orig_w, orig_h, payload):
        with server_socket_lock:
            usock = udp_socket
        if usock is None:
            return
        try:
            pkt = struct.pack('>BHHI', 0x03, orig_w, orig_h, len(payload)) + payload
            usock.sendto(pkt, (udp_target[0], udp_target[1]))
        except Exception:
            pass

    try:
        while client_info["running"]:
            if not swapping_monitors and not virtual_monitor_active and "Primary Screen" not in stream_mode and "Stream Monitor" not in stream_mode:
                break

            poll_udp_probe()

            if swapping_monitors:
                was_swapping = True
                time.sleep(0.02)
                continue

            if was_swapping or last_lum != current_luminosity or last_budget != current_budget:
                server_frame_state = None
                was_swapping = False
                last_lum = current_luminosity
                last_budget = current_budget

            if not frame_event.wait(timeout=0.02):
                continue
            frame_event.clear()

            with latest_frame_lock:
                img = latest_frame[0]
                latest_frame[0] = None

            if img is None:
                continue

            t0 = time.monotonic()
            try:
                payload, server_frame_state, orig_w, orig_h = encode_quadtree_fast_wrapper(
                    img, server_frame_state, out_stream_buf, current_budget
                )
            finally:
                img.close()

            if len(payload) > 1:
                send_udp_frame(orig_w, orig_h, payload)
                airtime = max(0.015, (len(payload) + 36) / 55000.0)
                elapsed = time.monotonic() - t0
                if airtime > elapsed:
                    time.sleep(airtime - elapsed)
    finally:
        client_info["running"] = False
        frame_event.set()
        with current_client_lock:
            current_client = None
        try:
            conn.close()
        except Exception:
            pass
        auto_remove_monitors()
        if app_gui:
            app_gui.log_message("DS Disconnected. Cleaned up.")

def start_server():
    global server_running, accept_thread
    if server_running:
        return
    auto_remove_monitors()
    server_running = True
    update_server_socket(current_port)
    accept_thread = threading.Thread(target=accept_loop, daemon=True)
    accept_thread.start()

def stop_server():
    global server_running, server_socket, udp_socket, current_client
    server_running = False
    with server_socket_lock:
        if server_socket:
            try:
                server_socket.close()
            except Exception:
                pass
            server_socket = None
        if udp_socket:
            try:
                udp_socket.close()
            except Exception:
                pass
            udp_socket = None
    with current_client_lock:
        if current_client:
            current_client["running"] = False
            try:
                current_client["conn"].close()
            except Exception:
                pass
            current_client = None

    auto_remove_monitors()
    if app_gui:
        app_gui.log_message("Server stopped. Cleaned up.")

def update_server_socket(port):
    global server_socket, udp_socket
    with server_socket_lock:
        if server_socket:
            try:
                server_socket.close()
            except Exception:
                pass
            server_socket = None
        if udp_socket:
            try:
                udp_socket.close()
            except Exception:
                pass
            udp_socket = None

        tsock = None
        usock = None
        try:
            tsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tsock.bind(("0.0.0.0", port))
            tsock.listen(1)
            tsock.settimeout(0.5)

            usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            usock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            usock.bind(("0.0.0.0", port))
            usock.setblocking(False)

            server_socket = tsock
            udp_socket = usock
        except Exception:
            if tsock is not None:
                try:
                    tsock.close()
                except Exception:
                    pass
            if usock is not None:
                try:
                    usock.close()
                except Exception:
                    pass
            server_socket = None
            udp_socket = None

def accept_loop():
    global server_running, server_socket
    while server_running:
        with server_socket_lock:
            sock = server_socket
        if sock is None:
            time.sleep(0.1)
            continue
        conn = None
        try:
            conn, addr = sock.accept()
            conn.settimeout(None)
            with current_client_lock:
                if current_client is not None:
                    conn.close()
                    continue

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except socket.timeout:
            continue
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

# ── 7. Dynamic GUI ────────────────────────────────────────────
class ServerGUI:
    def __init__(self, root):
        self.root = root
        root.title("DS Monitor Server")
        root.geometry("440x700")
        root.resizable(False, False)

        tk.Label(root, text="Server IP Address", font=("Arial", 10, "bold")).pack(pady=(10, 0))
        self.ip_listbox = tk.Listbox(root, height=3, selectmode=tk.SINGLE)
        self.ip_listbox.pack(fill=tk.X, padx=10, pady=5)
        self.refresh_ips()

        tk.Label(root, text=f"Detected System: {OS_TYPE} ({DISPLAY_SERVER})", font=("Arial", 9, "italic"), fg="blue").pack(pady=5)

        self.stream_mode_var = tk.BooleanVar(value=(DISPLAY_SERVER == "Wayland"))

        if DISPLAY_SERVER == "Wayland":
            tk.Label(root, text="Streaming Settings", font=("Arial", 10, "bold")).pack(pady=(5, 0))
            self.mode_var = tk.StringVar(value="Primary Screen (Auto-Scaled Fallback)")
            self.mode_combo = ttk.Combobox(root, textvariable=self.mode_var, width=35)
            self.mode_combo["values"] = ["Primary Screen (Auto-Scaled Fallback)"]
            self.mode_combo.current(0)
            self.mode_combo.config(state="disabled")
            self.mode_combo.pack(fill=tk.X, padx=10, pady=5)
        else:
            tk.Label(root, text="Monitor Settings", font=("Arial", 10, "bold")).pack(pady=(5, 0))
            tk.Checkbutton(root, text="Activate Streaming Mode (Stream physical monitors)", variable=self.stream_mode_var, command=self.toggle_streaming_mode).pack(pady=2)
            self.mode_var = tk.StringVar()
            self.mode_combo = ttk.Combobox(root, textvariable=self.mode_var, width=35)
            self.mode_combo["values"] = [
                "1 Screen (256x192) - DS Up/Down to swap",
                "1 Giant Screen (256x384)",
                "2 Screens (Dual 256x192)"
            ]
            self.mode_combo.current(0)
            self.mode_combo.config(state="readonly")
            self.mode_combo.pack(fill=tk.X, padx=10, pady=5)
            self.mode_combo.bind("<<ComboboxSelected>>", self.on_mode_select)

        global stream_mode
        stream_mode = self.mode_var.get()

        # Reset Monitor Streaming Button
        self.reset_mon_btn = tk.Button(root, text="Reset Monitor Streaming", command=self.reset_monitor_streaming, width=28)
        self.reset_mon_btn.pack(pady=3)

        frame_port = tk.Frame(root)
        frame_port.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(frame_port, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        tk.Entry(frame_port, textvariable=self.port_var, width=8).pack(side=tk.LEFT, padx=5)

        tk.Label(root, text="Display Adjustment", font=("Arial", 10, "bold")).pack(pady=(8, 0))

        lum_frame = tk.Frame(root)
        lum_frame.pack(fill=tk.X, padx=10, pady=2)
        tk.Label(lum_frame, text="Luminosity:", width=11, anchor=tk.W).pack(side=tk.LEFT)
        self.lum_scale = tk.Scale(
            lum_frame, from_=0.30, to=3.00, resolution=0.05,
            orient=tk.HORIZONTAL, length=280, showvalue=True,
            command=self.on_lum_change,
        )
        self.lum_scale.set(DEFAULT_LUMINOSITY)
        self.lum_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

        det_frame = tk.Frame(root)
        det_frame.pack(fill=tk.X, padx=10, pady=2)
        tk.Label(det_frame, text="Detail Level:", width=11, anchor=tk.W).pack(side=tk.LEFT)
        self.det_scale = tk.Scale(
            det_frame, from_=300, to=1430, resolution=10,
            orient=tk.HORIZONTAL, length=280, showvalue=True,
            command=self.on_det_change,
        )
        self.det_scale.set(DEFAULT_BUDGET)
        self.det_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(root, text="(Higher detail = more bytes/frame. Values above ~1400 may fragment on some routers.)",
                 font=("Arial", 8, "italic"), fg="#555").pack(pady=(0, 4))

        tk.Label(root, text="Server Settings", font=("Arial", 10, "bold")).pack(pady=(8, 0))

        self.log_box = tk.Text(root, height=3, state=tk.DISABLED, bg="#f9f9f9", font=("Courier", 9))
        self.log_box.pack(fill=tk.X, padx=10, pady=5)
        self.log_message("Ready")

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=10)
        self.start_btn = tk.Button(btn_frame, text="Start Server", command=self.toggle_server, width=12)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Refresh IP", command=self.refresh_ips, width=10).pack(side=tk.LEFT, padx=5)

        self.status_var = tk.StringVar(value="Stopped")
        tk.Label(root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, padx=10, pady=5)
        self.client_status_var = tk.StringVar(value="No client connected")
        tk.Label(root, textvariable=self.client_status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, padx=10, pady=5)

        self.update_client_status()

    def log_message(self, text):
        def append():
            self.log_box.config(state=tk.NORMAL)
            self.log_box.insert(tk.END, text + "\n")
            lines = int(self.log_box.index('end-1c').split('.')[0])
            if lines > 100:
                self.log_box.delete('1.0', f'{lines - 100}.0')
            self.log_box.see(tk.END)
            self.log_box.config(state=tk.DISABLED)
        self.root.after(0, append)

    def refresh_ips(self):
        self.ip_listbox.delete(0, tk.END)
        for name, ip in get_sorted_ips():
            self.ip_listbox.insert(tk.END, f"{name}: {ip}")

    def on_lum_change(self, val):
        global current_luminosity
        try:
            current_luminosity = float(val)
        except ValueError:
            pass

    def on_det_change(self, val):
        global current_budget
        try:
            current_budget = int(float(val))
        except ValueError:
            pass

    def reset_monitor_streaming(self):
        """Re-initializes monitor streaming and refreshes physical monitor list if in streaming mode."""
        if self.stream_mode_var.get() and DISPLAY_SERVER != "Wayland":
            self.refresh_physical_monitor_list()
        if not virtual_monitor_active and not current_client:
            self.log_message("Monitor list refreshed (connect DS to stream).")
            return
        self.trigger_monitor_swap(previous_mode=stream_mode)

    def refresh_physical_monitor_list(self):
        default_values = [
            "1 Screen (256x192) - DS Up/Down to swap",
            "1 Giant Screen (256x384)",
            "2 Screens (Dual 256x192)"
        ]
        new_values = list(default_values)
        try:
            with mss.MSS() as sct:
                for i, mon in enumerate(sct.monitors[1:], 1):
                    if i not in created_monitor_indices:
                        new_values.append(f"Stream Monitor {i} ({mon['width']}x{mon['height']})")
        except Exception:
            pass
        self.mode_combo["values"] = new_values

    def trigger_monitor_swap(self, previous_mode=None):
        global virtual_monitor_active, swapping_monitors
        if swapping_monitors:
            return
        if not virtual_monitor_active and not current_client:
            return

        mode_to_blank = previous_mode if previous_mode is not None else stream_mode

        def swap_task():
            global swapping_monitors
            swapping_monitors = True
            self.log_message("Blanking DS screen & re-initializing monitors...")

            # Wait briefly so handle_client pauses sending frames, then send a black frame of the OLD mode
            time.sleep(0.05)
            send_black_frame_for_mode(mode_to_blank)

            auto_remove_monitors()
            time.sleep(0.5)
            if auto_add_monitors():
                self.log_message("Monitor stream ready & resumed.")
            else:
                self.log_message("Monitor setup failed. Stream halted.")
            swapping_monitors = False

        threading.Thread(target=swap_task, daemon=True).start()

    def on_mode_select(self, event):
        global stream_mode
        new_mode = self.mode_var.get()
        if stream_mode != new_mode:
            old_mode = stream_mode
            stream_mode = new_mode
            self.trigger_monitor_swap(previous_mode=old_mode)

    def toggle_streaming_mode(self):
        global stream_mode
        if DISPLAY_SERVER == "Wayland":
            return

        default_values = [
            "1 Screen (256x192) - DS Up/Down to swap",
            "1 Giant Screen (256x384)",
            "2 Screens (Dual 256x192)"
        ]

        if self.stream_mode_var.get():
            self.refresh_physical_monitor_list()
            self.mode_combo.config(state="readonly")
        else:
            self.mode_combo["values"] = default_values
            if self.mode_var.get() not in default_values:
                self.mode_combo.current(0)

        new_mode = self.mode_var.get()
        if stream_mode != new_mode:
            old_mode = stream_mode
            stream_mode = new_mode
            self.trigger_monitor_swap(previous_mode=old_mode)

    def toggle_server(self):
        global current_port
        if server_running:
            stop_server()
            self.start_btn.config(text="Start Server")
            self.status_var.set("Stopped")
        else:
            try:
                port = int(self.port_var.get())
            except ValueError:
                return
            current_port = port
            start_server()
            self.start_btn.config(text="Stop Server")
            self.status_var.set(f"Server running on TCP/UDP port {port} - waiting for DS...")

    def update_client_status(self):
        with current_client_lock:
            client = current_client
        if client:
            self.client_status_var.set("Nintendo DS Connected (UDP Stream)!")
        else:
            self.client_status_var.set("Waiting for Nintendo DS...")
        self.root.after(1000, self.update_client_status)

if __name__ == "__main__":
    if OS_TYPE == "Windows" and not prepare_driver_files():
        sys.exit(0)
    root = tk.Tk()
    app_gui = ServerGUI(root)
    root.mainloop()