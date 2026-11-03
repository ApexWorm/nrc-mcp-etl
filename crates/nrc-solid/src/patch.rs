//! Curved surfaces as `patchDef2` control grids: cylinders, arches and bevels.
//!
//! Brushes cannot be curved, so the engine draws curves from Bézier patches. A circle is made
//! of quarter arcs, each a quadratic Bézier whose middle control point is the corner of the
//! square the quarter sits in (the standard construction; a mapper's editor makes the same
//! grid). Quarters share their end points, so `q` quarters need `2q + 1` columns. Along the
//! extrusion the surface is straight: three rows, the middle one half way.
//!
//! The output is `rows[width][height][5]` - x, y, z, u, v - the on-disk nesting of a
//! `patchDef2`. Texture coordinates run in texture repeats, one per `texture_units` of
//! surface, so a curve tiles at the same density as the brushes around it.

use std::f64::consts::FRAC_PI_2;

/// Which way a patch faces: the side the engine draws.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Facing {
    Out,
    In,
}

#[derive(Clone, Debug)]
pub struct PatchSpec {
    pub kind: String,
    /// The centre of the circle at the start of the extrusion.
    pub centre: [f64; 3],
    pub radius: f64,
    /// The depth along the positive extrusion axis.
    pub length: f64,
    /// Cylinder/bevel: x, y or z. Arch: x or y (z retains the historical y default).
    pub axis: char,
    /// Bevel: which quarter, as the angle in degrees its arc starts at (0, 90, 180, 270).
    pub start_deg: f64,
    pub facing: Facing,
    pub texture_units: f64,
}

/// The control grid, or why the spec cannot make one.
pub fn generate(spec: &PatchSpec) -> Result<Vec<Vec<[f64; 5]>>, String> {
    if !(spec.radius > 0.0 && spec.length > 0.0) {
        return Err("radius and length must be greater than zero".into());
    }
    if !matches!(spec.axis, 'x' | 'y' | 'z') {
        return Err("axis must be x, y or z".into());
    }
    let (quarters, start) = match spec.kind.as_str() {
        "cylinder" => (4, 0.0),
        "arch" => (2, 0.0),
        "bevel" => {
            if spec.start_deg.rem_euclid(90.0) != 0.0 {
                return Err("a bevel starts at 0, 90, 180 or 270 degrees".into());
            }
            (1, spec.start_deg.to_radians())
        }
        other => {
            return Err(format!(
                "unknown patch kind {other:?}: cylinder, arch or bevel"
            ))
        }
    };
    // The arc in its own plane: (a, b) around the centre, a quarter at a time.
    let mut arc: Vec<(f64, f64)> = Vec::with_capacity(2 * quarters + 1);
    for q in 0..quarters {
        let a0 = start + q as f64 * FRAC_PI_2;
        let a1 = a0 + FRAC_PI_2;
        let (c0, s0, c1, s1) = (
            snap(a0.cos()),
            snap(a0.sin()),
            snap(a1.cos()),
            snap(a1.sin()),
        );
        arc.push((c0, s0));
        arc.push((c0 + c1, s0 + s1)); // the corner of the quarter's square
        if q == quarters - 1 {
            arc.push((c1, s1));
        }
    }
    if spec.facing == Facing::In {
        arc.reverse();
    }
    let [cx, cy, cz] = spec.centre;
    let r = spec.radius;
    let quarter_len = FRAC_PI_2 * r;
    let rows = arc
        .iter()
        .enumerate()
        .map(|(i, &(a, b))| {
            // Arc length to this column: each column is half a quarter along.
            let u = (i as f64 * quarter_len / 2.0) / spec.texture_units;
            (0..3)
                .map(|j| {
                    let t = j as f64 * spec.length / 2.0;
                    let v = t / spec.texture_units;
                    let p = match (spec.kind.as_str(), spec.axis) {
                        // The arch's circle stands in the plane across its axis, bulging up.
                        ("arch", 'x') => [cx + t, cy + a * r, cz + b * r],
                        ("arch", _) => [cx + a * r, cy + t, cz + b * r],
                        // Cyclic permutations preserve the vertical cylinder's winding.
                        (_, 'x') => [cx + t, cy + a * r, cz + b * r],
                        (_, 'y') => [cx + b * r, cy + t, cz + a * r],
                        _ => [cx + a * r, cy + b * r, cz + t],
                    };
                    [p[0], p[1], p[2], u, v]
                })
                .collect()
        })
        .collect();
    Ok(rows)
}

/// cos and sin of right angles, exactly: 6e-17 is not zero, and it would leak into the file.
fn snap(v: f64) -> f64 {
    let r = v.round();
    if (v - r).abs() < 1e-12 {
        r
    } else {
        v
    }
}

// ---------------------------------------------------------------------------------------------
// Collision: what the engine actually collides with
// ---------------------------------------------------------------------------------------------
//
// The engine does not collide with the rendered curve. At load it rebuilds a coarser surface
// from the control points (`CM_GeneratePatchCollide`, etlegacy `qcommon/cm_patch.c:1451`):
// each row of quadratic segments is subdivided until the chord midpoint lies within
// `SUBDIVIDE_DISTANCE` of the curve, segments already that flat are collapsed to straight
// lines, duplicate columns are dropped, and the grid is transposed and treated the same way in
// the other direction. Every cell of the resulting grid becomes one quad facet, or two
// triangle facets when its halves are not coplanar. A trace whose deciding plane is a facet's
// back plane is not clipped (`cm_patch.c:1936`, "never clip against the back side"); for a
// player box the facet's bevels usually decide first, and in the engine a pipe facing inward
// stops a player outside it just as one facing out does (bench/dogfood/patches.py).
//
// This port follows those steps in the engine's single precision so the grid it produces is the
// engine's grid, not a finer or coarser one. The collision planes it reports carry the
// engine's winding, so `normal` points to the side that blocks.

/// `cm_patch.h:129`: the collision surface is never more than this far from the true curve.
pub const SUBDIVIDE_DISTANCE: f32 = 16.0;
/// `cm_patch.h:115`: the fixed grid the engine subdivides into. A larger grid is an error.
pub const MAX_GRID_SIZE: usize = 129;
/// `cm_patch.h:77`: facets per patch; more is an `ERR_DROP` at map load.
pub const MAX_FACETS: usize = 1024;
/// `cm_patch.h:130`.
const PLANE_TRI_EPSILON: f32 = 0.1;
/// `cm_patch.h:131`.
const WRAP_POINT_EPSILON: f32 = 0.1;
/// `cm_patch.c:412`.
const POINT_EPSILON: f32 = 0.1;

/// One collision facet: a planar triangle or quad that blocks from the side `normal` faces.
#[derive(Clone, Debug, PartialEq)]
pub struct CollisionFacet {
    /// Unit normal of the facet's surface plane, pointing to its blocking side.
    pub normal: [f64; 3],
    /// Plane distance: `normal . p == dist` on the facet.
    pub dist: f64,
    /// The facet's corners in order, three or four of them.
    pub points: Vec<[f64; 3]>,
}

/// The engine's collision surface for a patch, and the size of the grid it was built from.
#[derive(Clone, Debug, PartialEq)]
pub struct Collision {
    pub facets: Vec<CollisionFacet>,
    /// Width and height of the subdivided grid (after the transpose, as the engine holds it).
    pub grid: (usize, usize),
}

type P3 = [f32; 3];

struct CGrid {
    /// `points[w][h]`, as `cGrid_t` holds them.
    points: Vec<Vec<P3>>,
    wrap_width: bool,
    wrap_height: bool,
}

impl CGrid {
    fn width(&self) -> usize {
        self.points.len()
    }
    fn height(&self) -> usize {
        self.points.first().map_or(0, Vec::len)
    }
}

/// `CM_NeedsSubdivision`, `cm_patch.c:193`.
fn needs_subdivision(a: P3, b: P3, c: P3) -> bool {
    let mut d = 0.0f32;
    for i in 0..3 {
        let lmid = 0.5 * (a[i] + c[i]);
        let cmid = 0.5 * (0.5 * (a[i] + b[i]) + 0.5 * (b[i] + c[i]));
        d += (cmid - lmid) * (cmid - lmid);
    }
    d.sqrt() >= SUBDIVIDE_DISTANCE
}

/// `CM_Subdivide`, `cm_patch.c:230`: the sequence becomes a, out1, out2, out3, c.
fn subdivide(a: P3, b: P3, c: P3) -> (P3, P3, P3) {
    let mut o1 = [0.0; 3];
    let mut o2 = [0.0; 3];
    let mut o3 = [0.0; 3];
    for i in 0..3 {
        o1[i] = 0.5 * (a[i] + b[i]);
        o3[i] = 0.5 * (b[i] + c[i]);
        o2[i] = 0.5 * (o1[i] + o3[i]);
    }
    (o1, o2, o3)
}

/// `CM_SetGridWrapWidth`, `cm_patch.c:308`.
fn set_wrap_width(g: &mut CGrid) {
    let w = g.width();
    g.wrap_width = (0..g.height()).all(|j| {
        (0..3).all(|k| {
            let d = g.points[0][j][k] - g.points[w - 1][j][k];
            (-WRAP_POINT_EPSILON..=WRAP_POINT_EPSILON).contains(&d)
        })
    });
}

/// `CM_SubdivideGridColumns`, `cm_patch.c:345`.
fn subdivide_columns(g: &mut CGrid) -> Result<(), String> {
    let mut i = 0;
    while i + 2 < g.width() {
        let h = g.height();
        let flat = (0..h)
            .all(|j| !needs_subdivision(g.points[i][j], g.points[i + 1][j], g.points[i + 2][j]));
        if flat {
            // Close enough to straight everywhere: drop the approximating column.
            g.points.remove(i + 1);
            i += 1;
            continue;
        }
        if g.width() + 2 > MAX_GRID_SIZE {
            return Err(format!(
                "subdivides past the engine's {MAX_GRID_SIZE}-point collision grid"
            ));
        }
        let mut c1 = Vec::with_capacity(h);
        let mut c2 = Vec::with_capacity(h);
        let mut c3 = Vec::with_capacity(h);
        for j in 0..h {
            let (a, b, c) = subdivide(g.points[i][j], g.points[i + 1][j], g.points[i + 2][j]);
            c1.push(a);
            c2.push(b);
            c3.push(c);
        }
        g.points[i + 1] = c1;
        g.points.insert(i + 2, c2);
        g.points.insert(i + 3, c3);
        // The new approximating column at i+1 may need collapsing or more splitting.
    }
    Ok(())
}

/// `CM_ComparePoints`, `cm_patch.c:420`.
fn same_point(a: P3, b: P3) -> bool {
    (0..3).all(|k| (-POINT_EPSILON..=POINT_EPSILON).contains(&(a[k] - b[k])))
}

/// `CM_RemoveDegenerateColumns`, `cm_patch.c:445`.
fn remove_degenerate_columns(g: &mut CGrid) {
    let mut i = 0;
    while i + 1 < g.width() {
        let h = g.height();
        if (0..h).all(|j| same_point(g.points[i][j], g.points[i + 1][j])) {
            g.points.remove(i + 1);
            continue; // check against the next column
        }
        i += 1;
    }
}

/// `CM_TransposeGrid`, `cm_patch.c:246`.
fn transpose(g: &mut CGrid) {
    let (w, h) = (g.width(), g.height());
    let points = (0..h)
        .map(|j| (0..w).map(|i| g.points[i][j]).collect())
        .collect();
    g.points = points;
    std::mem::swap(&mut g.wrap_width, &mut g.wrap_height);
}

/// `CM_PlaneFromPoints`, `cm_patch.c:163`: normal = (c - a) x (b - a), in double precision.
fn plane_from_points(a: P3, b: P3, c: P3) -> Option<[f32; 4]> {
    let d1: [f64; 3] = std::array::from_fn(|k| (b[k] - a[k]) as f64);
    let d2: [f64; 3] = std::array::from_fn(|k| (c[k] - a[k]) as f64);
    let n = [
        d2[1] * d1[2] - d2[2] * d1[1],
        d2[2] * d1[0] - d2[0] * d1[2],
        d2[0] * d1[1] - d2[1] * d1[0],
    ];
    let len = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt();
    if len == 0.0 {
        return None;
    }
    let n = [n[0] / len, n[1] / len, n[2] / len];
    let d = n[0] * a[0] as f64 + n[1] * a[1] as f64 + n[2] * a[2] as f64;
    Some([n[0] as f32, n[1] as f32, n[2] as f32, d as f32])
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

/// `CM_FindPlane`, `cm_patch.c:598`: a triangle reuses an existing plane when it faces the same
/// way and all three corners lie within `PLANE_TRI_EPSILON` of it. That reuse is what decides
/// whether a grid cell becomes one quad facet or two triangles.
fn find_plane(planes: &mut Vec<[f32; 4]>, p1: P3, p2: P3, p3: P3) -> Option<usize> {
    let plane = plane_from_points(p1, p2, p3)?;
    let near = |pl: &[f32; 4], p: P3| {
        let d = dot(&p, pl) - pl[3];
        (-PLANE_TRI_EPSILON..=PLANE_TRI_EPSILON).contains(&d)
    };
    for (i, pl) in planes.iter().enumerate() {
        if dot(&plane, pl) < 0.0 {
            continue;
        }
        if near(pl, p1) && near(pl, p2) && near(pl, p3) {
            return Some(i);
        }
    }
    planes.push(plane);
    Some(planes.len() - 1)
}

/// The engine's collision facets for a patch whose control points are `rows[w][h]`, the
/// on-disk nesting of a `patchDef2` (q3map2 `patch.cpp:218-223` stores them so that the BSP's
/// `points[h * width + w]` is `rows[w][h]`, which `cm_patch.c:1482` reads back as
/// `grid.points[w][h]`).
///
/// Errors are the cases the engine refuses at map load (`cm_patch.c:1457-1471`, `MAX_FACETS`)
/// or that would overflow its fixed grid.
pub fn collision(rows: &[Vec<[f64; 3]>]) -> Result<Collision, String> {
    let width = rows.len();
    let height = rows.first().map_or(0, Vec::len);
    if rows.iter().any(|r| r.len() != height) {
        return Err("rows of unequal length".into());
    }
    if width <= 2 || height <= 2 {
        return Err(format!(
            "bad dimensions {width}x{height}: both must be at least 3"
        ));
    }
    if width % 2 == 0 || height % 2 == 0 {
        return Err(format!(
            "even dimensions {width}x{height} are invalid for quadratic meshes"
        ));
    }
    if width > MAX_GRID_SIZE || height > MAX_GRID_SIZE {
        return Err(format!(
            "larger than the engine's {MAX_GRID_SIZE}-point grid"
        ));
    }
    let mut g = CGrid {
        points: rows
            .iter()
            .map(|r| {
                r.iter()
                    .map(|p| [p[0] as f32, p[1] as f32, p[2] as f32])
                    .collect()
            })
            .collect(),
        wrap_width: false,
        wrap_height: false,
    };
    set_wrap_width(&mut g);
    subdivide_columns(&mut g)?;
    remove_degenerate_columns(&mut g);
    transpose(&mut g);
    set_wrap_width(&mut g);
    subdivide_columns(&mut g)?;
    remove_degenerate_columns(&mut g);
    let _ = (g.wrap_width, g.wrap_height); // only border planes use wrapping, not facet shape

    // CM_PatchCollideFromGrid, cm_patch.c:1241.
    let (w, h) = (g.width(), g.height());
    let mut planes: Vec<[f32; 4]> = Vec::new();
    let mut grid_planes = vec![vec![[None, None]; h.saturating_sub(1)]; w.saturating_sub(1)];
    for i in 0..w.saturating_sub(1) {
        for j in 0..h.saturating_sub(1) {
            let p = &g.points;
            grid_planes[i][j][0] = find_plane(&mut planes, p[i][j], p[i + 1][j], p[i + 1][j + 1]);
            grid_planes[i][j][1] = find_plane(&mut planes, p[i + 1][j + 1], p[i][j + 1], p[i][j]);
        }
    }
    let wide = |p: P3| [p[0] as f64, p[1] as f64, p[2] as f64];
    let facet = |plane: usize, pts: &[P3]| {
        let pl = planes[plane];
        CollisionFacet {
            normal: [pl[0] as f64, pl[1] as f64, pl[2] as f64],
            dist: pl[3] as f64,
            points: pts.iter().map(|&p| wide(p)).collect(),
        }
    };
    let mut facets = Vec::new();
    for i in 0..w.saturating_sub(1) {
        for j in 0..h.saturating_sub(1) {
            let p = &g.points;
            let [t0, t1] = grid_planes[i][j];
            if t0 == t1 {
                // Both halves on one plane: one quad facet, or nothing if both degenerate.
                if let Some(pl) = t0 {
                    facets.push(facet(
                        pl,
                        &[p[i][j], p[i + 1][j], p[i + 1][j + 1], p[i][j + 1]],
                    ));
                }
                continue;
            }
            // A degenerate half has no surface plane and fails CM_ValidateFacet.
            if let Some(pl) = t0 {
                facets.push(facet(pl, &[p[i][j], p[i + 1][j], p[i + 1][j + 1]]));
            }
            if let Some(pl) = t1 {
                facets.push(facet(pl, &[p[i + 1][j + 1], p[i][j + 1], p[i][j]]));
            }
        }
    }
    if facets.len() > MAX_FACETS {
        return Err(format!(
            "{} collision facets, over the engine's MAX_FACETS of {MAX_FACETS}",
            facets.len()
        ));
    }
    Ok(Collision {
        facets,
        grid: (w, h),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(kind: &str) -> PatchSpec {
        PatchSpec {
            kind: kind.into(),
            centre: [0.0, 0.0, 0.0],
            radius: 32.0,
            length: 128.0,
            axis: if kind == "arch" { 'x' } else { 'z' },
            start_deg: 0.0,
            facing: Facing::Out,
            texture_units: 128.0,
        }
    }

    #[test]
    fn grids_have_the_odd_widths_quarters_need_and_three_rows() {
        for (kind, w) in [("cylinder", 9), ("arch", 5), ("bevel", 3)] {
            let g = generate(&spec(kind)).unwrap();
            assert_eq!(g.len(), w, "{kind}");
            assert!(g.iter().all(|row| row.len() == 3), "{kind}");
        }
    }

    #[test]
    fn a_cylinder_closes_on_itself_and_its_corners_are_square_corners() {
        let g = generate(&spec("cylinder")).unwrap();
        let first = &g[0][0][..3];
        let last = &g[8][0][..3];
        assert_eq!(first, last, "the circle closes");
        assert_eq!(&g[0][0][..3], &[32.0, 0.0, 0.0]);
        assert_eq!(
            &g[1][0][..3],
            &[32.0, 32.0, 0.0],
            "corner of the first quarter's square"
        );
        assert_eq!(&g[2][0][..3], &[0.0, 32.0, 0.0]);
        assert_eq!(g[4][2][2], 128.0, "the top row is at the full height");
    }

    #[test]
    fn an_arch_spans_its_axis_and_bulges_up() {
        let g = generate(&spec("arch")).unwrap();
        assert_eq!(&g[0][0][..3], &[0.0, 32.0, 0.0]);
        assert_eq!(&g[2][0][..3], &[0.0, 0.0, 32.0], "the crown");
        assert_eq!(&g[4][0][..3], &[0.0, -32.0, 0.0]);
        assert_eq!(g[2][2][0], 128.0, "depth runs along x");
    }

    #[test]
    fn cylinders_extrude_along_each_axis_with_consistent_outward_winding() {
        for (axis, min, max) in [
            ('x', [10.0, -12.0, -2.0], [138.0, 52.0, 62.0]),
            ('y', [-22.0, 20.0, -2.0], [42.0, 148.0, 62.0]),
            ('z', [-22.0, -12.0, 30.0], [42.0, 52.0, 158.0]),
        ] {
            let centre = [10.0, 20.0, 30.0];
            let grid = generate(&PatchSpec {
                axis,
                centre,
                ..spec("cylinder")
            })
            .unwrap();
            for d in 0..3 {
                let values = || grid.iter().flatten().map(|point| point[d]);
                assert_eq!(values().fold(f64::INFINITY, f64::min), min[d], "{axis}");
                assert_eq!(values().fold(f64::NEG_INFINITY, f64::max), max[d], "{axis}");
            }
            // At the first corner, arc tangent cross extrusion must point away from centre.
            let p = grid[0][0];
            let tangent = std::array::from_fn::<_, 3, _>(|d| grid[1][0][d] - p[d]);
            let along = std::array::from_fn::<_, 3, _>(|d| grid[0][1][d] - p[d]);
            let normal = [
                tangent[1] * along[2] - tangent[2] * along[1],
                tangent[2] * along[0] - tangent[0] * along[2],
                tangent[0] * along[1] - tangent[1] * along[0],
            ];
            let dot: f64 = (0..3).map(|d| normal[d] * (p[d] - centre[d])).sum();
            assert!(dot > 0.0, "{axis} must preserve outward facing");
        }
    }

    #[test]
    fn bevels_rotate_their_quarter_with_the_extrusion_axis() {
        for (axis, end) in [
            ('x', [128.0, 32.0, 32.0]),
            ('y', [32.0, 128.0, 32.0]),
            ('z', [32.0, 32.0, 128.0]),
        ] {
            let grid = generate(&PatchSpec {
                axis,
                ..spec("bevel")
            })
            .unwrap();
            assert_eq!(grid[1][2][..3], end, "{axis}: corner at the far end");
        }
    }

    #[test]
    fn facing_in_reverses_the_columns() {
        let out = generate(&spec("arch")).unwrap();
        let mut inward = generate(&PatchSpec {
            facing: Facing::In,
            ..spec("arch")
        })
        .unwrap();
        inward.reverse();
        let pos = |g: &Vec<Vec<[f64; 5]>>| -> Vec<[f64; 3]> {
            g.iter().map(|r| [r[0][0], r[0][1], r[0][2]]).collect()
        };
        assert_eq!(pos(&out), pos(&inward));
    }

    #[test]
    fn bad_specs_are_refused() {
        assert!(generate(&spec("sphere")).is_err());
        assert!(generate(&PatchSpec {
            radius: 0.0,
            ..spec("cylinder")
        })
        .is_err());
        assert!(generate(&PatchSpec {
            start_deg: 45.0,
            ..spec("bevel")
        })
        .is_err());
        assert!(generate(&PatchSpec {
            axis: 'q',
            ..spec("cylinder")
        })
        .is_err());
    }

    fn positions(g: &[Vec<[f64; 5]>]) -> Vec<Vec<[f64; 3]>> {
        g.iter()
            .map(|r| r.iter().map(|p| [p[0], p[1], p[2]]).collect())
            .collect()
    }

    #[test]
    fn a_flat_patch_collapses_to_one_quad_facing_the_drawn_side() {
        // Width runs along +x and height along +y: the renderer draws +z (tr_surface.c:1696-1706
        // emits (w,h) (w,h+1) (w+1,h), clockwise seen from +z), and the engine collides there.
        let rows: Vec<Vec<[f64; 3]>> = (0..3)
            .map(|w| {
                (0..3)
                    .map(|h| [w as f64 * 32.0, h as f64 * 32.0, 8.0])
                    .collect()
            })
            .collect();
        let c = collision(&rows).unwrap();
        assert_eq!(c.grid, (2, 2));
        assert_eq!(c.facets.len(), 1);
        let f = &c.facets[0];
        assert_eq!(f.points.len(), 4);
        assert_eq!(f.normal, [0.0, 0.0, 1.0]);
        assert_eq!(f.dist, 8.0);
    }

    #[test]
    fn small_circles_collapse_to_chords_and_large_ones_subdivide_once() {
        // A quarter's chord midpoint is r*sqrt(2)/4 from the curve: under 16 below r = 45.25.
        for (radius, chords_per_quarter) in [(24.0, 1), (44.0, 1), (46.0, 2), (80.0, 2)] {
            let g = generate(&PatchSpec {
                radius,
                ..spec("cylinder")
            })
            .unwrap();
            let c = collision(&positions(&g)).unwrap();
            // Transposed: the circle runs along the height after CM_TransposeGrid.
            assert_eq!(c.grid, (2, 4 * chords_per_quarter + 1), "radius {radius}");
        }
    }

    #[test]
    fn generated_surfaces_collide_on_the_side_they_are_drawn() {
        for (kind, facing, outward) in [
            ("cylinder", Facing::Out, true),
            ("cylinder", Facing::In, false),
            ("arch", Facing::In, false),
            ("arch", Facing::Out, true),
            ("bevel", Facing::Out, true),
        ] {
            let mut s = spec(kind);
            s.facing = facing;
            s.radius = 80.0;
            let c = collision(&positions(&generate(&s).unwrap())).unwrap();
            assert!(!c.facets.is_empty());
            let along = match s.axis {
                'x' => 0,
                'y' => 1,
                _ => 2,
            };
            for f in &c.facets {
                let n = f.points.len() as f64;
                let mid: Vec<f64> = (0..3)
                    .map(|k| f.points.iter().map(|p| p[k]).sum::<f64>() / n)
                    .collect();
                // Away from the axis, measured across it.
                let radial: f64 = (0..3)
                    .filter(|&k| k != along)
                    .map(|k| f.normal[k] * (mid[k] - s.centre[k]))
                    .sum();
                assert_eq!(radial > 0.0, outward, "{kind} {facing:?}");
                let on_plane: f64 = (0..3).map(|k| f.normal[k] * f.points[0][k]).sum();
                assert!((on_plane - f.dist).abs() < 1e-3);
            }
        }
    }

    #[test]
    fn engine_refusals_are_errors() {
        let row = |n: usize| vec![[0.0; 3]; n];
        assert!(collision(&vec![row(3); 2]).is_err(), "too narrow");
        assert!(collision(&vec![row(4); 3]).is_err(), "even height");
        let tall: Vec<Vec<[f64; 3]>> = (0..3)
            .map(|w| (0..131).map(|h| [w as f64, h as f64, 0.0]).collect())
            .collect();
        assert!(collision(&tall).is_err(), "over MAX_GRID_SIZE");
    }

    #[test]
    fn a_fully_degenerate_patch_has_no_facets() {
        let c = collision(&vec![vec![[5.0, 5.0, 5.0]; 3]; 3]).unwrap();
        assert!(c.facets.is_empty());
    }
}
