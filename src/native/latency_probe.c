/*
 * latency_probe.c - Sub-millisecond Kernel PSI, Schedstat & IRQ Latency Sampler
 * Pairs with bpf/sched_latency.bpf.o for micro-stutter root-cause attribution.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <ctype.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct {
    double some_avg10;
    double full_avg10;
    uint64_t some_total_us;
    uint64_t full_total_us;
} PsiStat;

typedef struct {
    int pid;
    char comm[32];
    uint64_t wait_ns0;
    uint64_t slices0;
    double runq_delay_ms;
    uint64_t ctx_switches;
} SchedProc;

typedef struct {
    char irq[24];
    char name[48];
    uint64_t count0;
    uint64_t delta;
} IrqStat;

static bool read_psi(const char *path, PsiStat *out) {
    memset(out, 0, sizeof(*out));
    FILE *f = fopen(path, "r");
    if (!f) return false;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "some ", 5) == 0) {
            sscanf(line, "some avg10=%lf avg60=%*s avg300=%*s total=%llu",
                   &out->some_avg10, (unsigned long long *)&out->some_total_us);
        } else if (strncmp(line, "full ", 5) == 0) {
            sscanf(line, "full avg10=%lf avg60=%*s avg300=%*s total=%llu",
                   &out->full_avg10, (unsigned long long *)&out->full_total_us);
        }
    }
    fclose(f);
    return true;
}

static bool read_proc_sched(int pid, char *comm_out, size_t comm_sz, uint64_t *wait_ns, uint64_t *slices) {
    char path[128], buf[256];
    snprintf(path, sizeof(path), "/proc/%d/schedstat", pid);
    int fd = open(path, O_RDONLY);
    if (fd < 0) return false;
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return false;
    buf[n] = '\0';
    unsigned long long run_t = 0, wt = 0, sl = 0;
    if (sscanf(buf, "%llu %llu %llu", &run_t, &wt, &sl) != 3) return false;
    *wait_ns = (uint64_t)wt;
    *slices = (uint64_t)sl;

    if (comm_out && comm_sz > 0) {
        snprintf(path, sizeof(path), "/proc/%d/comm", pid);
        fd = open(path, O_RDONLY);
        if (fd < 0) {
            snprintf(comm_out, comm_sz, "pid:%d", pid);
        } else {
            n = read(fd, comm_out, comm_sz - 1);
            close(fd);
            if (n > 0) {
                comm_out[n] = '\0';
                while (n > 0 && (comm_out[n - 1] == '\n' || comm_out[n - 1] == ' ')) {
                    comm_out[--n] = '\0';
                }
            } else {
                snprintf(comm_out, comm_sz, "pid:%d", pid);
            }
        }
    }
    return true;
}

static size_t read_irq_counts(IrqStat *arr, size_t cap) {
    FILE *f = fopen("/proc/interrupts", "r");
    if (!f) return 0;
    char line[2048];
    if (!fgets(line, sizeof(line), f)) { fclose(f); return 0; } /* header */
    size_t ncpu = 0;
    char *tok_p = line;
    while (*tok_p) {
        while (*tok_p && isspace((unsigned char)*tok_p)) tok_p++;
        if (!*tok_p) break;
        ncpu++;
        while (*tok_p && !isspace((unsigned char)*tok_p)) tok_p++;
    }
    if (ncpu == 0) ncpu = 1;

    size_t n = 0;
    while (fgets(line, sizeof(line), f) && n < cap) {
        char *colon = strchr(line, ':');
        if (!colon) continue;
        *colon = '\0';
        char *id = line;
        while (*id && isspace((unsigned char)*id)) id++;
        uint64_t sum = 0;
        char *p = colon + 1;
        for (size_t c = 0; c < ncpu; c++) {
            while (*p && isspace((unsigned char)*p)) p++;
            if (!isdigit((unsigned char)*p)) break;
            sum += strtoull(p, &p, 10);
        }
        if (sum == 0) continue;
        char *desc = p;
        while (*desc && isspace((unsigned char)*desc)) desc++;
        size_t dlen = strlen(desc);
        while (dlen > 0 && (desc[dlen - 1] == '\n' || isspace((unsigned char)desc[dlen - 1]))) desc[--dlen] = '\0';
        snprintf(arr[n].irq, sizeof(arr[n].irq), "%.23s", id);
        snprintf(arr[n].name, sizeof(arr[n].name), "%.47s", dlen ? desc : id);
        arr[n].count0 = sum;
        arr[n].delta = 0;
        n++;
    }
    fclose(f);
    return n;
}

static int cmp_proc_desc(const void *a, const void *b) {
    const SchedProc *pa = (const SchedProc *)a;
    const SchedProc *pb = (const SchedProc *)b;
    if (pb->runq_delay_ms > pa->runq_delay_ms) return 1;
    if (pb->runq_delay_ms < pa->runq_delay_ms) return -1;
    return 0;
}

static int cmp_irq_desc(const void *a, const void *b) {
    const IrqStat *ia = (const IrqStat *)a;
    const IrqStat *ib = (const IrqStat *)b;
    if (ib->delta > ia->delta) return 1;
    if (ib->delta < ia->delta) return -1;
    return 0;
}

static void json_esc(const char *s, char *out, size_t sz) {
    size_t j = 0;
    for (size_t i = 0; s && s[i] && j + 2 < sz; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') {
            if (j + 2 >= sz) break;
            out[j++] = '\\'; out[j++] = (char)c;
        } else if (c >= 0x20) {
            out[j++] = (char)c;
        }
    }
    out[j] = '\0';
}

int main(int argc, char *argv[]) {
    bool as_json = false;
    int sample_ms = 250;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--json") == 0 || strcmp(argv[i], "-j") == 0) as_json = true;
        else if (strcmp(argv[i], "--sample-ms") == 0 && i + 1 < argc) {
            sample_ms = atoi(argv[++i]);
            if (sample_ms < 50) sample_ms = 50;
            if (sample_ms > 2000) sample_ms = 2000;
        }
    }

    PsiStat cpu0, io0, mem0, cpu1, io1, mem1;
    read_psi("/proc/pressure/cpu", &cpu0);
    read_psi("/proc/pressure/io", &io0);
    read_psi("/proc/pressure/memory", &mem0);

    const size_t MAXP = 1024;
    SchedProc *procs = calloc(MAXP, sizeof(SchedProc));
    size_t nproc = 0;
    DIR *pd = opendir("/proc");
    if (pd && procs) {
        struct dirent *de;
        while ((de = readdir(pd)) != NULL && nproc < MAXP) {
            if (!isdigit((unsigned char)de->d_name[0])) continue;
            int pid = atoi(de->d_name);
            if (pid <= 1) continue;
            if (read_proc_sched(pid, procs[nproc].comm, sizeof(procs[nproc].comm),
                                &procs[nproc].wait_ns0, &procs[nproc].slices0)) {
                procs[nproc].pid = pid;
                nproc++;
            }
        }
        closedir(pd);
    }

    const size_t MAXI = 128;
    IrqStat *irqs0 = calloc(MAXI, sizeof(IrqStat));
    IrqStat *irqs1 = calloc(MAXI, sizeof(IrqStat));
    size_t ni0 = irqs0 ? read_irq_counts(irqs0, MAXI) : 0;

    usleep((useconds_t)sample_ms * 1000U);

    read_psi("/proc/pressure/cpu", &cpu1);
    read_psi("/proc/pressure/io", &io1);
    read_psi("/proc/pressure/memory", &mem1);
    size_t ni1 = irqs1 ? read_irq_counts(irqs1, MAXI) : 0;

    for (size_t i = 0; i < ni1; i++) {
        for (size_t j = 0; j < ni0; j++) {
            if (strcmp(irqs1[i].irq, irqs0[j].irq) == 0) {
                irqs1[i].delta = (irqs1[i].count0 >= irqs0[j].count0) ? (irqs1[i].count0 - irqs0[j].count0) : 0;
                break;
            }
        }
    }
    qsort(irqs1, ni1, sizeof(IrqStat), cmp_irq_desc);

    size_t active_p = 0;
    double max_delay_ms = 0.0;
    double total_delay_ms = 0.0;
    for (size_t i = 0; i < nproc; i++) {
        uint64_t w1 = 0, s1 = 0;
        if (!read_proc_sched(procs[i].pid, NULL, 0, &w1, &s1)) continue;
        uint64_t dw = (w1 >= procs[i].wait_ns0) ? (w1 - procs[i].wait_ns0) : 0;
        uint64_t ds = (s1 >= procs[i].slices0) ? (s1 - procs[i].slices0) : 0;
        double dms = (double)dw / 1e6;
        if (dms < 0.01 && ds < 2) continue;
        procs[active_p] = procs[i];
        procs[active_p].runq_delay_ms = dms;
        procs[active_p].ctx_switches = ds;
        if (dms > max_delay_ms) max_delay_ms = dms;
        total_delay_ms += dms;
        active_p++;
    }
    qsort(procs, active_p, sizeof(SchedProc), cmp_proc_desc);

    const char *verdict = "HEALTHY";
    double cpu_psi_ms = (double)(cpu1.some_total_us >= cpu0.some_total_us ? cpu1.some_total_us - cpu0.some_total_us : 0) / 1000.0;
    double io_psi_ms = (double)(io1.some_total_us >= io0.some_total_us ? io1.some_total_us - io0.some_total_us : 0) / 1000.0;
    double mem_psi_ms = (double)(mem1.some_total_us >= mem0.some_total_us ? mem1.some_total_us - mem0.some_total_us : 0) / 1000.0;
    uint64_t top_irq_rate = (ni1 > 0) ? (irqs1[0].delta * 1000ULL / (uint64_t)sample_ms) : 0;

    if (mem_psi_ms > 15.0 || mem1.full_avg10 > 2.0) verdict = "MEMORY_RECLAIM_STALL";
    else if (io_psi_ms > 18.0 || io1.full_avg10 > 3.0) verdict = "NVME_IO_STALL";
    else if (top_irq_rate > 12000) verdict = "IRQ_STORM";
    else if (max_delay_ms > 18.0 || cpu1.some_avg10 > 12.0) verdict = "CPU_RUNQ_CONTENTION";

    bool bpf_ready = (access("bpf/sched_latency.bpf.o", F_OK) == 0);

    if (as_json) {
        printf("{\n"
               "  \"sample_ms\": %d,\n"
               "  \"bpf_object_ready\": %s,\n"
               "  \"verdict\": \"%s\",\n"
               "  \"max_runq_delay_ms\": %.2f,\n"
               "  \"total_runq_delay_ms\": %.2f,\n"
               "  \"psi\": {\n"
               "    \"cpu_avg10\": %.2f, \"cpu_stall_ms\": %.2f,\n"
               "    \"io_avg10\": %.2f, \"io_stall_ms\": %.2f,\n"
               "    \"mem_avg10\": %.2f, \"mem_stall_ms\": %.2f\n"
               "  },\n  \"top_delayed\": [",
               sample_ms, bpf_ready ? "true" : "false", verdict,
               max_delay_ms, total_delay_ms,
               cpu1.some_avg10, cpu_psi_ms, io1.some_avg10, io_psi_ms, mem1.some_avg10, mem_psi_ms);
        size_t tp = active_p < 6 ? active_p : 6;
        for (size_t i = 0; i < tp; i++) {
            char esc[96];
            json_esc(procs[i].comm, esc, sizeof(esc));
            printf("%s\n    {\"pid\": %d, \"name\": \"%s\", \"runq_delay_ms\": %.2f, \"switches\": %llu}",
                   i == 0 ? "" : ",", procs[i].pid, esc, procs[i].runq_delay_ms, (unsigned long long)procs[i].ctx_switches);
        }
        printf("\n  ],\n  \"top_irqs\": [");
        size_t ti = ni1 < 5 ? ni1 : 5;
        for (size_t i = 0; i < ti; i++) {
            char esc[128];
            json_esc(irqs1[i].name, esc, sizeof(esc));
            uint64_t rate = irqs1[i].delta * 1000ULL / (uint64_t)sample_ms;
            printf("%s\n    {\"irq\": \"%s\", \"source\": \"%s\", \"per_sec\": %llu}",
                   i == 0 ? "" : ",", irqs1[i].irq, esc, (unsigned long long)rate);
        }
        printf("\n  ]\n}\n");
    } else {
        printf("=== eBPF Latency Guard Probe (%d ms) ===\n", sample_ms);
        printf("  Verdict        : %s (eBPF CO-RE obj: %s)\n", verdict, bpf_ready ? "compiled" : "not found");
        printf("  Max Runq Delay : %.2f ms (Total waited: %.2f ms)\n", max_delay_ms, total_delay_ms);
        printf("  PSI Avg10      : CPU %.2f%% | IO %.2f%% | Memory %.2f%%\n", cpu1.some_avg10, io1.some_avg10, mem1.some_avg10);
        printf("\n  Top Scheduler-Delayed Tasks:\n");
        size_t tp = active_p < 6 ? active_p : 6;
        for (size_t i = 0; i < tp; i++) {
            printf("    %-7d %-18s %7.2f ms wait (%llu switches)\n",
                   procs[i].pid, procs[i].comm, procs[i].runq_delay_ms, (unsigned long long)procs[i].ctx_switches);
        }
        printf("\n  Top Active IRQs (/sec):\n");
        size_t ti = ni1 < 5 ? ni1 : 5;
        for (size_t i = 0; i < ti; i++) {
            printf("    %-8s %-30s %llu/s\n", irqs1[i].irq, irqs1[i].name,
                   (unsigned long long)(irqs1[i].delta * 1000ULL / (uint64_t)sample_ms));
        }
    }

    free(procs);
    free(irqs0);
    free(irqs1);
    return 0;
}
