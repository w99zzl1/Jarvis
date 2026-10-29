//! Parallel filesystem walk.
//!
//! Design notes (why it is shaped like this):
//!
//! * Every worker owns a private `Vec<Node>` arena. Creating a node therefore
//!   needs no lock and no atomics — the only shared mutable state is the work
//!   queue. A parent link is a `Link`, i.e. either the synthetic root or a
//!   `(thread, local_index)` pair that is remapped to a global index once,
//!   after the walk.
//! * The queue hands out *batches* of directories, so the mutex is taken once
//!   per 32 directories instead of once per directory.
//! * Subtree sizes cannot be accumulated during the walk (the children of a
//!   directory are created by whichever worker dequeued it), so they are
//!   computed afterwards in a single reverse pass over a DFS preorder. One
//!   pass, no atomics.
//! * Names stay as `OsString`. Almost no node ever needs a `String`, and the
//!   filter matching folds ASCII straight over the raw bytes.

use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

/// Directories handed to a worker per lock acquisition.
const BATCH: usize = 32;

/// Cap on total nodes. Hitting it yields a partial tree, never a silent cut.
pub const DEFAULT_NODE_LIMIT: u64 = 8 << 20;

/// How often a worker is allowed to publish a progress sample, in milliseconds.
///
/// Every worker races for the right to publish in a given window, so N threads
/// produce at most N samples per window instead of N samples per directory.
const PROGRESS_INTERVAL_MS: u64 = 120;

/// Time constant of the percentage ramp, in seconds.
///
/// Queue depth alone is a poor progress signal on a real filesystem: the frontier
/// of a deep tree is small while most of the tree is still unseen, so the
/// structural estimate reads ~95% a second into a full-disk scan and then sits
/// there. The ramp caps how fast the reported percentage may grow, so the bar
/// always advances while the walk runs. `percent` remains an estimate — the
/// exact numbers are `nodes`, `bytes` and `queued`.
const PROGRESS_TAU_SECS: f64 = 4.0;

    /// A progress sample from the walk.
    ///
    /// `nodes`, `bytes` and `queued` are exact. `percent` is an estimate: a walk
    /// has no known total, so it is extrapolated from queue depth, from a running
    /// bound on the directory total, and from elapsed time. It stays below 100
    /// until `done`.
#[derive(Clone, Copy, Debug)]
pub struct Progress {
    /// Nodes discovered so far, including the scanned root. Matches the final
    /// sample's node count in the report.
    pub nodes: u64,
    /// Sum of file sizes discovered so far.
    pub bytes: u64,
    /// Directories discovered but not yet finished.
    pub queued: u64,
    /// Estimated completion in 0..100. Always < 100 until `done`.
    pub percent: f64,
    /// Seconds since the walk started.
    pub elapsed: f64,
    /// True on the final sample of the walk.
    pub done: bool,
}

/// Callback for progress samples. Shared by every worker thread.
pub type ProgressFn<'a> = &'a (dyn Fn(Progress) + Sync);


/// Where a node lives, before the arenas are merged.
#[derive(Clone, Copy, Debug)]
pub enum Link {
    /// Global node 0: the scanned path itself.
    Root,
    /// (worker id, index inside that worker's arena).
    Remote(u16, u32),
}

#[derive(Clone)]
pub struct Node {
    pub name: OsString,
    /// Own size for files, 0 for directories.
    pub size: u64,
    pub parent: Link,
}

pub struct Scan {
    pub root: PathBuf,
    /// `nodes[0]` is the synthetic root; parallel to `parent` and `size`.
    pub nodes: Vec<Node>,
    /// Resolved global parent index; `-1` only for node 0.
    pub parent: Vec<i32>,
    /// Subtree totals.
    pub size: Vec<u64>,
    pub elapsed: Duration,
    /// The node cap or the timeout stopped the walk: `nodes` is incomplete.
    pub partial: bool,
    /// Which limit cut the walk short, if any. `partial` without a reason is not
    /// supposed to happen, so callers can surface this verbatim.
    pub stopped_by: Option<&'static str>,
    /// Directories skipped because access was denied.
    pub denied: u64,
    pub threads: usize,
}

struct Pending {
    dirs: Vec<(PathBuf, Link)>,
    /// Directories taken by a worker but not finished yet.
    ///
    /// Termination is driven by this counter rather than by a "all threads
    /// exited" flag: a thread that drains the queue and finds it empty must
    /// wait, and the last *work item* to finish is what releases it. Counting
    /// live threads deadlocks as soon as one thread finds an empty directory.
    inflight: usize,
}

struct Shared {
    pending: Mutex<Pending>,
    cv: Condvar,
    stop: AtomicBool,
    partial: AtomicBool,
    hit_node_limit: AtomicBool,
    hit_timeout: AtomicBool,
    seen: AtomicU64,
    denied: AtomicU64,
    node_limit: u64,
    /// Sum of file sizes, for the progress sample only.
    bytes: AtomicU64,
    /// Nodes actually created. `seen` cannot be used for this: it charges every
    /// directory twice, once when its parent lists it and once when a worker
    /// picks it up, which made the progress counter disagree with the node count
    /// the report ends up with.
    discovered: AtomicU64,
    /// Directories fully processed, the denominator of nodes-per-directory.
    dirs_done: AtomicU64,
    /// Timestamp of the last published sample, for the publish window.
    last_publish: AtomicU64,
    /// Highest `percent` handed out so far, in hundredths. The estimate is
    /// noisy — queue depth swings by thousands as workers discover siblings —
    /// so a raw ratio makes the bar jump backwards. Publishing the running
    /// maximum keeps it moving forward like every other progress bar.
    percent_published: AtomicU64,
    /// Smoothed queue depth, in thousandths.
    queue_ema: AtomicU64,
    /// Highest smoothed queue depth, in thousandths. Retained at a fraction
    /// once the queue drains, otherwise the estimate would collapse to 99%
    /// during a lull between two discovery bursts.
    queue_peak: AtomicU64,
    /// Smoothed nodes-per-directory, in thousandths.
    per_dir_ema: AtomicU64,
    /// Running maximum of "directories seen so far", in thousandths. Used as an
    /// upper bound on the directory total so the percentage cannot jump ahead.
    dirs_bound: AtomicU64,
}

impl Shared {
    /// Charge one node against the cap. Returns false once the walk must stop.
    ///
    /// The deadline is re-checked on every node rather than once per batch: a
    /// single batch on a drive root can hold hundreds of directories, and
    /// checking only between batches overshot a 1 s budget by 10 s. Workers race
    /// on the cap, so the tree can overshoot by at most one node per worker.
    fn charge(&self, deadline: Option<Instant>) -> bool {
        if self.stop.load(Ordering::Acquire) {
            return false;
        }
        if let Some(d) = deadline {
            if Instant::now() >= d {
                self.abort_timeout();
                return false;
            }
        }
        if self.seen.fetch_add(1, Ordering::Relaxed) >= self.node_limit {
            self.hit_node_limit.store(true, Ordering::Release);
            self.partial.store(true, Ordering::Release);
            self.stop.store(true, Ordering::Release);
            self.cv.notify_all();
            return false;
        }
        true
    }

    /// Cut the walk short because the deadline passed. Always partial: the tree
    /// is missing every directory still sitting in the queue.
    fn abort_timeout(&self) {
        self.hit_timeout.store(true, Ordering::Release);
        self.partial.store(true, Ordering::Release);
        self.stop.store(true, Ordering::Release);
        self.cv.notify_all();
    }

    fn take_batch(&self) -> Option<Vec<(PathBuf, Link)>> {
        let mut g = self.pending.lock().expect("walk queue poisoned");
        loop {
            if self.stop.load(Ordering::Acquire) {
                return None;
            }
            if !g.dirs.is_empty() {
                let n = BATCH.min(g.dirs.len());
                let at = g.dirs.len() - n;
                let batch = g.dirs.split_off(at);
                g.inflight += batch.len();
                return Some(batch);
            }
            if g.inflight == 0 {
                return None;
            }
            g = self.cv.wait(g).expect("walk queue poisoned");
        }
    }

    fn release(&self, n: usize) {
        let mut g = self.pending.lock().expect("walk queue poisoned");
        g.inflight = g.inflight.saturating_sub(n);
        let idle = g.dirs.is_empty() && g.inflight == 0;
        drop(g);
        if idle {
            self.cv.notify_all();
        }
    }

    /// Directories discovered but not yet finished.
    fn queued(&self) -> u64 {
        let g = self.pending.lock().expect("walk queue poisoned");
        (g.dirs.len() + g.inflight) as u64
    }

    /// Publish a sample if this worker wins the interval window.
    ///
    /// Returns true when the sample was published, so the caller knows whether
    /// it has to do the (lock-taking) queue read at all.
    fn claim_publish_slot(&self, now_ms: u64) -> bool {
        let last = self.last_publish.load(Ordering::Acquire);
        if now_ms.saturating_sub(last) < PROGRESS_INTERVAL_MS {
            return false;
        }
        self.last_publish
            .compare_exchange(last, now_ms, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }

    /// Smoothed sample of an integer signal, kept as `sample * 1000`.
    ///
    /// Workers race here, so the value can be off by one EMA step. That is
    /// irrelevant for a cosmetic progress estimate and avoids a lock. Callers
    /// pass the raw signal, not a pre-scaled one.
    fn ema_scaled(counter: &AtomicU64, sample: u64, weight_num: u64) -> u64 {
        let scaled = sample.saturating_mul(1000);
        let prev = counter.load(Ordering::Relaxed);
        let next = if prev == 0 {
            scaled
        } else {
            (prev * (100 - weight_num) + scaled * weight_num) / 100
        };
        counter.store(next, Ordering::Relaxed);
        next
    }

    /// Raise a running maximum, returning the value to use.
    fn running_max(counter: &AtomicU64, candidate: u64) -> u64 {
        let mut current = counter.load(Ordering::Relaxed);
        while candidate > current {
            match counter.compare_exchange_weak(
                current,
                candidate,
                Ordering::AcqRel,
                Ordering::Relaxed,
            ) {
                Ok(_) => return candidate,
                Err(seen) => current = seen,
            }
        }
        current
    }

    /// Build a sample.
    ///
    /// `percent` is the smallest of three independent estimates, so it can only
    /// lag behind reality, never overtake it:
    ///
    /// * by nodes: finished nodes over finished nodes plus the queue times the
    ///   average yield of a directory;
    /// * by directories: finished directories over a running maximum of
    ///   "directories seen so far", which cannot shrink and therefore cannot
    ///   report the walk as nearly finished during a lull between two discovery
    ///   bursts;
    /// * by time: a ramp with time constant `PROGRESS_TAU_SECS`, which keeps the
    ///   bar moving on deep trees where the queue is a hopeless predictor.
    ///
    /// The published value is additionally pinned to the running maximum, so the
    /// bar only ever moves forward.
    fn sample(&self, started: Instant, done: bool) -> Progress {
        // +1 for the synthetic root, which is created before the walk starts and
        // is counted in `Scan::nodes`.
        let nodes = self.discovered.load(Ordering::Relaxed) + 1;
        let dirs_done = self.dirs_done.load(Ordering::Relaxed);
        let elapsed = started.elapsed().as_secs_f64();

        let queued = self.queued();
        let queued_milli = Self::ema_scaled(&self.queue_ema, queued, 25);
        let peak = Self::running_max(&self.queue_peak, queued_milli);
        // Only part of a queued directory's subtree is still unseen: the entry
        // itself is already charged, so a whole directory per queue slot
        // overshoots. Half the smoothed queue plus a slice of the deepest queue
        // ever seen keeps the estimate from collapsing to zero in a lull.
        let effective_queue_milli = (queued_milli / 2).max(peak / 10).max(1_000);
        let effective_queue = effective_queue_milli as f64 / 1000.0;

        let dirs = dirs_done.max(1);
        let per_dir = Self::ema_scaled(&self.per_dir_ema, nodes / dirs, 15) as f64 / 1000.0;

        let percent = if done {
            self.percent_published.store(10_000, Ordering::Relaxed);
            100.0
        } else {
            let remaining_nodes = effective_queue * per_dir.max(1.0);
            let by_nodes = if nodes > 0 {
                nodes as f64 / (nodes as f64 + remaining_nodes)
            } else {
                0.0
            };
            let dirs_bound = Self::running_max(
                &self.dirs_bound,
                ((dirs_done as f64 + effective_queue) * 1000.0) as u64,
            ) as f64
                / 1000.0;
            let by_dirs = dirs_done as f64 / dirs_bound.max(1.0);
            let by_time = if elapsed > 0.0 {
                (elapsed / (elapsed + PROGRESS_TAU_SECS)).min(0.99)
            } else {
                0.0
            };

            let raw = (100.0 * by_nodes.min(by_dirs).min(by_time)).clamp(0.0, 99.0);
            let mut hundredths = (raw * 100.0).round() as u64;
            let prev = self.percent_published.load(Ordering::Relaxed);
            if hundredths < prev {
                hundredths = prev;
            }
            self.percent_published.store(hundredths, Ordering::Relaxed);
            hundredths as f64 / 100.0
        };
        Progress {
            nodes,
            bytes: self.bytes.load(Ordering::Relaxed),
            queued,
            percent,
            elapsed,
            done,
        }
    }
}

/// Walk `root` and return the flat node table with subtree sizes.
///
/// When `progress` is given it receives throttled samples from the worker
/// threads and exactly one final sample with `done: true`.
pub fn scan(
    root: &Path,
    node_limit: u64,
    timeout: Option<Duration>,
    progress: ProgressFn<'_>,
) -> io::Result<Scan> {
    let started = Instant::now();

    let meta = std::fs::metadata(root)?;
    if !meta.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("not a directory: {}", root.display()),
        ));
    }

    let threads = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
        .clamp(1, 32);

    let deadline = timeout.map(|t| started + t);
    let shared = Shared {
        pending: Mutex::new(Pending {
            dirs: vec![(root.to_path_buf(), Link::Root)],
            inflight: 0,
        }),
        cv: Condvar::new(),
        stop: AtomicBool::new(false),
        partial: AtomicBool::new(false),
        hit_node_limit: AtomicBool::new(false),
        hit_timeout: AtomicBool::new(false),
        seen: AtomicU64::new(0),
        denied: AtomicU64::new(0),
        node_limit,
        bytes: AtomicU64::new(0),
        discovered: AtomicU64::new(0),
        dirs_done: AtomicU64::new(0),
        last_publish: AtomicU64::new(0),
        percent_published: AtomicU64::new(0),
        queue_ema: AtomicU64::new(0),
        queue_peak: AtomicU64::new(0),
        per_dir_ema: AtomicU64::new(0),
        dirs_bound: AtomicU64::new(0),
    };

    // Arena 0 slot 0 holds the synthetic root, so `Link::Root` resolves to
    // global index 0 after the merge.
    let mut arenas: Vec<Vec<Node>> = (0..threads).map(|_| Vec::new()).collect();
    arenas[0].push(Node {
        name: root.as_os_str().to_os_string(),
        size: 0,
        parent: Link::Root,
    });

    std::thread::scope(|scope| {
        for (tid, arena) in arenas.iter_mut().enumerate() {
            let shared = &shared;
            let tid = tid as u16;
            scope.spawn(move || worker(tid, arena, shared, deadline, started, progress));
        }
    });

    progress(shared.sample(started, true));

    {
        let mut g = shared.pending.lock().expect("walk queue poisoned");
        g.inflight = 0;
        shared.cv.notify_all();
    }

    let elapsed = started.elapsed();
    let partial = shared.partial.load(Ordering::Acquire);
    let denied = shared.denied.load(Ordering::Relaxed);
    let stopped_by = if shared.hit_timeout.load(Ordering::Acquire) {
        Some("timeout")
    } else if shared.hit_node_limit.load(Ordering::Acquire) {
        Some("node_limit")
    } else {
        None
    };

    let (nodes, parent) = merge_arenas(&arenas);
    let size = aggregate(&nodes, &parent);

    Ok(Scan {
        root: root.to_path_buf(),
        nodes,
        parent,
        size,
        elapsed,
        partial,
        stopped_by,
        denied,
        threads,
    })
}

fn worker(
    tid: u16,
    arena: &mut Vec<Node>,
    shared: &Shared,
    deadline: Option<Instant>,
    started: Instant,
    progress: ProgressFn<'_>,
) {
    let mut batch_bytes: u64 = 0;
    loop {
        if shared.stop.load(Ordering::Acquire) {
            break;
        }
        if let Some(d) = deadline {
            if Instant::now() >= d {
                shared.abort_timeout();
                break;
            }
        }

        let batch = match shared.take_batch() {
            Some(b) => b,
            None => break,
        };
        let batch_len = batch.len();
        let mut children: Vec<(PathBuf, Link)> = Vec::new();

        for (dir, parent_link) in batch {
            if !shared.charge(deadline) {
                break;
            }

            let mut dir_bytes: u64 = 0;
            children.clear();
            match std::fs::read_dir(&dir) {
                Ok(rd) => {
                    for entry in rd.flatten() {
                        // Every entry is a node, so the cap has to be charged
                        // here rather than once per directory.
                        if !shared.charge(deadline) {
                            break;
                        }
                        // On Windows `ReadDir` caches `WIN32_FIND_DATAW`, so
                        // `file_type` and `metadata` do not cost a syscall.
                        let Ok(md) = entry.metadata() else {
                            continue;
                        };
                        let is_dir = md.is_dir();
                        let link = Link::Remote(tid, arena.len() as u32);
                        if !is_dir {
                            dir_bytes += md.len();
                        }
                        arena.push(Node {
                            name: entry.file_name(),
                            size: if is_dir { 0 } else { md.len() },
                            parent: parent_link,
                        });
                        shared.discovered.fetch_add(1, Ordering::Relaxed);
                        if is_dir && !md.is_symlink() {
                            // Never follow a link: it can point at an ancestor
                            // and turn the walk into an infinite loop.
                            children.push((entry.path(), link));
                        }
                    }
                }
                Err(_) => {
                    // A directory we cannot open is a leaf, not a failure.
                    shared.denied.fetch_add(1, Ordering::Relaxed);
                    shared.dirs_done.fetch_add(1, Ordering::Relaxed);
                    continue;
                }
            }

            shared.bytes.fetch_add(dir_bytes, Ordering::Relaxed);
            shared.dirs_done.fetch_add(1, Ordering::Relaxed);
            batch_bytes += dir_bytes;

            if !children.is_empty() {
                let mut g = shared.pending.lock().expect("walk queue poisoned");
                g.dirs.extend(children.drain(..));
                drop(g);
                shared.cv.notify_one();
            }
        }

        shared.release(batch_len);

        if batch_bytes > 0 {
            // Publishing once per batch bounds the cost; claim_publish_slot then
            // keeps several workers from flooding the callback in the same tick.
            let now_ms = started.elapsed().as_millis() as u64;
            if shared.claim_publish_slot(now_ms) {
                progress(shared.sample(started, false));
            }
        }
    }
}

fn merge_arenas(arenas: &[Vec<Node>]) -> (Vec<Node>, Vec<i32>) {
    let total: usize = arenas.iter().map(|a| a.len()).sum();
    let mut base = vec![0usize; arenas.len()];
    let mut acc = 0usize;
    for (i, a) in arenas.iter().enumerate() {
        base[i] = acc;
        acc += a.len();
    }

    let mut nodes: Vec<Node> = Vec::with_capacity(total);
    for arena in arenas {
        nodes.extend(arena.iter().cloned());
    }

    let mut parent = vec![-1i32; total];
    for i in 0..total {
        parent[i] = match nodes[i].parent {
            Link::Root => {
                if i == 0 {
                    -1
                } else {
                    0
                }
            }
            Link::Remote(t, idx) => (base[t as usize] + idx as usize) as i32,
        };
    }
    (nodes, parent)
}

/// Children in CSR layout: children of `i` are `kids[off[i]..off[i + 1]]`.
pub fn build_children(n: usize, parent: &[i32]) -> (Vec<u32>, Vec<u32>) {
    let mut offsets = vec![0u32; n + 1];
    for &p in parent {
        if p >= 0 {
            offsets[p as usize + 1] += 1;
        }
    }
    for i in 0..n {
        offsets[i + 1] += offsets[i];
    }
    let mut cursor = offsets.clone();
    let mut kids = vec![0u32; n.saturating_sub(1)];
    for (i, &p) in parent.iter().enumerate() {
        if p >= 0 {
            let slot = &mut cursor[p as usize];
            kids[*slot as usize] = i as u32;
            *slot += 1;
        }
    }
    (offsets, kids)
}

/// Subtree totals in one reverse pass over a DFS preorder.
pub fn aggregate(nodes: &[Node], parent: &[i32]) -> Vec<u64> {
    let n = nodes.len();
    let (offsets, kids) = build_children(n, parent);

    let mut order = Vec::with_capacity(n);
    let mut stack = vec![0u32];
    while let Some(x) = stack.pop() {
        order.push(x);
        let (lo, hi) = (offsets[x as usize] as usize, offsets[x as usize + 1] as usize);
        stack.extend_from_slice(&kids[lo..hi]);
    }

    let mut size = vec![0u64; n];
    for (i, node) in nodes.iter().enumerate() {
        size[i] = node.size;
    }
    for &i in order.iter().rev() {
        let p = parent[i as usize];
        if p >= 0 {
            size[p as usize] += size[i as usize];
        }
    }
    size
}
