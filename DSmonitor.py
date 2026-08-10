#!/usr/bin/env python3
import io
import sys
import os
import socket
import struct
import threading
import time
import queue
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
import pyautogui

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
pyautogui.FAILSAFE = False

server_running = False
server_socket = None
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
app_gui = None # Global reference to GUI for cross-thread status updates

# ── 2. Compiler Download Management (Windows Only) ────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMPILER_URL = "https://github.com/skeeto/w64devkit/releases/download/v1.20.0/w64devkit-1.20.0.zip"
COMPILER_DIR = os.path.join(SCRIPT_DIR, "w64devkit")

def prepare_compiler():
    if not OS_TYPE == "Windows": return "gcc" 
    
    try:
        subprocess.run(["gcc", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return "gcc"
    except FileNotFoundError:
        pass 

    gcc_path = os.path.join(COMPILER_DIR, "bin", "gcc.exe")
    if os.path.exists(gcc_path):
        return gcc_path

    ans = messagebox.askyesno("Compiler Required", "To ensure zero-lag gaming performance, this script requires a portable C-Compiler (~80MB).\n\nDownload it now?")
    if not ans:
        sys.exit(0)

    try:
        os.makedirs(COMPILER_DIR, exist_ok=True)
        zip_path = os.path.join(SCRIPT_DIR, "compiler.zip")
        
        print("Downloading portable GCC compiler...")
        req = urllib.request.Request(COMPILER_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
            out_file.write(response.read())
            
        print("Extracting compiler...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(COMPILER_DIR) 
            
        os.remove(zip_path)
        return gcc_path
    except Exception as e:
        messagebox.showerror("Download Error", f"Failed to download compiler: {e}")
        sys.exit(1)

# ── 3. Contrast-Aware Multi-Pass C-Encoder ──
C_CODE = """
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MAX_STREAM_SIZE 500000
#define MAX_NODES 65000

#define S_SPLIT 0x00
#define S_SOLID 0x01
#define S_RLE 0x03
#define S_RAW 0x04
#define S_BTC 0x05
#define S_PAL4 0x06
#define S_UNCHANGED 0xFF

static const int bayer[4][4] = {
    {0, 8, 2, 10}, {12, 4, 14, 6}, {3, 11, 1, 9}, {15, 7, 13, 5}
};

static inline uint8_t rgb_to_index(uint8_t r, uint8_t g, uint8_t b) {
    return ((r >> 5) << 5) | ((g >> 5) << 2) | (b >> 6);
}

static inline int color_dist(uint8_t c1, uint8_t c2) {
    int r1 = (c1 >> 5) & 7; int g1 = (c1 >> 2) & 7; int b1 = c1 & 3;
    int r2 = (c2 >> 5) & 7; int g2 = (c2 >> 2) & 7; int b2 = c2 & 3;
    return abs(r1-r2)*3 + abs(g1-g2)*4 + abs(b1-b2)*2;
}

uint8_t global_stream[MAX_STREAM_SIZE];
uint32_t stream_pos = 0;
int safety_abort = 0;

static inline void update_state(int x, int y, int size, const uint8_t* raw, uint8_t* server_state, int width) {
    if (!server_state) return;
    for (int row = 0; row < size; row++) {
        int offset = ((y + row) * width + x) * 3;
        memcpy(server_state + offset, raw + offset, size * 3);
    }
}

/* ---------- Compressors ---------- */

static int try_rle(const uint8_t* idx, int size, uint8_t* out) {
    int pos = 0;
    int total = size * size;
    for (int row = 0; row < size; row++) {
        const uint8_t* rp = idx + row * size;
        uint8_t prev = rp[0];
        int run = 1;
        for (int col = 1; col < size; col++) {
            if (rp[col] == prev && run < 255) run++;
            else {
                if (pos + 2 > 254) return 0; 
                out[pos++] = (uint8_t)run; out[pos++] = prev;
                prev = rp[col]; run = 1;
            }
        }
        if (pos + 2 > 254) return 0;
        out[pos++] = (uint8_t)run; out[pos++] = prev;
    }
    return pos;
}

static int try_btc(const uint8_t* R, const uint8_t* G, const uint8_t* B, const uint8_t* Y, int size, uint8_t* out) {
    int count = size * size;
    uint8_t min_y = 255, max_y = 0;
    for (int i = 0; i < count; i++) {
        if (Y[i] < min_y) min_y = Y[i];
        if (Y[i] > max_y) max_y = Y[i];
    }
    if (max_y - min_y < 6) return 0; 
    
    uint8_t thresh = (min_y + max_y) >> 1;
    uint32_t r0=0,g0=0,b0=0,n0=0;
    uint32_t r1=0,g1=0,b1=0,n1=0;
    
    int mask_bytes = (count + 7) >> 3;
    memset(out + 2, 0, mask_bytes);
    
    for (int i = 0; i < count; i++) {
        if (Y[i] >= thresh) {
            out[2 + (i >> 3)] |= (1 << (i & 7));
            r1 += R[i]; g1 += G[i]; b1 += B[i]; n1++;
        } else {
            r0 += R[i]; g0 += G[i]; b0 += B[i]; n0++;
        }
    }
    if (n0 == 0 || n1 == 0) return 0;
    
    out[0] = rgb_to_index(r0/n0, g0/n0, b0/n0);
    out[1] = rgb_to_index(r1/n1, g1/n1, b1/n1);
    return 2 + mask_bytes;
}

static int try_pal4(const uint8_t* idx, int size, uint8_t* out) {
    int count = size * size;
    int freq[256] = {0};
    for (int i = 0; i < count; i++) freq[idx[i]]++;
    uint8_t pal[4];
    int pal_count = 0;
    for (int iter = 0; iter < 4; iter++) {
        int best_f = -1, best_c = 0;
        for (int c = 0; c < 256; c++) {
            if (freq[c] > best_f) { best_f = freq[c]; best_c = c; }
        }
        if (best_f <= 0) break;
        pal[pal_count++] = (uint8_t)best_c;
        freq[best_c] = 0;
    }
    if (pal_count == 0) return 0;
    while (pal_count < 4) pal[pal_count++] = pal[0];
    int pos = 0;
    out[pos++] = pal[0]; out[pos++] = pal[1]; out[pos++] = pal[2]; out[pos++] = pal[3];
    for (int i = 0; i < count; i += 4) {
        uint8_t b = 0;
        for (int j = 0; j < 4 && (i + j) < count; j++) {
            int best = 0, best_err = 9999;
            for (int p = 0; p < 4; p++) {
                int err = color_dist(idx[i+j], pal[p]);
                if (err < best_err) { best_err = err; best = p; }
            }
            b |= (best << (j * 2));
        }
        out[pos++] = b;
    }
    return pos;
}

/* ---------- The Multi-Pass Tree Memory ---------- */

typedef struct {
    int type;      
    int parent;
    int children[4];
    int costs[7];
    int current_state;
    int cost;
    int x, y, size;
    uint8_t avg_r, avg_g, avg_b;
    int can_downgrade;
} Node;

Node tree[MAX_NODES]; 
int node_count = 0;

int build_tree(int x, int y, int size, const uint8_t* raw, uint8_t* server_state, int width, int parent_idx) {
    if (node_count >= MAX_NODES - 5) {
        safety_abort = 1;
        return 0; 
    }

    int n_idx = node_count++;
    Node* n = &tree[n_idx];
    n->parent = parent_idx;
    n->x = x; n->y = y; n->size = size;
    n->can_downgrade = 1;
    n->type = 1; 
    
    for (int i=0; i<7; i++) n->costs[i] = 999999;
    
    if (server_state && size < 256) {
        int changed = 0;
        for (int row = 0; row < size; row++) {
            int off = ((y + row) * width + x) * 3;
            if (memcmp(raw + off, server_state + off, size * 3) != 0) { changed = 1; break; }
        }
        if (!changed) {
            n->costs[S_UNCHANGED] = 1;
            n->current_state = S_UNCHANGED;
            n->cost = 1;
            n->can_downgrade = 0;
            return n_idx;
        }
    }
    
    int tol = (size >= 16) ? 8 : 12;
    int off0 = (y * width + x) * 3;
    uint8_t r0 = raw[off0], g0 = raw[off0+1], b0 = raw[off0+2];
    int is_solid = 1;
    uint32_t sum_r = 0, sum_g = 0, sum_b = 0;
    for (int row = 0; row < size; row++) {
        int row_off = ((y + row) * width + x) * 3;
        for (int col = 0; col < size; col++) {
            int c = row_off + col * 3;
            sum_r += raw[c]; sum_g += raw[c+1]; sum_b += raw[c+2];
            if (is_solid && (abs((int)raw[c] - r0) > tol || abs((int)raw[c+1] - g0) > tol || abs((int)raw[c+2] - b0) > tol)) {
                is_solid = 0;
            }
        }
    }
    int n_pixels = size * size;
    n->avg_r = sum_r / n_pixels;
    n->avg_g = sum_g / n_pixels;
    n->avg_b = sum_b / n_pixels;
    if (is_solid) n->costs[S_SOLID] = 4;
    
    if (size > 32) {
        int half = size / 2;
        n->children[0] = build_tree(x, y, half, raw, server_state, width, n_idx);
        n->children[1] = build_tree(x + half, y, half, raw, server_state, width, n_idx);
        n->children[2] = build_tree(x, y + half, half, raw, server_state, width, n_idx);
        n->children[3] = build_tree(x + half, y + half, half, raw, server_state, width, n_idx);
        n->costs[S_SPLIT] = 1 + tree[n->children[0]].cost + tree[n->children[1]].cost + 
                                tree[n->children[2]].cost + tree[n->children[3]].cost;
        n->current_state = S_SPLIT;
        n->cost = n->costs[S_SPLIT];
        n->can_downgrade = 0; 
        return n_idx;
    }
    
    int count = size * size;
    uint8_t indices[1024], R[1024], G[1024], B[1024], Y[1024];
    int idx = 0;
    for (int row = 0; row < size; row++) {
        int off = ((y + row) * width + x) * 3;
        for (int col = 0; col < size; col++) {
            int c = off + col * 3;
            R[idx] = raw[c]; G[idx] = raw[c+1]; B[idx] = raw[c+2];
            Y[idx] = (R[idx] * 77 + G[idx] * 150 + B[idx] * 29) >> 8;
            indices[idx] = rgb_to_index(R[idx], G[idx], B[idx]);
            idx++;
        }
    }
    
    n->costs[S_RAW] = count + 1;
    
    uint8_t dummy_rle[2048], dummy_btc[256], dummy_pal[512];
    
    int r_sz = try_rle(indices, size, dummy_rle);
    if (r_sz > 0) n->costs[S_RLE] = r_sz + 2;
    
    int b_sz = try_btc(R, G, B, Y, size, dummy_btc);
    if (b_sz > 0) n->costs[S_BTC] = b_sz + 1;
    
    if (size >= 8) {
        int p_sz = try_pal4(indices, size, dummy_pal);
        if (p_sz > 0) n->costs[S_PAL4] = p_sz + 1;
    }
    
    if (size > 4) {
        int half = size / 2;
        n->children[0] = build_tree(x, y, half, raw, server_state, width, n_idx);
        n->children[1] = build_tree(x + half, y, half, raw, server_state, width, n_idx);
        n->children[2] = build_tree(x, y + half, half, raw, server_state, width, n_idx);
        n->children[3] = build_tree(x + half, y + half, half, raw, server_state, width, n_idx);
        n->costs[S_SPLIT] = 1 + tree[n->children[0]].cost + tree[n->children[1]].cost + 
                                tree[n->children[2]].cost + tree[n->children[3]].cost;
    }
    
    int best_cost = n->costs[S_SOLID];
    int best_state = S_SOLID;
    
    if (n->costs[S_SPLIT] < best_cost) { best_cost = n->costs[S_SPLIT]; best_state = S_SPLIT; }
    if (n->costs[S_RLE] < best_cost) { best_cost = n->costs[S_RLE]; best_state = S_RLE; }
    if (n->costs[S_BTC] < best_cost) { best_cost = n->costs[S_BTC]; best_state = S_BTC; }
    if (n->costs[S_PAL4] < best_cost) { best_cost = n->costs[S_PAL4]; best_state = S_PAL4; }
    if (n->costs[S_RAW] < best_cost) { best_cost = n->costs[S_RAW]; best_state = S_RAW; }
    
    n->current_state = best_state;
    n->cost = best_cost;
    
    if (best_state == S_SOLID) {
        n->can_downgrade = 0;
    }
    return n_idx;
}

void update_costs(int n_idx) {
    Node* n = &tree[n_idx];
    if (n->current_state == S_SPLIT) {
        n->cost = 1 + tree[n->children[0]].cost + tree[n->children[1]].cost + 
                      tree[n->children[2]].cost + tree[n->children[3]].cost;
    } else if (n->current_state == S_UNCHANGED) {
        n->cost = 1;
    } else {
        n->cost = n->costs[n->current_state];
        if (n->current_state == S_SOLID) n->cost = 4;
    }
    if (n->parent != -1) update_costs(n->parent);
}

void emit_tree(int n_idx, const uint8_t* raw, uint8_t* server_state, int width) {
    if (safety_abort) return;
    if (stream_pos >= MAX_STREAM_SIZE - 2000) { safety_abort = 1; return; }

    Node* n = &tree[n_idx];
    
    if (n->current_state == S_UNCHANGED) {
        global_stream[stream_pos++] = 0xFF;
        return;
    }
    if (n->current_state == S_SPLIT) {
        global_stream[stream_pos++] = 0x00;
        emit_tree(n->children[0], raw, server_state, width);
        emit_tree(n->children[1], raw, server_state, width);
        emit_tree(n->children[2], raw, server_state, width);
        emit_tree(n->children[3], raw, server_state, width);
        return;
    }
    if (n->current_state == S_SOLID) {
        global_stream[stream_pos++] = 0x01;
        global_stream[stream_pos++] = n->avg_r;
        global_stream[stream_pos++] = n->avg_g;
        global_stream[stream_pos++] = n->avg_b;
        update_state(n->x, n->y, n->size, raw, server_state, width);
        return;
    }
    
    int count = n->size * n->size;
    uint8_t indices[1024], R[1024], G[1024], B[1024], Y[1024];
    int idx = 0;
    for (int row = 0; row < n->size; row++) {
        int off = ((n->y + row) * width + n->x) * 3;
        for (int col = 0; col < n->size; col++) {
            int c = off + col * 3;
            R[idx] = raw[c]; G[idx] = raw[c+1]; B[idx] = raw[c+2];
            Y[idx] = (R[idx] * 77 + G[idx] * 150 + B[idx] * 29) >> 8;
            indices[idx] = rgb_to_index(R[idx], G[idx], B[idx]);
            idx++;
        }
    }
    
    global_stream[stream_pos++] = n->current_state;
    
    if (n->current_state == S_RLE) {
        uint8_t buf[2048]; int sz = try_rle(indices, n->size, buf);
        global_stream[stream_pos++] = (uint8_t)sz;
        memcpy(&global_stream[stream_pos], buf, sz); stream_pos += sz;
    } else if (n->current_state == S_BTC) {
        uint8_t buf[256]; int sz = try_btc(R, G, B, Y, n->size, buf);
        memcpy(&global_stream[stream_pos], buf, sz); stream_pos += sz;
    } else if (n->current_state == S_PAL4) {
        uint8_t buf[512]; int sz = try_pal4(indices, n->size, buf);
        memcpy(&global_stream[stream_pos], buf, sz); stream_pos += sz;
    } else if (n->current_state == S_RAW) {
        for (int row = 0; row < n->size; row++) {
            int off = ((n->y + row) * width + n->x) * 3;
            for (int col = 0; col < n->size; col++) {
                int c = off + col * 3;
                int threshold = bayer[(n->y + row) & 3][(n->x + col) & 3];
                int rr = (int)raw[c]   + ((threshold * 32) >> 4) - 16;
                int gg = (int)raw[c+1] + ((threshold * 32) >> 4) - 16;
                int bb = (int)raw[c+2] + ((threshold * 64) >> 4) - 32;
                if (rr < 0) rr = 0; if (rr > 255) rr = 255;
                if (gg < 0) gg = 0; if (gg > 255) gg = 255;
                if (bb < 0) bb = 0; if (bb > 255) bb = 255;
                global_stream[stream_pos++] = rgb_to_index((uint8_t)rr, (uint8_t)gg, (uint8_t)bb);
            }
        }
    }
    update_state(n->x, n->y, n->size, raw, server_state, width);
}

int get_total_cost(int n_idx) {
    Node* n = &tree[n_idx];
    if (n->current_state == S_SPLIT) {
        return 1 + get_total_cost(n->children[0]) + get_total_cost(n->children[1]) + 
                   get_total_cost(n->children[2]) + get_total_cost(n->children[3]);
    }
    return n->cost; 
}

int compare_leaves(const void* a, const void* b) {
    int idx_a = *(int*)a;
    int idx_b = *(int*)b;
    return tree[idx_b].cost - tree[idx_a].cost;
}

/* ---------- Entry ---------- */

#ifdef _WIN32
__declspec(dllexport)
#endif
int encode_quadtree_fast(const uint8_t* raw, uint8_t* server_state, int width, int height, int orig_w, int orig_h, uint8_t* out_stream) {
    node_count = 0;
    stream_pos = 0;
    safety_abort = 0;
    
    // FIX: Only write EXACTLY 2 bytes here so it perfectly aligns with main.c's decoder!
    global_stream[stream_pos++] = (uint8_t)(orig_w & 0xFF);
    global_stream[stream_pos++] = (uint8_t)(orig_h & 0xFF);

    int root = build_tree(0, 0, height, raw, server_state, width, -1);
    if (safety_abort) return 0; 

    int total_bytes = get_total_cost(root);
    int BUDGET = 6500; 
    
    if (total_bytes > BUDGET) {
        int leaves[MAX_NODES];
        int leaf_count = 0;
        for (int i=0; i<node_count; i++) {
            if (tree[i].current_state != S_SPLIT && tree[i].current_state != S_UNCHANGED && tree[i].can_downgrade) {
                leaves[leaf_count++] = i;
            }
        }
        
        qsort(leaves, leaf_count, sizeof(int), compare_leaves);
        
        while (total_bytes > BUDGET) {
            int downgraded_any = 0;
            for (int i = 0; i < leaf_count && total_bytes > BUDGET; i++) {
                int n_idx = leaves[i];
                Node* n = &tree[n_idx];
                if (!n->can_downgrade) continue;
                
                int next_state = -1;
                if (n->current_state == S_RLE || n->current_state == S_RAW) {
                    if (n->costs[S_PAL4] < n->cost) next_state = S_PAL4;
                    else if (n->costs[S_BTC] < n->cost) next_state = S_BTC;
                    else next_state = S_SOLID; 
                } else if (n->current_state == S_PAL4) {
                    if (n->costs[S_BTC] < n->cost) next_state = S_BTC;
                    else next_state = S_SOLID;
                } else if (n->current_state == S_BTC) {
                    next_state = S_SOLID;
                }
                
                if (next_state != -1) {
                    int old_cost = n->cost;
                    n->current_state = next_state;
                    n->cost = (next_state == S_SOLID) ? 4 : n->costs[next_state];
                    if (next_state == S_SOLID) n->can_downgrade = 0;
                    
                    total_bytes -= (old_cost - n->cost);
                    downgraded_any = 1;
                } else {
                    n->can_downgrade = 0;
                }
            }
            if (!downgraded_any) break;
        }
    }

    emit_tree(root, raw, server_state, width);
    if (safety_abort) return 0;

    memcpy(out_stream, global_stream, stream_pos);
    return stream_pos;
}
"""

def setup_fast_encoder():
    gcc_cmd = prepare_compiler()
    dll_name = "encoder.dll" if OS_TYPE == "Windows" else "encoder.dylib" if OS_TYPE == "Darwin" else "encoder.so"
    
    dll_path = os.path.abspath(os.path.join(SCRIPT_DIR, dll_name))
    c_path = os.path.abspath(os.path.join(SCRIPT_DIR, "encoder_temp.c"))
    gcc_abs = os.path.abspath(gcc_cmd) if OS_TYPE == "Windows" and gcc_cmd != "gcc" else gcc_cmd
    
    if OS_TYPE == "Windows":
        if gcc_abs != "gcc":
            compiler_bin_dir = os.path.dirname(gcc_abs)
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
        try: os.remove(dll_path)
        except: pass

    print(f"Auto-compiling high-speed C-Extension for {OS_TYPE}...")
    with open(c_path, "w") as f: 
        f.write(C_CODE)
    try:
        subprocess.run(compile_cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        messagebox.showerror("GCC Compiler Error", f"Failed to compile C-Extension!\n\nCommand:\n{' '.join(compile_cmd)}\n\nOutput:\n{e.stderr}")
        sys.exit(1)
    except Exception as e:
        messagebox.showerror("Fatal Error", f"Unexpected compilation error:\n{e}")
        sys.exit(1)
    finally:
        if os.path.exists(c_path): 
            try: os.remove(c_path)
            except: pass
    
    try:
        encoder = ctypes.CDLL(dll_path)
        encoder.encode_quadtree_fast.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8), 
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_uint8)
        ]
        encoder.encode_quadtree_fast.restype = ctypes.c_int
        return encoder
    except OSError as e: 
        messagebox.showerror("DLL Load Error", f"Failed to load compiled DLL.\n{e}")
        sys.exit(1)

fast_encoder = setup_fast_encoder()

def encode_quadtree_fast_wrapper(img, server_frame_state):
    orig_w, orig_h = img.size
    
    def next_pow2(n):
        if n == 0: return 1
        return 1 << (n - 1).bit_length()
    root_size = max(next_pow2(orig_w), next_pow2(orig_h))
    
    if root_size > 512:
        return b"", server_frame_state, orig_w, orig_h
    
    if img.size != (root_size, root_size):
        padded = Image.new('RGB', (root_size, root_size), (0, 0, 0))
        padded.paste(img, (0, 0))
        img = padded

    width, height = img.size
    raw_bytes = img.tobytes()
    if not server_frame_state or len(server_frame_state) != len(raw_bytes):
        server_frame_state = (ctypes.c_uint8 * len(raw_bytes))()
        ctypes.memset(server_frame_state, 0, len(raw_bytes))
    raw_ctypes = (ctypes.c_uint8 * len(raw_bytes)).from_buffer_copy(raw_bytes)
    out_stream = (ctypes.c_uint8 * 500000)()
    
    stream_len = fast_encoder.encode_quadtree_fast(raw_ctypes, server_frame_state, width, height, orig_w, orig_h, out_stream)
    
    if stream_len == 0:
        return b"", server_frame_state, orig_w, orig_h
        
    return bytes(out_stream[:stream_len]), server_frame_state, orig_w, orig_h

# ── 4. Multi-OS Hardware Mouse Handlers ───────────────────────
if OS_TYPE == "Windows":
    class POINT(ctypes.Structure): _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    class CURSORINFO(ctypes.Structure): _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint), ("hCursor", ctypes.c_void_p), ("ptScreenPos", POINT)]
    user32 = ctypes.windll.user32
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    try: H_IBEAM, H_HAND = user32.LoadCursorW(None, 32513), user32.LoadCursorW(None, 32649)
    except: H_IBEAM = H_HAND = None

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
                    draw.line([(rel_x, rel_y-7), (rel_x, rel_y+7)], fill="black", width=2)
                    draw.line([(rel_x, rel_y-6), (rel_x, rel_y+6)], fill="white", width=1)
                elif ci.hCursor == H_HAND:
                    draw.polygon([(rel_x, rel_y), (rel_x+5, rel_y+12), (rel_x+8, rel_y+10), (rel_x+12, rel_y+14), (rel_x+14, rel_y+12), (rel_x+10, rel_y+8), (rel_x+13, rel_y+5)], fill="white", outline="black")
                else:
                    draw.polygon([(rel_x, rel_y), (rel_x+12, rel_y+12), (rel_x+4, rel_y+12), (rel_x, rel_y+18)], fill="white", outline="black")
    return img

# ── 5. Cross-Platform Virtual Monitors ────────────────────────
def run_elevated_windows(bat_path, cwd):
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", bat_path, "", cwd, 0)
    return ret > 32

def prepare_driver_files():
    if not OS_TYPE == "Windows": return True
    DRIVER_URL = "https://www.amyuni.com/downloads/usbmmidd_v2.zip"
    DRIVER_DIR = os.path.join(SCRIPT_DIR, "usbmmidd_v2")
    if not os.path.exists(DRIVER_DIR):
        os.makedirs(DRIVER_DIR)
    exe_name = "deviceinstaller64.exe" if platform.machine().endswith('64') else "deviceinstaller.exe"
    exe_path = os.path.join(DRIVER_DIR, exe_name)

    if not os.path.exists(exe_path):
        ans = messagebox.askyesno("Driver Download", "The required Virtual Monitor files were not found locally.\n\nDo you want to download them now? (approx. 250KB)")
        if not ans: return False
        try:
            zip_path = os.path.join(DRIVER_DIR, "usbmmidd.zip")
            req = urllib.request.Request(DRIVER_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file: out_file.write(response.read())
            with zipfile.ZipFile(zip_path, 'r') as zip_ref: zip_ref.extractall(DRIVER_DIR)
            os.remove(zip_path)

            if not os.path.exists(exe_path):
                for item in os.listdir(DRIVER_DIR):
                    item_path = os.path.join(DRIVER_DIR, item)
                    if os.path.isdir(item_path):
                        for subitem in os.listdir(item_path):
                            shutil.move(os.path.join(item_path, subitem), os.path.join(DRIVER_DIR, subitem))
                        os.rmdir(item_path)
                        break
        except Exception as e:
            messagebox.showerror("Download Error", f"Failed to download files: {e}")
            return False
    return True

# ── Hardware Diff Resolution & Monitor Mapping ──
def get_active_windows_displays():
    """Returns a set of active Windows Display DeviceNames."""
    if OS_TYPE != "Windows": return set()
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32

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
    if OS_TYPE != "Windows": return
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32

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

        for attempt in range(5):
            current_mode = DEVMODEW()
            current_mode.dmSize = ctypes.sizeof(DEVMODEW)

            if user32.EnumDisplaySettingsW(target_dev_name, -1, ctypes.byref(current_mode)):
                if current_mode.dmPelsWidth == target_w and current_mode.dmPelsHeight == target_h:
                    break 

                current_mode.dmPelsWidth = target_w
                current_mode.dmPelsHeight = target_h
                current_mode.dmFields = 0x00080000 | 0x00100000 

                res = user32.ChangeDisplaySettingsExW(target_dev_name, ctypes.byref(current_mode), None, 1, None)
                if res == 0: 
                    break
            time.sleep(1.0)
    except Exception as e:
        print(f"Resolution switch failed: {e}")

def rename_virtual_monitors():
    if OS_TYPE != "Windows": return
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
                                    ds_name = "Nintendo DS Display"
                                    winreg.SetValueEx(param_key, "FriendlyName", 0, winreg.REG_SZ, ds_name)
                            except Exception:
                                pass
    except Exception:
        pass

def create_virtual_monitor(mode):
    w, h, count = (256, 384, 1) if "Giant" in mode else (256, 192, 2) if "2 Screens" in mode else (256, 192, 1)
    
    if OS_TYPE == "Windows":
        DRIVER_DIR = os.path.join(SCRIPT_DIR, "usbmmidd_v2")
        exe_name = "deviceinstaller64.exe" if platform.machine().endswith('64') else "deviceinstaller.exe"
        commands = [f'cd /d "%~dp0"', f'{exe_name} enableidd 0', f'{exe_name} enableidd 0', f'{exe_name} remove usbmmidd', 'timeout /t 1 /nobreak > NUL']
        commands.extend([f'{exe_name} install usbmmidd.inf usbmmidd', 'timeout /t 2 /nobreak > NUL'])
        reg_path = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\usbmmIdd\Parameters\Monitors"
        commands.extend([f'reg delete "{reg_path}" /f > NUL 2>&1', f'reg add "{reg_path}" /v 0 /t REG_SZ /d "{w},{h}" /f'])
        if count == 2: commands.append(f'reg add "{reg_path}" /v 1 /t REG_SZ /d "{w},{h}" /f')
        commands.append(f'reg add "{reg_path}" /ve /t REG_SZ /d "0" /f')
        for _ in range(count):
            commands.append(f'{exe_name} enableidd 1')
            if count > 1: commands.append('timeout /t 2 /nobreak > NUL')
        bat_path = os.path.join(DRIVER_DIR, "run_action.bat")
        with open(bat_path, "w") as f: f.write("\n".join(commands))
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
                    virtual_port = line.split()[0]; break
            subprocess.run(["xrandr", "--newmode", mode_name] + mode_params.split())
            subprocess.run(["xrandr", "--addmode", virtual_port, mode_name])
            subprocess.run(["xrandr", "--output", virtual_port, "--mode", mode_name, "--right-of", "eDP1"]) 
            return True
        except Exception as e:
            print(f"X11 Virtual Monitor failed: {e}")
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
        with open(bat_path, "w") as f: f.write(f'cd /d "%~dp0"\n{exe_name} enableidd 0\n{exe_name} enableidd 0')
        run_elevated_windows(bat_path, DRIVER_DIR)
    elif OS_TYPE == "Linux" and DISPLAY_SERVER == "X11":
        try: subprocess.run(["xrandr", "--output", "VIRTUAL1", "--off"])
        except: pass

def get_local_ips():
    ips = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips["Primary Network"] = s.getsockname()[0]
        s.close()
    except Exception: pass
    return ips

def get_sorted_ips():
    return [(name, ip) for name, ip in get_local_ips().items()]

# ── Automated Monitor Flow ─────────────────────────────────────
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
        
        # Wait for Windows to register the devices (Bypasses MSS coordinate failures)
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
            
        # Now wait for MSS to explicitly see monitors with the exact requested size
        found = False
        new_mss_indices = []
        for _ in range(20):
            with mss.MSS() as temp_sct:
                matched_indices = []
                for idx, m in enumerate(temp_sct.monitors):
                    if idx == 0: continue 
                    if m['width'] == w and m['height'] == h:
                        matched_indices.append(idx)
                
                if len(matched_indices) >= expected_new:
                    new_mss_indices = matched_indices[-expected_new:]
                    found = True
                    break
            time.sleep(0.5)
            
        if not found:
            print("Warning: Could not verify monitor sizes in MSS. Falling back to latest indices.")
            with mss.MSS() as temp_sct:
                new_mss_indices = [len(temp_sct.monitors) - 1]
                
        selected_indices = new_mss_indices
        created_monitor_indices = new_mss_indices.copy()
    else:
        # Mac/Linux Fallback
        with mss.MSS() as sct: monitors_before = len(sct.monitors)
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

# ── 6. Client & Network Handler ────────────────────────────────
def handle_client(conn, addr):
    global current_client, stream_mode, virtual_monitor_active, selected_indices, swapping_monitors
    
    client_info = {"conn": conn, "cmd_queue": queue.Queue(), "running": True}
    with current_client_lock: current_client = client_info
    sock_lock = threading.Lock()

    def recv_all(sock, n):
        data = bytearray()
        while len(data) < n and client_info["running"]:
            try:
                packet = sock.recv(n - len(data))
                if not packet: return None
                data.extend(packet)
            except: return None
        return bytes(data) if len(data) == n else None

    def recv_thread():
        try:
            while client_info["running"]:
                cmd_bytes = recv_all(conn, 1)
                if not cmd_bytes: break
                c = cmd_bytes[0]
                if c < 12: 
                    ds_keys = ["enter", "backspace", "esc", "win", "right", "left", "up", "down", "pagedown", "pageup", "space", "shift"]
                    client_info["cmd_queue"].put({"type": "KEYBOARD_PRESS", "key": ds_keys[c]})
                elif c == 0xFC: 
                    count_bytes = recv_all(conn, 1)
                    if not count_bytes: break
                    count = count_bytes[0]
                    if count > 0:
                        data = recv_all(conn, count * 4)
                        if recv_all(conn, 1) == b'\xFD':
                            px, py = (data[-4] << 8) | data[-3], (data[-2] << 8) | data[-1]
                            client_info["cmd_queue"].put({"type": "TAP", "x": px, "y": py})
                    else: recv_all(conn, 1)
        except Exception: pass
        finally: client_info["cmd_queue"].put({"type": "QUIT"})

    threading.Thread(target=recv_thread, daemon=True).start()

    if app_gui: app_gui.log_message("DS Connected! Preparing stream...")
    
    swapping_monitors = True 
    
    def spawn_monitors_task():
        global swapping_monitors
        if not auto_add_monitors():
            if app_gui: app_gui.log_message("Monitor spawn failed. Disconnecting.")
            client_info["running"] = False
        else:
            if app_gui: app_gui.log_message("Stream Active.")
        swapping_monitors = False
        
    threading.Thread(target=spawn_monitors_task, daemon=True).start()

    sct = mss.MSS()
    server_frame_state = None 
    last_frame_time = time.monotonic()
    FRAME_INTERVAL = 0.033 
    was_swapping = False 

    def get_frame_data():
        try:
            if not selected_indices: return None
            
            if "Primary Screen" in stream_mode or "Stream Monitor" in stream_mode:
                idx = selected_indices[0] if selected_indices else 1
                idx = min(idx, len(sct.monitors) - 1)
                mon = sct.monitors[idx]
                img = draw_cursor(Image.frombytes("RGB", sct.grab(mon).size, sct.grab(mon).bgra, "raw", "BGRX"), mon)
                return img.resize((256, 192), Image.Resampling.LANCZOS)
            
            if len(sct.monitors) <= max(selected_indices): return None

            if "2 Screens" in stream_mode:
                idx1 = selected_indices[0] if len(selected_indices) > 0 else 1
                idx2 = selected_indices[1] if len(selected_indices) > 1 else idx1
                idx1 = min(idx1, len(sct.monitors) - 1)
                idx2 = min(idx2, len(sct.monitors) - 1)
                
                mon1, mon2 = sct.monitors[idx1], sct.monitors[idx2] 
                img1 = draw_cursor(Image.frombytes("RGB", sct.grab(mon1).size, sct.grab(mon1).bgra, "raw", "BGRX"), mon1).resize((256, 192), Image.Resampling.LANCZOS)
                img2 = draw_cursor(Image.frombytes("RGB", sct.grab(mon2).size, sct.grab(mon2).bgra, "raw", "BGRX"), mon2).resize((256, 192), Image.Resampling.LANCZOS)
                canvas = Image.new('RGB', (256, 384))
                canvas.paste(img1, (0, 0))
                canvas.paste(img2, (0, 192))
                return canvas
            else:
                idx = selected_indices[0] if selected_indices else 1
                idx = min(idx, len(sct.monitors) - 1)
                mon = sct.monitors[idx]
                img = draw_cursor(Image.frombytes("RGB", sct.grab(mon).size, sct.grab(mon).bgra, "raw", "BGRX"), mon)
                return img.resize((256, 384), Image.Resampling.LANCZOS) if "Giant" in stream_mode else img.resize((256, 192), Image.Resampling.LANCZOS)
        except Exception: return None

    def send_frame_data(w, h, payload):
        with sock_lock:
            try:
                conn.settimeout(1.5) 
                header = struct.pack('>BHH', 0x03, w, h) + struct.pack('>I', len(payload))
                conn.sendall(header + payload + b'\xFE')
                conn.settimeout(None)
            except Exception as e:
                print(f"Network choke detected, dropping client to prevent DS crash: {e}")
                client_info["running"] = False

    try:
        while client_info["running"]:
            if not swapping_monitors and not virtual_monitor_active and "Primary Screen" not in stream_mode and "Stream Monitor" not in stream_mode: 
                break
                
            commands = []
            try:
                commands.append(client_info["cmd_queue"].get(timeout=0.01))
                while True:
                    try: commands.append(client_info["cmd_queue"].get_nowait())
                    except queue.Empty: break
            except queue.Empty: pass

            for cmd in commands:
                if cmd["type"] == "QUIT":
                    client_info["running"] = False
                    break
                elif cmd["type"] == "TAP":
                    try:
                        if swapping_monitors or not selected_indices: continue
                        if "Primary Screen" in stream_mode or "Stream Monitor" in stream_mode:
                            idx = selected_indices[0] if selected_indices else 1
                            idx = min(idx, len(sct.monitors) - 1)
                            mon = sct.monitors[idx]
                            target_x = mon["left"] + int((cmd["x"]/256.0)*mon["width"])
                            target_y = mon["top"] + int((cmd["y"]/192.0)*mon["height"])
                        elif "2 Screens" in stream_mode:
                            idx2 = selected_indices[1] if len(selected_indices) > 1 else (selected_indices[0] if selected_indices else 1)
                            idx2 = min(idx2, len(sct.monitors) - 1)
                            mon = sct.monitors[idx2]
                            target_x, target_y = mon["left"] + cmd["x"], mon["top"] + cmd["y"]
                        elif "Giant" in stream_mode:
                            idx = selected_indices[0] if selected_indices else 1
                            idx = min(idx, len(sct.monitors) - 1)
                            mon = sct.monitors[idx]
                            target_x, target_y = mon["left"] + cmd["x"], mon["top"] + (cmd["y"] + 192)
                        else:
                            idx = selected_indices[0] if selected_indices else 1
                            idx = min(idx, len(sct.monitors) - 1)
                            mon = sct.monitors[idx]
                            target_x, target_y = mon["left"] + cmd["x"], mon["top"] + cmd["y"]
                        pyautogui.click(x=target_x, y=target_y)
                    except Exception: pass
                elif cmd["type"] == "KEYBOARD_PRESS":
                    pyautogui.press(cmd["key"])

            if not client_info["running"]: break

            now = time.monotonic()
            
            if swapping_monitors:
                if now - last_frame_time >= FRAME_INTERVAL:
                    with sock_lock:
                        try: conn.sendall(b'\xFE')
                        except: pass
                    last_frame_time = time.monotonic() 
                was_swapping = True
                
            else:
                if was_swapping:
                    sct.close()
                    sct = mss.MSS()
                    server_frame_state = None
                    
                    wipe_img = Image.new('RGB', (256, 384), (0, 0, 0))
                    payload, _, _, _ = encode_quadtree_fast_wrapper(wipe_img, None)
                    if len(payload) > 3:
                        send_frame_data(256, 384, payload)
                        
                    was_swapping = False
                    last_frame_time = time.monotonic()
                    continue 

                if now - last_frame_time >= FRAME_INTERVAL:
                    img = get_frame_data()
                    if img is not None:
                        payload, server_frame_state, orig_w, orig_h = encode_quadtree_fast_wrapper(img, server_frame_state)
                        if len(payload) > 3:
                            send_frame_data(orig_w, orig_h, payload)
                        else:
                            with sock_lock:
                                try: conn.sendall(b'\xFE')
                                except: pass
                        last_frame_time = time.monotonic() 
    finally:
        with current_client_lock: current_client = None
        sct.close()
        conn.close()
        auto_remove_monitors()
        if app_gui: app_gui.log_message("DS Disconnected. Cleaned up.")

def start_server():
    global server_running, server_socket, accept_thread
    if server_running: return
    auto_remove_monitors() 
    server_running, accept_thread = True, threading.Thread(target=accept_loop, daemon=True)
    accept_thread.start()
    update_server_socket(current_port)

def stop_server():
    global server_running, server_socket, current_client
    server_running = False
    with server_socket_lock:
        if server_socket:
            try: server_socket.close()
            except: pass
            server_socket = None
    with current_client_lock:
        if current_client:
            current_client["running"] = False
            try: current_client["conn"].close()
            except: pass
            current_client = None
    
    auto_remove_monitors()
    if app_gui: app_gui.log_message("Server stopped. Cleaned up.")

def update_server_socket(port):
    global server_socket
    with server_socket_lock:
        if server_socket:
            try: server_socket.close()
            except: pass
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.listen(1)
            sock.settimeout(0.5)
            server_socket = sock
        except Exception as e: server_socket = None

def accept_loop():
    global server_running, server_socket
    while server_running:
        with server_socket_lock: sock = server_socket
        if sock is None:
            time.sleep(0.1)
            continue
        try:
            conn, addr = sock.accept()
            conn.settimeout(None)
            with current_client_lock:
                if current_client is not None:
                    conn.close()
                    continue
                    
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384) 
            
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except socket.timeout: continue
        except Exception: pass

# ── 7. Dynamic GUI ──────────────────────────────────────────────
class ServerGUI:
    def __init__(self, root):
        self.root = root
        root.title("DS Monitor Server")
        root.geometry("420x550")
        root.resizable(False, False)

        tk.Label(root, text="Server IP Address", font=("Arial", 10, "bold")).pack(pady=(10,0))
        self.ip_listbox = tk.Listbox(root, height=3, selectmode=tk.SINGLE)
        self.ip_listbox.pack(fill=tk.X, padx=10, pady=5)
        self.refresh_ips()

        tk.Label(root, text=f"Detected System: {OS_TYPE} ({DISPLAY_SERVER})", font=("Arial", 9, "italic"), fg="blue").pack(pady=5)
        
        self.stream_mode_var = tk.BooleanVar(value=(DISPLAY_SERVER == "Wayland"))
        
        # Monitor/Streaming Settings based on Display Server
        if DISPLAY_SERVER == "Wayland":
            tk.Label(root, text="Streaming Settings", font=("Arial", 10, "bold")).pack(pady=(5,0))
            self.mode_var = tk.StringVar(value="Primary Screen (Auto-Scaled Fallback)")
            self.mode_combo = ttk.Combobox(root, textvariable=self.mode_var, width=35)
            self.mode_combo["values"] = ["Primary Screen (Auto-Scaled Fallback)"]
            self.mode_combo.current(0)
            self.mode_combo.config(state="disabled")
            self.mode_combo.pack(fill=tk.X, padx=10, pady=5)
        else:
            tk.Label(root, text="Monitor Settings", font=("Arial", 10, "bold")).pack(pady=(5,0))
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

        frame_port = tk.Frame(root)
        frame_port.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(frame_port, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        tk.Entry(frame_port, textvariable=self.port_var, width=8).pack(side=tk.LEFT, padx=5)

        # Server Settings & Logs
        tk.Label(root, text="Server Settings", font=("Arial", 10, "bold")).pack(pady=(10,0))
        
        self.log_box = tk.Text(root, height=4, state=tk.DISABLED, bg="#f9f9f9", font=("Courier", 9))
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
            self.log_box.see(tk.END)
            self.log_box.config(state=tk.DISABLED)
        self.root.after(0, append)

    def refresh_ips(self):
        self.ip_listbox.delete(0, tk.END)
        for name, ip in get_sorted_ips(): self.ip_listbox.insert(tk.END, f"{name}: {ip}")

    def trigger_monitor_swap(self):
        global virtual_monitor_active, swapping_monitors
        if virtual_monitor_active:
            def swap_task():
                global swapping_monitors
                swapping_monitors = True
                self.log_message("Pausing stream to change mode...")
                
                auto_remove_monitors()
                time.sleep(1) 
                
                if auto_add_monitors():
                    self.log_message("Mode changed. Stream resumed.")
                else:
                    self.log_message("Mode swap failed. Stream halted.")
                swapping_monitors = False
            threading.Thread(target=swap_task, daemon=True).start()

    def on_mode_select(self, event):
        global stream_mode
        new_mode = self.mode_var.get()
        if stream_mode != new_mode:
            stream_mode = new_mode
            self.trigger_monitor_swap()

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
            new_values = list(default_values)
            try:
                with mss.MSS() as sct:
                    for i, mon in enumerate(sct.monitors[1:], 1):
                        # Filter out ONLY the monitors we mapped as virtual ones
                        if i not in created_monitor_indices:
                            new_values.append(f"Stream Monitor {i} ({mon['width']}x{mon['height']})")
            except Exception: pass
            
            self.mode_combo["values"] = new_values
            self.mode_combo.config(state="readonly") 
        else:
            self.mode_combo["values"] = default_values
            if self.mode_var.get() not in default_values:
                self.mode_combo.current(0)
                
        new_mode = self.mode_var.get()
        if stream_mode != new_mode:
            stream_mode = new_mode
            self.trigger_monitor_swap()

    def toggle_server(self):
        global current_port
        if server_running:
            stop_server()
            self.start_btn.config(text="Start Server")
            self.status_var.set("Stopped")
        else:
            try: port = int(self.port_var.get())
            except ValueError: return
            current_port = port
            start_server()
            self.start_btn.config(text="Stop Server")
            self.status_var.set(f"Server running on port {port} – waiting for DS...")

    def update_client_status(self):
        with current_client_lock: client = current_client
        if client: 
            self.client_status_var.set("Nintendo DS Connected!")
        else: 
            self.client_status_var.set("Waiting for Nintendo DS...")
        self.root.after(1000, self.update_client_status)

if __name__ == "__main__":
    if OS_TYPE == "Windows" and not prepare_driver_files():
        sys.exit(0)
    root = tk.Tk()
    app_gui = ServerGUI(root)
    root.mainloop()