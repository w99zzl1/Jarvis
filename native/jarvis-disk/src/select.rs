//! Selection: turn a 100k+ node scan into the few hundred nodes a model can
//! actually read.
//!
//! Three layers, mirroring the behaviour that previously lived in Python:
//!
//!   1. prune   - drop junk subtrees by name (temp, caches, system volumes),
//!   2. collapse- show large-but-uninteresting trees as one line,
//!   3. budget  - spend `max_nodes` / `max_children` best-first, always
//!                following the fattest branch.
//!
//! Everything hidden is counted so the caller can report it.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::ffi::OsStr;

use crate::walk::Scan;

/// Tier A - never interesting. The whole subtree is dropped.
pub const PRUNE_NAMES: &[&str] = &[
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    ".crashpad",
    "crashdumps",
    "crashpad",
    "inetscache",
    ".sass-cache",
    ".eslintcache",
    ".gradle-cache",
    "tmp",
    "temp",
    ".terraform.lock.d",
    "webcache",
    "shadercache",
    "gpucache",
];

/// Only pruned at the top of a drive - a project folder named "windows" or
/// "temp" is legitimate and must survive.
pub const PRUNE_ROOT_NAMES: &[&str] = &[
    "$recycle.bin",
    "system volume information",
    "windows",
    "windows.old",
    "$winreagent",
    "$windows.~bt",
    "recovery",
    "perflogs",
    "program files",
    "program files (x86)",
    "driverstore",
    "msocache",
    "$winreagentrt",
    "config.msi",
];

/// Tier B - big enough to matter, contents are noise. One line, never
/// descended into (one level of context is still shown).
pub const COLLAPSE_NAMES: &[&str] = &[
    ".gradle",
    ".rustup",
    ".vscode",
    ".cargo",
    ".nuget",
    ".m2",
    ".pnpm-store",
    ".yarn",
    "site-packages",
    ".venv",
    "venv",
    "env",
    "target",
    "build",
    "dist",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".turbo",
    ".svelte-kit",
    ".dart_tool",
    ".terraform",
    "vendor",
    "obj",
    ".git-crypt",
];

#[derive(Clone)]
pub struct Options {
    pub max_depth: usize,
    pub max_nodes: usize,
    pub max_children: usize,
    pub min_size_mb: f64,
    /// Per-folder relative threshold. A child is listed only when its own size
    /// is at least this fraction of the folder's total; the rest are summed into
    /// one `[скрыто в ...]` line. 0.0 disables the filter.
    pub min_ratio: f64,
    pub prune_junk: bool,
    pub files: bool,
    pub exclude: Vec<String>,
    pub include: Vec<String>,
    pub top_count: usize,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            max_depth: 4,
            max_nodes: 500,
            max_children: 8,
            min_size_mb: 0.0,
            min_ratio: 0.02,
            prune_junk: true,
            files: true,
            exclude: Vec::new(),
            include: Vec::new(),
            top_count: 15,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    File,
    Dir,
    /// A directory whose contents are represented by a single size line.
    Collapsed,
    /// Synthetic: the sum of this folder's children that fell under `min_ratio`.
    Hidden,
}

pub struct Entry {
    /// Index of the parent entry, `-1` for the root.
    pub parent: i32,
    pub name: String,
    pub size: u64,
    pub kind: Kind,
}

pub struct Selection {
    pub entries: Vec<Entry>,
    pub top_consumers: Vec<(String, u64)>,
    pub largest_files: Vec<(String, u64)>,
    pub pruned_nodes: u64,
    pub pruned_bytes: u64,
    pub collapsed_nodes: u64,
    pub collapsed_bytes: u64,
    pub budget_skipped: u64,
    pub largest_pruned: Vec<(String, u64)>,
    /// Totals over every folder, for the report footer. The per-folder map is
    /// consumed internally to emit the `[скрыто в ...]` entries.
    pub hidden_bytes: u64,
    pub hidden_nodes: u64,
}

/// ASCII-lowercase a name into `buf` without allocating.
///
/// Every name in the filter lists is ASCII, so a non-ASCII name can never match
/// and a failed UTF-8 check simply means "no match".
fn fold<'a>(name: &OsStr, buf: &'a mut [u8; 256]) -> Option<&'a str> {
    let bytes = name.as_encoded_bytes();
    if bytes.len() > buf.len() {
        return None;
    }
    for (i, &b) in bytes.iter().enumerate() {
        buf[i] = b.to_ascii_lowercase();
    }
    std::str::from_utf8(&buf[..bytes.len()]).ok()
}

struct Ctx<'a> {
    scan: &'a Scan,
    offsets: Vec<u32>,
    kids: Vec<u32>,
    is_dir: Vec<bool>,
    dead: Vec<bool>,
    collapse: HashSet<String>,
    prune: HashSet<String>,
    prune_root: HashSet<String>,
    include: HashSet<String>,
}

impl<'a> Ctx<'a> {
    fn children(&self, node: u32) -> &[u32] {
        let lo = self.offsets[node as usize] as usize;
        let hi = self.offsets[node as usize + 1] as usize;
        &self.kids[lo..hi]
    }

    /// Nodes in a subtree, including `node` itself.
    fn count_subtree(&self, node: u32) -> u64 {
        let mut total = 0u64;
        let mut stack = vec![node];
        while let Some(cur) = stack.pop() {
            total += 1;
            for &c in self.children(cur) {
                if !self.dead[c as usize] {
                    stack.push(c);
                }
            }
        }
        total
    }

    fn mark_dead(&mut self, node: u32) {
        let mut stack = vec![node];
        while let Some(cur) = stack.pop() {
            self.dead[cur as usize] = true;
            for &c in self.children(cur) {
                if !self.dead[c as usize] {
                    stack.push(c);
                }
            }
        }
    }

    fn in_set(&self, node: u32, set: &HashSet<String>) -> bool {
        let mut buf = [0u8; 256];
        match fold(&self.scan.nodes[node as usize].name, &mut buf) {
            Some(s) => set.contains(s),
            None => false,
        }
    }

    fn is_collapsed(&self, node: u32) -> bool {
        self.is_dir[node as usize] && self.in_set(node, &self.collapse)
    }

    /// Reconstruct a display path, walking up at most `hops` parents.
    fn path_of(&self, node: u32, hops: usize) -> String {
        let mut chain: Vec<&OsStr> = vec![&self.scan.nodes[node as usize].name];
        let mut cur = self.scan.parent[node as usize];
        let mut left = hops;
        while cur >= 0 && cur != 0 && left > 0 {
            chain.push(&self.scan.nodes[cur as usize].name);
            cur = self.scan.parent[cur as usize];
            left -= 1;
        }
        let joined = chain
            .iter()
            .rev()
            .map(|s| s.to_string_lossy())
            .collect::<Vec<_>>()
            .join("\\");
        if cur == 0 {
            let root = self.scan.nodes[0].name.to_string_lossy();
            format!("{}\\{}", root, joined)
        } else {
            joined
        }
    }
}

pub fn select(scan: &Scan, opts: &Options) -> Selection {
    let n = scan.nodes.len();
    if n == 0 {
        return Selection {
            entries: Vec::new(),
            top_consumers: Vec::new(),
            largest_files: Vec::new(),
            pruned_nodes: 0,
            pruned_bytes: 0,
            collapsed_nodes: 0,
            collapsed_bytes: 0,
            budget_skipped: 0,
            largest_pruned: Vec::new(),
            hidden_bytes: 0,
            hidden_nodes: 0,
        };
    }

    let (offsets, kids) = crate::walk::build_children(n, &scan.parent);
    let mut is_dir = vec![false; n];
    for p in &scan.parent {
        if *p >= 0 {
            is_dir[*p as usize] = true;
        }
    }
    is_dir[0] = true;

    let mut prune: HashSet<String> = if opts.prune_junk {
        PRUNE_NAMES.iter().map(|s| s.to_string()).collect()
    } else {
        HashSet::new()
    };
    for e in &opts.exclude {
        prune.insert(e.to_lowercase());
    }
    let prune_root: HashSet<String> = if opts.prune_junk {
        PRUNE_ROOT_NAMES.iter().map(|s| s.to_string()).collect()
    } else {
        HashSet::new()
    };
    let collapse: HashSet<String> = COLLAPSE_NAMES.iter().map(|s| s.to_string()).collect();
    let include: HashSet<String> = opts.include.iter().map(|s| s.to_lowercase()).collect();

    let mut ctx = Ctx {
        scan,
        offsets,
        kids,
        is_dir,
        dead: vec![false; n],
        collapse,
        prune,
        prune_root,
        include,
    };

    // --- layer 1: prune junk subtrees -------------------------------------
    let mut pruned_nodes = 0u64;
    let mut pruned_bytes = 0u64;
    let mut pruned_labels: Vec<(String, u64)> = Vec::new();
    let mut stack = vec![0u32];
    while let Some(cur) = stack.pop() {
        let at_root = cur == 0;
        // Copied out because `mark_dead` needs `&mut ctx`.
        let kids: Vec<u32> = ctx.children(cur).to_vec();
        for child in kids {
            if ctx.dead[child as usize] {
                continue;
            }
            let hit = ctx.in_set(child, &ctx.prune)
                || (at_root && ctx.in_set(child, &ctx.prune_root));
            if hit && !ctx.in_set(child, &ctx.include) {
                // Sizes of a directory are subtree totals and pruned subtrees
                // are disjoint, so this adds up exactly.
                pruned_nodes += ctx.count_subtree(child);
                pruned_bytes += scan.size[child as usize];
                pruned_labels.push((
                    scan.nodes[child as usize].name.to_string_lossy().into_owned(),
                    scan.size[child as usize],
                ));
                ctx.mark_dead(child);
                continue;
            }
            stack.push(child);
        }
    }

    // --- top consumers (depth 1, by subtree size) --------------------------
    let mut root_kids: Vec<u32> = ctx
        .children(0)
        .iter()
        .copied()
        .filter(|&c| !ctx.dead[c as usize])
        .collect();
    root_kids.sort_by_key(|&c| std::cmp::Reverse(scan.size[c as usize]));
    let top_consumers: Vec<(String, u64)> = root_kids
        .iter()
        .take(opts.top_count)
        .map(|&c| {
            (
                scan.nodes[c as usize].name.to_string_lossy().into_owned(),
                scan.size[c as usize],
            )
        })
        .collect();

    // --- largest individual files ------------------------------------------
    let mut files: Vec<u32> = Vec::new();
    if opts.files {
        files = (0..n as u32)
            .filter(|&i| {
                i != 0 && !ctx.is_dir[i as usize] && !ctx.dead[i as usize] && scan.size[i as usize] > 0
            })
            .collect();
        files.sort_by_key(|&i| std::cmp::Reverse(scan.size[i as usize]));
    }
    let largest_files: Vec<(String, u64)> = files
        .iter()
        .take(opts.top_count)
        .map(|&i| (ctx.path_of(i, 6), scan.size[i as usize]))
        .collect();

    // --- layers 2+3: collapse + budget, best-first -------------------------
    let min_bytes = (opts.min_size_mb * 1024.0 * 1024.0) as u64;
    let mut shown: HashSet<u32> = HashSet::new();
    shown.insert(0);
    let mut depth_of: std::collections::HashMap<u32, usize> = std::collections::HashMap::new();
    depth_of.insert(0, 0);
    // node -> the children that were selected for display
    let mut emit: std::collections::HashMap<u32, Vec<u32>> = std::collections::HashMap::new();
    let mut collapsed_nodes = 0u64;
    let mut collapsed_bytes = 0u64;
    let mut budget_skipped = 0u64;
    let mut heap: BinaryHeap<Reverse<(u64, u32)>> = BinaryHeap::new();
    heap.push(Reverse((scan.size[0], 0u32)));

    let place = |node: u32,
                     depth: usize,
                     shown: &mut HashSet<u32>,
                     depth_of: &mut std::collections::HashMap<u32, usize>,
                     collapsed_nodes: &mut u64,
                     collapsed_bytes: &mut u64| {
        shown.insert(node);
        depth_of.insert(node, depth);
        if ctx.is_collapsed(node) {
            *collapsed_nodes += ctx.count_subtree(node);
            *collapsed_bytes += scan.size[node as usize];
        }
    };

    // Visible children of `node`, filtered and sorted biggest-first.
    let visible = |ctx: &Ctx, node: u32, files: bool, min_bytes: u64| -> Vec<u32> {
        let mut list: Vec<u32> = ctx
            .children(node)
            .iter()
            .copied()
            .filter(|&c| !ctx.dead[c as usize])
            .collect();
        if !files {
            list.retain(|&c| ctx.is_dir[c as usize]);
        }
        if min_bytes > 0 {
            let kept: Vec<u32> = list
                .iter()
                .copied()
                .filter(|&c| scan.size[c as usize] >= min_bytes)
                .collect();
            if !kept.is_empty() {
                list = kept;
            } else if let Some(&first) = list.first() {
                // Never report a directory as empty just because everything in
                // it is below the floor.
                list = vec![first];
            }
        }
        list.sort_by_key(|&c| std::cmp::Reverse(scan.size[c as usize]));
        list
    };

    // Per-folder relative filter. Returns the children worth listing plus the
    // `(bytes, nodes)` of the ones folded into the hidden line.
    //
    // The divisor is the sum over the *surviving* children rather than the
    // folder's aggregated size: a folder that had junk pruned out of it must not
    // silently raise the bar for what is left to see.
    let ratio_split = |list: Vec<u32>| -> (Vec<u32>, (u64, u64)) {
        if opts.min_ratio <= 0.0 || list.len() < 2 {
            return (list, (0, 0));
        }
        let total: u64 = list.iter().map(|&c| scan.size[c as usize]).sum();
        if total == 0 {
            return (list, (0, 0));
        }
        let floor = (total as f64 * opts.min_ratio) as u64;
        let mut keep: Vec<u32> = Vec::with_capacity(list.len());
        let mut small_bytes = 0u64;
        let mut small_nodes = 0u64;
        let mut biggest = list[0];
        for &c in &list {
            if scan.size[c as usize] > scan.size[biggest as usize] {
                biggest = c;
            }
        }
        for c in list {
            if scan.size[c as usize] >= floor {
                keep.push(c);
            } else {
                small_bytes += scan.size[c as usize];
                small_nodes += 1;
            }
        }
        if keep.is_empty() {
            // Everything is under the bar: keep the largest so the folder is
            // never reported as empty, and do not claim anything was hidden.
            return (vec![biggest], (0, 0));
        }
        (keep, (small_bytes, small_nodes))
    };

    let mut hidden: HashMap<u32, (u64, u64)> = HashMap::new();
    let mut hidden_bytes: u64 = 0;
    let mut hidden_nodes: u64 = 0;

    while let Some(Reverse((_size, node))) = heap.pop() {
        if shown.len() >= opts.max_nodes {
            break;
        }
        let depth = *depth_of.get(&node).unwrap_or(&0);
        if depth >= opts.max_depth {
            continue;
        }

        let (mut kids_here, folded) = ratio_split(visible(&ctx, node, opts.files, min_bytes));
        if folded.1 > 0 {
            hidden.insert(node, folded);
            hidden_bytes += folded.0;
            hidden_nodes += folded.1;
        }
        if kids_here.len() > opts.max_children {
            budget_skipped += (kids_here.len() - opts.max_children) as u64;
            kids_here.truncate(opts.max_children);
        }

        let mut picked: Vec<u32> = Vec::new();
        for child in kids_here {
            if shown.len() >= opts.max_nodes {
                budget_skipped += 1;
                continue;
            }
            let is_collapsed = ctx.is_collapsed(child);
            place(
                child,
                depth + 1,
                &mut shown,
                &mut depth_of,
                &mut collapsed_nodes,
                &mut collapsed_bytes,
            );
            picked.push(child);

            if is_collapsed {
                // One level of context (target -> debug/release), then stop:
                // the rest of the subtree is already accounted for.
                let (grand_list, folded) = ratio_split(visible(&ctx, child, opts.files, min_bytes));
                if folded.1 > 0 {
                    hidden.insert(child, folded);
                    hidden_bytes += folded.0;
                    hidden_nodes += folded.1;
                }
                let mut grands: Vec<u32> = Vec::new();
                for g in grand_list.into_iter().take(opts.max_children) {
                    if shown.len() >= opts.max_nodes {
                        break;
                    }
                    place(
                        g,
                        depth + 2,
                        &mut shown,
                        &mut depth_of,
                        &mut collapsed_nodes,
                        &mut collapsed_bytes,
                    );
                    grands.push(g);
                }
                if !grands.is_empty() {
                    emit.insert(child, grands);
                }
                continue;
            }
            if ctx.is_dir[child as usize] && depth + 1 < opts.max_depth {
                heap.push(Reverse((scan.size[child as usize], child)));
            }
        }
        if !picked.is_empty() {
            emit.insert(node, picked);
        }
    }

    // --- flatten into a parent-indexed list for the caller ------------------
    let mut entries: Vec<Entry> = Vec::with_capacity(shown.len());
    entries.push(Entry {
        parent: -1,
        name: scan.nodes[0].name.to_string_lossy().into_owned(),
        size: scan.size[0],
        kind: Kind::Dir,
    });

    // Each entry carries its parent index; the caller turns that into absolute
    // paths. A `Kind::Hidden` entry is appended right after its folder's kept
    // children so the summary sits with the folder it describes.
    let mut stack: Vec<(u32, i32)> = vec![(0u32, 0i32)];
    while let Some((node, parent_idx)) = stack.pop() {
        let Some(kids) = emit.get(&node) else { continue };
        // Siblings are appended in forward order (the caller rebuilds the child
        // lists in this order) and pushed reversed, so the LIFO stack hands the
        // same order back. The base is captured once: entries.len() already
        // grows inside the loop and must not be folded into the index.
        let base = entries.len() as i32;
        let mut block: Vec<(u32, i32)> = Vec::with_capacity(kids.len());
        for (i, &child) in kids.iter().enumerate() {
            let idx = base + i as i32;
            let kind = if !ctx.is_dir[child as usize] {
                Kind::File
            } else if ctx.is_collapsed(child) {
                Kind::Collapsed
            } else {
                Kind::Dir
            };
            entries.push(Entry {
                parent: parent_idx,
                name: scan.nodes[child as usize].name.to_string_lossy().into_owned(),
                size: scan.size[child as usize],
                kind,
            });
            block.push((child, idx));
        }
        if let Some(&(bytes, nodes)) = hidden.get(&node) {
            // Keyed on the count, not the size: empty files can be folded too, and
            // a line saying "0 B under the ratio" is still information.
            if nodes > 0 {
                entries.push(Entry {
                    parent: parent_idx,
                    name: String::new(),
                    size: bytes,
                    kind: Kind::Hidden,
                });
            }
        }
        stack.extend(block.into_iter().rev());
    }

    pruned_labels.sort_by(|a, b| b.1.cmp(&a.1));

    Selection {
        entries,
        top_consumers,
        largest_files,
        pruned_nodes,
        pruned_bytes,
        collapsed_nodes,
        collapsed_bytes,
        budget_skipped,
        largest_pruned: pruned_labels,
        hidden_bytes,
        hidden_nodes,
    }
}
