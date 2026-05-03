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
