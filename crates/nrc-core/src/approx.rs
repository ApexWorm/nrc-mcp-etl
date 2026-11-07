//! Brush geometry in floating point, for brushes the exact path declines.
//!
//! [`crate::winding::brush_geometry`] derives a brush exactly, and refuses one whose plane
//! points are off the integer grid ([`Degeneracy::NotExactlyRepresentable`]) rather than
//! guess. That refusal is right for validation and wrong for a consumer that needs the
//! brush's *shape*: every brush of a map decompiled from a BSP has three-decimal plane points,
//! and leaving them all out of collision makes a decompiled map's walls read as open space.
//!
//! This module is that consumer's fallback, kept apart from the exact kernel so nothing exact
//! depends on it: q3map2's own method (a world-sized polygon on each face plane, clipped by
//! every other face), with tolerances, and corners welded and put back on the integer grid
//! when within tolerance of it. Nothing here is consulted for on-grid input.

use crate::math::{Aabb, Axis, Vec3, MAX_WORLD_COORD};
use crate::model::Face;
#[cfg(doc)]
use crate::winding::Degeneracy;

/// Why [`approx_geometry`] found no shape either.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ApproxDegeneracy {
    /// Fewer than four planes cannot bound a volume.
    TooFewFaces(usize),
    /// A face's three points are collinear or coincident, so it defines no plane.
    FaceHasNoPlane(usize),
    /// The half-spaces enclose no volume: contradictory planes, or a brush collapsed flat.
    EmptyIntersection,
    /// The planes leave the brush open on some side.
    Unbounded,
    /// A corner lies beyond the world limit, where q3map2 drops the whole brush.
    OutsideWorld,
}

impl std::fmt::Display for ApproxDegeneracy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApproxDegeneracy::TooFewFaces(n) => {
                write!(
                    f,
                    "only {n} face(s); at least 4 are needed to bound a volume"
                )
            }
            ApproxDegeneracy::FaceHasNoPlane(i) => {
                write!(f, "face {i} has collinear or coincident plane points")
            }
            ApproxDegeneracy::EmptyIntersection => write!(f, "the face planes enclose no volume"),
            ApproxDegeneracy::Unbounded => write!(f, "the face planes leave the brush open"),
            ApproxDegeneracy::OutsideWorld => write!(
                f,
                "a corner lies beyond the +/-{MAX_WORLD_COORD:.0} world limit, where the \
                 compiler drops the brush"
            ),
        }
    }
}

/// Plane-side tolerance of [`approx_geometry`], in world units.
///
/// Sized for the rounding a `.map` writer puts into plane points: `q3map2 -convert` prints
/// them to three decimals (`%.3f`, convert_map.cpp:222), so a written plane can sit about
/// 1e-3 off the compiled one at the brush. Ten times that still decides nothing a player
/// could feel.
pub const APPROX_EPSILON: f64 = 0.01;

/// How far apart two corners of [`approx_geometry`] may be and still be one corner, and how
/// far from the integer grid a corner may be and still be put on it, in world units.
///
/// Wider than [`APPROX_EPSILON`] because a corner is the meeting of three planes, and where
/// they meet at a shallow angle a thousandth of tilt in each moves it by several hundredths
/// (oasis decompiled has corners 0.06 off). This is q3map2's own weld distance: it clips
/// brush windings with `CLIP_EPSILON` 0.1 (q3map2.h:131) and treats points within
/// `ON_EPSILON` 0.1 of a plane as on it (polylib.h:61).
pub const APPROX_WELD: f64 = 0.1;

/// A brush's geometry derived in floating point, for faces the exact path declines.
///
/// Nothing here is exact: a vertex lies on a plane only to within [`APPROX_EPSILON`], so
/// redundant-plane and on-grid answers are tolerance answers. It exists for consumers that
/// need the brush's *shape* (collision, bounds, line of sight) and would otherwise have to
/// leave the brush out entirely, which is a far larger error than a hundredth of a unit.
#[derive(Clone, Debug, PartialEq)]
pub struct ApproxGeometry {
    /// Corner positions, merged to within [`APPROX_WELD`] and snapped to the integer grid
    /// when within that of it.
    pub vertices: Vec<Vec3>,
    /// One entry per input face, in input order: the face's plane (normal out of the brush)
    /// and its polygon as indices into `vertices`, in winding order. A face whose polygon was
    /// clipped away (or that repeats an earlier face's plane) has an empty index list.
    pub faces: Vec<(crate::math::Plane, Vec<usize>)>,
}

impl ApproxGeometry {
    pub fn bounds(&self) -> Aabb {
        let mut b = Aabb::EMPTY;
        for v in &self.vertices {
            b.extend(*v);
        }
        b
    }

    /// Faces bounding no area, as [`crate::winding::BrushGeometry::redundant_faces`] but to tolerance.
    pub fn redundant_faces(&self) -> Vec<usize> {
        self.faces
            .iter()
            .enumerate()
            .filter(|(_, (_, v))| v.len() < 3)
            .map(|(i, _)| i)
            .collect()
    }

    /// Smallest distance between opposite faces, as [`crate::winding::BrushGeometry::min_thickness`].
    pub fn min_thickness(&self) -> Option<f64> {
        let planes: Vec<_> = self
            .faces
            .iter()
            .filter(|(_, v)| v.len() >= 3)
            .map(|(p, _)| *p)
            .collect();
        let mut best: Option<f64> = None;
        for (i, a) in planes.iter().enumerate() {
            for b in &planes[i + 1..] {
                if a.normal.dot(b.normal) < -0.9998 {
                    let t = (-a.dist - b.dist).abs();
                    best = Some(best.map_or(t, |x: f64| x.min(t)));
                }
            }
        }
        best
    }

    /// Vertices off the integer grid (those within [`APPROX_WELD`] of it were snapped).
    pub fn off_grid_vertices(&self) -> Vec<usize> {
        let off = |c: f64| c != c.round();
        self.vertices
            .iter()
            .enumerate()
            .filter(|(_, v)| off(v.x) || off(v.y) || off(v.z))
            .map(|(i, _)| i)
            .collect()
    }
}

/// A square on `plane` large enough to cover the whole world: q3map2's
/// `BaseWindingForPlane`.
fn base_winding(plane: &crate::math::Plane) -> Vec<Vec3> {
    let n = plane.normal;
    let up = match n.major_axis() {
        Axis::Z => crate::math::vec3(1.0, 0.0, 0.0),
        _ => crate::math::vec3(0.0, 0.0, 1.0),
    };
    // Gram-Schmidt keeps `up` in the plane; the major-axis choice keeps it far from `n`.
    let up = match (up - n * up.dot(n)).normalized() {
        Some(u) => u,
        None => return Vec::new(),
    };
    let right = up.cross(n);
    let size = MAX_WORLD_COORD * 4.0;
    let origin = n * plane.dist;
    let (up, right) = (up * size, right * size);
    vec![
        origin - right + up,
        origin + right + up,
        origin + right - up,
        origin - right - up,
    ]
}

/// Keep the part of `w` behind `plane` (where the brush is), treating points within `eps`
/// of it as on it: q3map2's `ChopWindingInPlace`.
fn chop_behind(w: &[Vec3], plane: &crate::math::Plane, eps: f64) -> Vec<Vec3> {
    let dists: Vec<f64> = w.iter().map(|p| plane.distance_to(*p)).collect();
    if dists.iter().all(|d| *d <= eps) {
        return w.to_vec();
    }
    if dists.iter().all(|d| *d >= -eps) {
        return Vec::new();
    }
    let mut out = Vec::with_capacity(w.len() + 1);
    for i in 0..w.len() {
        let (p, dp) = (w[i], dists[i]);
        let (q, dq) = (w[(i + 1) % w.len()], dists[(i + 1) % w.len()]);
        if dp <= eps {
            out.push(p);
        }
        // An edge from strictly behind to strictly in front, or back, crosses the plane.
        if (dp < -eps && dq > eps) || (dp > eps && dq < -eps) {
            let t = dp / (dp - dq);
            out.push(p + (q - p) * t);
        }
    }
    out
}

/// Derive a brush's shape in floating point, by clipping a world-sized polygon on each face
/// plane against every other face (the method q3map2 itself uses).
///
/// This is the fallback for brushes [`crate::winding::brush_geometry`] declines as
/// [`Degeneracy::NotExactlyRepresentable`], chiefly maps decompiled from a BSP, whose plane
/// points are printed to three decimals. Callers consult it only after the exact path has
/// declined, so on-grid input keeps its exact answers. Validation keeps using the exact path:
/// only it can say that a vertex is on the grid or a plane is redundant without a tolerance.
pub fn approx_geometry(faces: &[Face]) -> Result<ApproxGeometry, ApproxDegeneracy> {
    if faces.len() < 4 {
        return Err(ApproxDegeneracy::TooFewFaces(faces.len()));
    }
    let mut planes = Vec::with_capacity(faces.len());
    for (i, f) in faces.iter().enumerate() {
        let p = f.plane().ok_or(ApproxDegeneracy::FaceHasNoPlane(i))?;
        let n = p.normal;
        if !(n.x.is_finite() && n.y.is_finite() && n.z.is_finite() && p.dist.is_finite()) {
            return Err(ApproxDegeneracy::FaceHasNoPlane(i));
        }
        planes.push(p);
    }

    let eps = APPROX_EPSILON;
    let mut vertices: Vec<Vec3> = Vec::new();
    let mut out_faces = Vec::with_capacity(planes.len());
    for (i, plane) in planes.iter().enumerate() {
        let mut w = base_winding(plane);
        for (j, other) in planes.iter().enumerate() {
            if w.is_empty() {
                break;
            }
            if i == j {
                continue;
            }
            // A second copy of this face's plane neither clips it nor is clipped by it; the
            // first copy keeps the polygon, as `RemoveDuplicateBrushPlanes` would.
            if plane.normal.dot(other.normal) > 1.0 - 1e-9 && (plane.dist - other.dist).abs() < eps
            {
                if j < i {
                    w.clear();
                }
                continue;
            }
            w = chop_behind(&w, other, eps);
        }
        let mut idx: Vec<usize> = Vec::with_capacity(w.len());
        for p in w {
            // A corner within tolerance of the integer grid is taken to be on it. A
            // decompiled brush was almost always compiled from on-grid source, and its
            // printed planes put the corners back within hundredths of where they were:
            // snapping restores the authored shape, so a point on a face (a cell centre,
            // say) is judged as it is in the source.
            let weld = APPROX_WELD;
            let snap = |c: f64| {
                if (c - c.round()).abs() <= weld {
                    c.round()
                } else {
                    c
                }
            };
            let p = crate::math::vec3(snap(p.x), snap(p.y), snap(p.z));
            let near = weld * weld;
            let at = match vertices
                .iter()
                .position(|v| (*v - p).length_squared() <= near)
            {
                Some(at) => at,
                None => {
                    vertices.push(p);
                    vertices.len() - 1
                }
            };
            if !idx.contains(&at) {
                idx.push(at);
            }
        }
        if idx.len() < 3 {
            idx.clear();
        }
        out_faces.push((*plane, idx));
    }

    // Every face of a real brush has some corner well behind it. A brush that collapsed
    // flat has a face with all its corners on it (to tolerance): it encloses nothing,
    // however many corners it has.
    let has_volume = out_faces
        .iter()
        .all(|(p, idx)| idx.is_empty() || vertices.iter().any(|v| p.distance_to(*v) < -2.0 * eps));
    if vertices.len() < 4 || !has_volume {
        return Err(ApproxDegeneracy::EmptyIntersection);
    }
    // Corners out at the base polygon's edge mean the planes do not close: there is no
    // bounded volume to collide with, and a world-sized one would be far worse than none.
    // Short of that, a corner past the world limit is a brush the compiler drops
    // (`COORD_OUT_OF_BOUNDS` in validate.rs), so it is no collision either.
    let reach = vertices
        .iter()
        .map(|v| v.x.abs().max(v.y.abs()).max(v.z.abs()))
        .fold(0.0, f64::max);
    if reach > 2.0 * MAX_WORLD_COORD {
        return Err(ApproxDegeneracy::Unbounded);
    }
    if reach > MAX_WORLD_COORD {
        return Err(ApproxDegeneracy::OutsideWorld);
    }
    Ok(ApproxGeometry {
        vertices,
        faces: out_faces,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Brush;
    use crate::parse::parse_map;
    use crate::winding::{brush_geometry, Degeneracy};

    fn first_brush(src: &str) -> Brush {
        parse_map(src).unwrap().entities[0].prims[0]
            .as_brush()
            .unwrap()
            .clone()
    }

    #[test]
    fn approx_geometry_agrees_with_the_exact_path_on_grid() {
        // A wedge with a sloped face: exact and float answers must name the same corners.
        let b = first_brush(
            "{\n{\n\
            ( 0 0 0 ) ( 64 0 0 ) ( 0 64 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 64 0 ) ( 0 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 8 ) ( 64 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 64 0 ) ( 64 64 0 ) ( 0 64 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 32 ) ( 0 64 32 ) ( 64 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n",
        );
        let exact = brush_geometry(&b.faces).unwrap();
        let approx = approx_geometry(&b.faces).unwrap();
        // The same corners, bit for bit (snapping puts them back on the grid exactly).
        assert_eq!(exact.vertices.len(), approx.vertices.len());
        for v in exact.vertex_positions() {
            assert!(
                approx.vertices.contains(&v),
                "{v} missing from {:?}",
                approx.vertices
            );
        }
        assert_eq!(exact.redundant_faces(), approx.redundant_faces());
        assert!(approx.off_grid_vertices().is_empty());
    }

    #[test]
    fn approx_geometry_evaluates_a_decompiled_brush_the_exact_path_declines() {
        // The 0..64 cube as `q3map2 -convert` writes it: three-decimal points, some of them
        // far from the brush, one plane a rounding error off true.
        let b = first_brush(
            "{\n{\n\
            ( -887.941 1916.262 0.000 ) ( -1120.216 1916.262 0.000 ) ( -887.941 1808.632 0.000 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 64.000 0.000 64.000 ) ( 0.000 0.000 64.000 ) ( 64.000 64.001 64.000 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0.000 -1000.000 0.000 ) ( 0.000 1000.000 0.000 ) ( 0.001 0.000 1000.000 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 64.000 0.000 0.000 ) ( 64.000 0.000 1.000 ) ( 64.000 1.000 0.000 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0.000 0.000 0.000 ) ( 0.000 0.000 1.000 ) ( 1.000 0.000 0.000 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0.000 64.000 0.000 ) ( 1.000 64.000 0.000 ) ( 0.000 64.000 1.000 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0.500 0.500 0.500 ) ( 1.500 0.500 0.500 ) ( 0.500 1.500 0.500 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n",
        );
        assert!(matches!(
            brush_geometry(&b.faces),
            Err(Degeneracy::NotExactlyRepresentable(_))
        ));
        let g = approx_geometry(&b.faces).unwrap();
        // The last face is a second floor at z=0.5, which cuts the cube to 0.5..64.
        let bb = g.bounds();
        assert_eq!(g.vertices.len(), 8, "{:?}", g.vertices);
        assert!(
            (bb.min.x).abs() < 0.01 && (bb.max.x - 64.0).abs() < 0.01,
            "{bb:?}"
        );
        assert!(
            (bb.min.z - 0.5).abs() < 0.01 && (bb.max.z - 64.0).abs() < 0.01,
            "{bb:?}"
        );
        assert_eq!(
            g.redundant_faces(),
            vec![0],
            "the z=0 floor is cut away by z=0.5"
        );
        assert_eq!(
            g.off_grid_vertices().len(),
            4,
            "the z=0.5 corners are off the grid"
        );
        // The x=0 face leans by a millionth: its corners are put back on the grid exactly.
        assert!(
            g.vertices.iter().all(|v| v.x == 0.0 || v.x == 64.0),
            "{:?}",
            g.vertices
        );
    }

    #[test]
    fn approx_geometry_refuses_what_encloses_nothing() {
        let flat = first_brush(
            "{\n{\n\
            ( 0 0 0.5 ) ( 8 0 0.5 ) ( 0 8 0.5 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0.5 ) ( 0 8 0.5 ) ( 8 0 0.5 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 8 0 ) ( 0 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 8 ) ( 8 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 8 8 0 ) ( 8 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 0 8 8 ) ( 8 8 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n",
        );
        assert_eq!(
            approx_geometry(&flat.faces),
            Err(ApproxDegeneracy::EmptyIntersection)
        );
        // Four planes that leave one direction open bound nothing either.
        let open = first_brush(
            "{\n{\n\
            ( 0 0 0.5 ) ( 0 8 0.5 ) ( 8 0 0.5 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 8 0 ) ( 0 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 8 ) ( 8 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 8 8 0 ) ( 8 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n",
        );
        assert_eq!(
            approx_geometry(&open.faces),
            Err(ApproxDegeneracy::Unbounded)
        );
        // A brush past the world limit is one q3map2 drops, so it is not collision.
        let far = first_brush(
            "{\n{\n\
            ( 0 0 99996 ) ( 8 0 99996 ) ( 0 8 99996 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 100000 ) ( 0 8 100000 ) ( 8 0 100000 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 8 0 ) ( 0 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 8 ) ( 8 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 8 8 0 ) ( 8 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 0 8 8 ) ( 8 8 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n",
        );
        assert!(matches!(
            brush_geometry(&far.faces),
            Err(Degeneracy::NotExactlyRepresentable(_))
        ));
        assert_eq!(
            approx_geometry(&far.faces),
            Err(ApproxDegeneracy::OutsideWorld)
        );
    }
}
