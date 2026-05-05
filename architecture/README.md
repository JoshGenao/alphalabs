# Architecture Boundary

`SRS-ARCH-001` is enforced by keeping core ATP runtime services in Rust crates
under `crates/` and exposing user-authored strategy interfaces from the Python
package under `python/atp_strategy`.

The objective source of truth for the boundary is
`architecture/runtime_services.json`. The automated check in
`tools/architecture_check.py` verifies that every declared core service has a
Rust crate manifest and Rust source, that those service directories contain no
Python implementation files, that container configuration points core services
at the Rust runtime image, and that the Python package exposes the Strategy API.

`SRS-ARCH-002` is enforced by the same metadata file's
`dependency_direction` block. `tools/dependency_boundary_check.py` validates
the allowed internal Cargo dependency graph and scans lower-layer Rust crates
for forbidden dashboard, orchestrator, and vendor-adapter imports. The check
can be run directly:

```bash
python3 tools/dependency_boundary_check.py
```

The negative fixtures prove the check fails for the required boundary
violations:

```bash
python3 tools/dependency_boundary_check.py --fixture lower-layer-orchestrator-import
python3 tools/dependency_boundary_check.py --fixture lower-layer-vendor-adapter-import
python3 tools/dependency_boundary_check.py --fixture lower-layer-dashboard-import
```

`SRS-ARCH-003` is enforced by the same metadata file's `adapter_isolation`
block. `crates/atp-adapters` owns the public brokerage and data-provider
interfaces plus compile-only stubs for Interactive Brokers, Databento, Sharadar,
user Parquet, and a future provider. `tools/adapter_isolation_check.py` verifies
the interface surface, compiles the adapter crate, scans core crates for vendor
imports, and compiles a temporary fictional alternative-data adapter without
modifying core source files:

```bash
python3 tools/adapter_isolation_check.py
```

The negative fixtures prove core modules cannot import vendor SDKs directly:

```bash
python3 tools/adapter_isolation_check.py --fixture core-imports-ib
python3 tools/adapter_isolation_check.py --fixture core-imports-databento
python3 tools/adapter_isolation_check.py --fixture core-imports-sharadar
```

`SRS-ARCH-004` is enforced by the `deployment` block in
`architecture/runtime_services.json`. `tools/deployment_check.py` reads
`docker-compose.yml`, `.env.example`, and `docs/DEPLOYMENT.md` and
asserts that the Phase 1 stack declares the required services
(orchestrator, execution, strategy and simulation engines, market data,
data layer, factor pipeline, notifications, dashboard/API, Jupyter, IB
Gateway, strategy runtime), passes the required environment variables,
mounts the SSD primary tier and NAS archive tier, binds the dashboard
to loopback, ships every Phase 1 Dockerfile, and documents that cloud
VPS deployment is a future target with explicit portability constraints.

```bash
python3 tools/deployment_check.py
```

The negative fixtures prove the check fails when a required deployment
artefact regresses:

```bash
python3 tools/deployment_check.py --fixture missing-jupyter
python3 tools/deployment_check.py --fixture missing-ssd
python3 tools/deployment_check.py --fixture missing-portability-doc
```
