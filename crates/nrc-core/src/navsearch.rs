//! A* over a walk grid whose edges are supplied lazily by the caller.
//!
//! The grid itself — which cells are walkable, which moves clear the player's body — is
//! decided by the Python `NavGrid`, and this module never second-guesses it: a node's edges
//! are asked for the first time the search expands it, through a caller-supplied
//! [`Expander`], and kept. So a thousand short link checks over one map pay for each cell's
//! clearance tests once, and the search itself runs at native speed.
//!
//! Nodes are `(layer, ix, iy, iz)`: a layer is a stance grid sharing one lattice, which is
//! how a mixed-stance route moves between grids at a cell.
//!
//! The search reproduces the two Python searches it replaces step for step: the same
//! priority tuple `(f, stance changes, layer, insertion order)`, the same
//! "equal cost, fewer changes" relaxation, the same budget accounting, and a heuristic
//! computed exactly as CPython's `math.dist` computes it, so ties break the same way and
//! the same path comes back, not merely one of equal cost.

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};

/// A grid node: `(layer, ix, iy, iz)`.
pub type Key = (u8, i32, i32, i32);

/// One outgoing move as the caller reports it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Move {
    pub to: Key,
    /// Unweighted world length of the move; multiplied by the layer's weight.
    pub length: f64,
    /// A stance change: costs the search's `transition_cost` instead of `length`.
    pub change: bool,
}

/// Supplies a node's outgoing moves, in the caller's own neighbour order.
pub trait Expander {
    type Error;
    fn expand(&mut self, node: Key) -> Result<Vec<Move>, Self::Error>;
}

impl<E, F: FnMut(Key) -> Result<Vec<Move>, E>> Expander for F {
    type Error = E;
    fn expand(&mut self, node: Key) -> Result<Vec<Move>, E> {
        self(node)
    }
}

/// When the node budget runs out — the two Python searches count differently.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Budget {
    /// `analysis.path_distance`: a node is closed and counted, the goal test runs, and the
    /// search gives up once more than `max_nodes` have been expanded.
    AfterGoalTest,
    /// `routes.find_route`: the search stops before closing a new node once `max_nodes`
    /// are closed, goal or not.
    BeforeClose,
}

#[derive(Clone, Debug)]
pub struct Query {
    /// Start nodes, all at cost zero, in insertion order.
    pub sources: Vec<Key>,
    pub goals: Vec<Key>,
    /// Points the heuristic measures to (usually the goal centres).
    pub goal_points: Vec<[f64; 3]>,
    /// Heuristic multiplier (the cheapest layer weight).
    pub h_weight: f64,
    /// Cost multiplier per layer, indexed by `Key.0`.
    pub layer_weights: Vec<f64>,
    pub transition_cost: f64,
    pub max_nodes: usize,
    pub budget: Budget,
    /// Prune every node whose `g + h` exceeds this. The heuristic never overestimates, so
    /// no route of cost `<= bound` is lost, and when the best route is within the bound the
    /// result is the unbounded result.
    pub bound: Option<f64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Status {
    Found,
    /// The open set emptied: no route (within `bound`, when nodes were pruned).
    Exhausted,
    /// The node budget ran out first.
    Budget,
}

#[derive(Clone, Debug)]
pub struct Outcome {
    pub status: Status,
    /// Nodes closed.
    pub expanded: usize,
    /// Whether the bound pruned anything — an exhausted search is then only "no route
    /// within the bound".
    pub pruned: bool,
    pub cost: Option<f64>,
    /// Source to goal, inclusive.
    pub path: Vec<Key>,
}

#[derive(Clone, Copy)]
struct Edge {
    to: u32,
    length: f64,
    change: bool,
}

const NONE: u32 = u32::MAX;

#[derive(Clone, Copy)]
struct Entry {
    f: f64,
    changes: u32,
    layer: u8,
    tie: u64,
    node: u32,
}

impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Entry {}
impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Entry {
    /// Reversed, so `BinaryHeap` pops the smallest tuple first, as `heapq` does. `tie` is
    /// unique, so the node never decides.
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .f
            .total_cmp(&self.f)
            .then(other.changes.cmp(&self.changes))
            .then(other.layer.cmp(&self.layer))
            .then(other.tie.cmp(&self.tie))
    }
}

/// The lattice frame, edges learned so far, and per-search scratch.
pub struct Graph {
    origin: [f64; 3],
    cell: f64,
    /// Resting heights that differ from the cell's lower boundary (`NavGrid.stand_z`),
    /// keyed by node; a node absent here sits on its boundary.
    heights: HashMap<Key, f64>,
    ids: HashMap<Key, u32>,
    keys: Vec<Key>,
    /// Per node, `(start, len)` into `edges` once expanded.
    adjacency: Vec<Option<(u32, u32)>>,
    edges: Vec<Edge>,
    stamp: Vec<u32>,
    closed: Vec<u32>,
    best: Vec<f64>,
    changes: Vec<u32>,
    came: Vec<u32>,
    epoch: u32,
}

impl Graph {
    pub fn new(origin: [f64; 3], cell: f64) -> Self {
        Graph {
            origin,
            cell,
            heights: HashMap::new(),
            ids: HashMap::new(),
            keys: Vec::new(),
            adjacency: Vec::new(),
            edges: Vec::new(),
            stamp: Vec::new(),
            closed: Vec::new(),
            best: Vec::new(),
            changes: Vec::new(),
            came: Vec::new(),
            epoch: 0,
        }
    }

    /// Record a node's moves ahead of any search (computed elsewhere, e.g. in parallel).
    /// A node whose moves are already known keeps them; returns whether these were used.
    pub fn learn(&mut self, node: Key, moves: &[Move]) -> bool {
        let id = self.id(node);
        if self.adjacency[id as usize].is_some() {
            return false;
        }
        let start = self.edges.len() as u32;
        for m in moves {
            let to = self.id(m.to);
            self.edges.push(Edge {
                to,
                length: m.length,
                change: m.change,
            });
        }
        self.adjacency[id as usize] = Some((start, self.edges.len() as u32 - start));
        true
    }

    /// Whether a node's moves are known.
    pub fn knows(&self, node: Key) -> bool {
        self.ids
            .get(&node)
            .is_some_and(|&id| self.adjacency[id as usize].is_some())
    }

    /// Nodes seen, nodes whose edges are known, and edges.
    pub fn sizes(&self) -> (usize, usize, usize) {
        let expanded = self.adjacency.iter().filter(|a| a.is_some()).count();
        (self.keys.len(), expanded, self.edges.len())
    }

    /// Set where a node's body rests when that is not its cell's lower boundary.
    pub fn set_height(&mut self, key: Key, z: f64) {
        self.heights.insert(key, z);
    }

    /// World position of a node: centred horizontally, on its floor vertically — the
    /// same expression, in the same order, as `NavGrid.centre` (a recorded resting
    /// height wins over the cell boundary).
    pub fn centre(&self, key: Key) -> [f64; 3] {
        [
            self.origin[0] + (key.1 as f64 + 0.5) * self.cell,
            self.origin[1] + (key.2 as f64 + 0.5) * self.cell,
            match self.heights.get(&key) {
                Some(&z) => z,
                None => self.origin[2] + key.3 as f64 * self.cell,
            },
        ]
    }

    fn id(&mut self, key: Key) -> u32 {
        if let Some(&id) = self.ids.get(&key) {
            return id;
        }
        let id = self.keys.len() as u32;
        self.ids.insert(key, id);
        self.keys.push(key);
        self.adjacency.push(None);
        self.stamp.push(0);
        self.closed.push(0);
        self.best.push(f64::INFINITY);
        self.changes.push(0);
        self.came.push(NONE);
        id
    }

    fn edges_of<X: Expander>(
        &mut self,
        node: u32,
        expander: &mut X,
    ) -> Result<(u32, u32), X::Error> {
        if let Some(span) = self.adjacency[node as usize] {
            return Ok(span);
        }
        let moves = expander.expand(self.keys[node as usize])?;
        let start = self.edges.len() as u32;
        for m in moves {
            let to = self.id(m.to);
            self.edges.push(Edge {
                to,
                length: m.length,
                change: m.change,
            });
        }
        let span = (start, self.edges.len() as u32 - start);
        self.adjacency[node as usize] = Some(span);
        Ok(span)
    }

    fn heuristic(&self, node: u32, q: &Query) -> f64 {
        let p = self.centre(self.keys[node as usize]);
        let mut h = f64::INFINITY;
        for g in &q.goal_points {
            let d = math_dist(&p, g);
            if d < h {
                h = d;
            }
        }
        h * q.h_weight
    }

    /// Run one query. Edges learned along the way are kept for the next.
    pub fn search<X: Expander>(
        &mut self,
        q: &Query,
        expander: &mut X,
    ) -> Result<Outcome, X::Error> {
        self.epoch = self.epoch.wrapping_add(1);
        if self.epoch == 0 {
            self.stamp.iter_mut().for_each(|s| *s = 0);
            self.closed.iter_mut().for_each(|s| *s = 0);
            self.epoch = 1;
        }
        let epoch = self.epoch;
        let limit = q.bound.map(|b| b + b.abs() * 1e-12 + 1e-9);
        let goals: Vec<u32> = q.goals.iter().map(|&k| self.id(k)).collect();
        let mut is_goal: HashMap<u32, ()> = HashMap::with_capacity(goals.len());
        for &g in &goals {
            is_goal.insert(g, ());
        }
        let mut heap = BinaryHeap::new();
        let mut pruned = false;
        let mut tie: u64 = 0;
        for &k in &q.sources {
            let id = self.id(k);
            let i = id as usize;
            if self.stamp[i] != epoch {
                self.stamp[i] = epoch;
                self.best[i] = 0.0;
                self.changes[i] = 0;
                self.came[i] = NONE;
            }
            let f = self.heuristic(id, q);
            if limit.is_some_and(|l| f > l) {
                pruned = true;
            } else {
                heap.push(Entry {
                    f,
                    changes: 0,
                    layer: k.0,
                    tie,
                    node: id,
                });
            }
            tie += 1;
        }
        let mut expanded = 0usize;
        let mut reached = NONE;
        while let Some(entry) = heap.pop() {
            let current = entry.node;
            let ci = current as usize;
            if self.closed[ci] == epoch {
                continue;
            }
            if q.budget == Budget::BeforeClose && expanded >= q.max_nodes {
                return Ok(Outcome {
                    status: Status::Budget,
                    expanded,
                    pruned,
                    cost: None,
                    path: vec![],
                });
            }
            self.closed[ci] = epoch;
            expanded += 1;
            if is_goal.contains_key(&current) {
                reached = current;
                break;
            }
            if q.budget == Budget::AfterGoalTest && expanded > q.max_nodes {
                return Ok(Outcome {
                    status: Status::Budget,
                    expanded,
                    pruned,
                    cost: None,
                    path: vec![],
                });
            }
            let (start, len) = self.edges_of(current, expander)?;
            let cost_here = self.best[ci];
            let changes_here = self.changes[ci];
            let layer_weight = q
                .layer_weights
                .get(self.keys[ci].0 as usize)
                .copied()
                .unwrap_or(1.0);
            for e in start..start + len {
                let edge = self.edges[e as usize];
                let n = edge.to as usize;
                if self.closed[n] == epoch {
                    continue;
                }
                let (step, change) = if edge.change {
                    (q.transition_cost, 1)
                } else {
                    (edge.length * layer_weight, 0)
                };
                let candidate = cost_here + step;
                let change_count = changes_here + change;
                if self.stamp[n] != epoch {
                    self.stamp[n] = epoch;
                    self.best[n] = f64::INFINITY;
                    self.changes[n] = 0;
                    self.came[n] = NONE;
                }
                let known = self.best[n];
                if candidate < known || (candidate == known && change_count < self.changes[n]) {
                    let f = candidate + self.heuristic(edge.to, q);
                    if limit.is_some_and(|l| f > l) {
                        pruned = true;
                        continue;
                    }
                    self.best[n] = candidate;
                    self.changes[n] = change_count;
                    self.came[n] = current;
                    tie += 1;
                    heap.push(Entry {
                        f,
                        changes: change_count,
                        layer: self.keys[n].0,
                        tie,
                        node: edge.to,
                    });
                }
            }
        }
        if reached == NONE {
            return Ok(Outcome {
                status: Status::Exhausted,
                expanded,
                pruned,
                cost: None,
                path: vec![],
            });
        }
        let mut path = vec![self.keys[reached as usize]];
        let mut at = reached;
        while self.came[at as usize] != NONE {
            at = self.came[at as usize];
            path.push(self.keys[at as usize]);
        }
        path.reverse();
        Ok(Outcome {
            status: Status::Found,
            expanded,
            pruned,
            cost: Some(self.best[reached as usize]),
            path,
        })
    }
}

/// `math.dist` as CPython 3.12 computes it (`vector_norm` in `Modules/mathmodule.c`):
/// scaled, with the squares and their sum carried in double length and one correction
/// step. A naive `sqrt` differs in the last bit often enough to reorder ties.
pub fn math_dist(p: &[f64; 3], q: &[f64; 3]) -> f64 {
    let v = [
        (p[0] - q[0]).abs(),
        (p[1] - q[1]).abs(),
        (p[2] - q[2]).abs(),
    ];
    let max = v[0].max(v[1]).max(v[2]);
    if v.iter().any(|x| x.is_nan()) {
        return if max.is_infinite() { max } else { f64::NAN };
    }
    if max.is_infinite() || max == 0.0 {
        return max;
    }
    let bits = max.to_bits();
    let biased = ((bits >> 52) & 0x7ff) as i32;
    if biased == 0 {
        // Subnormal: never a grid distance; the plain formula is good enough.
        return (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
    }
    let max_e = biased - 1022; // frexp: max = m * 2^max_e, m in [0.5, 1)
    let scale = 2f64.powi(-max_e);
    let (mut csum, mut frac1, mut frac2) = (1.0f64, 0.0f64, 0.0f64);
    for &x in &v {
        let x = x * scale;
        let (hi, lo) = dl_mul(x, x);
        let (s, t) = dl_fast_sum(csum, hi);
        csum = s;
        frac1 += lo;
        frac2 += t;
    }
    let mut h = (csum - 1.0 + (frac1 + frac2)).sqrt();
    let (hi, lo) = dl_mul(-h, h);
    let (s, t) = dl_fast_sum(csum, hi);
    csum = s;
    frac1 += lo;
    frac2 += t;
    let x = csum - 1.0 + (frac1 + frac2);
    h += x / (2.0 * h);
    h / scale
}

fn dl_mul(x: f64, y: f64) -> (f64, f64) {
    let hi = x * y;
    (hi, x.mul_add(y, -hi))
}

fn dl_fast_sum(a: f64, b: f64) -> (f64, f64) {
    let x = a + b;
    let z = x - a;
    (x, b - z)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A 5x5 open floor with one wall column at x=2 for y<4: the route must go round.
    fn floor(node: Key) -> Result<Vec<Move>, ()> {
        let (l, x, y, z) = node;
        let mut out = vec![];
        for (dx, dy) in [
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ] {
            let (nx, ny) = (x + dx, y + dy);
            if !(0..5).contains(&nx) || !(0..5).contains(&ny) || (nx == 2 && ny < 4) {
                continue;
            }
            out.push(Move {
                to: (l, nx, ny, z),
                length: ((dx * dx + dy * dy) as f64).sqrt() * 16.0,
                change: false,
            });
        }
        Ok(out)
    }

    fn query(from: Key, to: Key, g: &Graph) -> Query {
        Query {
            sources: vec![from],
            goals: vec![to],
            goal_points: vec![g.centre(to)],
            h_weight: 1.0,
            layer_weights: vec![1.0],
            transition_cost: 0.0,
            max_nodes: 1000,
            budget: Budget::AfterGoalTest,
            bound: None,
        }
    }

    #[test]
    fn routes_round_a_wall() {
        let mut g = Graph::new([0.0; 3], 16.0);
        let q = query((0, 0, 0, 0), (0, 4, 0, 0), &g);
        let out = g.search(&q, &mut floor).unwrap();
        assert_eq!(out.status, Status::Found);
        // Up to y=3 diagonal-ish, through (2,4), back down: 2 diagonals... cost > straight.
        assert!(out.cost.unwrap() > 64.0);
        assert_eq!(out.path.first(), Some(&(0, 0, 0, 0)));
        assert_eq!(out.path.last(), Some(&(0, 4, 0, 0)));
        assert!(out.path.contains(&(0, 2, 4, 0)));
        // A second query reuses the learned edges and gets the same answer.
        let again = g.search(&q, &mut floor).unwrap();
        assert_eq!(again.cost, out.cost);
        assert_eq!(again.path, out.path);
    }

    #[test]
    fn bound_prunes_without_changing_a_result_inside_it() {
        let mut g = Graph::new([0.0; 3], 16.0);
        let mut q = query((0, 0, 0, 0), (0, 4, 0, 0), &g);
        let free = g.search(&q, &mut floor).unwrap();
        q.bound = Some(free.cost.unwrap());
        let tight = g.search(&q, &mut floor).unwrap();
        assert_eq!(tight.cost, free.cost);
        assert_eq!(tight.path, free.path);
        q.bound = Some(64.0);
        let short = g.search(&q, &mut floor).unwrap();
        assert_eq!(short.status, Status::Exhausted);
        assert!(short.pruned);
    }

    #[test]
    fn budget_modes() {
        let mut g = Graph::new([0.0; 3], 16.0);
        let mut q = query((0, 0, 0, 0), (0, 4, 0, 0), &g);
        q.max_nodes = 3;
        assert_eq!(g.search(&q, &mut floor).unwrap().status, Status::Budget);
        q.budget = Budget::BeforeClose;
        let out = g.search(&q, &mut floor).unwrap();
        assert_eq!((out.status, out.expanded), (Status::Budget, 3));
    }

    #[test]
    fn dist_matches_cpython() {
        // Values from CPython 3.12 `math.dist`.
        assert_eq!(math_dist(&[0.0, 0.0, 0.0], &[3.0, 4.0, 0.0]), 5.0);
        assert_eq!(math_dist(&[1.0, 2.0, 3.0], &[1.0, 2.0, 3.0]), 0.0);
        assert_eq!(
            math_dist(&[0.1, 0.2, 0.3], &[0.0, 0.0, 0.0]).to_bits(),
            0.3741657386773941f64.to_bits()
        );
    }

    #[test]
    fn learned_moves_are_used_without_asking_again() {
        let mut lazy = Graph::new([0.0; 3], 16.0);
        let q = query((0, 0, 0, 0), (0, 4, 0, 0), &lazy);
        let expected = lazy.search(&q, &mut floor).unwrap();
        let mut eager = Graph::new([0.0; 3], 16.0);
        for x in 0..5 {
            for y in 0..5 {
                let moves = floor((0, x, y, 0)).unwrap();
                assert!(eager.learn((0, x, y, 0), &moves));
                assert!(!eager.learn((0, x, y, 0), &[]), "known moves are kept");
            }
        }
        let mut never = |_: Key| -> Result<Vec<Move>, ()> { panic!("asked for a learned node") };
        let out = eager.search(&q, &mut never).unwrap();
        assert_eq!(
            (out.cost, out.path, out.expanded),
            (expected.cost, expected.path, expected.expanded)
        );
    }
}
