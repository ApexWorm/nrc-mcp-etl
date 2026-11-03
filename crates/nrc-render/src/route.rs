//! Route annotations drawn in the same top-down camera as the map geometry.

use crate::{camera::Camera, canvas::Canvas, canvas::Rgb, font};
use nrc_core::math::Vec3;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stance {
    Stand,
    Crouch,
    Prone,
}

impl Stance {
    pub fn colour(self) -> Rgb {
        match self {
            Self::Stand => Rgb(66, 196, 255),
            Self::Crouch => Rgb(255, 190, 80),
            Self::Prone => Rgb(190, 140, 255),
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Stand => "STAND",
            Self::Crouch => "CROUCH",
            Self::Prone => "PRONE",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Segment {
    pub stance: Stance,
    pub points: Vec<Vec3>,
}

#[derive(Clone, Debug)]
pub struct Transition {
    pub position: Vec3,
    pub from_stance: Stance,
    pub to_stance: Stance,
}

#[derive(Clone, Debug, Default)]
pub struct RouteOverlay {
    pub segments: Vec<Segment>,
    pub transitions: Vec<Transition>,
}

impl RouteOverlay {
    pub fn validate(&self) -> Result<(), String> {
        let count: usize = self.segments.iter().map(|s| s.points.len()).sum();
        if count > 250_000 || self.transitions.len() > 250_000 {
            return Err("route overlay exceeds 250000 points or transitions".into());
        }
        for p in self
            .segments
            .iter()
            .flat_map(|s| &s.points)
            .chain(self.transitions.iter().map(|t| &t.position))
        {
            if p.to_array().iter().any(|v| !v.is_finite()) {
                return Err("route coordinates must be finite".into());
            }
        }
        Ok(())
    }
}

const INK: Rgb = Rgb(240, 244, 250);
const HALO: Rgb = Rgb(12, 15, 20);

// Clip before rasterizing. An off-map endpoint must not produce an unbounded pixel loop.
fn clipped_line(canvas: &mut Canvas, a: (f64, f64), b: (f64, f64), colour: Rgb) {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    if [a.0, a.1, b.0, b.1, dx, dy].iter().any(|v| !v.is_finite()) {
        return;
    }
    let (mut lo, mut hi): (f64, f64) = (0.0, 1.0);
    for (p, q) in [
        (-dx, a.0),
        (dx, canvas.width as f64 - 1.0 - a.0),
        (-dy, a.1),
        (dy, canvas.height as f64 - 1.0 - a.1),
    ] {
        if p.abs() < f64::EPSILON {
            if q < 0.0 {
                return;
            }
        } else if p < 0.0 {
            lo = lo.max(q / p);
        } else {
            hi = hi.min(q / p);
        }
    }
    if lo > hi {
        return;
    }
    let clipped = |t: f64| {
        (
            (a.0 + t * dx).clamp(0.0, canvas.width as f64 - 1.0),
            (a.1 + t * dy).clamp(0.0, canvas.height as f64 - 1.0),
            0.0,
        )
    };
    let (a, b) = (clipped(lo), clipped(hi));
    canvas.line(a, b, colour, f32::INFINITY);
}

fn stroke(canvas: &mut Canvas, a: (f64, f64), b: (f64, f64), colour: Rgb) {
    for radius in [2_i32, 1] {
        let c = if radius == 2 { HALO } else { colour };
        for x in -radius..=radius {
            for y in -radius..=radius {
                clipped_line(
                    canvas,
                    (a.0 + x as f64, a.1 + y as f64),
                    (b.0 + x as f64, b.1 + y as f64),
                    c,
                );
            }
        }
    }
}

fn marker(canvas: &mut Canvas, camera: &Camera, point: Vec3, label: &str) {
    let Some((x, y, _)) = camera.project(point) else {
        return;
    };
    if !x.is_finite()
        || !y.is_finite()
        || x < -8.0
        || y < -8.0
        || x > canvas.width as f64 + 8.0
        || y > canvas.height as f64 + 8.0
    {
        return;
    }
    let (x, y) = (x.round() as i64, y.round() as i64);
    canvas.rect_fill(x - 4, y - 4, x + 4, y + 4, HALO);
    canvas.rect_outline(x - 4, y - 4, x + 4, y + 4, INK);
    let scale = if canvas.width >= 700 { 2 } else { 1 };
    let tx = (x + 8).clamp(
        0,
        (canvas.width as i64 - font::text_width(label, scale)).max(0),
    );
    let ty = (y - 4).clamp(0, canvas.height as i64 - font::text_height(scale));
    canvas.rect_fill(
        tx - 1,
        ty - 1,
        tx + font::text_width(label, scale),
        ty + font::text_height(scale),
        HALO,
    );
    font::draw_text(canvas, tx, ty, label, scale, INK);
}

pub fn draw(canvas: &mut Canvas, camera: &Camera, route: &RouteOverlay) {
    for segment in &route.segments {
        for pair in segment.points.windows(2) {
            if let (Some(a), Some(b)) = (camera.project(pair[0]), camera.project(pair[1])) {
                stroke(canvas, (a.0, a.1), (b.0, b.1), segment.stance.colour());
            }
        }
    }
    for (i, transition) in route.transitions.iter().enumerate() {
        marker(canvas, camera, transition.position, &format!("{}", i + 1));
    }
    let start = route.segments.iter().flat_map(|s| &s.points).next();
    let end = route
        .segments
        .iter()
        .rev()
        .flat_map(|s| s.points.iter().rev())
        .next();
    if let (Some(start), Some(end)) = (start, end) {
        if start == end {
            marker(canvas, camera, *start, "S/E");
        } else {
            marker(canvas, camera, *start, "S");
            marker(canvas, camera, *end, "E");
        }
    }
    // Compact legend stays outside the map when the caller reserves at least 48px padding.
    if canvas.width >= 280 && canvas.height >= 100 {
        let scale = if canvas.width >= 700 { 2 } else { 1 };
        let spacing = if scale == 2 { 110 } else { 84 };
        let y = canvas.height as i64 - 40;
        canvas.rect_fill(8, y - 4, 14 + 3 * spacing, y + 30, HALO);
        for (i, stance) in [Stance::Stand, Stance::Crouch, Stance::Prone]
            .iter()
            .enumerate()
        {
            let x = 14 + i as i64 * spacing;
            canvas.rect_fill(x, y, x + 10, y + 6, stance.colour());
            font::draw_text(canvas, x + 15, y, stance.label(), scale, INK);
        }
        font::draw_text(
            canvas,
            14,
            y + 18,
            "S START  E END  # STANCE CHANGE",
            scale,
            INK,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use nrc_core::math::vec3;

    #[test]
    fn clips_far_outside_lines_before_rasterizing() {
        let mut c = Canvas::new(64, 64, HALO);
        clipped_line(&mut c, (-1e9, 32.0), (1e9, 32.0), INK);
        assert_eq!(c.get(0, 32), Some(INK));
        assert_eq!(c.get(63, 32), Some(INK));
        clipped_line(&mut c, (-1e9, -1e9), (-1e8, -1e8), INK);
        assert_eq!(c.get(0, 0), Some(HALO));
    }

    #[test]
    fn rejects_nonfinite_world_points() {
        let route = RouteOverlay {
            segments: vec![Segment {
                stance: Stance::Stand,
                points: vec![vec3(f64::NAN, 0.0, 0.0)],
            }],
            ..Default::default()
        };
        assert!(route.validate().is_err());
    }

    #[test]
    fn projects_stances_over_geometry_with_the_supplied_map_camera() {
        use crate::camera::OrthoAxis;
        use nrc_core::math::Aabb;
        let mut c = Canvas::new(240, 240, HALO);
        let camera = Camera::fit_ortho(
            OrthoAxis::Top,
            Aabb {
                min: vec3(0.0, 0.0, 0.0),
                max: vec3(100.0, 100.0, 100.0),
            },
            240,
            240,
            20.0,
        );
        // A nearer opaque surface must not hide this explicitly non-depth-tested annotation.
        c.put_depth(120, 160, -1000.0, INK);
        let route = RouteOverlay {
            segments: [Stance::Stand, Stance::Crouch, Stance::Prone]
                .into_iter()
                .enumerate()
                .map(|(i, stance)| {
                    let y = 30.0 + i as f64 * 20.0;
                    Segment {
                        stance,
                        points: vec![vec3(10.0, y, 0.0), vec3(90.0, y, 0.0)],
                    }
                })
                .collect(),
            ..Default::default()
        };
        draw(&mut c, &camera, &route);
        for (stance, y) in [
            (Stance::Stand, 160),
            (Stance::Crouch, 120),
            (Stance::Prone, 80),
        ] {
            assert_eq!(c.get(120, y), Some(stance.colour()));
        }
    }
}
