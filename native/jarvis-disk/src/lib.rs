//! `jarvis_disk` - native disk scanner for Jarvis, called in-process from the
//! Python agent through PyO3. No subprocess, no temporary JSON file.
//!
//! The Rust side owns everything that is expensive: the parallel walk, subtree
//! aggregation, and the prune/collapse/budget selection. What crosses the FFI
//! boundary is already bounded to a few hundred entries, so Python only has to
//! format the report.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::PyDict;

mod select;
mod walk;

use select::{Kind, Options, Selection};

/// Default node cap, mirroring the previous native scanner.
const NODE_LIMIT: u64 = walk::DEFAULT_NODE_LIMIT;

#[pyclass(frozen, get_all, skip_from_py_object)]
#[derive(Clone)]
struct EntryInfo {
    parent: i32,
    name: String,
    size: u64,
    /// 0 = file, 1 = directory, 2 = collapsed directory, 3 = hidden-size summary.
    kind: u8,
}

#[pyclass(frozen, get_all, skip_from_py_object)]
#[derive(Clone)]
struct Report {
    root: String,
    /// Whole-tree total, i.e. the subtree sum of the scanned path.
    total: u64,
    /// Nodes visited during the walk, before any filtering.
    nodes_scanned: usize,
    /// Seconds spent walking the filesystem.
    scan_seconds: f64,
    /// The walk hit the node cap or the timeout: `entries` is incomplete.
    partial: bool,
    /// `"timeout"`, `"node_limit"` or `None`, so the caller can say which.
    stopped_by: Option<String>,
    /// Directories that could not be opened.
    denied: u64,
    /// Worker threads used.
    threads: usize,
    /// Node cap that was in force.
    node_limit: u64,

    entries: Vec<EntryInfo>,
    top_consumers: Vec<(String, u64)>,
    largest_files: Vec<(String, u64)>,

    pruned_nodes: u64,
    pruned_bytes: u64,
    collapsed_nodes: u64,
    collapsed_bytes: u64,
    budget_skipped: u64,
    largest_pruned: Vec<(String, u64)>,
    /// Bytes folded into per-folder `[скрыто в ...]` lines by `min_ratio`.
    hidden_bytes: u64,
    /// Number of entries folded into those lines.
    hidden_nodes: u64,
}

fn build_report(
    path: &PathBuf,
    opts: Options,
    node_limit: u64,
    timeout: Duration,
    progress: Option<Arc<dyn Fn(walk::Progress) + Send + Sync>>,
) -> Report {
    let noop = |_p: walk::Progress| {};
    let scan = walk::scan(
        path,
        node_limit,
        Some(timeout),
        match &progress {
            Some(cb) => cb.as_ref(),
            None => &noop,
        },
    )
    .expect("validated by caller");
    let sel: Selection = select::select(&scan, &opts);

    Report {
        root: scan.root.to_string_lossy().into_owned(),
        total: scan.size.first().copied().unwrap_or(0),
        nodes_scanned: scan.nodes.len(),
        scan_seconds: scan.elapsed.as_secs_f64(),
        partial: scan.partial,
        stopped_by: scan.stopped_by.map(str::to_string),
        denied: scan.denied,
        threads: scan.threads,
        node_limit,

        entries: sel
            .entries
            .into_iter()
            .map(|e| EntryInfo {
                parent: e.parent,
                name: e.name,
                size: e.size,
                kind: match e.kind {
                    Kind::File => 0,
                    Kind::Dir => 1,
                    Kind::Collapsed => 2,
                    Kind::Hidden => 3,
                },
            })
            .collect(),
        top_consumers: sel.top_consumers,
        largest_files: sel.largest_files,
        pruned_nodes: sel.pruned_nodes,
        pruned_bytes: sel.pruned_bytes,
        collapsed_nodes: sel.collapsed_nodes,
        collapsed_bytes: sel.collapsed_bytes,
        budget_skipped: sel.budget_skipped,
        largest_pruned: sel.largest_pruned,
        hidden_bytes: sel.hidden_bytes,
        hidden_nodes: sel.hidden_nodes,
    }
}

/// Walk `path` and return the already-filtered tree.
///
/// `timeout` is in seconds and is clamped to 10..=900. Raises `FileNotFoundError`
/// when `path` does not exist and `NotADirectoryError` when it is a file.
///
/// `progress`, when given, is called as `progress(dict)` from the worker threads
/// while the walk runs, throttled to ~8/s per worker, and once more with
/// `done=True` at the end. The GIL is released for the walk, so the callback
/// must be cheap; it is invoked from threads the caller did not create.
#[pyfunction]
#[pyo3(signature = (
    path,
    max_depth = 4,
    max_nodes = 500,
    max_children = 8,
    min_size_mb = 0.0,
    min_ratio = 0.02,
    prune_junk = true,
    files = true,
    exclude = None,
    include = None,
    timeout = 120,
    top_count = 15,
    node_limit = None,
    progress = None,
))]
#[allow(clippy::too_many_arguments)]
fn scan_disk(
    py: Python<'_>,
    path: &str,
    max_depth: usize,
    max_nodes: usize,
    max_children: usize,
    min_size_mb: f64,
    min_ratio: f64,
    prune_junk: bool,
    files: bool,
    exclude: Option<Vec<String>>,
    include: Option<Vec<String>>,
    timeout: u64,
    top_count: usize,
    node_limit: Option<u64>,
    progress: Option<Py<PyAny>>,
) -> PyResult<Report> {
    let _ = py;

    let target = PathBuf::from(path);
    let meta = std::fs::metadata(&target).map_err(|e| match e.kind() {
        std::io::ErrorKind::NotFound => {
            PyErr::new::<pyo3::exceptions::PyFileNotFoundError, _>(format!(
                "path does not exist: {}",
                target.display()
            ))
        }
        std::io::ErrorKind::PermissionDenied => pyo3::exceptions::PyPermissionError::new_err(
            format!("access denied: {}", target.display()),
        ),
        _ => pyo3::exceptions::PyOSError::new_err(format!(
            "{}: {}",
            target.display(),
            e
        )),
    })?;
    if !meta.is_dir() {
        return Err(pyo3::exceptions::PyNotADirectoryError::new_err(format!(
            "not a directory: {}",
            target.display()
        )));
    }

    let opts = Options {
        max_depth: max_depth.clamp(1, 24),
        max_nodes: max_nodes.clamp(10, 20_000),
        max_children: max_children.clamp(1, 200),
        min_size_mb: min_size_mb.max(0.0),
        min_ratio: min_ratio.clamp(0.0, 1.0),
        prune_junk,
        files,
        exclude: exclude.unwrap_or_default(),
        include: include.unwrap_or_default(),
        top_count: top_count.clamp(1, 100),
    };

    let seconds = timeout.clamp(10, 900);
    let limit = node_limit.unwrap_or(NODE_LIMIT).clamp(1_000, 64 << 20);

    // Bridge the Rust callback into Python. Failures inside the callback are
    // swallowed on purpose: a broken progress sink must not fail the scan.
    let bridge: Option<Arc<dyn Fn(walk::Progress) + Send + Sync>> = progress.map(|cb| {
        Arc::new(move |p: walk::Progress| {
            let _ = Python::attach(|py| {
                let d = PyDict::new(py);
                d.set_item("nodes", p.nodes).ok();
                d.set_item("bytes", p.bytes).ok();
                d.set_item("queued", p.queued).ok();
                d.set_item("percent", p.percent).ok();
                d.set_item("elapsed", p.elapsed).ok();
                d.set_item("done", p.done).ok();
                let _ = cb.bind(py).call1((d,));
            });
        }) as Arc<dyn Fn(walk::Progress) + Send + Sync>
    });

    // Keep the GIL released for the whole scan: the walk is pure syscall work,
    // so the Python side stays responsive while it runs.
    let result = py.detach(|| {
        build_report(
            &target,
            opts,
            limit,
            Duration::from_secs(seconds),
            bridge,
        )
    });
    Ok(result)
}

/// Cheap probe: is the native module usable, and what does it think the host is?
#[pyfunction]
fn info() -> PyResult<(String, usize)> {
    let threads = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1);
    Ok((env!("CARGO_PKG_VERSION").to_string(), threads))
}

#[pymodule]
fn jarvis_disk(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(scan_disk, m)?)?;
    m.add_function(wrap_pyfunction!(info, m)?)?;
    m.add_class::<Report>()?;
    m.add_class::<EntryInfo>()?;
    Ok(())
}
