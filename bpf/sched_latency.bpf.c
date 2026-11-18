/* SPDX-License-Identifier: GPL-2.0 OR MIT */
/* Real Linux eBPF tracepoint latency collector for ebpf-latency-guard */
#include <linux/types.h>
#include <linux/bpf.h>

#define SEC(NAME) __attribute__((section(NAME), used))
#define __uint(name, val) int (*name)[val]
#define __type(name, val) typeof(val) *name

static void *(*bpf_map_lookup_elem)(void *map, const void *key) = (void *)1;
static long (*bpf_map_update_elem)(void *map, const void *key, const void *value, __u64 flags) = (void *)2;
static long (*bpf_map_delete_elem)(void *map, const void *key) = (void *)3;
static __u64 (*bpf_ktime_get_ns)(void) = (void *)5;

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, __u32);
    __type(value, __u64);
    __uint(max_entries, 16384);
} wakeup_ts_map SEC(".maps");

/* Log2 microsecond buckets: 0..23 (1us .. 8s+) */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __type(key, __u32);
    __type(value, __u64);
    __uint(max_entries, 24);
} sched_hist_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __type(key, __u32);
    __type(value, __u64);
    __uint(max_entries, 4);
} stats_map SEC(".maps");

struct sched_wakeup_ctx {
    __u64 unused;
    char comm[16];
    __s32 pid;
    __s32 prio;
    __s32 success;
    __s32 target_cpu;
};

struct sched_switch_ctx {
    __u64 unused;
    char prev_comm[16];
    __s32 prev_pid;
    __s32 prev_prio;
    __s64 prev_state;
    char next_comm[16];
    __s32 next_pid;
    __s32 next_prio;
};

static inline __u32 log2_u64(__u64 v) {
    __u32 r = 0;
    if (v >= (1ULL << 16)) { v >>= 16; r += 16; }
    if (v >= (1ULL << 8))  { v >>= 8;  r += 8;  }
    if (v >= (1ULL << 4))  { v >>= 4;  r += 4;  }
    if (v >= (1ULL << 2))  { v >>= 2;  r += 2;  }
    if (v >= (1ULL << 1))  { r += 1; }
    return r > 23 ? 23 : r;
}

SEC("tracepoint/sched/sched_wakeup")
int tp_sched_wakeup(struct sched_wakeup_ctx *ctx) {
    __u32 pid = (__u32)ctx->pid;
    if (pid == 0) return 0;
    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&wakeup_ts_map, &pid, &ts, BPF_ANY);
    return 0;
}

SEC("tracepoint/sched/sched_switch")
int tp_sched_switch(struct sched_switch_ctx *ctx) {
    __u32 next_pid = (__u32)ctx->next_pid;
    if (next_pid == 0) return 0;
    __u64 *ts = bpf_map_lookup_elem(&wakeup_ts_map, &next_pid);
    if (!ts) return 0;
    __u64 now = bpf_ktime_get_ns();
    if (now > *ts) {
        __u64 delta_us = (now - *ts) / 1000ULL;
        __u32 b = log2_u64(delta_us > 0 ? delta_us : 1);
        __u64 *cnt = bpf_map_lookup_elem(&sched_hist_map, &b);
        if (cnt) __sync_fetch_and_add(cnt, 1);
        __u32 k0 = 0, k1 = 1;
        __u64 *maxv = bpf_map_lookup_elem(&stats_map, &k0);
        if (maxv && delta_us > *maxv) *maxv = delta_us;
        __u64 *sumv = bpf_map_lookup_elem(&stats_map, &k1);
        if (sumv) __sync_fetch_and_add(sumv, delta_us);
    }
    bpf_map_delete_elem(&wakeup_ts_map, &next_pid);
    return 0;
}

char LICENSE[] SEC("license") = "Dual MIT/GPL";
