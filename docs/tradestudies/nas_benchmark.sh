#!/usr/bin/env bash
# =============================================================================
# ATP NAS Throughput Benchmark
# =============================================================================
# Purpose:  Validate TrueNAS I/O throughput against ATP workload patterns
#           to resolve TBD-4 (SyRS-001) — single-tier NAS vs. tiered SSD cache
#
# Usage:    sudo bash nas_benchmark.sh /mnt/nas/benchmark_test
#           (Point to a directory on your NAS mount)
#
# Duration: ~15-20 minutes
# Space:    Requires ~12 GB free on the NAS (cleaned up after)
#
# Prerequisites:
#   - fio:   sudo apt install fio
#   - NAS mounted via NFS at the specified path
#   - Run as root (sudo) for accurate results (bypasses NFS client caching)
# =============================================================================

set -euo pipefail

# --- Configuration ---
TEST_DIR="${1:?Usage: sudo bash nas_benchmark.sh /mnt/nas/benchmark_test}"
RESULTS_FILE="nas_benchmark_results_$(date +%Y%m%d_%H%M%S).txt"

# ATP workload parameters
CONCURRENT_STRATEGIES=60        # SYS-9: 30 live + 30 paper
BACKTEST_FILE_SIZE="4G"         # Typical backtest dataset (~4 GB of Parquet/bar data)
INGESTION_FILE_SIZE="2G"        # Nightly ingestion write volume
FACTOR_PIPELINE_FILES=100       # Factor pipeline reads ~100 files (sectors/groups)
SYMBOL_LOOKUP_FILES=8000        # SYS-32: 8,000+ securities

# Pass/fail thresholds (derived from ATP requirements)
# These are the MINIMUM acceptable values for single-tier NAS
THRESH_SEQ_READ_MBPS=80         # Backtesting: must sustain 80+ MB/s sequential read
THRESH_SEQ_WRITE_MBPS=50        # Ingestion: must sustain 50+ MB/s sequential write
THRESH_RAND_READ_IOPS=200       # Concurrent strategy lookups: 200+ random read IOPS
THRESH_MIXED_READ_MBPS=40       # Read throughput under concurrent write load
THRESH_CONCURRENT_IOPS=150      # 60 concurrent readers: 150+ aggregate IOPS

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# --- Helper functions ---

print_header() {
    echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

print_test() {
    echo -e "\n${YELLOW}▸ TEST $1: $2${NC}"
    echo -e "  Pattern:   $3"
    echo -e "  Maps to:   $4"
}

check_result() {
    local label="$1"
    local value="$2"
    local threshold="$3"
    local unit="$4"

    # Use awk for float comparison
    local pass
    pass=$(awk "BEGIN {print ($value >= $threshold) ? 1 : 0}")

    if [ "$pass" -eq 1 ]; then
        echo -e "  ${GREEN}✓ PASS${NC}  ${label}: ${BOLD}${value} ${unit}${NC} (threshold: ≥${threshold} ${unit})"
        echo "PASS | ${label}: ${value} ${unit} (threshold: >=${threshold} ${unit})" >> "$RESULTS_FILE"
    else
        echo -e "  ${RED}✗ FAIL${NC}  ${label}: ${BOLD}${value} ${unit}${NC} (threshold: ≥${threshold} ${unit})"
        echo "FAIL | ${label}: ${value} ${unit} (threshold: >=${threshold} ${unit})" >> "$RESULTS_FILE"
    fi
}

extract_fio_metric() {
    # Extract a metric from fio JSON output
    # Usage: extract_fio_metric file.json "jobs.0.read.bw"
    # For percentile keys with dots: extract_fio_metric file.json "jobs.0.read.clat_ns.percentile" "99.000000"
    local json_file="$1"
    local jq_path="$2"
    local sub_key="${3:-}"
    python3 -c "
import json
with open('${json_file}') as f:
    data = json.load(f)
result = data
for key in '${jq_path}'.split('.'):
    if key.isdigit():
        result = result[int(key)]
    else:
        result = result[key]
sub_key = '${sub_key}'
if sub_key:
    result = result[sub_key]
print(f'{result:.2f}' if isinstance(result, float) else result)
"
}

cleanup() {
    echo -e "\n${CYAN}Cleaning up test files...${NC}"
    rm -rf "${TEST_DIR}/fio_*" "${TEST_DIR}/test_*" 2>/dev/null || true
}

trap cleanup EXIT

# --- Preflight checks ---

print_header "ATP NAS THROUGHPUT BENCHMARK"
echo -e "  Date:       $(date)"
echo -e "  Test dir:   ${TEST_DIR}"
echo -e "  Results:    ${RESULTS_FILE}"

echo "" > "$RESULTS_FILE"
echo "ATP NAS Throughput Benchmark — $(date)" >> "$RESULTS_FILE"
echo "Test directory: ${TEST_DIR}" >> "$RESULTS_FILE"
echo "==========================================" >> "$RESULTS_FILE"

# Check prerequisites
for cmd in fio python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}Error: ${cmd} not found. Install with: sudo apt install ${cmd}${NC}"
        exit 1
    fi
done

# Verify mount point (check the parent directory, not the test subdir)
NAS_MOUNT=$(df "${TEST_DIR%/*}" --output=target 2>/dev/null | tail -1 || echo "")
if [ -n "$NAS_MOUNT" ] && mountpoint -q "$NAS_MOUNT" 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} NAS mount verified: ${NAS_MOUNT}"
else
    echo -e "${YELLOW}Warning: Could not verify NAS mount. Ensure NAS is mounted at ${TEST_DIR%/*}${NC}"
fi

mkdir -p "${TEST_DIR}"

# Record mount info
echo "" >> "$RESULTS_FILE"
echo "Mount info:" >> "$RESULTS_FILE"
mount | grep nfs >> "$RESULTS_FILE" 2>/dev/null || echo "(no NFS mounts detected)" >> "$RESULTS_FILE"
echo "" >> "$RESULTS_FILE"

# Drop caches before starting (requires root)
if [ "$(id -u)" -eq 0 ]; then
    sync
    echo 3 > /proc/sys/vm/drop_caches
    echo -e "  ${GREEN}✓${NC} Caches dropped (running as root)"
else
    echo -e "  ${YELLOW}⚠${NC} Not running as root — results may be inflated by OS cache"
    echo "WARNING: Not running as root, results may include cached reads" >> "$RESULTS_FILE"
fi

# =============================================================================
# TEST 1: Sequential Read (Backtesting)
# =============================================================================
print_test "1/6" "Sequential read throughput" \
    "Single-threaded large sequential read (4 GB)" \
    "Backtesting engine reading historical data (SYS-14)"

# First, write the test file
fio --name=seqwrite_prep \
    --directory="${TEST_DIR}" \
    --rw=write \
    --bs=1M \
    --size="${BACKTEST_FILE_SIZE}" \
    --numjobs=1 \
    --output=/dev/null \
    --output-format=json \
    2>/dev/null

# Drop caches again
[ "$(id -u)" -eq 0 ] && { sync; echo 3 > /proc/sys/vm/drop_caches; }

fio --name=seq_read \
    --directory="${TEST_DIR}" \
    --rw=read \
    --bs=1M \
    --size="${BACKTEST_FILE_SIZE}" \
    --numjobs=1 \
    --direct=1 \
    --output-format=json \
    --output="${TEST_DIR}/fio_seq_read.json" \
    2>/dev/null

SEQ_READ_BW=$(extract_fio_metric "${TEST_DIR}/fio_seq_read.json" "jobs.0.read.bw")
SEQ_READ_MBPS=$(awk "BEGIN {printf \"%.1f\", ${SEQ_READ_BW} / 1024}")

check_result "Sequential read" "$SEQ_READ_MBPS" "$THRESH_SEQ_READ_MBPS" "MB/s"

# =============================================================================
# TEST 2: Sequential Write (Nightly Ingestion)
# =============================================================================
print_test "2/6" "Sequential write throughput" \
    "Single-threaded large sequential write (2 GB)" \
    "Nightly data ingestion writing to NAS (SYS-22a, SYS-23)"

# Drop caches
[ "$(id -u)" -eq 0 ] && { sync; echo 3 > /proc/sys/vm/drop_caches; }

fio --name=seq_write \
    --directory="${TEST_DIR}" \
    --rw=write \
    --bs=1M \
    --size="${INGESTION_FILE_SIZE}" \
    --numjobs=1 \
    --direct=1 \
    --output-format=json \
    --output="${TEST_DIR}/fio_seq_write.json" \
    2>/dev/null

SEQ_WRITE_BW=$(extract_fio_metric "${TEST_DIR}/fio_seq_write.json" "jobs.0.write.bw")
SEQ_WRITE_MBPS=$(awk "BEGIN {printf \"%.1f\", ${SEQ_WRITE_BW} / 1024}")

check_result "Sequential write" "$SEQ_WRITE_MBPS" "$THRESH_SEQ_WRITE_MBPS" "MB/s"

# =============================================================================
# TEST 3: Random Read IOPS (Concurrent Symbol Lookups)
# =============================================================================
print_test "3/6" "Random read IOPS" \
    "4K random reads, queue depth 32 (simulates index/metadata lookups)" \
    "Strategy containers querying different symbols concurrently (SYS-9, SYS-27)"

# Drop caches
[ "$(id -u)" -eq 0 ] && { sync; echo 3 > /proc/sys/vm/drop_caches; }

fio --name=rand_read \
    --directory="${TEST_DIR}" \
    --rw=randread \
    --bs=4k \
    --size=1G \
    --numjobs=1 \
    --iodepth=32 \
    --direct=1 \
    --runtime=60 \
    --time_based \
    --output-format=json \
    --output="${TEST_DIR}/fio_rand_read.json" \
    2>/dev/null

RAND_READ_IOPS=$(extract_fio_metric "${TEST_DIR}/fio_rand_read.json" "jobs.0.read.iops")
RAND_READ_IOPS_INT=$(awk "BEGIN {printf \"%.0f\", ${RAND_READ_IOPS}}")

check_result "Random read IOPS" "$RAND_READ_IOPS_INT" "$THRESH_RAND_READ_IOPS" "IOPS"

# Also report latency (critical for strategy responsiveness)
RAND_READ_LAT_US=$(extract_fio_metric "${TEST_DIR}/fio_rand_read.json" "jobs.0.read.clat_ns.mean")
RAND_READ_LAT_MS=$(awk "BEGIN {printf \"%.2f\", ${RAND_READ_LAT_US} / 1000000}")
echo -e "  ${CYAN}ℹ${NC}  Random read avg latency: ${BOLD}${RAND_READ_LAT_MS} ms${NC}"
echo "INFO | Random read avg latency: ${RAND_READ_LAT_MS} ms" >> "$RESULTS_FILE"

P99_LAT_US=$(extract_fio_metric "${TEST_DIR}/fio_rand_read.json" "jobs.0.read.clat_ns.percentile" "99.000000")
P99_LAT_MS=$(awk "BEGIN {printf \"%.2f\", ${P99_LAT_US} / 1000000}")
echo -e "  ${CYAN}ℹ${NC}  Random read p99 latency: ${BOLD}${P99_LAT_MS} ms${NC}"
echo "INFO | Random read p99 latency: ${P99_LAT_MS} ms" >> "$RESULTS_FILE"

# =============================================================================
# TEST 4: Concurrent Random Reads (60 Strategy Containers)
# =============================================================================
print_test "4/6" "Concurrent random reads (60 workers)" \
    "60 parallel workers doing 64K random reads (simulates container fleet)" \
    "60 strategy containers reading market data simultaneously at market open (SYS-9, NFR-P10)"

# Create a larger test file for concurrent reads
fio --name=concurrent_prep \
    --directory="${TEST_DIR}" \
    --rw=write \
    --bs=1M \
    --size=4G \
    --numjobs=1 \
    --output=/dev/null \
    --output-format=json \
    2>/dev/null

# Drop caches
[ "$(id -u)" -eq 0 ] && { sync; echo 3 > /proc/sys/vm/drop_caches; }

fio --name=concurrent_read \
    --directory="${TEST_DIR}" \
    --rw=randread \
    --bs=64k \
    --size=4G \
    --numjobs=60 \
    --iodepth=4 \
    --direct=1 \
    --runtime=60 \
    --time_based \
    --group_reporting \
    --output-format=json \
    --output="${TEST_DIR}/fio_concurrent.json" \
    2>/dev/null

CONC_IOPS=$(extract_fio_metric "${TEST_DIR}/fio_concurrent.json" "jobs.0.read.iops")
CONC_IOPS_INT=$(awk "BEGIN {printf \"%.0f\", ${CONC_IOPS}}")
CONC_BW=$(extract_fio_metric "${TEST_DIR}/fio_concurrent.json" "jobs.0.read.bw")
CONC_MBPS=$(awk "BEGIN {printf \"%.1f\", ${CONC_BW} / 1024}")

check_result "Concurrent IOPS (60 workers)" "$CONC_IOPS_INT" "$THRESH_CONCURRENT_IOPS" "IOPS"
echo -e "  ${CYAN}ℹ${NC}  Concurrent aggregate throughput: ${BOLD}${CONC_MBPS} MB/s${NC}"
echo "INFO | Concurrent aggregate throughput: ${CONC_MBPS} MB/s" >> "$RESULTS_FILE"

CONC_LAT_US=$(extract_fio_metric "${TEST_DIR}/fio_concurrent.json" "jobs.0.read.clat_ns.mean")
CONC_LAT_MS=$(awk "BEGIN {printf \"%.2f\", ${CONC_LAT_US} / 1000000}")
echo -e "  ${CYAN}ℹ${NC}  Concurrent avg latency: ${BOLD}${CONC_LAT_MS} ms${NC}"
echo "INFO | Concurrent avg read latency (60 workers): ${CONC_LAT_MS} ms" >> "$RESULTS_FILE"

# =============================================================================
# TEST 5: Mixed Read/Write (Ingestion + Live Strategies)
# =============================================================================
print_test "5/6" "Mixed read/write (70% read / 30% write)" \
    "Simulates nightly ingestion writing while strategies are still reading" \
    "Late-running strategies reading data while ingestion has started (SYS-63)"

# Drop caches
[ "$(id -u)" -eq 0 ] && { sync; echo 3 > /proc/sys/vm/drop_caches; }

fio --name=mixed_rw \
    --directory="${TEST_DIR}" \
    --rw=randrw \
    --rwmixread=70 \
    --bs=64k \
    --size=2G \
    --numjobs=8 \
    --iodepth=8 \
    --direct=1 \
    --runtime=60 \
    --time_based \
    --group_reporting \
    --output-format=json \
    --output="${TEST_DIR}/fio_mixed.json" \
    2>/dev/null

MIXED_READ_BW=$(extract_fio_metric "${TEST_DIR}/fio_mixed.json" "jobs.0.read.bw")
MIXED_READ_MBPS=$(awk "BEGIN {printf \"%.1f\", ${MIXED_READ_BW} / 1024}")
MIXED_WRITE_BW=$(extract_fio_metric "${TEST_DIR}/fio_mixed.json" "jobs.0.write.bw")
MIXED_WRITE_MBPS=$(awk "BEGIN {printf \"%.1f\", ${MIXED_WRITE_BW} / 1024}")

check_result "Mixed read throughput" "$MIXED_READ_MBPS" "$THRESH_MIXED_READ_MBPS" "MB/s"
echo -e "  ${CYAN}ℹ${NC}  Mixed write throughput: ${BOLD}${MIXED_WRITE_MBPS} MB/s${NC}"
echo "INFO | Mixed write throughput: ${MIXED_WRITE_MBPS} MB/s" >> "$RESULTS_FILE"

# =============================================================================
# TEST 6: Factor Pipeline Pattern (Many Small Sequential Reads)
# =============================================================================
print_test "6/6" "Factor pipeline pattern" \
    "100 sequential reads of 10 MB files (simulates reading per-sector data files)" \
    "Factor pipeline loading data for 8,000+ securities in batches (SYS-32, SYS-33)"

# Create 100 test files
echo -e "  Creating 100 test files..."
for i in $(seq 1 100); do
    dd if=/dev/urandom of="${TEST_DIR}/test_factor_${i}.dat" bs=1M count=10 2>/dev/null
done

# Drop caches
[ "$(id -u)" -eq 0 ] && { sync; echo 3 > /proc/sys/vm/drop_caches; }

# Time reading all 100 files sequentially
START_TIME=$(date +%s%N)
for i in $(seq 1 100); do
    dd if="${TEST_DIR}/test_factor_${i}.dat" of=/dev/null bs=1M 2>/dev/null
done
END_TIME=$(date +%s%N)

ELAPSED_MS=$(( (END_TIME - START_TIME) / 1000000 ))
TOTAL_MB=1000  # 100 files × 10 MB
FACTOR_MBPS=$(awk "BEGIN {printf \"%.1f\", ${TOTAL_MB} / (${ELAPSED_MS} / 1000.0)}")

echo -e "  ${CYAN}ℹ${NC}  Factor pipeline pattern: ${BOLD}${FACTOR_MBPS} MB/s${NC} (${ELAPSED_MS} ms for ${TOTAL_MB} MB across 100 files)"
echo "INFO | Factor pipeline pattern: ${FACTOR_MBPS} MB/s (${ELAPSED_MS} ms for ${TOTAL_MB} MB)" >> "$RESULTS_FILE"

# Note: no formal pass/fail here — this is informational for factor pipeline sizing

# =============================================================================
# SUMMARY
# =============================================================================
print_header "BENCHMARK SUMMARY"

echo "" >> "$RESULTS_FILE"
echo "==========================================" >> "$RESULTS_FILE"
echo "SUMMARY" >> "$RESULTS_FILE"
echo "==========================================" >> "$RESULTS_FILE"

# Count pass/fail
PASS_COUNT=$(grep -c "^PASS" "$RESULTS_FILE")
FAIL_COUNT=$(grep -c "^FAIL" "$RESULTS_FILE")
TOTAL_TESTS=$((PASS_COUNT + FAIL_COUNT))

echo -e ""
echo -e "  Tests run:    ${TOTAL_TESTS}"
echo -e "  ${GREEN}Passed:     ${PASS_COUNT}${NC}"
echo -e "  ${RED}Failed:     ${FAIL_COUNT}${NC}"
echo ""

echo "Tests: ${TOTAL_TESTS} | Passed: ${PASS_COUNT} | Failed: ${FAIL_COUNT}" >> "$RESULTS_FILE"

if [ "$FAIL_COUNT" -eq 0 ]; then
    echo -e "  ${GREEN}${BOLD}VERDICT: SINGLE-TIER NAS IS VIABLE${NC}"
    echo -e "  NAS throughput meets all ATP workload requirements."
    echo -e "  TBD-4 recommendation: single-tier NAS, no SSD hot-cache needed."
    echo "" >> "$RESULTS_FILE"
    echo "VERDICT: SINGLE-TIER NAS IS VIABLE" >> "$RESULTS_FILE"
    echo "TBD-4: No SSD hot-data cache required." >> "$RESULTS_FILE"

elif [ "$FAIL_COUNT" -le 2 ]; then
    echo -e "  ${YELLOW}${BOLD}VERDICT: TIERED STORAGE RECOMMENDED${NC}"
    echo -e "  NAS throughput is marginal. Recommended architecture:"
    echo -e "    - 1 TB local SSD: hot data (recent bars, active strategy data, backtest working set)"
    echo -e "    - 20 TB NAS: cold storage (historical archive, completed backtests, raw ingestion)"
    echo -e "  TBD-4 recommendation: implement tiered storage with SSD read-cache."
    echo "" >> "$RESULTS_FILE"
    echo "VERDICT: TIERED STORAGE RECOMMENDED" >> "$RESULTS_FILE"
    echo "TBD-4: SSD hot-data cache needed for acceptable performance." >> "$RESULTS_FILE"

else
    echo -e "  ${RED}${BOLD}VERDICT: NAS INSUFFICIENT — SSD-PRIMARY ARCHITECTURE REQUIRED${NC}"
    echo -e "  NAS throughput is too low for ATP workloads. Recommended architecture:"
    echo -e "    - 1 TB local SSD: primary storage for all active data"
    echo -e "    - 20 TB NAS: archival only (nightly sync of completed data)"
    echo -e "  TBD-4 recommendation: SSD as primary, NAS as archive/backup."
    echo "" >> "$RESULTS_FILE"
    echo "VERDICT: NAS INSUFFICIENT — SSD-PRIMARY REQUIRED" >> "$RESULTS_FILE"
    echo "TBD-4: SSD must be primary storage; NAS for archival only." >> "$RESULTS_FILE"
fi

echo ""
echo -e "  Full results saved to: ${BOLD}${RESULTS_FILE}${NC}"
echo ""

# =============================================================================
# INTERPRETATION GUIDE
# =============================================================================
print_header "INTERPRETING RESULTS FOR TBD-4"

cat << 'EOF'

  The benchmark tests six I/O patterns that map to ATP subsystems:

  Test 1 — Sequential read (backtesting)
    WHY IT MATTERS: Backtesting reads gigabytes of historical bar data
    sequentially. If this is slow, backtests take hours instead of minutes.
    Threshold 80 MB/s allows a 4 GB dataset to load in ~50 seconds.

  Test 2 — Sequential write (nightly ingestion)
    WHY IT MATTERS: Nightly ingestion writes daily bars, minute bars, and
    option chains. Must complete in the overnight window.
    Threshold 50 MB/s is conservative; ingestion is I/O-light relative
    to its pacing-limited API call rate.

  Test 3 — Random read IOPS (symbol lookups)
    WHY IT MATTERS: This is the HDD killer. When 60 strategy containers
    each request data for different symbols, the disk heads seek randomly.
    HDDs typically deliver 75-150 random IOPS. If this test fails, you
    NEED an SSD cache for the hot working set.

  Test 4 — Concurrent random reads (60 workers)
    WHY IT MATTERS: Simulates market open, when all 60 containers wake up
    simultaneously and request current bar data. This is the worst-case
    I/O scenario for the ATP.

  Test 5 — Mixed read/write
    WHY IT MATTERS: Validates that nightly ingestion writing to the NAS
    doesn't starve strategy containers that are still reading. SYS-63
    requires concurrent read/write without blocking.

  Test 6 — Factor pipeline (many small files)
    WHY IT MATTERS: The factor pipeline reads data for 8,000+ securities
    organized by sector/group. If the NAS is slow at opening many files
    in sequence, the pipeline may miss its pre-market deadline (SYS-33).

  WHAT TO DO WITH THE RESULTS:
  ────────────────────────────
  • All pass     → Mount NAS directly, no SSD cache. Simplest architecture.
  • Test 3 or 4  → SSD cache for "hot" data (recent 30 days of bars for
    fail only      active symbols). NAS for everything else. The data layer
                   needs a cache-management component.
  • Most fail    → SSD is primary storage for all runtime data. NAS becomes
                   archival. Nightly job syncs completed data to NAS.

  Attach this results file to the SyRS TBD-4 resolution.

EOF
