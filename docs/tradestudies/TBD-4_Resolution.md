# TBD-4 Resolution: NAS Throughput Benchmark Results and Architecture Decision

**Date:** 2026-03-20  
**Owner:** Engineering  
**Status:** Resolved  
**Decision:** SSD-primary runtime storage + NAS archival tier

---

## Benchmark results

**Date:** 2026-03-20 16:45 UTC  
**Environment:** TrueNAS, ~8 GB RAM (4 GB ZFS ARC), spinning HDDs, direct-attached to Proxmox host via 10.0.0.20, NFS v3 mount (rsize=131072, wsize=131072 — server-negotiated from requested 1048576).

| # | Test | ATP Workload | Result | Threshold | Verdict |
|---|------|-------------|--------|-----------|---------|
| 1 | Sequential read | Backtesting dataset load (SYS-14) | 112.3 MB/s | ≥ 80 MB/s | **PASS** |
| 2 | Sequential write | Nightly ingestion (SYS-22a, SYS-23) | 16.3 MB/s | ≥ 50 MB/s | **FAIL** |
| 3 | Random read IOPS | Symbol lookups (SYS-9, SYS-27) | 471 IOPS / 2.12 ms avg / 35.39 ms p99 | ≥ 200 IOPS | **PASS** |
| 4 | Concurrent reads (60 workers) | Market open — all containers active (NFR-P10) | 116 IOPS / 7.2 MB/s / **513 ms avg latency** | ≥ 150 IOPS | **FAIL** |
| 5 | Mixed read/write (70/30) | Ingestion while strategies read (SYS-63) | 3.8 MB/s read / 1.7 MB/s write | ≥ 40 MB/s read | **FAIL** |
| 6 | Factor pipeline pattern | 100 sequential file reads (SYS-32) | 696.9 MB/s (ARC-cached) | Informational | N/A |

**Overall: 2 PASS, 3 FAIL, 1 informational. Verdict: NAS insufficient as sole storage tier.**

### Analysis of key results

**Test 4 (concurrent reads) is the decisive failure.** At 60 concurrent workers, aggregate throughput collapsed to 7.2 MB/s with an average read latency of **513 ms per request**. This means each strategy container waits over half a second for a single data read when all containers are active simultaneously (e.g., at market open). Since NFR-P1 allocates the entire order signal-to-acknowledgement budget at 1,000 ms, a 513 ms data read leaves only 487 ms for communication channel traversal, execution engine processing, and IB Gateway submission — an unacceptably thin margin that will produce p95 violations under load.

**Test 5 (mixed read/write) confirms concurrent write contention.** When nightly ingestion overlaps with strategy container reads (which can occur if ingestion starts before all strategies finish post-close processing), read throughput drops to 3.8 MB/s — a 97% reduction from the 112 MB/s sequential baseline. This confirms that SYS-63 (concurrent read/write without blocking) cannot be satisfied by NAS alone.

**Test 3 (single-worker random reads) passed at 471 IOPS.** This demonstrates adequate performance for low-concurrency scenarios (e.g., a single backtest or Jupyter session). The NAS is suitable for workloads that do not compete with 60 simultaneous containers.

**Test 6 (factor pipeline) result of 696.9 MB/s is ARC-cached.** The 100 × 10 MB test files (1 GB total) fit entirely within the 4 GB ZFS ARC. In production, the factor pipeline loads daily bars for 8,000+ securities (~500 MB–1 GB), which should also fit in ARC for sequential batch processing. The factor pipeline's serial access pattern is ARC-friendly — this workload may perform acceptably from NAS even without SSD caching.

**Sequential write at 16.3 MB/s** is caused by NFS sync flush behavior on ZFS without a SLOG device. The NFS mount also shows rsize/wsize negotiated down to 128K (from the requested 1 MB), which may further constrain sequential throughput. However, this does not impact ATP operations because nightly ingestion write volume is modest (~1–2 GB/night) and the actual bottleneck is IB pacing limits, not I/O throughput.

### NFS rsize/wsize note

The mount info shows `rsize=131072,wsize=131072` despite `rsize=1048576,wsize=1048576` being specified in fstab. The NFS v3 server (TrueNAS) negotiated the values down to 128K. This should be investigated in TrueNAS NFS share settings (Services → NFS → check if a maximum read/write size is configured). Raising this to 1 MB may improve sequential throughput but will not resolve the concurrent random read failure, which is caused by HDD seek contention.

---

## Architecture decision

**SSD as primary runtime storage, NAS as archival and source-of-truth.**

The benchmark results placed the NAS firmly in the "insufficient" category (3 of 5 tests failed), not the "marginal/tiered" category. The 513 ms concurrent read latency is not a borderline miss — it is fundamentally incompatible with the ATP's latency requirements. The architecture therefore treats the SSD as the primary read/write target for all runtime operations, with the NAS serving as the archival tier and backup target.

### Primary tier: Local 1 TB SSD (Proxmox host)

Purpose: All runtime read and write I/O for latency-sensitive consumers.

Contents:
- Recent bar data (last 90 days, daily + minute) for all securities with active strategies or on the minute-bar watchlist
- Factor pipeline input data (full universe daily bars, most recent snapshot)
- Backtest working sets (loaded on demand, evicted after completion)
- Warm-up data for strategy startup (SYS-8)
- Active strategy state persistence (NFR-R3)
- System and strategy logs (SYS-38, SYS-61)
- Nightly ingestion staging (writes land on SSD first, then sync to NAS)

Estimated steady-state usage: 100–300 GB (well within 1 TB capacity)

### Archival tier: 20 TB NAS (TrueNAS, direct-attached)

Purpose: Long-term source of truth and archival storage. Not accessed during runtime operations under normal conditions.

Contents:
- Full historical archive (all daily bars, all minute bars, all option chain snapshots)
- Sharadar fundamental data (full history)
- Databento historical options imports
- User-uploaded Parquet files
- Completed backtest results and trade logs
- Nightly sync from SSD (the NAS copy is the durable archive)

### Data flow

```
Nightly ingestion (SYS-22a/b, SYS-23, SYS-26)
    │
    ▼
  SSD (write — primary runtime storage)
    │
    ├── Strategy containers (60x) read bar data, indicators
    ├── Factor pipeline reads full universe daily bars
    ├── Backtesting engine reads historical data
    └── Jupyter research reads datasets
    │
    ▼
  NAS sync (post-ingestion background job)
    │  - Copies newly ingested data from SSD to NAS
    │  - NAS holds the complete durable archive
    │  - Runs at lowest priority in workload hierarchy (SYS-57)
    │
    ▼
  NAS (archival source of truth)

Cold-read path (infrequent):
  If a strategy or backtest requests data older than the SSD retention
  window (e.g., 10-year backtest), the data layer reads from NAS and
  caches the result on SSD. Sequential NAS reads (112 MB/s) are adequate
  for bulk historical loads that occur outside of peak trading hours.
```

### Storage management policy

- **Ingestion writes:** All nightly ingestion writes land on the SSD first.
  A post-ingestion sync job copies newly ingested data to the NAS at lowest
  workload priority (SYS-57). The NAS archive is the durable copy.
- **SSD retention window:** The SSD retains the most recent N days
  (configurable, default 90) of bar data for all securities, plus the full
  working set for any active backtest or Jupyter session. Factor pipeline
  inputs (latest daily snapshot for full universe) are always on SSD.
- **Eviction:** When SSD usage exceeds a configurable high-water mark (default
  80% = 800 GB), the storage manager evicts the oldest data by date, starting
  with securities not on the active strategy list or watchlist. Data for
  securities with running live strategy containers is never evicted.
- **Cold reads:** Read requests for data outside the SSD retention window
  are served from NAS. The sequential NAS read speed (112 MB/s) is adequate
  for bulk historical loads (e.g., 10-year backtests) that typically occur
  outside peak trading hours. Cold-read results are cached on SSD.
- **Durability:** The NAS archive is the source of truth. If the SSD fails,
  all data is recoverable from NAS (data loss limited to in-flight ingestion
  not yet synced — at most one night's data, re-runnable).

---

## SyRS impact

### Revised requirements

**NFR-SC2 — revised:**
> The data layer shall accommodate the full US equity universe (8,000+
> securities) at daily and minute-bar resolution, plus option chain snapshots
> and Sharadar fundamental data, using tiered storage: a local 1 TB SSD as
> the primary runtime storage tier and the 20 TB NAS as the archival tier.
> Storage growth estimates shall be documented in SRS.

### New requirements

> **SYS-67** | The data layer shall implement a tiered storage architecture
> with the local SSD as the primary runtime storage tier and the NAS as the
> archival tier. All data ingestion shall write to the SSD first. A
> post-ingestion sync job shall copy newly ingested data to the NAS at
> lowest workload priority (SYS-57). The SSD shall retain at minimum the
> most recent 90 days of bar data (configurable) for all securities. Data
> older than the SSD retention window shall remain available on the NAS for
> cold reads. | C-5, BG-6, SN-1.26 | P1

> **SYS-68** | The unified data access interface (SYS-27) shall transparently
> serve read requests from the SSD when the requested data is within the
> retention window, falling back to the NAS for historical data outside the
> retention window, without requiring consumers (strategy containers, factor
> pipeline, backtesting engine, research environment) to be aware of the
> storage tier. Cold-read results from NAS shall be cached on the SSD. |
> SN-1.28, BG-5 | P1

> **SYS-69** | The data layer shall implement a storage eviction policy for
> the SSD. When SSD usage exceeds a configurable high-water mark (default:
> 80% of SSD capacity), the storage manager shall evict data by age,
> prioritizing removal of data for securities not on the active strategy
> list or minute-bar watchlist. The storage manager shall never evict data
> for securities with currently running live strategy containers. | C-5,
> BG-6 | P1

### Deployment environment update (Section 8)

| Property | Value |
|----------|-------|
| Primary storage | Local 1 TB SSD (Proxmox host), ext4 or XFS, mounted at /data/ssd |
| Archival storage | NAS, 20 TB, NFS v3, direct-attached (10.0.0.20), mounted at /mnt/nas/data |
| Storage architecture | Tiered: SSD primary (runtime) + NAS archival (sync + cold reads) |

### Traceability

| SyRS ID | StRS Need(s) | StRS Business Goal(s) |
|---------|-------------|----------------------|
| SYS-67 | SN-1.26, C-5 | BG-6 |
| SYS-68 | SN-1.28 | BG-2, BG-5 |
| SYS-69 | C-5 | BG-6 |

---

## Appendix: Benchmark artifacts

**Attached:** `nas_benchmark_results_20260320_164526.txt` — complete benchmark
output from full 6-test run on 2026-03-20.

**Script:** `nas_benchmark.sh` — ATP-specific I/O benchmark, archived in
project documentation.

**Follow-up item:** Investigate TrueNAS NFS share settings to determine why
rsize/wsize negotiated to 128K instead of the requested 1 MB. While not
architecture-critical (the concurrent I/O failure is HDD-caused, not
chunk-size-caused), resolving this may improve sequential write throughput
for the NAS archival sync job.
