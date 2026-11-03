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
}
