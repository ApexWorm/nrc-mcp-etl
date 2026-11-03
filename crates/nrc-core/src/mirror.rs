//! Mirroring a map's geometry across an axis-aligned plane.
//!
//! A reflection is not a rotation, and three things go wrong if it is treated as one:
//!
//! - **Plane orientation.** A face's plane is the three points in file order; its normal is
//!   `cross(c - a, b - a)` (q3map2 `PlaneFromPoints`). Reflecting every point reverses the
//!   handedness of that cross product, so a brush whose points are merely reflected has every
//!   plane facing inward and compiles to nothing. Swapping the second and third points
//!   restores the outward normal. Patches have the same problem with their grid order: the
//!   surface normal comes from the order of the rows and columns (`patch.h`), so the rows
//!   are reversed.
//! - **Texture projection.** Each face's texture coordinates are a function of the point and
//!   of axes derived from the face normal, and those axes do not simply reflect with it. Every
//!   texdef convention is rewritten so that each mirrored point gets the texture coordinate its
//!   original had: the result is the mirror image of the original, seams and alignment
//!   included. Patches carry texture coordinates per control point, so they need nothing.
//! - **Number text.** Coordinates keep their source text when mirrored about 0 (a sign is
//!   added or removed); otherwise they are recomputed.
//!
//! Entity keys (origins, facings) are not geometry and are the caller's business; what they
//! mean is per game.

use crate::math::{vec3, Vec3};
use crate::model::{Entity, Face, Map, Patch, Primitive, TexDef};
use crate::num::Num;

/// What a mirror changed.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MirrorStats {
    pub brushes: usize,
    pub faces: usize,
    pub patches: usize,
    /// Primitive blocks kept as raw text, which cannot be mirrored and were left as they are.
    pub raw_blocks: usize,
    /// Faces whose plane points are collinear, mirrored without a texture fix.
    pub degenerate_faces: usize,
}

/// `2·at − n`, keeping the source text when `at` is 0 and the text is a plain number.
pub fn reflect_num(n: &Num, at: f64) -> Num {
    let value = 2.0 * at - n.value();
    if at == 0.0 {
        if let Some(t) = n.text() {
            if n.value() == 0.0 {
                return n.clone();
            }
            let flipped = match t.strip_prefix('-') {
                Some(rest) => rest.to_string(),
                None => format!("-{}", t.strip_prefix('+').unwrap_or(t)),
            };
            if flipped.parse::<f64>().ok() == Some(value) {
                return Num::parsed(&flipped, value);
            }
        }
    }
    Num::new(value)
}

fn reflect_vec(v: Vec3, axis: usize) -> Vec3 {
    let mut a = v.to_array();
    a[axis] = -a[axis];
    a.into()
}

/// The texture axes brush primitives project onto, from the face normal (NetRadiant-custom
/// `ComputeAxisBase`, tools/quake3/common/qmath.h: straight up and down are special cases).
fn axis_base(n: Vec3) -> (Vec3, Vec3) {
    let close = |a: Vec3, b: Vec3| (a - b).to_array().iter().all(|d| d.abs() < 1e-6);
    let up = vec3(0.0, 0.0, 1.0);
    if close(n, up) {
        (vec3(0.0, 1.0, 0.0), vec3(1.0, 0.0, 0.0))
    } else if close(n, -up) {
        (vec3(0.0, 1.0, 0.0), vec3(-1.0, 0.0, 0.0))
    } else {
        let s = n.cross(up).normalized().unwrap_or(vec3(0.0, 1.0, 0.0));
        let t = n.cross(s).normalized().unwrap_or(vec3(1.0, 0.0, 0.0));
        (-s, t)
    }
}

/// Quake 3's axial base axes (q3map2 `baseaxis`): normal, s axis, t axis. The first best
/// match for the face normal wins, as in `TextureAxisFromPlane`.
const BASE_AXIS: [[[f64; 3]; 3]; 6] = [
    [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, -1.0, 0.0]],
    [[0.0, 0.0, -1.0], [1.0, 0.0, 0.0], [0.0, -1.0, 0.0]],
    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]],
    [[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]],
    [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]],
    [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]],
];

fn axial_base(n: Vec3) -> ([f64; 3], [f64; 3]) {
    let mut best = 0;
    let mut best_dot = 0.0;
    for (i, b) in BASE_AXIS.iter().enumerate() {
        let d = n.dot(b[0].into());
        if d > best_dot {
            best_dot = d;
            best = i;
        }
    }
    (BASE_AXIS[best][1], BASE_AXIS[best][2])
}

fn set(n: &mut Num, v: f64) {
    if n.value() != v {
        *n = Num::new(v);
    }
}

/// Rewrite one (already mirrored) face's texture mapping so every point keeps the texture
/// coordinate its reflection had. `old_normal` is the face's normal before the mirror.
fn mirror_texdef(face: &mut Face, old_normal: Vec3, axis: usize, at: f64) {
    let new_normal = reflect_vec(old_normal, axis);
    match &mut face.tex {
        TexDef::BrushPrimitives { m } => {
            // A point p projects to (p·S, p·T) and the matrix maps that to (s, t). The
            // reflected point p' = R p + 2·at·e projects onto the new face's axes S', T';
            // R S' is ±S (likewise T), so each projected coordinate is negated or kept and
            // shifted by K = 2·at·S'[axis]. Fold both into the matrix column and offset.
            let (s_old, t_old) = axis_base(old_normal);
            let (s_new, t_new) = axis_base(new_normal);
            for (col, old, new) in [(0, s_old, s_new), (1, t_old, t_new)] {
                let sign = if reflect_vec(new, axis).dot(old) < 0.0 {
                    -1.0
                } else {
                    1.0
                };
                let k = 2.0 * at * new.to_array()[axis];
                for row in m.iter_mut() {
                    let a = sign * row[col].value();
                    let c = row[2].value() - a * k;
                    if sign < 0.0 {
                        let negated = reflect_num(&row[col], 0.0);
                        row[col] = negated;
                    }
                    set(&mut row[2], c);
                }
            }
        }
        TexDef::Valve220 { u, v, scale, .. } => {
            // s = p·u / scale + offset; with u' = R u, p'·u' = p·u + 2·at·u'[axis].
            let scales = [scale[0].value(), scale[1].value()];
            for (vec, scale) in [(u, scales[0]), (v, scales[1])] {
                let scale = if scale == 0.0 { 1.0 } else { scale };
                let comp = vec[axis].value();
                vec[axis] = reflect_num(&vec[axis], 0.0);
                if at != 0.0 {
                    let off = vec[3].value() - 2.0 * at * (-comp) / scale;
                    set(&mut vec[3], off);
                }
            }
        }
        TexDef::Axial {
            shift,
            rotate,
            scale,
        } => {
            // QuakeTextureVecs rotates the base axes in their component plane, then divides
            // by the scale. Negating the coordinate along the s (t) axis is the same as
            // negating the rotation and the s (t) scale; `at` moves the shift.
            let (bs, bt) = axial_base(new_normal);
            let theta = rotate.value().to_radians();
            let (sin, cos) = theta.sin_cos();
            let (sx, sy) = (scale[0].value(), scale[1].value());
            let (sx, sy) = (
                if sx == 0.0 { 1.0 } else { sx },
                if sy == 0.0 { 1.0 } else { sy },
            );
            let (s0, s1) = (shift[0].value(), shift[1].value());
            let (ss, st) = (axis_sign(bs), axis_sign(bt));
            let flipped = if bs[axis] != 0.0 {
                Some((0, s0 + 2.0 * at * ss * cos / sx, s1 - 2.0 * at * st * sin / sy))
            } else if bt[axis] != 0.0 {
                Some((1, s0 + 2.0 * at * ss * sin / sx, s1 + 2.0 * at * st * cos / sy))
            } else {
                None
            };
            if let Some((which, n0, n1)) = flipped {
                set(&mut shift[0], n0);
                set(&mut shift[1], n1);
                scale[which] = reflect_num(&scale[which], 0.0);
                *rotate = reflect_num(rotate, 0.0);
            }
        }
    }
}

/// The sign of an axial base vector's one nonzero component.
fn axis_sign(b: [f64; 3]) -> f64 {
    b.iter().copied().find(|c| *c != 0.0).unwrap_or(1.0)
}

fn mirror_patch(p: &mut Patch, axis: usize, at: f64) {
    for row in p.rows.iter_mut() {
        for point in row.iter_mut() {
            if let Some(n) = point.get_mut(axis) {
                *n = reflect_num(n, at);
            }
        }
    }
    p.rows.reverse();
}

/// Mirror every brush and patch in `map` across the plane `coordinate[axis] = at`.
///
/// `axis` is 0, 1 or 2 (x, y, z). Brushes stay valid (outward planes), textures come out as
/// the mirror image of the original, patches keep their facing. Entity keys are untouched.
pub fn mirror_geometry(map: &mut Map, axis: usize, at: f64) -> MirrorStats {
    let mut stats = MirrorStats::default();
    for entity in map.entities.iter_mut() {
        mirror_entity(entity, axis, at, &mut stats);
    }
    stats
}

/// Mirror one entity's brushes and patches, adding what changed to `stats`.
///
/// Two mirrors along different axes make a half turn, which is how a part that must keep
/// its own handedness (a vehicle the game turns to face its path) is put back the right
/// way round after the whole map is mirrored.
pub fn mirror_entity(entity: &mut Entity, axis: usize, at: f64, stats: &mut MirrorStats) {
    assert!(axis < 3, "axis is 0, 1 or 2");
    {
        for prim in entity.prims.iter_mut() {
            match prim {
                Primitive::Brush(b) => {
                    stats.brushes += 1;
                    for face in b.faces.iter_mut() {
                        stats.faces += 1;
                        let old = face.plane().map(|p| p.normal);
                        for point in face.points.iter_mut() {
                            point[axis] = reflect_num(&point[axis], at);
                        }
                        face.points.swap(1, 2);
                        match old {
                            Some(n) => mirror_texdef(face, n, axis, at),
                            None => stats.degenerate_faces += 1,
                        }
                    }
                }
                Primitive::Patch(p) => {
                    stats.patches += 1;
                    mirror_patch(p, axis, at);
                }
                Primitive::Raw(_) => stats.raw_blocks += 1,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parse::parse_map;
    use crate::write::write_map;

    /// Texture coordinate of point `p` on `face` (brush primitives / valve / axial).
    fn st(face: &Face, p: Vec3) -> (f64, f64) {
        let n = face.plane().unwrap().normal;
        match &face.tex {
            TexDef::BrushPrimitives { m } => {
                let (s, t) = axis_base(n);
                let (x, y) = (p.dot(s), p.dot(t));
                let f = |r: &[Num; 3]| r[0].value() * x + r[1].value() * y + r[2].value();
                (f(&m[0]), f(&m[1]))
            }
            TexDef::Valve220 { u, v, scale, .. } => {
                let d = |a: &[Num; 4], sc: f64| {
                    (p.dot(vec3(a[0].value(), a[1].value(), a[2].value()))) / sc + a[3].value()
                };
                (d(u, scale[0].value()), d(v, scale[1].value()))
            }
            TexDef::Axial {
                shift,
                rotate,
                scale,
            } => {
                let (bs, bt) = axial_base(n);
                let (sv, tv) = (
                    bs.iter().position(|c| *c != 0.0).unwrap(),
                    bt.iter().position(|c| *c != 0.0).unwrap(),
                );
                let (sin, cos) = rotate.value().to_radians().sin_cos();
                let mut vs = bs;
                let mut vt = bt;
                for vec in [&mut vs, &mut vt] {
                    let ns = cos * vec[sv] - sin * vec[tv];
                    let nt = sin * vec[sv] + cos * vec[tv];
                    vec[sv] = ns;
                    vec[tv] = nt;
                }
                let pa = p.to_array();
                let dot = |v: [f64; 3]| v[0] * pa[0] + v[1] * pa[1] + v[2] * pa[2];
                (
                    dot(vs) / scale[0].value() + shift[0].value(),
                    dot(vt) / scale[1].value() + shift[1].value(),
                )
            }
        }
    }

    fn close(a: (f64, f64), b: (f64, f64)) -> bool {
        (a.0 - b.0).abs() < 1e-6 && (a.1 - b.1).abs() < 1e-6
    }

    const BP: &str = r#"{
"classname" "worldspawn"
{
brushDef
{
( 0 0 64 ) ( 0 64 64 ) ( 64 0 64 ) ( ( 0.015625 0.002 0.25 ) ( -0.003 0.015625 0.5 ) ) a/b 0 0 0
( 0 0 0 ) ( 64 0 0 ) ( 0 64 0 ) ( ( 0.015625 0.001 0.125 ) ( 0 0.015625 0.75 ) ) a/b 0 0 0
( 0 0 0 ) ( 0 0 64 ) ( 64 0 0 ) ( ( 0.02 0.003 0.3 ) ( 0.001 0.015625 0.1 ) ) a/b 0 0 0
( 0 64 0 ) ( 64 64 0 ) ( 0 64 64 ) ( ( 0.015625 0 0 ) ( 0 0.015625 0 ) ) a/b 0 0 0
( 0 0 0 ) ( 0 64 0 ) ( 0 0 64 ) ( ( 0.011 0.004 0.2 ) ( 0.002 0.015625 0.4 ) ) a/b 0 0 0
( 64 0 0 ) ( 64 0 64 ) ( 64 64 0 ) ( ( 0.015625 0 0 ) ( 0 0.015625 0 ) ) a/b 0 0 0
( 0 0 0 ) ( 64 64 64 ) ( 64 0 64 ) ( ( 0.01 0.02 0.3 ) ( 0.03 -0.01 0.2 ) ) a/b 0 0 0
}
}
}
"#;

    fn check_mirror(src: &str, axis: usize, at: f64) {
        let original = parse_map(src).unwrap();
        let mut mirrored = original.clone();
        let stats = mirror_geometry(&mut mirrored, axis, at);
        assert_eq!(stats.degenerate_faces, 0);
        let a = original.entities[0].brushes().next().unwrap();
        let b = mirrored.entities[0].brushes().next().unwrap();
        for (fa, fb) in a.faces.iter().zip(&b.faces) {
            let na = fa.plane().unwrap();
            let nb = fb.plane().unwrap();
            // The plane is the reflection of the original, facing outward.
            let expect = reflect_vec(na.normal, axis);
            assert!((nb.normal - expect).length() < 1e-9, "{:?} vs {:?}", nb.normal, expect);
            // Every point keeps its texture coordinate.
            for p in fa.point_vecs() {
                let mut q = p.to_array();
                q[axis] = 2.0 * at - q[axis];
                assert!(close(st(fa, p), st(fb, q.into())), "{:?} {:?}", fa.tex, fb.tex);
            }
            for p in [vec3(3.0, 5.0, 7.0), vec3(-11.0, 13.0, 2.0)] {
                // Project an arbitrary point onto the original plane, then reflect it.
                let p = p - na.normal * na.distance_to(p);
                let mut q = p.to_array();
                q[axis] = 2.0 * at - q[axis];
                assert!(close(st(fa, p), st(fb, q.into())));
            }
        }
        // Mirroring twice gives the original geometry back.
        let mut twice = mirrored.clone();
        mirror_geometry(&mut twice, axis, at);
        let c = twice.entities[0].brushes().next().unwrap();
        for (fa, fc) in a.faces.iter().zip(&c.faces) {
            assert_eq!(fa.point_vecs(), fc.point_vecs());
        }
    }

    #[test]
    fn brush_primitives_mirror_keeps_planes_outward_and_texture_coordinates() {
        check_mirror(BP, 0, 0.0);
        check_mirror(BP, 1, 0.0);
        check_mirror(BP, 0, 96.0);
        check_mirror(BP, 1, -40.0);
    }

    #[test]
    fn mirroring_about_zero_keeps_number_text() {
        let mut m = parse_map(BP).unwrap();
        mirror_geometry(&mut m, 0, 0.0);
        let out = write_map(&m);
        assert!(out.contains("( 0 0 64 ) ( -64 0 64 ) ( 0 64 64 )"), "{out}");
    }

    #[test]
    fn axial_and_valve_texdefs_mirror_their_coordinates() {
        let axial = r#"{
"classname" "worldspawn"
{
( 0 0 64 ) ( 0 64 64 ) ( 64 0 64 ) a/b 8 4 30 0.5 0.25 0 0 0
( 0 0 0 ) ( 64 0 0 ) ( 0 64 0 ) a/b 3 1 -15 0.5 0.5 0 0 0
( 0 0 0 ) ( 0 0 64 ) ( 64 0 0 ) a/b 1 2 45 0.25 0.5 0 0 0
( 0 64 0 ) ( 64 64 0 ) ( 0 64 64 ) a/b 0 0 0 0.5 0.5 0 0 0
( 0 0 0 ) ( 0 64 0 ) ( 0 0 64 ) a/b 5 6 10 0.5 1 0 0 0
( 64 0 0 ) ( 64 0 64 ) ( 64 64 0 ) a/b 0 0 0 0.5 0.5 0 0 0
}
}
"#;
        check_mirror(axial, 0, 0.0);
        check_mirror(axial, 0, 32.0);
        check_mirror(axial, 1, 24.0);
        let valve = r#"{
"classname" "worldspawn"
"mapversion" "220"
{
( 0 0 64 ) ( 0 64 64 ) ( 64 0 64 ) a/b [ 1 0 0 8 ] [ 0 -1 0 4 ] 0 0.5 0.25
( 0 0 0 ) ( 64 0 0 ) ( 0 64 0 ) a/b [ 0.8 0.6 0 3 ] [ -0.6 0.8 0 1 ] 0 0.5 0.5
( 0 0 0 ) ( 0 0 64 ) ( 64 0 0 ) a/b [ 1 0 0 1 ] [ 0 0 -1 2 ] 0 0.25 0.5
( 0 64 0 ) ( 64 64 0 ) ( 0 64 64 ) a/b [ 1 0 0 0 ] [ 0 0 -1 0 ] 0 0.5 0.5
( 0 0 0 ) ( 0 64 0 ) ( 0 0 64 ) a/b [ 0 1 0 5 ] [ 0 0 -1 6 ] 0 0.5 1
( 64 0 0 ) ( 64 0 64 ) ( 64 64 0 ) a/b [ 0 1 0 0 ] [ 0 0 -1 0 ] 0 0.5 0.5
}
}
"#;
        // The valve check computes s = p·u / scale + offset, as the format means it.
        let m = parse_map(valve).unwrap();
        assert!(matches!(
            m.entities[0].brushes().next().unwrap().faces[0].tex,
            TexDef::Valve220 { .. }
        ));
        check_mirror(valve, 0, 0.0);
        check_mirror(valve, 0, 16.0);
    }

    #[test]
    fn patches_keep_their_facing() {
        let src = r#"{
"classname" "worldspawn"
{
patchDef2
{
a/b
( 3 3 0 0 0 )
(
( ( 0 0 0 0 0 ) ( 0 32 0 0 0.5 ) ( 0 64 0 0 1 ) )
( ( 32 0 16 0.5 0 ) ( 32 32 16 0.5 0.5 ) ( 32 64 16 0.5 1 ) )
( ( 64 0 32 1 0 ) ( 64 32 32 1 0.5 ) ( 64 64 32 1 1 ) )
)
}
}
}
"#;
        let original = parse_map(src).unwrap();
        let mut m = original.clone();
        let stats = mirror_geometry(&mut m, 0, 0.0);
        assert_eq!(stats.patches, 1);
        let p = m.entities[0].patches().next().unwrap();
        let q = original.entities[0].patches().next().unwrap();
        // Rows reversed and x negated: the first row is the old last row, reflected.
        assert_eq!(p.rows[0][1][0].value(), -64.0);
        assert_eq!(p.rows[0][1][3].value(), q.rows[2][1][3].value());
        // Facing (a flat, tilted patch): the normal from the grid tangents is reflected.
        let n = |p: &Patch| {
            let a = p.control_points();
            let (r0, r1, c1) = (a[0], a[3], a[1]);
            (r1 - r0).cross(c1 - r0)
        };
        let before = n(q);
        let after = n(p);
        assert!((after - reflect_vec(before, 0)).length() < 1e-9);
        assert!(write_map(&m).contains("patchDef2"));
    }
}
