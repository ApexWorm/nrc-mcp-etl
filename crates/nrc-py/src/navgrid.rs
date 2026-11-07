//! The walk grid's voxel pass, one x-slab at a time.
//!
//! `nrc_mcp.analysis.build_navgrid` used to fill a dense byte per cell for the whole map and
//! then test every standing place against the expanded body obstacles in Python. On a
//! full-size map that is tens of millions of bytes and hundreds of thousands of Python-level
//! brush tests per grid. This does the same arithmetic, in the same order, but holds only one
//! x-slab (`ny * nz` bytes) at a time and keeps each column as its solid runs, so memory
//! scales with the map's surfaces rather than its volume.
//!
//! Every floating-point expression mirrors the Python reference (`analysis._voxelize_python`)
//! operation for operation, so the two agree cell for cell; `python/tests` compares them.

/// Half-space tolerance: `analysis.PLANE_EPS`.
const PLANE_EPS: f64 = 1.0e-6;

/// `analysis.REST_NUDGE`: how far a resting body rises past an obstacle whose top rounds to
/// at or below the height it already holds.
const REST_NUDGE: f64 = 1.0e-4;

/// One convex brush: half-spaces `a x + b y + c z <= d` and its bounds.
pub struct Convex {
    pub planes: Vec<(f64, f64, f64, f64)>,
    pub mins: [f64; 3],
    pub maxs: [f64; 3],
}

/// One patch collision facet: its front-plane normal and its corners in order.
pub struct Facet {
    pub normal: (f64, f64, f64),
    pub corners: Vec<(f64, f64, f64)>,
}

/// What `voxelize` was asked to judge a floor by.
pub struct Params {
    pub origin: [f64; 3],
    pub dims: [usize; 3],
    pub cell: f64,
    pub headroom_cells: usize,
    pub crouch_cells: usize,
    pub sample_cap: usize,
    pub facet_shift: f64,
    /// How far the body may rise above the floor's own top to clear what is under it.
    pub step: f64,
}

/// The grid, sparse: solid runs per column and the standing cells.
#[derive(Default)]
pub struct Voxels {
    /// `offsets[ix * ny + iy] .. offsets[ix * ny + iy + 1]` index `bounds`.
    pub offsets: Vec<u32>,
    /// Flattened half-open runs `[start, end)` of solid cells, ascending, maximal.
    pub bounds: Vec<u32>,
    /// `(ix, iy, standing z's, the height the body rests at on each)` in column order.
    pub walkable: Vec<(u32, u32, Vec<u32>, Vec<f64>)>,
    pub restricted: Vec<(u32, u32, u32)>,
    pub clearance_restricted: Vec<(u32, u32, u32)>,
    pub solid_cells: u64,
    pub floor_cells: u64,
    pub crouch_only: u64,
    pub too_low: u64,
    pub body_blocked: u64,
}

impl Convex {
    /// `Solid.z_span`, exactly.
    fn z_span(&self, x: f64, y: f64) -> Option<(f64, f64)> {
        let (mut lo, mut hi) = (self.mins[2], self.maxs[2]);
        for &(a, b, c, d) in &self.planes {
            let rest = d - a * x - b * y;
            if c > PLANE_EPS {
                let bound = rest / c;
                if bound < hi {
                    hi = bound;
                }
            } else if c < -PLANE_EPS {
                let bound = rest / c;
                if bound > lo {
                    lo = bound;
                }
            } else if rest < 0.0 {
                return None;
            }
        }
        (lo <= hi).then_some((lo, hi))
    }

    /// `segment_clear(p, p)` failing on this one solid: the point is `PLANE_EPS` inside.
    fn holds_point(&self, p: [f64; 3]) -> bool {
        for i in 0..3 {
            if p[i] < self.mins[i] || p[i] > self.maxs[i] {
                return false;
            }
        }
        for &(a, b, c, d) in &self.planes {
            let distance = (d - PLANE_EPS) - (a * p[0] + b * p[1] + c * p[2]);
            if distance < 0.0 {
                return false;
            }
        }
        true
    }
}

/// `analysis._rest_height`, exactly: the lowest height at or above `top` where a body at
/// `(x, y)` is clear of every obstacle in `candidates`, rising to the highest top of those
/// that hold it each time (a set, so the order they are listed in cannot matter). None when
/// that is more than `step` above `top`.
fn rest_height(
    candidates: &[usize],
    obstacles: &[Placed<Convex>],
    x: f64,
    y: f64,
    top: f64,
    step: f64,
) -> Option<f64> {
    let mut z = top;
    loop {
        let point = [x, y, z];
        let mut held = false;
        let mut next = f64::NEG_INFINITY;
        for &i in candidates {
            let b = &obstacles[i].item;
            if b.holds_point(point) {
                held = true;
                next = next.max(b.z_span(x, y)?.1);
            }
        }
        if !held {
            return Some(z);
        }
        // Two obstacles sharing a top can round either side of it: rise past the noise.
        if next <= z {
            next = z + REST_NUDGE;
        }
        if next - top > step {
            return None;
        }
        z = next;
    }
}

/// `analysis._clip_polygon`, exactly.
fn clip(
    points: &[(f64, f64, f64)],
    axis: usize,
    bound: f64,
    keep_below: bool,
) -> Vec<(f64, f64, f64)> {
    let get = |p: &(f64, f64, f64)| match axis {
        0 => p.0,
        1 => p.1,
        _ => p.2,
    };
    let mut out = Vec::with_capacity(points.len() + 2);
    let count = points.len();
    for k in 0..count {
        let (a, b) = (points[k], points[(k + 1) % count]);
        let da = if keep_below {
            get(&a) - bound
        } else {
            bound - get(&a)
        };
        let db = if keep_below {
            get(&b) - bound
        } else {
            bound - get(&b)
        };
        if da <= 0.0 {
            out.push(a);
        }
        if (da < 0.0 && 0.0 < db) || (db < 0.0 && 0.0 < da) {
            let t = da / (da - db);
            out.push((
                a.0 + (b.0 - a.0) * t,
                a.1 + (b.1 - a.1) * t,
                a.2 + (b.2 - a.2) * t,
            ));
        }
    }
    out
}

fn ceil_i(v: f64) -> i64 {
    v.ceil() as i64
}

fn floor_i(v: f64) -> i64 {
    v.floor() as i64
}

/// Clamp an inclusive index range to `[0, n)`; None when nothing is left.
fn clamp(lo: i64, hi: i64, n: usize) -> Option<(usize, usize)> {
    let lo = lo.max(0);
    let hi = hi.min(n as i64 - 1);
    (lo <= hi).then_some((lo as usize, hi as usize))
}

struct Placed<T> {
    item: T,
    x: (usize, usize),
    y: (usize, usize),
}

/// Bucket items by the first slab they touch, so each slab activates only what reaches it.
fn starting<T>(placed: &[Placed<T>], nx: usize) -> Vec<Vec<usize>> {
    let mut starts = vec![Vec::new(); nx];
    for (i, p) in placed.iter().enumerate() {
        starts[p.x.0].push(i);
    }
    starts
}

/// Voxelize brushes and facets, then judge every floor.
///
/// A cell is solid when its centre is inside a brush or a facet passes through it, or when a
/// brush is thinner than a cell in that column and lies inside it between two centres; a floor
/// is an empty cell on a solid one; it is walkable with `headroom_cells` clear above it and,
/// when `clearance` is given, its centre point outside every expanded body obstacle.
pub fn voxelize(
    brushes: Vec<Convex>,
    facets: Vec<Facet>,
    clearance: Option<Vec<Convex>>,
    p: &Params,
) -> Voxels {
    let [nx, ny, nz] = p.dims;
    let [ox, oy, oz] = p.origin;
    let cell = p.cell;

    let brushes: Vec<Placed<Convex>> = brushes
        .into_iter()
        .filter_map(|b| {
            let x = clamp(
                ceil_i((b.mins[0] - ox) / cell - 0.5),
                floor_i((b.maxs[0] - ox) / cell - 0.5),
                nx,
            )?;
            let y = clamp(
                ceil_i((b.mins[1] - oy) / cell - 0.5),
                floor_i((b.maxs[1] - oy) / cell - 0.5),
                ny,
            )?;
            Some(Placed { item: b, x, y })
        })
        .collect();

    let shift = p.facet_shift;
    let facets: Vec<Placed<Vec<(f64, f64, f64)>>> = facets
        .into_iter()
        .filter_map(|f| {
            let (a, b, c) = f.normal;
            let polygon: Vec<(f64, f64, f64)> = f
                .corners
                .iter()
                .map(|q| (q.0 - a * shift, q.1 - b * shift, q.2 - c * shift))
                .collect();
            if polygon.is_empty() {
                return None;
            }
            let min = |g: fn(&(f64, f64, f64)) -> f64| {
                polygon.iter().map(g).fold(f64::INFINITY, f64::min)
            };
            let max = |g: fn(&(f64, f64, f64)) -> f64| {
                polygon.iter().map(g).fold(f64::NEG_INFINITY, f64::max)
            };
            let x = clamp(
                floor_i((min(|q| q.0) - ox) / cell),
                floor_i((max(|q| q.0) - ox) / cell),
                nx,
            )?;
            let y = clamp(
                floor_i((min(|q| q.1) - oy) / cell),
                floor_i((max(|q| q.1) - oy) / cell),
                ny,
            )?;
            Some(Placed {
                item: polygon,
                x,
                y,
            })
        })
        .collect();

    // Body obstacles are tested exactly against each point's own bounds; the slab and row
    // ranges are only a broad phase, widened a cell so rounding cannot drop a candidate.
    let clearance: Vec<Placed<Convex>> = clearance
        .unwrap_or_default()
        .into_iter()
        .filter_map(|b| {
            let x = clamp(
                floor_i((b.mins[0] - ox) / cell - 0.5) - 1,
                ceil_i((b.maxs[0] - ox) / cell - 0.5) + 1,
                nx,
            )?;
            let y = clamp(
                floor_i((b.mins[1] - oy) / cell - 0.5) - 1,
                ceil_i((b.maxs[1] - oy) / cell - 0.5) + 1,
                ny,
            )?;
            Some(Placed { item: b, x, y })
        })
        .collect();

    let brush_starts = starting(&brushes, nx);
    let facet_starts = starting(&facets, nx);
    let clear_starts = starting(&clearance, nx);
    let (mut brush_on, mut facet_on, mut clear_on) = (Vec::new(), Vec::new(), Vec::new());
    let mut rows: Vec<Vec<usize>> = vec![Vec::new(); ny];

    let mut out = Voxels {
        offsets: Vec::with_capacity(nx * ny + 1),
        ..Default::default()
    };
    out.offsets.push(0);
    let mut slab = vec![0u8; ny * nz];
    // The exact top of the solid ending in each cell, so a floor is judged at its surface
    // rather than at the cell boundary voxel rounding put it on.
    let mut tops = vec![f64::NEG_INFINITY; ny * nz];

    for ix in 0..nx {
        slab.fill(0);
        tops.fill(f64::NEG_INFINITY);
        brush_on.extend_from_slice(&brush_starts[ix]);
        brush_on.retain(|&i| brushes[i].x.1 >= ix);
        facet_on.extend_from_slice(&facet_starts[ix]);
        facet_on.retain(|&i| facets[i].x.1 >= ix);
        clear_on.extend_from_slice(&clear_starts[ix]);
        clear_on.retain(|&i| clearance[i].x.1 >= ix);

        let x = ox + (ix as f64 + 0.5) * cell;
        for &i in &brush_on {
            let b = &brushes[i];
            for iy in b.y.0..=b.y.1 {
                let y = oy + (iy as f64 + 0.5) * cell;
                let Some((s0, s1)) = b.item.z_span(x, y) else {
                    continue;
                };
                let (mut lo, mut hi) = (
                    ceil_i((s0 - oz) / cell - 0.5),
                    floor_i((s1 - oz) / cell - 0.5),
                );
                if lo > hi {
                    // Thinner than a cell here, between two centres: fill the cell it lies in,
                    // or a thin floor slab over a void vanishes and its floor with it.
                    if s1 <= s0 {
                        continue;
                    }
                    lo = floor_i(((s0 + s1) / 2.0 - oz) / cell);
                    hi = lo;
                }
                if let Some((z0, z1)) = clamp(lo, hi, nz) {
                    slab[iy * nz + z0..=iy * nz + z1].fill(1);
                    let t = &mut tops[iy * nz + z1];
                    *t = t.max(s1);
                }
            }
        }

        let x_lo = ox + ix as f64 * cell;
        for &i in &facet_on {
            let f = &facets[i];
            let strip = clip(&f.item, 0, x_lo, false);
            let strip = if strip.is_empty() {
                strip
            } else {
                clip(&strip, 0, x_lo + cell, true)
            };
            if strip.is_empty() {
                continue;
            }
            for iy in f.y.0..=f.y.1 {
                let y_lo = oy + iy as f64 * cell;
                let piece = clip(&strip, 1, y_lo, false);
                let piece = if piece.is_empty() {
                    piece
                } else {
                    clip(&piece, 1, y_lo + cell, true)
                };
                if piece.is_empty() {
                    continue;
                }
                let n = piece.len() as f64;
                let mx = piece.iter().fold(0.0, |s, q| s + q.0) / n;
                let my = piece.iter().fold(0.0, |s, q| s + q.1) / n;
                if !(x_lo < mx && mx < x_lo + cell && y_lo < my && my < y_lo + cell) {
                    continue;
                }
                let zmin = piece.iter().map(|q| q.2).fold(f64::INFINITY, f64::min);
                let zmax = piece.iter().map(|q| q.2).fold(f64::NEG_INFINITY, f64::max);
                let z0 = floor_i((zmin - oz) / cell).max(0);
                let z1 = (nz as i64 - 1).min(z0.max(ceil_i((zmax - oz) / cell) - 1));
                if z0 > z1 {
                    continue;
                }
                slab[iy * nz + z0 as usize..=iy * nz + z1 as usize].fill(1);
                let t = &mut tops[iy * nz + z1 as usize];
                *t = t.max(zmax);
            }
        }

        for row in rows.iter_mut() {
            row.clear();
        }
        for &i in &clear_on {
            let b = &clearance[i];
            for row in &mut rows[b.y.0..=b.y.1] {
                row.push(i);
            }
        }

        for iy in 0..ny {
            let column = &slab[iy * nz..(iy + 1) * nz];
            let first = out.bounds.len();
            let mut z = 0;
            while z < nz {
                if column[z] == 0 {
                    z += 1;
                    continue;
                }
                let start = z;
                while z < nz && column[z] == 1 {
                    z += 1;
                }
                out.bounds.push(start as u32);
                out.bounds.push(z as u32);
                out.solid_cells += (z - start) as u64;
            }
            let runs = &out.bounds[first..];
            let mut walkable = Vec::new();
            let mut rests = Vec::new();
            for k in (0..runs.len()).step_by(2) {
                let stand_on = runs[k + 1] as usize;
                if stand_on >= nz {
                    break; // the solid reaches the top: nothing above it is modelled
                }
                out.floor_cells += 1;
                let next = if k + 2 < runs.len() {
                    runs[k + 2] as usize
                } else {
                    nz
                };
                let clear = next - stand_on;
                let at = (ix as u32, iy as u32, stand_on as u32);
                if clear >= p.headroom_cells {
                    let boundary = oz + stand_on as f64 * cell;
                    let rest = if clearance.is_empty() {
                        Some(boundary)
                    } else {
                        let top = tops[iy * nz + stand_on - 1];
                        let top = if top.is_finite() { top } else { boundary };
                        let y = oy + (iy as f64 + 0.5) * cell;
                        rest_height(&rows[iy], &clearance, x, y, top, p.step)
                    };
                    match rest {
                        Some(z) => {
                            walkable.push(stand_on as u32);
                            rests.push(z);
                        }
                        None => {
                            out.body_blocked += 1;
                            if out.clearance_restricted.len() < p.sample_cap {
                                out.clearance_restricted.push(at);
                            }
                        }
                    }
                    continue;
                }
                if clear >= p.crouch_cells {
                    out.crouch_only += 1;
                } else {
                    out.too_low += 1;
                }
                if out.restricted.len() < p.sample_cap {
                    out.restricted.push(at);
                }
            }
            if !walkable.is_empty() {
                out.walkable.push((ix as u32, iy as u32, walkable, rests));
            }
            out.offsets.push(out.bounds.len() as u32);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cube(mins: [f64; 3], maxs: [f64; 3]) -> Convex {
        Convex {
            planes: vec![
                (1.0, 0.0, 0.0, maxs[0]),
                (-1.0, 0.0, 0.0, -mins[0]),
                (0.0, 1.0, 0.0, maxs[1]),
                (0.0, -1.0, 0.0, -mins[1]),
                (0.0, 0.0, 1.0, maxs[2]),
                (0.0, 0.0, -1.0, -mins[2]),
            ],
            mins,
            maxs,
        }
    }

    fn params(dims: [usize; 3]) -> Params {
        Params {
            origin: [0.0; 3],
            dims,
            cell: 16.0,
            headroom_cells: 4,
            crouch_cells: 2,
            sample_cap: 4096,
            facet_shift: 0.01,
            step: 18.0,
        }
    }

    #[test]
    fn a_floor_under_open_space_is_walkable() {
        // A 64-unit floor slab, 128 units of air above it.
        let floor = cube([0.0, 0.0, 0.0], [64.0, 64.0, 16.0]);
        let v = voxelize(vec![floor], vec![], None, &params([4, 4, 9]));
        assert_eq!(v.walkable.len(), 16);
        assert!(v
            .walkable
            .iter()
            .all(|(_, _, z, r)| z == &vec![1] && r == &vec![16.0]));
        assert_eq!(v.solid_cells, 16);
        assert_eq!(v.offsets.len(), 17);
        assert_eq!(&v.bounds[0..2], &[0, 1]);
    }

    #[test]
    fn a_low_ceiling_restricts_and_an_obstacle_blocks() {
        let floor = cube([0.0, 0.0, 0.0], [64.0, 64.0, 16.0]);
        let ceiling = cube([0.0, 0.0, 48.0], [32.0, 64.0, 64.0]);
        let body = cube([44.0, 0.0, 0.0], [64.0, 64.0, 100.0]);
        let v = voxelize(
            vec![floor, ceiling],
            vec![],
            Some(vec![body]),
            &params([4, 4, 9]),
        );
        // x columns 0-1 have 2 cells clear: crouch only. Column 3 (centre 56) is blocked.
        assert_eq!(v.crouch_only, 8);
        assert_eq!(v.body_blocked, 4);
        let on_floor = |w: &&(u32, u32, Vec<u32>, Vec<f64>)| w.2.contains(&1);
        assert_eq!(
            v.walkable
                .iter()
                .filter(on_floor)
                .map(|w| w.0)
                .collect::<Vec<_>>(),
            vec![2; 4]
        );
        // The ceiling's own roof, with 5 cells of air, is walkable.
        assert!(v
            .walkable
            .iter()
            .filter(|w| w.0 < 2)
            .all(|w| w.2 == vec![4]));
        assert_eq!(v.restricted.len(), 8);
    }

    fn expanded(b: &Convex, radius: f64, height: f64) -> Convex {
        Convex {
            planes: b
                .planes
                .iter()
                .map(|&(a, bb, c, d)| {
                    (
                        a,
                        bb,
                        c,
                        d + radius * (a.abs() + bb.abs()) + height * (-c).max(0.0),
                    )
                })
                .collect(),
            mins: [b.mins[0] - radius, b.mins[1] - radius, b.mins[2] - height],
            maxs: [b.maxs[0] + radius, b.maxs[1] + radius, b.maxs[2]],
        }
    }

    #[test]
    fn a_floor_off_the_cell_grid_rests_the_body_on_its_surface() {
        // Floor top at 5 (not a multiple of 16), a 1-unit rubble strip on it, a wall.
        let floor = cube([0.0, 0.0, -32.0], [64.0, 64.0, 5.0]);
        let rubble = cube([20.0, 0.0, 5.0], [24.0, 64.0, 6.0]);
        let wall = cube([60.0, 0.0, 5.0], [64.0, 64.0, 100.0]);
        let body: Vec<Convex> = [&floor, &rubble, &wall]
            .iter()
            .map(|b| expanded(b, 18.0, 56.0))
            .collect();
        let mut p = params([4, 4, 9]);
        p.origin = [0.0, 0.0, -32.0];
        let v = voxelize(vec![floor, rubble, wall], vec![], Some(body), &p);
        let rest = |ix: u32| {
            v.walkable
                .iter()
                .find(|w| w.0 == ix && w.1 == 1)
                .map(|w| w.3[0])
        };
        // x=8 and x=24 and x=40 are within 18 of the rubble (2..42): the body rests on it.
        assert_eq!(rest(0), Some(6.0));
        assert_eq!(rest(1), Some(6.0));
        assert_eq!(rest(2), Some(6.0));
        assert_eq!(rest(3), None, "a wall is not stepped onto");
    }
}
