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
