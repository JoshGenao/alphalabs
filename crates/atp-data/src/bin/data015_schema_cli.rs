//! SRS-DATA-015 schema-evolution operator CLI — the **inspection** half of the verification method.
//!
//! SRS-DATA-015 is verified by "Test, inspection". The tests prove the readers behave; this tool is
//! what an operator actually runs to *inspect* a deployment and answer the two AC questions without
//! reading any source:
//!
//! - `report`  — the registry: for every persisted entity, which version this build WRITES, which
//!   range it READS, who owns the format, and the evolution posture. Answers "does each persisted
//!   entity record a schema version?" for the whole system at once.
//! - `inspect` — point it at a real deployment directory (or one file) and it reports, per file, the
//!   entity it is, the version the FILE declares, and whether this build can read it. Answers "is the
//!   data already on disk still queryable by this build?" — the no-bulk-migration question.
//!
//! ## Identification is by magic header, and admits when it cannot tell
//! A file is identified by matching its first line against a registered magic. A file whose format
//! carries no magic (the line/JSON-keyed logs, whose version travels per record) cannot be identified
//! from its bytes alone, so `inspect --dir` reports it as `entity:unidentified` rather than guessing —
//! naming the wrong entity would attach a version range that does not govern the file. Pass
//! `--entity <id> --file <path>` to interpret such a file explicitly.
//!
//! `inspect` is strictly READ-ONLY: it opens files for reading only and never writes, renames, or
//! migrates anything. That is the point — proving old data is readable must not involve rewriting it.
//!
//! Exit code is NON-ZERO when any inspected file declares a version this build cannot read, so the
//! command works as a pre-upgrade gate in a script.
//!
//! ## What `version_supported` does and does NOT assert (read this before trusting it)
//! This tool answers the **schema-evolution** question and only that one: *does this build support
//! the schema version this file declares?* It deliberately does not claim that every record's BODY
//! satisfies its owning feature's invariants.
//!
//! That boundary is a design decision, not an omission. Validating record bodies here would mean
//! re-implementing four other features' record schemas inside the data layer — the SRS-RESV-003
//! trigger event, the SRS-LOG-001 log record, the SRS-MD-006 readiness alert, and the SRS-SAFE-001
//! activation record. Three of those live in Python and one in `atp-orchestrator`, which `atp-data`
//! must not depend on (the one-way dependency direction), so the copies could not even be compiled
//! against their originals — they would silently rot as their owners evolved, and a stale copy in a
//! *gate* is worse than no copy at all.
//!
//! Body validity is enforced where it belongs: in each owning reader, each of which fails closed on
//! a record it cannot reconstruct (`resv003_hot_swap_trigger_cli::validate_trigger_log_line`,
//! `atp_logging.persistence._record_from_mapping`, `atp_readiness.probes.JsonlAlertSink.read`,
//! `atp_safety.state.load_last_activation`). The one entity this crate owns — the access journal —
//! IS shape-validated here, because its reader is next door.
//!
//! So: `version_supported:yes` means "this build understands the layout this file declares", which
//! is exactly what an operator needs before an upgrade. It does not mean "every record is valid".

use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use atp_data::schema_registry::{
    descriptor, supports_version, validate_registry, SchemaDescriptor, PERSISTED_ENTITIES,
};
use atp_types::json_scan::{parse_strict_i64, top_level_json_field};

const USAGE: &str = "\
data015_schema_cli — SRS-DATA-015 schema evolution for stored data entities

USAGE:
    data015_schema_cli report
    data015_schema_cli inspect --dir <path>
    data015_schema_cli inspect --file <path> [--entity <entity-id>]

report   Print the schema registry: every persisted entity, the version this build writes, the
         version range it reads, the owning SRS feature, and its evolution posture.

inspect  Identify persisted files and report the version each one DECLARES plus whether this build
         supports that version. Identification is by magic header; a file whose format carries no
         magic is reported as 'unidentified' under --dir unless named with --entity. Read-only: no
         file is written, renamed, or migrated. Exits NON-ZERO if any inspected file declares a
         version this build cannot read.

         SCOPE: 'version_supported' is a statement about the file's declared SCHEMA VERSION, not a
         validation of every record body. Body invariants are enforced by each format's owning
         reader (SRS-RESV-003 / SRS-LOG-001 / SRS-MD-006 / SRS-SAFE-001), each of which fails closed
         on a record it cannot reconstruct. The access journal, owned by this crate, is additionally
         shape-checked here.

Postures: ranged = every version in [min, current] is read as written; migrate-on-read = older
versions are read and upgraded IN MEMORY (bytes on disk untouched); pinned = exactly one version is
accepted and anything else is refused.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(code) => code,
        Err(err) => {
            eprintln!("data015_schema_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<ExitCode, String> {
    // A malformed registry would make every report below a confident lie, so it is checked before
    // anything is printed.
    validate_registry().map_err(|err| format!("schema registry is invalid: {err}"))?;
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "report" => cmd_report(rest),
        "inspect" => cmd_inspect(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(ExitCode::SUCCESS)
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// report
// --------------------------------------------------------------------------- //

fn cmd_report(rest: &[String]) -> Result<ExitCode, String> {
    if !rest.is_empty() {
        return Err(format!("report takes no arguments\n\n{USAGE}"));
    }
    println!("entities:{}", PERSISTED_ENTITIES.len());
    for entity in PERSISTED_ENTITIES {
        println!("entity:{}", entity.entity_id);
        println!("  owner:{}", entity.owner_srs);
        println!("  writer:{}", entity.writer_path);
        println!("  magic:{}", entity.magic.unwrap_or("-"));
        println!("  writes_version:{}", entity.current_version);
        println!(
            "  reads_versions:{}..{}",
            entity.min_supported_version, entity.current_version
        );
        println!("  posture:{}", entity.posture);
        println!("  reads_version_less_payload:{}", entity.legacy_unversioned);
    }
    Ok(ExitCode::SUCCESS)
}

// --------------------------------------------------------------------------- //
// inspect
// --------------------------------------------------------------------------- //

/// What one inspected file turned out to be.
struct Inspection {
    path: PathBuf,
    entity: Option<&'static SchemaDescriptor>,
    /// What the FILE declares about its own layout.
    declared: DeclaredVersion,
}

/// What a payload says its schema version is — **three** states, not two.
///
/// Collapsing "present but unparseable" into "absent" is the dangerous simplification: for an entity
/// that accepts version-less legacy payloads, `Absent` means *readable*, so a malformed or future
/// version that degraded to `Absent` would be reported readable and walk straight past the
/// pre-upgrade gate. Only a genuinely missing key may take the legacy path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DeclaredVersion {
    /// No version field at all — a pre-SRS-DATA-015 payload.
    Absent,
    /// A well-formed integer version.
    Valid(i64),
    /// A version field is present but is not a JSON integer (a float, string, bool, null), or the
    /// record could not be parsed at all. Never readable: this build cannot know what it is holding.
    Invalid,
    /// The entity could not be identified, so nothing is claimed about its version.
    Unknown,
}

impl Inspection {
    /// Whether this build can read the file. An unidentified file is not claimed either way — it is
    /// reported as `unknown`, never as readable.
    fn readable(&self) -> Option<bool> {
        let entity = self.entity?;
        match self.declared {
            // An unidentified entity is not claimed either way.
            DeclaredVersion::Unknown => None,
            // One predicate, shared with the per-record scan, so the file-level verdict and the
            // per-record verdict can never disagree about what "readable" means.
            declared => Some(readable_declaration(entity, declared)),
        }
    }
}

fn cmd_inspect(rest: &[String]) -> Result<ExitCode, String> {
    let parsed = InspectArgs::parse(rest)?;
    let inspections = match (&parsed.dir, &parsed.file) {
        (Some(dir), None) => inspect_dir(dir)?,
        (None, Some(file)) => vec![inspect_file(file, parsed.entity)?],
        _ => {
            return Err(format!(
                "inspect needs exactly one of --dir or --file\n\n{USAGE}"
            ))
        }
    };

    let mut unreadable = 0usize;
    let mut unidentified = 0usize;
    println!("inspected:{}", inspections.len());
    for inspection in &inspections {
        println!("file:{}", inspection.path.display());
        match inspection.entity {
            Some(entity) => {
                println!("  entity:{}", entity.entity_id);
                println!("  owner:{}", entity.owner_srs);
                println!(
                    "  supported:{}..{}",
                    entity.min_supported_version, entity.current_version
                );
            }
            None => {
                unidentified += 1;
                println!("  entity:unidentified");
            }
        }
        match inspection.declared {
            DeclaredVersion::Valid(version) => println!("  declared_version:{version}"),
            DeclaredVersion::Absent => println!("  declared_version:none(legacy)"),
            DeclaredVersion::Invalid => println!("  declared_version:invalid"),
            DeclaredVersion::Unknown => println!("  declared_version:unknown"),
        }
        match inspection.readable() {
            Some(true) => println!("  version_supported:yes"),
            Some(false) => {
                unreadable += 1;
                println!("  version_supported:no");
            }
            None => println!("  version_supported:unknown"),
        }
    }
    println!("unidentified:{unidentified}");
    println!("unsupported_version:{unreadable}");
    if unreadable > 0 {
        eprintln!(
            "data015_schema_cli: {unreadable} file(s) declare a schema version this build cannot \
             read — upgrade the build before reading them, or keep the writing build available"
        );
        return Ok(ExitCode::FAILURE);
    }
    Ok(ExitCode::SUCCESS)
}

/// Walk `dir` and inspect every regular file, identifying by magic header only.
fn inspect_dir(dir: &Path) -> Result<Vec<Inspection>, String> {
    if !dir.is_dir() {
        return Err(format!("not a directory: {}", dir.display()));
    }
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    // Deterministic order: sort each directory's entries so two runs over the same tree print the
    // same report (the output is operator evidence, so it must be reproducible).
    while let Some(current) = stack.pop() {
        let mut entries: Vec<PathBuf> = fs::read_dir(&current)
            .map_err(|err| format!("cannot read {}: {err}", current.display()))?
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.path())
            .collect();
        entries.sort();
        for path in entries {
            if path.is_dir() {
                stack.push(path);
            } else if path.is_file() {
                out.push(inspect_file(&path, None)?);
            }
        }
    }
    out.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(out)
}

/// Inspect one file. With `forced` the caller names the entity (the only way to interpret a
/// magic-less format); otherwise identification is by magic header.
fn inspect_file(
    path: &Path,
    forced: Option<&'static SchemaDescriptor>,
) -> Result<Inspection, String> {
    let file =
        fs::File::open(path).map_err(|err| format!("cannot open {}: {err}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut first = String::new();
    // A binary or empty file simply yields no usable first line; that is an unidentified file, not an
    // error — `inspect --dir` must survive whatever is lying in a deployment directory.
    let first_line = match reader.read_line(&mut first) {
        Ok(0) => String::new(),
        Ok(_) => first.trim_end_matches(['\r', '\n']).to_string(),
        Err(_) => String::new(),
    };

    let entity = forced.or_else(|| identify_by_magic(&first_line));
    let declared = match entity {
        None => DeclaredVersion::Unknown,
        // A per-record format's file is only as readable as its LEAST readable record: a log whose
        // first line is legacy but whose fifth declares a future version is not a file this build
        // can read, and the real readers fail closed over every complete record. Inspecting only
        // the first line would approve exactly that file.
        Some(entity) if entity.magic.is_none() => declared_version_over_records(entity, path),
        Some(entity) => declared_version(entity, &first_line, &mut reader),
    };
    Ok(Inspection {
        path: path.to_path_buf(),
        entity,
        declared,
    })
}

/// The file-level verdict for a per-record (magic-less) format: scan EVERY complete record.
///
/// A trailing fragment with no terminating newline is a torn tail — a crash mid-append — and is
/// dropped rather than treated as corruption, matching what the real readers
/// (`access_journal::complete_lines`, `JsonlLogStore`) do. A whole-file JSON object (the kill-switch
/// activation record, the config vault envelope) is written on a single line, so it is simply the
/// one-record case of the same scan.
fn declared_version_over_records(entity: &SchemaDescriptor, path: &Path) -> DeclaredVersion {
    let contents = match fs::read_to_string(path) {
        Ok(contents) => contents,
        // Not UTF-8 at all: this build cannot read the file, and must not say otherwise.
        Err(_) => return DeclaredVersion::Invalid,
    };
    let complete = match contents.rfind('\n') {
        Some(last) => &contents[..=last],
        // No terminating newline anywhere: the single line is a torn tail with nothing complete
        // before it. Judge it as written rather than silently reporting an empty file.
        None => contents.as_str(),
    };

    let mut best: Option<DeclaredVersion> = None;
    for record in complete.lines().filter(|line| !line.trim().is_empty()) {
        let declared = version_from_record_line(entity, record);
        // The first record this build could not read decides the file, and is what gets reported —
        // an operator needs the offending version, not the healthiest one.
        if !readable_declaration(entity, declared) {
            return declared;
        }
        best = Some(match (best, declared) {
            (Some(DeclaredVersion::Valid(a)), DeclaredVersion::Valid(b)) => {
                DeclaredVersion::Valid(a.max(b))
            }
            (Some(DeclaredVersion::Valid(a)), _) => DeclaredVersion::Valid(a),
            (_, current) => current,
        });
    }
    // An empty file declares nothing and contains nothing unreadable.
    best.unwrap_or(DeclaredVersion::Absent)
}

/// Whether `declared` is a declaration this build can act on for `entity` — the shared predicate
/// behind both the per-record scan above and [`Inspection::readable`].
fn readable_declaration(entity: &SchemaDescriptor, declared: DeclaredVersion) -> bool {
    match declared {
        DeclaredVersion::Valid(version) => supports_version(entity, version),
        DeclaredVersion::Absent => entity.legacy_unversioned,
        DeclaredVersion::Invalid | DeclaredVersion::Unknown => false,
    }
}

/// The registered entity whose magic header equals `first_line`, if any.
fn identify_by_magic(first_line: &str) -> Option<&'static SchemaDescriptor> {
    PERSISTED_ENTITIES
        .iter()
        .find(|entity| entity.magic == Some(first_line))
}

/// The version the file declares, read from the bytes.
///
/// The magic-bearing formats in this repo share one shape: `<magic>` then a checksum line then the
/// schema-version line, or `<magic>` then the version line. Both are handled by scanning the next few
/// lines for the first bare integer — the version is always the first integer-only line after the
/// magic. A format whose version is embedded in the magic itself (the rollback state) declares it
/// there, so its registered `current_version` is the declared version.
///
/// Returns [`DeclaredVersion::Absent`] only when the payload genuinely carries no version field.
fn declared_version(
    entity: &SchemaDescriptor,
    first_line: &str,
    reader: &mut BufReader<fs::File>,
) -> DeclaredVersion {
    // Version-in-magic: the header already fixed it.
    if entity.magic == Some(first_line) && first_line.contains(" v") {
        return DeclaredVersion::Valid(entity.current_version);
    }
    if entity.magic.is_none() {
        // Line/JSON-keyed formats: the version travels per record, inside the first line.
        return version_from_record_line(entity, first_line);
    }
    // Magic-bearing formats: read the version from the EXACT line its layout puts it on.
    //
    // An earlier version of this scanned forward for "the first integer that looks like a plausible
    // version", which is a trap: a real future store declaring v99 has its version line skipped as
    // implausible, and the very next header line — the record COUNT — is then read as the version.
    // The file would be reported readable at whatever its record count happened to be. A layout is
    // a fact about the format, not something to infer from the values.
    let version_line_offset = match header_layout(entity) {
        HeaderLayout::MagicChecksumVersion => 1, // magic consumed; skip the checksum line
        HeaderLayout::MagicVersion => 0,         // magic consumed; the version is next
    };
    for _ in 0..version_line_offset {
        let mut skipped = String::new();
        match reader.read_line(&mut skipped) {
            Ok(0) | Err(_) => return DeclaredVersion::Invalid,
            Ok(_) => {}
        }
    }
    let mut version_line = String::new();
    match reader.read_line(&mut version_line) {
        Ok(0) | Err(_) => return DeclaredVersion::Invalid,
        Ok(_) => {}
    }
    match parse_strict_i64(version_line.trim()) {
        Some(value) => DeclaredVersion::Valid(value),
        None => DeclaredVersion::Invalid,
    }
}

/// Where a magic-bearing format puts its schema version, as a fact about the format rather than
/// something inferred from the bytes.
enum HeaderLayout {
    /// `<magic>\n<checksum>\n<version>\n…` — every durable store in the repo.
    MagicChecksumVersion,
    /// `<magic>\n<version>\n…` — the backtest run digest/manifest preimages, which carry no
    /// checksum line because the digest IS the checksum over them.
    MagicVersion,
}

fn header_layout(entity: &SchemaDescriptor) -> HeaderLayout {
    match entity.entity_id {
        "backtest-run-digest" | "backtest-run-manifest" => HeaderLayout::MagicVersion,
        // market-data-store, backtest-record-store, notification-event-store,
        // live-execution-state, order-outbox, paper-state-snapshot.
        _ => HeaderLayout::MagicChecksumVersion,
    }
}

/// How a magic-less entity encodes one persisted record — and therefore what "a legacy payload"
/// legitimately looks like for it.
///
/// This is entity-AWARE on purpose. A shared fallback that treated "neither a `v` tag nor a JSON
/// object" as `Absent` would report arbitrary garbage as a readable legacy payload for every entity
/// that accepts version-less data, which is the opposite of what a pre-upgrade gate is for: the
/// real readers would reject those bytes outright.
enum RecordEncoding {
    /// The access journal: `v<N>\t…` when versioned, and a bare tab-delimited
    /// `<access_ts>\t<job_kind>\t<job_id>\t<SYMBOL>` line when legacy.
    JournalLine,
    /// A JSON object per record, carrying its version under [`SchemaDescriptor`]-specific key.
    JsonObject,
}

fn record_encoding(entity: &SchemaDescriptor) -> RecordEncoding {
    match entity.entity_id {
        "access-journal" => RecordEncoding::JournalLine,
        _ => RecordEncoding::JsonObject,
    }
}

/// The JSON key an entity records its version under. Every format SRS-DATA-015 retrofitted uses
/// `schema_version`; the pre-existing config vault has always used `version`, and reading it with
/// the wrong key would report a versioned envelope as version-less.
fn version_key(entity: &SchemaDescriptor) -> &'static str {
    match entity.entity_id {
        "config-vault-envelope" => "version",
        _ => "schema_version",
    }
}

/// The schema version declared inside one record of a magic-less format.
///
/// Strict in both directions, and [`DeclaredVersion::Absent`] is returned ONLY for a payload that is
/// well-formed for its entity and genuinely carries no version field:
///
/// * **journal** — a `v`-tag whose value is not a plain integer (`v1.5`) is `Invalid`, not "no tag";
///   an untagged line must still have the legacy tab-delimited shape to count as legacy.
/// * **JSON** — the object is scanned STRUCTURALLY, so the key is found wherever a writer places it,
///   while a version-shaped string *value* (an operator-supplied rationale) never spoofs one and a
///   nested key is never read as the record's. Anything that is not a well-formed JSON object is
///   `Invalid`.
fn version_from_record_line(entity: &SchemaDescriptor, line: &str) -> DeclaredVersion {
    match record_encoding(entity) {
        RecordEncoding::JournalLine => {
            if let Some(rest) = line.strip_prefix('v') {
                return match rest.split_once('\t') {
                    Some((version, body)) if is_legacy_journal_body(body) => {
                        match parse_strict_i64(version) {
                            Some(value) => DeclaredVersion::Valid(value),
                            None => DeclaredVersion::Invalid,
                        }
                    }
                    _ => DeclaredVersion::Invalid,
                };
            }
            if is_legacy_journal_body(line) {
                DeclaredVersion::Absent
            } else {
                DeclaredVersion::Invalid
            }
        }
        RecordEncoding::JsonObject => match top_level_json_field(line, version_key(entity)) {
            Err(_) => DeclaredVersion::Invalid,
            Ok(None) => DeclaredVersion::Absent,
            Ok(Some(raw)) => match parse_strict_i64(raw) {
                Some(value) => DeclaredVersion::Valid(value),
                None => DeclaredVersion::Invalid,
            },
        },
    }
}

/// Whether `body` has the access journal's record shape:
/// `<access_ts>\t<job_kind>\t<job_id>\t<SYMBOL>` — exactly four tab-delimited fields, an integer
/// timestamp, a known job kind, and non-empty id/symbol.
///
/// Mirrors what `atp_data::access_journal`'s own parser requires, so `readable:yes` on a legacy
/// journal means the real reader would accept the line rather than merely that no version tag was
/// found.
fn is_legacy_journal_body(body: &str) -> bool {
    let fields: Vec<&str> = body.trim_end_matches('\n').split('\t').collect();
    if fields.len() != 4 {
        return false;
    }
    parse_strict_i64(fields[0]).is_some()
        && matches!(fields[1], "backtest" | "factor-pipeline")
        && !fields[2].trim().is_empty()
        && !fields[3].trim().is_empty()
}

// --------------------------------------------------------------------------- //
// Argument parsing — explicit allow-list, no third-party parser
// --------------------------------------------------------------------------- //

struct InspectArgs {
    dir: Option<PathBuf>,
    file: Option<PathBuf>,
    entity: Option<&'static SchemaDescriptor>,
}

impl InspectArgs {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut dir = None;
        let mut file = None;
        let mut entity = None;
        let mut index = 0;
        while index < args.len() {
            let flag = args[index].as_str();
            let mut value = || -> Result<String, String> {
                index += 1;
                args.get(index)
                    .cloned()
                    .ok_or_else(|| format!("{flag} needs a value"))
            };
            match flag {
                "--dir" => dir = Some(PathBuf::from(value()?)),
                "--file" => file = Some(PathBuf::from(value()?)),
                "--entity" => {
                    let id = value()?;
                    entity = Some(descriptor(&id).ok_or_else(|| {
                        format!(
                            "unknown entity id '{id}'; run `data015_schema_cli report` for the list"
                        )
                    })?);
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
            index += 1;
        }
        if entity.is_some() && file.is_none() {
            return Err("--entity names how to interpret --file; pass --file too".to_string());
        }
        Ok(Self { dir, file, entity })
    }
}
