//! Python bindings for the geometry kernel.
//!
//! The MCP server holds an open map across many tool calls, so the boundary is a
//! [`PyMap`] object rather than a set of free functions over file paths — re-parsing a
//! 850 KB map for every query would be wasteful and, worse, would let the map on disk
//! drift from the map being reasoned about.
//!
//! Values cross the boundary as plain Python dicts and lists. That keeps the Python side
//! free of wrapper types it would have to learn, and means an MCP tool can hand a result
//! straight to `json.dumps` without a serialization layer in between.
//!
//! Anything that can fail raises `ValueError` carrying the kernel's own message, including
//! the source line, because a parse failure a mapper cannot locate is not much better than
//! a crash.

use nrc_core::model::{unwritable_pair, Entity, Primitive};
use nrc_core::stats::map_stats;
use nrc_core::validate::{validate_map, Severity, Thresholds};
use nrc_core::{parse_map, write_map, Map};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::path::PathBuf;

fn err(msg: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(msg.to_string())
}

fn primitive_source(primitive: &Primitive) -> String {
    let mut map = Map::default();
    let mut entity = Entity::default();
    entity.prims.push(primitive.clone());
    map.entities.push(entity);
    write_map(&map)
}

fn parse_overlay(name: &str) -> PyResult<nrc_render::Overlay> {
    use nrc_render::Overlay;
    Ok(match name {
        "shaded" => Overlay::Shaded,
        "structural" | "structural_detail" => Overlay::StructuralDetail,
        "caulk" => Overlay::Caulk,
        "offgrid" | "off_grid" => Overlay::OffGrid,
        other => {
            return Err(err(format!(
                "unknown overlay {other:?}: use shaded, structural, caulk or off_grid"
            )))
        }
    })
}

/// An open `.map` document.
#[pyclass(name = "Map", module = "nrc_py")]
pub struct PyMap {
    inner: Map,
    /// The bytes we loaded, kept so `round_trip()` can answer honestly rather than
    /// re-deriving what it hopes the file said.
    original: String,
    path: Option<PathBuf>,
}

#[pymethods]
impl PyMap {
    /// Parse from a string.
    #[staticmethod]
    fn parse(source: &str) -> PyResult<Self> {
        let inner = parse_map(source).map_err(err)?;
        Ok(Self {
            inner,
            original: source.to_string(),
            path: None,
        })
    }

    /// Load from disk.
    #[staticmethod]
    fn load(path: PathBuf) -> PyResult<Self> {
        let bytes = std::fs::read(&path).map_err(|e| err(format!("{}: {e}", path.display())))?;
        let source = String::from_utf8(bytes).map_err(|e| {
            err(format!(
                "{} is not valid UTF-8 (byte {}) — is it really a .map?",
                path.display(),
                e.utf8_error().valid_up_to()
            ))
        })?;
        let inner = parse_map(&source).map_err(|e| err(format!("{}: {e}", path.display())))?;
        Ok(Self {
            inner,
            original: source,
            path: Some(path),
        })
    }

    /// Serialize back to `.map` source.
    fn source(&self) -> String {
        write_map(&self.inner)
    }

    /// A working copy that preserves the original round-trip baseline and disk path.
    fn copy(&self) -> Self {
        Self {
            inner: self.inner.clone(),
            original: self.original.clone(),
            path: self.path.clone(),
        }
    }

    /// Canonical source for each brush, including texture mapping, flags and comments.
    fn brush_sources(&self) -> Vec<(usize, usize, String)> {
        self.inner
            .entities
            .iter()
            .enumerate()
            .flat_map(|(ei, e)| {
                e.prims
                    .iter()
                    .enumerate()
                    .filter(|(_, p)| matches!(p, Primitive::Brush(_)))
                    .map(move |(pi, p)| (ei, pi, primitive_source(p)))
            })
            .collect()
    }

    /// Replace an exact set of unchanged brushes in one entity, preserving all neighbours.
    /// The replacement is a one-entity map containing only the new brushes.
    fn replace_brushes(
        &mut self,
        entity: usize,
        mut expected: Vec<(usize, String)>,
        replacement: &str,
    ) -> PyResult<usize> {
        if expected.is_empty() {
            return Err(err("no generated brushes were identified"));
        }
        let mut parsed = parse_map(replacement).map_err(err)?;
        if parsed.entities.len() != 1
            || parsed.entities[0].prims.is_empty()
            || parsed.entities[0]
                .prims
                .iter()
                .any(|p| !matches!(p, Primitive::Brush(_)))
        {
            return Err(err("replacement must contain one entity of brushes"));
        }
        let target = self
            .inner
            .entities
            .get_mut(entity)
            .ok_or_else(|| err(format!("no entity {entity}")))?;
        expected.sort_by_key(|(index, _)| *index);
        if expected.windows(2).any(|w| w[0].0 == w[1].0) {
            return Err(err("duplicate brush indices"));
        }
        for (index, source) in &expected {
            match target.prims.get(*index) {
                Some(p @ Primitive::Brush(_)) if primitive_source(p) == *source => {}
                _ => return Err(err(format!("generated brush {entity}:{index} changed"))),
            }
        }
        let first = expected[0].0;
        for (index, _) in expected.iter().rev() {
            target.prims.remove(*index);
        }
        let new_prims = std::mem::take(&mut parsed.entities[0].prims);
        let count = new_prims.len();
        target.prims.splice(first..first, new_prims);
        Ok(count)
    }

    /// Write to disk. Defaults to the path this map was loaded from.
    ///
    /// Writes to an exclusively created sibling and renames only after the write succeeds.
    #[pyo3(signature = (path=None))]
    fn save(&self, path: Option<PathBuf>) -> PyResult<String> {
        let target = path
            .or_else(|| self.path.clone())
            .ok_or_else(|| err("no path given and this map was parsed from a string"))?;
        use std::io::Write;
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT_SAVE: AtomicU64 = AtomicU64::new(0);
        let parent = target.parent().unwrap_or_else(|| std::path::Path::new("."));
        let (temporary, mut file) = loop {
            let temporary = parent.join(format!(
                ".nrc-save-{}-{}.tmp",
                std::process::id(),
                NEXT_SAVE.fetch_add(1, Ordering::Relaxed)
            ));
            match std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)
            {
                Ok(file) => break (temporary, file),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(err(format!("{}: {e}", target.display()))),
            }
        };
        let result = (|| -> std::io::Result<()> {
            if let Ok(metadata) = std::fs::metadata(&target) {
                file.set_permissions(metadata.permissions())?;
            }
            file.write_all(self.source().as_bytes())?;
            file.sync_all()?;
            drop(file);
            std::fs::rename(&temporary, &target)
        })();
        if let Err(e) = result {
            let _ = std::fs::remove_file(&temporary);
            return Err(err(format!("{}: {e}", target.display())));
        }
        Ok(target.display().to_string())
    }

    /// Whether re-serializing reproduces the bytes we loaded, and where it first differs.
    ///
    /// The §3.2 gate, available at runtime: the MCP layer calls this before offering to
    /// write to a user's map, so "we can reproduce your file exactly" is a checked claim
    /// rather than a promise made once in a test suite.
    fn round_trip<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = self.source();
        let d = PyDict::new(py);
        d.set_item("identical", out == self.original)?;
        d.set_item("input_bytes", self.original.len())?;
        d.set_item("output_bytes", out.len())?;
        if out != self.original {
            let a = self.original.as_bytes();
            let b = out.as_bytes();
            let offset = (0..a.len().min(b.len()))
                .find(|&i| a[i] != b[i])
                .unwrap_or(a.len().min(b.len()));
            let line = 1 + a[..offset].iter().filter(|&&c| c == b'\n').count();
            let diff = PyDict::new(py);
            diff.set_item("line", line)?;
            diff.set_item("byte_offset", offset)?;
            diff.set_item(
                "expected",
                self.original.lines().nth(line - 1).unwrap_or(""),
            )?;
            diff.set_item("actual", out.lines().nth(line - 1).unwrap_or(""))?;
            d.set_item("first_difference", diff)?;
        }
        Ok(d)
    }

    /// Map statistics. `grid` is the authoring grid alignment is measured against.
    #[pyo3(signature = (grid=1))]
    fn stats<'py>(&self, py: Python<'py>, grid: i64) -> PyResult<Bound<'py, PyDict>> {
        let s = map_stats(&self.inner, grid);
        let d = PyDict::new(py);
        d.set_item("entities", s.entities)?;
        d.set_item("brushes", s.brushes)?;
        d.set_item("patches", s.patches)?;
        d.set_item("raw_primitives", s.raw_primitives)?;
        d.set_item("faces", s.faces)?;
        d.set_item("detail_brushes", s.detail_brushes)?;
        d.set_item("structural_brushes", s.structural_brushes)?;
        d.set_item("is_valve220", s.is_valve220)?;
        d.set_item("grid", s.grid)?;
        d.set_item("vertices_on_grid", s.on_grid)?;
        d.set_item("vertices_total", s.total_vertices)?;
        d.set_item("grid_fraction", s.grid_fraction())?;
        d.set_item("unevaluated_brushes", s.unevaluated_brushes)?;
        d.set_item(
            "texdef_kinds",
            s.texdef_kinds
                .iter()
                .map(|k| k.as_str())
                .collect::<Vec<_>>(),
        )?;

        let patch_kinds = PyDict::new(py);
        for (k, v) in &s.patch_kinds {
            patch_kinds.set_item(k, v)?;
        }
        d.set_item("patch_kinds", patch_kinds)?;

        let ents = PyDict::new(py);
        for (k, v) in &s.entity_counts {
            ents.set_item(k, v)?;
        }
        d.set_item("entity_counts", ents)?;

        let top = PyList::empty(py);
        for (shader, faces) in s.top_shaders(20) {
            let e = PyDict::new(py);
            e.set_item("shader", shader)?;
            e.set_item("faces", faces)?;
            top.append(e)?;
        }
        d.set_item("top_shaders", top)?;

        if s.bounds_empty {
            d.set_item("bounds", py.None())?;
        } else {
            let b = PyDict::new(py);
            b.set_item("min", s.bounds_min)?;
            b.set_item("max", s.bounds_max)?;
            b.set_item(
                "size",
                [
                    s.bounds_max[0] - s.bounds_min[0],
                    s.bounds_max[1] - s.bounds_min[1],
                    s.bounds_max[2] - s.bounds_min[2],
                ],
            )?;
            d.set_item("bounds", b)?;
        }
        Ok(d)
    }

    /// Geometry and format findings. Game-agnostic: game rules live in the profile layer.
    #[pyo3(signature = (grid=1, severity_min="info"))]
    fn validate<'py>(
        &self,
        py: Python<'py>,
        grid: i64,
        severity_min: &str,
    ) -> PyResult<Bound<'py, PyDict>> {
        let floor = match severity_min {
            "error" => Severity::Error,
            "warning" => Severity::Warning,
            "info" => Severity::Info,
            other => {
                return Err(err(format!(
                    "severity_min must be one of error, warning, info — got {other:?}"
                )))
            }
        };
        let t = Thresholds {
            grid,
            ..Default::default()
        };
        let report = validate_map(&self.inner, &t);

        let findings = PyList::empty(py);
        for f in report.sorted() {
            if f.severity < floor {
                continue;
            }
            let e = PyDict::new(py);
            e.set_item("severity", f.severity.as_str())?;
            e.set_item("code", f.code)?;
            e.set_item("message", &f.message)?;
            e.set_item("location", f.location.to_string())?;
            e.set_item("entity", f.location.entity)?;
            e.set_item("primitive", f.location.primitive)?;
            e.set_item("face", f.location.face)?;
            e.set_item("rule_source", f.rule_source)?;
            e.set_item("confidence", f.confidence.as_str())?;
            findings.append(e)?;
        }

        let summary = PyDict::new(py);
        summary.set_item("error", report.count(Severity::Error))?;
        summary.set_item("warning", report.count(Severity::Warning))?;
        summary.set_item("info", report.count(Severity::Info))?;

        let d = PyDict::new(py);
        d.set_item("findings", findings)?;
        d.set_item("summary", summary)?;
        Ok(d)
    }

    /// Entities, optionally filtered by classname, with their keys in file order.
    #[pyo3(signature = (classname=None, with_keys=true))]
    fn entities<'py>(
        &self,
        py: Python<'py>,
        classname: Option<&str>,
        with_keys: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        for (i, e) in self.inner.entities.iter().enumerate() {
            if let Some(want) = classname {
                if e.classname() != want {
                    continue;
                }
            }
            let d = PyDict::new(py);
            d.set_item("index", i)?;
            d.set_item("classname", e.classname())?;
            d.set_item("brushes", e.brushes().count())?;
            d.set_item("patches", e.patches().count())?;
            match e.origin() {
                Some(o) => d.set_item("origin", [o.x, o.y, o.z])?,
                None => d.set_item("origin", py.None())?,
            }
            if with_keys {
                // A list of pairs, not a dict: key order is meaningful and duplicate keys
                // occur in real maps, both of which a dict would silently destroy.
                let keys = PyList::empty(py);
                for (k, v) in &e.keys {
                    keys.append((k, v))?;
                }
                d.set_item("keys", keys)?;
            }
            out.append(d)?;
        }
        Ok(out)
    }

    /// Append an entity with these keys, in this order, and return its index.
    ///
    /// Edits the map in place, so it keeps its path and the bytes it was loaded from:
    /// `save()` still knows where to write, and `round_trip()` reports the edit as an edit.
    /// Re-parsing the source with the entity spliced in loses both.
    fn add_entity(&mut self, keys: Vec<(String, String)>) -> PyResult<usize> {
        for (k, v) in &keys {
            if let Some(why) = unwritable_pair(k, v) {
                return Err(err(why));
            }
        }
        let e = Entity {
            keys,
            ..Entity::default()
        };
        check_classname(&self.inner, None, e.classname())?;
        self.inner.entities.push(e);
        Ok(self.inner.entities.len() - 1)
    }

    /// Set keys on one entity, so each value is the one the engine reads.
    ///
    /// Every pair is checked before any is applied, so a refusal leaves the entity as it was.
    /// Returns one dict per key: `key`, `previous` (the value read before, or None) and
    /// `duplicates_removed` — a later duplicate is what the engine reads, so it has to go.
    fn set_keys<'py>(
        &mut self,
        py: Python<'py>,
        entity: usize,
        keys: Vec<(String, String)>,
    ) -> PyResult<Bound<'py, PyList>> {
        self.entity_index(entity)?;
        for (k, v) in &keys {
            if let Some(why) = unwritable_pair(k, v) {
                return Err(err(why));
            }
            if k.eq_ignore_ascii_case("classname") {
                check_classname(&self.inner, Some(entity), v)?;
            }
        }
        let e = &mut self.inner.entities[entity];
        let out = PyList::empty(py);
        for (k, v) in keys {
            let (previous, dropped) = e.set_effective(&k, v);
            let d = PyDict::new(py);
            d.set_item("key", k)?;
            d.set_item("previous", previous)?;
            d.set_item("duplicates_removed", dropped)?;
            out.append(d)?;
        }
        Ok(out)
    }

    /// Mirror every brush and patch across the plane `coordinate[axis] = at`, in place.
    ///
    /// Planes stay outward, textures come out as the mirror image, patches keep their
    /// facing (`nrc_core::mirror`). Entity keys are not touched. With `entity`, only that
    /// entity's primitives. Returns what was changed.
    #[pyo3(signature = (axis, at, entity=None))]
    fn mirror_geometry<'py>(
        &mut self,
        py: Python<'py>,
        axis: usize,
        at: f64,
        entity: Option<usize>,
    ) -> PyResult<Bound<'py, PyDict>> {
        if axis > 2 || !at.is_finite() {
            return Err(err("axis is 0, 1 or 2 and at a finite coordinate"));
        }
        let s = match entity {
            None => nrc_core::mirror::mirror_geometry(&mut self.inner, axis, at),
            Some(i) => {
                let e = self
                    .inner
                    .entities
                    .get_mut(i)
                    .ok_or_else(|| err(format!("no entity {i}")))?;
                let mut s = nrc_core::mirror::MirrorStats::default();
                nrc_core::mirror::mirror_entity(e, axis, at, &mut s);
                s
            }
        };
        let d = PyDict::new(py);
        d.set_item("brushes", s.brushes)?;
        d.set_item("faces", s.faces)?;
        d.set_item("patches", s.patches)?;
        d.set_item("raw_blocks", s.raw_blocks)?;
        d.set_item("degenerate_faces", s.degenerate_faces)?;
        Ok(d)
    }

    /// A fingerprint of every brush's geometry: `(entity, primitive, hash)`.
    ///
    /// The hash covers the plane points only, so a brush that moves or changes shape gets a new
    /// one and a retextured brush keeps it. Stable within one process, which is all it is for:
    /// telling the brushes a map was opened with from those added or reshaped since.
    fn brush_fingerprints(&self) -> Vec<(usize, usize, u64)> {
        use std::hash::{Hash, Hasher};
        let mut out = Vec::new();
        for (ei, e) in self.inner.entities.iter().enumerate() {
            for (pi, p) in e.prims.iter().enumerate() {
                let Primitive::Brush(b) = p else { continue };
                let mut h = std::collections::hash_map::DefaultHasher::new();
                for f in &b.faces {
                    for pt in &f.points {
                        for c in pt {
                            c.value().to_bits().hash(&mut h);
                        }
                    }
                }
                out.push((ei, pi, h.finish()));
            }
        }
        out
    }

    /// Set or clear the detail contents bit on whole brushes, leaving every other bit alone.
    ///
    /// Every brush is checked before any is changed, so a refusal leaves the map as it was. The
    /// engine treats a brush as detail if any face carries the bit, so it is set or cleared on
    /// all of them. A face with no contents trio (Valve 220 may omit it) gains one. Returns one
    /// dict per brush: `entity`, `primitive`, `was_detail`, `faces`.
    fn set_brush_detail<'py>(
        &mut self,
        py: Python<'py>,
        primitives: Vec<(usize, usize)>,
        detail: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        use nrc_core::model::SurfaceFlags;
        use nrc_core::num::Num;
        use nrc_core::stats::BRUSH_DETAIL_MASK;
        for &(ei, pi) in &primitives {
            self.entity_index(ei)?;
            match self.inner.entities[ei].prims.get(pi) {
                Some(Primitive::Brush(_)) => {}
                Some(Primitive::Patch(_)) => {
                    return Err(err(format!(
                        "entity {ei} primitive {pi} is a patch; patches carry no brush contents"
                    )))
                }
                Some(Primitive::Raw(r)) => {
                    return Err(err(format!(
                        "entity {ei} primitive {pi} is an unrecognized `{}` block",
                        r.keyword
                    )))
                }
                None => return Err(err(format!("entity {ei} has no primitive {pi}"))),
            }
        }
        let out = PyList::empty(py);
        for (ei, pi) in primitives {
            let Some(Primitive::Brush(b)) = self.inner.entities[ei].prims.get_mut(pi) else {
                unreachable!("checked above")
            };
            let was = b.faces.iter().any(|f| {
                f.surface
                    .as_ref()
                    .is_some_and(|s| s.contents.value() as i64 & BRUSH_DETAIL_MASK != 0)
            });
            for f in &mut b.faces {
                let s = f.surface.get_or_insert(SurfaceFlags {
                    contents: Num::new(0.0),
                    flags: Num::new(0.0),
                    value: Num::new(0.0),
                });
                let c = s.contents.value() as i64;
                let c = if detail {
                    c | BRUSH_DETAIL_MASK
                } else {
                    c & !BRUSH_DETAIL_MASK
                };
                if c != s.contents.value() as i64 {
                    s.contents = Num::new(c as f64);
                }
            }
            let d = PyDict::new(py);
            d.set_item("entity", ei)?;
            d.set_item("primitive", pi)?;
            d.set_item("was_detail", was)?;
            d.set_item("faces", b.faces.len())?;
            out.append(d)?;
        }
        Ok(out)
    }

    /// Remove keys from one entity, every occurrence of each in any spelling, since the engine
    /// matches key names without regard to case. Returns `(key, removed)` pairs.
    fn remove_keys(&mut self, entity: usize, keys: Vec<String>) -> PyResult<Vec<(String, bool)>> {
        self.entity_index(entity)?;
        if keys.iter().any(|k| k.eq_ignore_ascii_case("classname")) {
            return Err(err(
                "refusing to remove classname: an entity without one is not an entity to the \
                 engine. Set a different classname instead.",
            ));
        }
        let e = &mut self.inner.entities[entity];
        Ok(keys
            .into_iter()
            .map(|k| {
                let removed = e.remove_effective(&k);
                (k, removed)
            })
            .collect())
    }

    /// Every patch in the map, with its shader and dimensions.
    ///
    /// `brush_geometry` refuses a patch, and without this a caller wanting patch shaders has to
    /// re-parse the `.map` text — a second, worse parser, which is exactly what this binding
    /// exists to prevent. A shader audit that skips patches misreports twice over: a patch-only
    /// shader looks unreferenced, and a patch's missing shader is not reported at all.
    fn patches<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        for (ei, e) in self.inner.entities.iter().enumerate() {
            for (pi, prim) in e.prims.iter().enumerate() {
                let Primitive::Patch(p) = prim else { continue };
                let d = PyDict::new(py);
                d.set_item("entity", ei)?;
                d.set_item("primitive", pi)?;
                d.set_item("classname", e.classname())?;
                d.set_item("kind", &p.kind)?;
                d.set_item("shader", &p.shader)?;
                d.set_item("width", p.width())?;
                d.set_item("height", p.height())?;
                d.set_item("dimensions_consistent", p.dimensions_consistent())?;
                let mut bb = nrc_core::math::Aabb::EMPTY;
                for c in p.control_points() {
                    bb.extend(c);
                }
                if bb.is_empty() {
                    d.set_item("bounds", py.None())?;
                } else {
                    d.set_item("bounds", (bb.min.to_array(), bb.max.to_array()))?;
                }
                out.append(d)?;
            }
        }
        Ok(out)
    }

    /// Every patch's collision surface, as the engine builds it at map load.
    ///
    /// One dict per patch: `entity`, `primitive`, `classname`, `shader`, and either `facets`
    /// (each `(normal, dist, points)`: a planar triangle or quad that blocks from the side
    /// `normal` faces) with the subdivided `grid` size, or `error` naming why the engine would
    /// refuse the patch. See `nrc_solid::patch::collision` for the port and its sources.
    fn patch_collision<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        for (ei, e) in self.inner.entities.iter().enumerate() {
            for (pi, prim) in e.prims.iter().enumerate() {
                let Primitive::Patch(p) = prim else { continue };
                let d = PyDict::new(py);
                d.set_item("entity", ei)?;
                d.set_item("primitive", pi)?;
                d.set_item("classname", e.classname())?;
                d.set_item("shader", &p.shader)?;
                let rows: Option<Vec<Vec<[f64; 3]>>> = p
                    .rows
                    .iter()
                    .map(|r| {
                        r.iter()
                            .map(|c| {
                                (c.len() >= 3).then(|| [c[0].value(), c[1].value(), c[2].value()])
                            })
                            .collect()
                    })
                    .collect();
                let result = match rows {
                    None => Err("a control point has fewer than three coordinates".to_string()),
                    Some(_) if !p.dimensions_consistent() => {
                        Err("declared dimensions do not match the control points".to_string())
                    }
                    Some(rows) => nrc_solid::patch::collision(&rows),
                };
                match result {
                    Ok(c) => {
                        let facets = PyList::empty(py);
                        for f in &c.facets {
                            let points: Vec<(f64, f64, f64)> =
                                f.points.iter().map(|q| (q[0], q[1], q[2])).collect();
                            facets.append((
                                (f.normal[0], f.normal[1], f.normal[2]),
                                f.dist,
                                points,
                            ))?;
                        }
                        d.set_item("facets", facets)?;
                        d.set_item("grid", c.grid)?;
                        d.set_item("error", py.None())?;
                    }
                    Err(e) => {
                        d.set_item("facets", PyList::empty(py))?;
                        d.set_item("grid", py.None())?;
                        d.set_item("error", e)?;
                    }
                }
                out.append(d)?;
            }
        }
        Ok(out)
    }

    /// Axis-aligned bounds as `(min, max)`, or `None` for an empty map.
    fn bounds(&self) -> Option<([f64; 3], [f64; 3])> {
        let b = self.inner.bounds();
        if b.is_empty() {
            None
        } else {
            Some((b.min.to_array(), b.max.to_array()))
        }
    }

    /// Exact vertices of one brush, plus what the kernel could determine about it.
    ///
    /// With `approximate=True`, a brush the exact path declines only because its plane
    /// points are off the integer grid (a map decompiled from a BSP, say) is evaluated in
    /// floating point instead (`nrc_core::approx_geometry`) and comes back `usable` with
    /// `approximate: True` and the exact path's `exact_reason`. Its shape is good to a
    /// hundredth of a unit; its `off_grid_vertices` and `redundant_faces` are tolerance
    /// answers. On-grid brushes are answered exactly either way.
    #[pyo3(signature = (entity, primitive, approximate=false))]
    fn brush_geometry<'py>(
        &self,
        py: Python<'py>,
        entity: usize,
        primitive: usize,
        approximate: bool,
    ) -> PyResult<Bound<'py, PyDict>> {
        let e = self
            .inner
            .entities
            .get(entity)
            .ok_or_else(|| err(format!("no entity {entity}")))?;
        let p = e
            .prims
            .get(primitive)
            .ok_or_else(|| err(format!("entity {entity} has no primitive {primitive}")))?;
        let b = match p {
            Primitive::Brush(b) => b,
            Primitive::Patch(_) => return Err(err("that primitive is a patch, not a brush")),
            Primitive::Raw(r) => {
                return Err(err(format!(
                    "that primitive is an unrecognized `{}` block and cannot be analysed",
                    r.keyword
                )))
            }
        };

        let d = PyDict::new(py);
        d.set_item("faces", b.faces.len())?;
        // Per-face contents and the derived detail flag. Without these a caller has to re-parse
        // the `.map` text to learn whether a brush is structural, which is exactly the second,
        // worse parser this binding exists to prevent.
        let contents: Vec<i64> = b
            .faces
            .iter()
            .map(|f| f.surface.as_ref().map_or(0, |s| s.contents.value() as i64))
            .collect();
        d.set_item(
            "detail",
            contents
                .iter()
                .any(|c| c & nrc_core::stats::BRUSH_DETAIL_MASK != 0),
        )?;
        d.set_item("face_contents", &contents)?;
        d.set_item(
            "surface_flags",
            b.faces
                .iter()
                .map(|f| f.surface.as_ref().map_or(0, |s| s.flags.value() as i64))
                .collect::<Vec<i64>>(),
        )?;
        d.set_item(
            "shaders",
            b.faces
                .iter()
                .map(|f| f.shader.as_str())
                .collect::<Vec<_>>(),
        )?;
        match nrc_core::brush_geometry(&b.faces) {
            Err(deg @ nrc_core::Degeneracy::NotExactlyRepresentable(_)) if approximate => {
                match nrc_core::approx_geometry(&b.faces) {
                    Err(why) => {
                        d.set_item("usable", false)?;
                        d.set_item("reason", format!("{deg}; in floating point, {why}"))?;
                    }
                    Ok(g) => {
                        d.set_item("usable", true)?;
                        d.set_item("approximate", true)?;
                        d.set_item("exact_reason", deg.to_string())?;
                        let verts = PyList::empty(py);
                        for p in &g.vertices {
                            verts.append([p.x, p.y, p.z])?;
                        }
                        d.set_item("vertices", verts)?;
                        d.set_item("redundant_faces", g.redundant_faces())?;
                        d.set_item("min_thickness", g.min_thickness())?;
                        d.set_item("off_grid_vertices", g.off_grid_vertices())?;
                        let bb = g.bounds();
                        if !bb.is_empty() {
                            d.set_item("bounds", (bb.min.to_array(), bb.max.to_array()))?;
                        }
                    }
                }
            }
            Err(deg) => {
                d.set_item("usable", false)?;
                d.set_item("reason", deg.to_string())?;
            }
            Ok(g) => {
                d.set_item("usable", true)?;
                let verts = PyList::empty(py);
                for v in &g.vertices {
                    let p = v.to_vec3();
                    verts.append([p.x, p.y, p.z])?;
                }
                d.set_item("vertices", verts)?;
                d.set_item("redundant_faces", g.redundant_faces())?;
                d.set_item("min_thickness", g.min_thickness())?;
                d.set_item("off_grid_vertices", g.off_grid_vertices(1))?;
                let bb = g.bounds();
                if !bb.is_empty() {
                    d.set_item("bounds", (bb.min.to_array(), bb.max.to_array()))?;
                }
            }
        }
        Ok(d)
    }

    /// Render a view, returning `(png_bytes, annotations)`.
    ///
    /// `eye_height` is required for `view="player_eye"` and must come from the game profile
    /// — hardcoding a standing height here would be the §7.4 seam violation the design
    /// document explicitly warns about, and the figure it assumed was wrong for the first
    /// target game anyway.
    #[pyo3(signature = (
        view = "top",
        overlay = "shaded",
        width = 900,
        height = 700,
        grid = 1,
        grid_spacing = 64.0,
        wireframe = None,
        hide_invisible = false,
        annotate = true,
        eye = None,
        target = None,
        yaw_deg = 0.0,
        eye_height = None,
        fov_deg = 55.0,
        frame = None,
        padding = 26.0,
        tga = false,
        cut_above = None,
        route_segments = None,
        route_transitions = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn render<'py>(
        &self,
        py: Python<'py>,
        view: &str,
        overlay: &str,
        width: u32,
        height: u32,
        grid: i64,
        grid_spacing: f64,
        wireframe: Option<bool>,
        hide_invisible: bool,
        annotate: bool,
        eye: Option<[f64; 3]>,
        target: Option<[f64; 3]>,
        yaw_deg: f64,
        eye_height: Option<f64>,
        fov_deg: f64,
        frame: Option<[f64; 4]>,
        padding: f64,
        tga: bool,
        cut_above: Option<f64>,
        route_segments: Option<&Bound<'_, PyList>>,
        route_transitions: Option<&Bound<'_, PyList>>,
    ) -> PyResult<(Bound<'py, pyo3::types::PyBytes>, Bound<'py, PyDict>)> {
        use nrc_render::camera::OrthoAxis;
        use nrc_render::{RenderOptions, SceneOptions, View};

        let overlay = parse_overlay(overlay)?;
        let route = route_overlay_from(route_segments, route_transitions)?;
        if route.is_some() && view != "top" {
            return Err(err("route overlays require view='top'"));
        }
        let scene = SceneOptions {
            grid,
            ..Default::default()
        };
        let spacing = if grid_spacing > 0.0 {
            Some(grid_spacing)
        } else {
            None
        };
        let to_vec = |a: [f64; 3]| nrc_core::math::vec3(a[0], a[1], a[2]);

        let result = if view == "sheet" || view == "contact_sheet" {
            nrc_render::contact_sheet(
                &self.inner,
                &nrc_render::ContactSheetOptions {
                    width,
                    height,
                    overlay,
                    grid_spacing: spacing,
                    scene,
                    hide_invisible,
                    player_eye: match (eye, eye_height) {
                        (Some(p), Some(h)) => Some((to_vec(p), yaw_deg, h)),
                        _ => None,
                    },
                },
            )
        } else {
            let v = match view {
                "top" => View::Ortho(OrthoAxis::Top),
                "front" => View::Ortho(OrthoAxis::Front),
                "side" => View::Ortho(OrthoAxis::Side),
                "perspective" => View::Perspective {
                    eye: eye.map(to_vec),
                    target: target.map(to_vec),
                    fov_deg,
                },
                "player_eye" => {
                    let position = eye.map(to_vec).ok_or_else(|| {
                        err("view='player_eye' needs `eye` as the floor position")
                    })?;
                    let eye_height = eye_height.ok_or_else(|| {
                        err(
                            "view='player_eye' needs `eye_height`; read it from the game \
                             profile rather than assuming a value",
                        )
                    })?;
                    View::PlayerEye {
                        position,
                        yaw_deg,
                        eye_height,
                        fov_deg,
                    }
                }
                other => {
                    return Err(err(format!(
                        "unknown view {other:?}: use top, front, side, perspective, \
                         player_eye or sheet"
                    )))
                }
            };
            nrc_render::render(
                &self.inner,
                &RenderOptions {
                    width,
                    height,
                    view: v,
                    overlay,
                    wireframe,
                    draw_edges: true,
                    hide_invisible,
                    grid_spacing: spacing,
                    annotate,
                    scene,
                    // [xmin, ymin, xmax, ymax]; z spans everything, so only the plan matters.
                    frame: frame.map(|[x0, y0, x1, y1]| nrc_core::math::Aabb {
                        min: nrc_core::math::vec3(x0, y0, -65536.0),
                        max: nrc_core::math::vec3(x1, y1, 65536.0),
                    }),
                    padding,
                    tga,
                    cut_above,
                    route,
                },
            )
        }
        .map_err(err)?;

        let a = &result.annotations;
        let d = PyDict::new(py);
        d.set_item("view", &a.view)?;
        d.set_item("overlay", &a.overlay)?;
        d.set_item("width", a.width)?;
        d.set_item("height", a.height)?;
        d.set_item("grid", a.grid)?;
        d.set_item("off_grid_vertices", a.off_grid_vertices)?;
        d.set_item("skipped_brushes", a.skipped_brushes)?;
        d.set_item("skipped_examples", a.skipped_examples.clone())?;
        d.set_item("units_per_pixel", a.units_per_pixel)?;
        d.set_item("camera_eye", a.camera_eye)?;
        d.set_item("camera_target", a.camera_target)?;
        d.set_item("notes", a.notes.clone())?;
        d.set_item("png_bytes", result.png.len())?;

        let counts = PyDict::new(py);
        counts.set_item("structural_brushes", a.counts.structural)?;
        counts.set_item("detail_brushes", a.counts.detail)?;
        counts.set_item("brush_entities", a.counts.brush_entity)?;
        counts.set_item("patches", a.counts.patches)?;
        counts.set_item("facets", a.counts.facets)?;
        counts.set_item("invisible_facets", a.counts.caulk_facets)?;
        d.set_item("counts", counts)?;

        match (a.bounds_min, a.bounds_max) {
            (Some(lo), Some(hi)) => {
                let b = PyDict::new(py);
                b.set_item("min", lo)?;
                b.set_item("max", hi)?;
                b.set_item("size", a.size)?;
                d.set_item("bounds", b)?;
            }
            _ => d.set_item("bounds", py.None())?,
        }

        // A TGA, when asked for, comes back in place of the PNG: it is what the caller writes.
        let bytes = result.tga.as_deref().unwrap_or(&result.png);
        Ok((pyo3::types::PyBytes::new(py, bytes), d))
    }

    #[getter]
    fn path(&self) -> Option<String> {
        self.path.as_ref().map(|p| p.display().to_string())
    }

    #[getter]
    fn entity_count(&self) -> usize {
        self.inner.entities.len()
    }

    fn __repr__(&self) -> String {
        format!(
            "<nrc_py.Map {} entities, {} brushes, {} patches{}>",
            self.inner.entities.len(),
            self.inner.brush_count(),
            self.inner.patch_count(),
            match &self.path {
                Some(p) => format!(" from {}", p.display()),
                None => String::new(),
            }
        )
    }
}

impl PyMap {
    fn entity_index(&self, entity: usize) -> PyResult<()> {
        let n = self.inner.entities.len();
        if entity >= n {
            return Err(err(format!(
                "no entity {entity}: this map has {n} (0 to {}); query_entities lists them",
                n.saturating_sub(1)
            )));
        }
        Ok(())
    }
}

/// Refuse a classname that would break the map's one structural invariant: exactly one
/// worldspawn, which every compiler reads as the world. `entity` is the one being changed, or
/// None for an entity about to be added.
fn check_classname(m: &Map, entity: Option<usize>, classname: &str) -> PyResult<()> {
    if classname.is_empty() {
        return Err(err("an entity needs a non-empty classname"));
    }
    let world = m.entities.iter().position(|e| e.is_worldspawn());
    if let (Some(i), Some(w)) = (entity, world) {
        if i == w && classname != "worldspawn" {
            return Err(err(format!(
                "refusing to change the classname of entity {w}: it is the worldspawn, and a \
                 map without one does not compile"
            )));
        }
    }
    if classname == "worldspawn" && world.is_some() && world != entity {
        return Err(err(format!(
            "refusing a second worldspawn: entity {} already is one, and a map has exactly one",
            world.unwrap_or_default()
        )));
    }
    Ok(())
}

/// Parse and immediately re-serialize, reporting whether the bytes match.
#[pyfunction]
fn round_trip_check<'py>(py: Python<'py>, source: &str) -> PyResult<Bound<'py, PyDict>> {
    PyMap::parse(source)?.round_trip(py)
}

#[pymodule]
fn nrc_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMap>()?;
    m.add_function(wrap_pyfunction!(round_trip_check, m)?)?;
    m.add_function(wrap_pyfunction!(solid_compile, m)?)?;
    m.add_function(wrap_pyfunction!(solid_commit, m)?)?;
    m.add_function(wrap_pyfunction!(patch_commit, m)?)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Solid IR (§4)
// ---------------------------------------------------------------------------

use nrc_solid::ir::Node;
use nrc_solid::prim::Axis as SolidAxis;

fn ivec_from(obj: &Bound<'_, PyAny>, what: &str) -> PyResult<nrc_core::exact::IVec3> {
    let v: Vec<f64> = obj
        .extract()
        .map_err(|_| err(format!("{what} must be a list of three numbers")))?;
    if v.len() != 3 {
        return Err(err(format!("{what} must have exactly three components")));
    }
    for c in &v {
        if !c.is_finite() || c.fract() != 0.0 {
            return Err(err(format!(
                "{what} must be whole numbers — the Solid IR works on the integer grid, and \
                 {c} is not on it"
            )));
        }
    }
    Ok(nrc_core::exact::ivec3(
        v[0] as i64,
        v[1] as i64,
        v[2] as i64,
    ))
}

fn axis_from(d: &Bound<'_, PyDict>, key: &str, default: &str) -> PyResult<SolidAxis> {
    let raw: String = match d.get_item(key)? {
        Some(v) => v
            .extract()
            .map_err(|_| err(format!("{key} must be a string")))?,
        None => default.to_string(),
    };
    SolidAxis::parse(&raw).ok_or_else(|| err(format!("{key} must be x, y or z — got {raw:?}")))
}

fn get<'py>(d: &Bound<'py, PyDict>, key: &str, op: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(key)?
        .ok_or_else(|| err(format!("a {op:?} node needs a {key:?} field")))
}

fn route_overlay_from(
    segments: Option<&Bound<'_, PyList>>,
    transitions: Option<&Bound<'_, PyList>>,
) -> PyResult<Option<nrc_render::route::RouteOverlay>> {
    use nrc_render::route::{RouteOverlay, Segment, Stance, Transition};
    if segments.is_none() && transitions.is_none() {
        return Ok(None);
    }
    let stance = |dict: &Bound<'_, PyDict>, key: &str| -> PyResult<Stance> {
        let value: String = get(dict, key, "route")?.extract()?;
        match value.as_str() {
            "stand" => Ok(Stance::Stand),
            "crouch" => Ok(Stance::Crouch),
            "prone" => Ok(Stance::Prone),
            _ => Err(err(format!("{key} must be stand, crouch or prone"))),
        }
    };
    let point = |p: [f64; 3]| nrc_core::math::vec3(p[0], p[1], p[2]);
    let mut out = RouteOverlay::default();
    if let Some(segments) = segments {
        for item in segments {
            let d = item
                .cast::<PyDict>()
                .map_err(|_| err("each route segment must be a dict"))?;
            let points: Vec<[f64; 3]> = get(d, "points", "route segment")?.extract()?;
            out.segments.push(Segment {
                stance: stance(d, "stance")?,
                points: points.into_iter().map(point).collect(),
            });
        }
    }
    if let Some(transitions) = transitions {
        for item in transitions {
            let d = item
                .cast::<PyDict>()
                .map_err(|_| err("each route transition must be a dict"))?;
            out.transitions.push(Transition {
                position: point(get(d, "position", "route transition")?.extract()?),
                from_stance: stance(d, "from_stance")?,
                to_stance: stance(d, "to_stance")?,
            });
        }
    }
    out.validate().map_err(err)?;
    Ok(Some(out))
}

fn i64_from(d: &Bound<'_, PyDict>, key: &str, op: &str) -> PyResult<i64> {
    get(d, key, op)?
        .extract()
        .map_err(|_| err(format!("{key} must be a whole number in a {op:?} node")))
}

fn usize_from(d: &Bound<'_, PyDict>, key: &str, op: &str) -> PyResult<usize> {
    let v = i64_from(d, key, op)?;
    usize::try_from(v).map_err(|_| err(format!("{key} must not be negative, got {v}")))
}

fn f64_or(d: &Bound<'_, PyDict>, key: &str, default: f64) -> PyResult<f64> {
    match d.get_item(key)? {
        Some(v) => v
            .extract()
            .map_err(|_| err(format!("{key} must be a number"))),
        None => Ok(default),
    }
}

fn child(d: &Bound<'_, PyDict>, key: &str, op: &str, depth: usize) -> PyResult<Box<Node>> {
    let raw = get(d, key, op)?;
    let dict = raw
        .cast::<PyDict>()
        .map_err(|_| err(format!("{key} in a {op:?} node must be another IR node")))?;
    Ok(Box::new(node_from_dict(dict, depth + 1)?))
}

fn children(d: &Bound<'_, PyDict>, key: &str, op: &str, depth: usize) -> PyResult<Vec<Node>> {
    let raw = get(d, key, op)?;
    let list: Vec<Bound<'_, PyAny>> = raw
        .extract()
        .map_err(|_| err(format!("{key} in a {op:?} node must be a list of IR nodes")))?;
    let mut out = Vec::with_capacity(list.len());
    for item in list {
        let dict = item
            .cast::<PyDict>()
            .map_err(|_| err(format!("every entry of {key} must be an IR node")))?;
        out.push(node_from_dict(dict, depth + 1)?);
    }
    Ok(out)
}

/// Turn a Python dict into an IR node.
///
/// Errors name the field and the operator, because an agent debugging a nested tree from the
/// outside has nothing else to go on. The depth guard is here as well as in the evaluator: a
/// deeply nested dict would otherwise blow the stack during *conversion*, before the
/// evaluator's own limit could apply.
fn node_from_dict(d: &Bound<'_, PyDict>, depth: usize) -> PyResult<Node> {
    if depth > nrc_solid::ir::MAX_DEPTH {
        return Err(err(format!(
            "IR nesting deeper than {} is not accepted",
            nrc_solid::ir::MAX_DEPTH
        )));
    }
    let op: String = d
        .get_item("op")?
        .ok_or_else(|| err("every IR node needs an \"op\" field"))?
        .extract()
        .map_err(|_| err("\"op\" must be a string"))?;

    Ok(match op.as_str() {
        "box" => Node::Box {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
        },
        "wedge" => Node::Wedge {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            along: axis_from(d, "along", "x")?,
            up: axis_from(d, "up", "z")?,
        },
        "prism" | "cylinder" => Node::Prism {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
            sides: usize_from(d, "sides", &op)?,
            start_deg: f64_or(d, "start_deg", 0.0)?,
        },
        "cone" => Node::Cone {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
            sides: usize_from(d, "sides", &op)?,
            start_deg: f64_or(d, "start_deg", 0.0)?,
        },
        "pyramid" => Node::Pyramid {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
        },
        "stair" => Node::Stair {
            origin: ivec_from(&get(d, "origin", &op)?, "origin")?,
            width: i64_from(d, "width", &op)?,
            steps: usize_from(d, "steps", &op)?,
            rise: i64_from(d, "rise", &op)?,
            run: i64_from(d, "run", &op)?,
            along: axis_from(d, "along", "x")?,
            up: axis_from(d, "up", "z")?,
        },
        "pipe" => Node::Pipe {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
            wall: i64_from(d, "wall", &op)?,
            sides: usize_from(d, "sides", &op)?,
            start_deg: f64_or(d, "start_deg", 0.0)?,
        },
        "arch" => Node::Arch {
            centre: ivec_from(
                &get(d, "centre", &op).or_else(|_| get(d, "center", &op))?,
                "centre",
            )?,
            outer_radius: i64_from(d, "outer_radius", &op)?,
            thickness: i64_from(d, "thickness", &op)?,
            depth: i64_from(d, "depth", &op)?,
            segments: usize_from(d, "segments", &op)?,
            axis: axis_from(d, "axis", "z")?,
        },
        "planes" => {
            let raw: Vec<Vec<f64>> = get(d, "planes", &op)?
                .extract()
                .map_err(|_| err("planes must be a list of [nx, ny, nz, d] lists"))?;
            let mut out = Vec::with_capacity(raw.len());
            for (i, pl) in raw.iter().enumerate() {
                if pl.len() != 4 {
                    return Err(err(format!(
                        "plane {i} needs exactly four numbers [nx, ny, nz, d], got {}",
                        pl.len()
                    )));
                }
                for c in pl {
                    if !c.is_finite() || c.fract() != 0.0 {
                        return Err(err(format!(
                            "plane {i} has a non-integer coefficient ({c}); half-spaces must be \
                             integral so the geometry stays exact"
                        )));
                    }
                }
                if pl[0] == 0.0 && pl[1] == 0.0 && pl[2] == 0.0 {
                    return Err(err(format!("plane {i} has a zero normal")));
                }
                out.push(nrc_core::exact::IPlane {
                    nx: pl[0] as i128,
                    ny: pl[1] as i128,
                    nz: pl[2] as i128,
                    d: pl[3] as i128,
                });
            }
            Node::Planes(out)
        }
        "union" => Node::Union(children(d, "parts", &op, depth)?),
        "intersect" => Node::Intersect(children(d, "parts", &op, depth)?),
        "subtract" => Node::Subtract {
            from: child(d, "from", &op, depth)?,
            cut: children(d, "cut", &op, depth)?,
        },
        "hollow" => Node::Hollow {
            solid: child(d, "solid", &op, depth)?,
            thickness: i64_from(d, "thickness", &op)?,
            open_faces: match d.get_item("open_faces")? {
                Some(v) => v
                    .extract()
                    .map_err(|_| err("open_faces must be a list of face indices"))?,
                None => Vec::new(),
            },
        },
        "carve_opening" => Node::CarveOpening {
            wall: child(d, "wall", &op, depth)?,
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
        },
        "translate" => Node::Translate {
            node: child(d, "node", &op, depth)?,
            by: ivec_from(&get(d, "by", &op)?, "by")?,
        },
        "mirror" => Node::Mirror {
            node: child(d, "node", &op, depth)?,
            axis: axis_from(d, "axis", "x")?,
            at: match d.get_item("at")? {
                Some(v) => v.extract().map_err(|_| err("at must be a whole number"))?,
                None => 0,
            },
        },
        "array" => Node::Array {
            node: child(d, "node", &op, depth)?,
            count: usize_from(d, "count", &op)?,
            offset: ivec_from(&get(d, "offset", &op)?, "offset")?,
        },
        other => {
            return Err(err(format!(
                "unknown operator {other:?}. Available: box, wedge, prism, cone, pyramid, \
                 stair, pipe, arch, planes, union, intersect, subtract, hollow, \
                 carve_opening, translate, mirror, array"
            )))
        }
    })
}

fn texture_spec_from(d: Option<&Bound<'_, PyDict>>) -> PyResult<nrc_solid::emit::TextureSpec> {
    let mut spec = nrc_solid::emit::TextureSpec::default();
    let Some(d) = d else { return Ok(spec) };
    // A misspelt key ("nort") would otherwise be dropped in silence and the face left on the
    // default shader, which is exactly the kind of quiet wrongness this layer exists to stop.
    const KNOWN: [&str; 11] = [
        "default", "top", "bottom", "north", "south", "east", "west", "scale", "offset", "rotate",
        "detail",
    ];
    for key in d.keys() {
        let k: String = key
            .extract()
            .map_err(|_| err("texture keys must be strings"))?;
        if !KNOWN.contains(&k.as_str()) {
            return Err(err(format!(
                "unknown texture key {k:?}; known: {}",
                KNOWN.join(", ")
            )));
        }
    }
    for (key, slot) in [
        ("north", &mut spec.north),
        ("south", &mut spec.south),
        ("east", &mut spec.east),
        ("west", &mut spec.west),
    ] {
        if let Some(v) = d.get_item(key)? {
            *slot = Some(
                v.extract()
                    .map_err(|_| err(format!("{key} texture must be a string")))?,
            );
        }
    }
    if let Some(v) = d.get_item("offset")? {
        spec.offset = v
            .extract()
            .map_err(|_| err("texture offset must be two numbers [s, t]"))?;
    }
    if let Some(v) = d.get_item("rotate")? {
        spec.rotate = v
            .extract()
            .map_err(|_| err("texture rotate must be a number of degrees"))?;
    }
    if let Some(v) = d.get_item("default")? {
        spec.default = v
            .extract()
            .map_err(|_| err("default texture must be a string"))?;
    }
    if let Some(v) = d.get_item("top")? {
        spec.top = Some(
            v.extract()
                .map_err(|_| err("top texture must be a string"))?,
        );
    }
    if let Some(v) = d.get_item("bottom")? {
        spec.bottom = Some(
            v.extract()
                .map_err(|_| err("bottom texture must be a string"))?,
        );
    }
    if let Some(v) = d.get_item("scale")? {
        spec.scale = v
            .extract()
            .map_err(|_| err("texture scale must be a number"))?;
    }
    if let Some(v) = d.get_item("detail")? {
        spec.detail = v
            .extract()
            .map_err(|_| err("detail must be true or false"))?;
    }
    Ok(spec)
}

/// Add primitives to the entity they belong to: entity `target_entity`, the worldspawn, or a
/// new entity of `target_classname`. Any class but the worldspawn gets a new entity every
/// time: joining the first of the class silently made a second door or explosive part of the
/// first one - one entity, both volumes, one set of keys and one script block. Adding to an
/// existing brush entity takes its index. `new_entity` is kept for callers that pass it; for
/// the worldspawn it is refused, as a second worldspawn is. Returns the entity's index.
fn place_prims(
    map: &mut Map,
    prims: Vec<Primitive>,
    target_classname: &str,
    new_entity: bool,
    target_entity: Option<usize>,
    d: &Bound<'_, PyDict>,
) -> PyResult<usize> {
    if let Some(i) = target_entity {
        let n = map.entities.len();
        let e = map.entities.get_mut(i).ok_or_else(|| {
            err(format!(
                "no entity {i}: this map has {n} (0 to {})",
                n.saturating_sub(1)
            ))
        })?;
        e.prims.extend(prims);
        return Ok(i);
    }
    if target_classname == "worldspawn" && !new_entity {
        if let Some(i) = map.entities.iter().position(|e| e.is_worldspawn()) {
            map.entities[i].prims.extend(prims);
            return Ok(i);
        }
    }
    if let Some(why) = unwritable_pair("classname", target_classname) {
        return Err(err(why));
    }
    check_classname(map, None, target_classname)?;
    let mut e = nrc_core::model::Entity::default();
    e.set("classname", target_classname);
    e.prims = prims;
    map.entities.push(e);
    d.set_item("created_entity", target_classname)?;
    Ok(map.entities.len() - 1)
}

/// Add a curved surface - a cylinder, an arch or a bevel - as a `patchDef2`.
#[pyfunction]
#[pyo3(signature = (map, spec, shader, target_classname="worldspawn", new_entity=false, target_entity=None, label=None))]
#[allow(clippy::too_many_arguments)]
fn patch_commit<'py>(
    py: Python<'py>,
    map: &Bound<'py, PyMap>,
    spec: &Bound<'py, PyDict>,
    shader: &str,
    target_classname: &str,
    new_entity: bool,
    target_entity: Option<usize>,
    label: Option<String>,
) -> PyResult<Bound<'py, PyDict>> {
    use nrc_solid::patch::{generate, Facing, PatchSpec};
    if new_entity && target_entity.is_some() {
        return Err(err("pass new_entity or target_entity, not both"));
    }
    let get = |k: &str| spec.get_item(k);
    let kind: String = get("kind")?
        .ok_or_else(|| err("spec needs a kind: cylinder, arch or bevel"))?
        .extract()?;
    let centre: [f64; 3] = get("centre")?
        .ok_or_else(|| err("spec needs a centre [x, y, z]"))?
        .extract()
        .map_err(|_| err("centre must be three numbers"))?;
    let radius: f64 = get("radius")?
        .ok_or_else(|| err("spec needs a radius"))?
        .extract()?;
    let length: f64 = match get("length")? {
        Some(v) => v.extract()?,
        None => get("height")?
            .ok_or_else(|| err("spec needs a length (or height)"))?
            .extract()?,
    };
    let axis: String = match get("axis")? {
        Some(v) => v.extract()?,
        None => "z".into(),
    };
    let axis = match axis.as_str() {
        "x" => 'x',
        "y" => 'y',
        "z" => 'z',
        _ => return Err(err("axis must be x, y or z")),
    };
    let facing = match get("facing")? {
        Some(v) => match v.extract::<String>()?.as_str() {
            "out" => Facing::Out,
            "in" => Facing::In,
            other => return Err(err(format!("facing must be out or in, got {other:?}"))),
        },
        None if kind == "arch" => Facing::In,
        None => Facing::Out,
    };
    let start_deg: f64 = match get("start_deg")? {
        Some(v) => v.extract()?,
        None => 0.0,
    };
    let texture_units: f64 = match get("texture_units")? {
        Some(v) => v.extract()?,
        None => 128.0,
    };
    let rows = generate(&PatchSpec {
        kind: kind.clone(),
        centre,
        radius,
        length,
        axis,
        start_deg,
        facing,
        texture_units,
    })
    .map_err(err)?;
    let (w, h) = (rows.len(), rows.first().map_or(0, Vec::len));
    let n = nrc_core::num::Num::new;
    let patch = nrc_core::model::Patch {
        leading: vec![format!(
            "// nrc-mcp patch {kind}{}",
            label
                .as_deref()
                .map(|l| format!(" ({l})"))
                .unwrap_or_default()
        )],
        kind: "patchDef2".into(),
        shader: shader.to_string(),
        header: vec![n(w as f64), n(h as f64), n(0.0), n(0.0), n(0.0)],
        rows: rows
            .iter()
            .map(|col| {
                col.iter()
                    .map(|p| p.iter().map(|&c| n(c)).collect())
                    .collect()
            })
            .collect(),
    };
    let d = PyDict::new(py);
    let mut m = map.borrow_mut();
    let index = place_prims(
        &mut m.inner,
        vec![Primitive::Patch(patch)],
        target_classname,
        new_entity,
        target_entity,
        &d,
    )?;
    d.set_item("entity", index)?;
    d.set_item("width", w)?;
    d.set_item("height", h)?;
    d.set_item("committed", true)?;
    Ok(d)
}

/// Compile an IR tree and describe the result, without touching any map.
#[pyfunction]
#[pyo3(signature = (ir, textures=None, grid=1))]
fn solid_compile<'py>(
    py: Python<'py>,
    ir: &Bound<'py, PyDict>,
    textures: Option<&Bound<'py, PyDict>>,
    grid: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let node = node_from_dict(ir, 0)?;
    let evaluated = nrc_solid::ir::evaluate(&node).map_err(|e| err(e.to_string()))?;
    let spec = texture_spec_from(textures)?;
    let (brushes, report) = nrc_solid::emit::emit(&evaluated.solid, &spec, grid);

    let d = PyDict::new(py);
    d.set_item("op", node.kind())?;
    d.set_item("nodes", node.node_count())?;
    d.set_item("depth", node.depth())?;
    d.set_item("parts", evaluated.solid.len())?;
    d.set_item("brushes", report.brushes)?;
    d.set_item("faces", report.faces)?;
    d.set_item("off_grid_vertices", report.off_grid_vertices)?;
    d.set_item("non_integer_plane_faces", report.non_integer_faces)?;
    d.set_item("volume", evaluated.solid.volume())?;
    let mut warnings = evaluated.warnings.clone();
    warnings.extend(report.warnings.clone());
    d.set_item("warnings", warnings)?;

    let b = evaluated.solid.bounds();
    if b.is_empty() {
        d.set_item("bounds", py.None())?;
    } else {
        let bb = PyDict::new(py);
        bb.set_item("min", b.min.to_array())?;
        bb.set_item("max", b.max.to_array())?;
        bb.set_item("size", b.size().to_array())?;
        d.set_item("bounds", bb)?;
    }
    // Minimum thickness across parts, which is what the thin-brush post-condition checks.
    let thin = evaluated
        .solid
        .parts
        .iter()
        .filter_map(|p| p.min_thickness())
        .fold(f64::INFINITY, f64::min);
    d.set_item(
        "min_thickness",
        if thin.is_finite() { Some(thin) } else { None },
    )?;
    d.set_item("_brush_count_check", brushes.len())?;
    Ok(d)
}

/// Compile an IR tree and insert the resulting brushes into a map.
///
/// A free function rather than a method so that `PyMap` keeps a single `#[pymethods]` block;
/// it takes the map by reference and mutates it in place either way.
///
/// `dry_run` compiles and reports without touching the map, which is what `solid_preview`
/// uses. Nothing is written to disk here regardless — that still needs an explicit `save`.
#[pyfunction]
#[pyo3(signature = (map, ir, textures=None, grid=1, target_classname="worldspawn", dry_run=false, label=None, new_entity=false, target_entity=None))]
#[allow(clippy::too_many_arguments)]
fn solid_commit<'py>(
    py: Python<'py>,
    map: &Bound<'py, PyMap>,
    ir: &Bound<'py, PyDict>,
    textures: Option<&Bound<'py, PyDict>>,
    grid: i64,
    target_classname: &str,
    dry_run: bool,
    label: Option<String>,
    new_entity: bool,
    target_entity: Option<usize>,
) -> PyResult<Bound<'py, PyDict>> {
    if new_entity && target_entity.is_some() {
        return Err(err("pass new_entity or target_entity, not both"));
    }
    let node = node_from_dict(ir, 0)?;
    let evaluated = nrc_solid::ir::evaluate(&node).map_err(|e| err(e.to_string()))?;
    let spec = texture_spec_from(textures)?;
    let (brushes, report) = nrc_solid::emit::emit(&evaluated.solid, &spec, grid);

    if brushes.is_empty() {
        return Err(err(
            "compilation produced no brushes; check the warnings from solid_compile",
        ));
    }

    let d = PyDict::new(py);
    d.set_item("brushes_created", brushes.len())?;
    d.set_item("faces", report.faces)?;
    d.set_item("off_grid_vertices", report.off_grid_vertices)?;
    d.set_item("non_integer_plane_faces", report.non_integer_faces)?;
    let mut warnings = evaluated.warnings.clone();
    warnings.extend(report.warnings.clone());
    d.set_item("warnings", warnings)?;
    d.set_item("dry_run", dry_run)?;
    let undo_group = label
        .clone()
        .map(|l| format!("solid_commit:{l}"))
        .unwrap_or_else(|| format!("solid_commit:{}", node.kind()));
    d.set_item("undo_group", undo_group)?;

    if dry_run {
        d.set_item("committed", false)?;
        return Ok(d);
    }

    let mut m = map.borrow_mut();
    // Marker comments name the group, so a human reading the .map afterwards can see where the
    // brushes came from and delete them as a unit.
    let count = brushes.len();
    let mut prims: Vec<nrc_core::model::Primitive> = brushes
        .into_iter()
        .map(nrc_core::model::Primitive::Brush)
        .collect();
    if let Some(label) = &label {
        if label.contains(['\n', '\r']) {
            return Err(err("solid label must fit on one line"));
        }
        for primitive in &mut prims {
            if let Primitive::Brush(b) = primitive {
                b.leading.push(format!("// nrc-mcp solid:{label}"));
            }
        }
    }
    if let Some(nrc_core::model::Primitive::Brush(b)) = prims.first_mut() {
        b.leading
            .push(format!("// nrc-mcp {} ({count} brushes)", node.kind()));
    }

    let index = place_prims(
        &mut m.inner,
        prims,
        target_classname,
        new_entity,
        target_entity,
        &d,
    )?;
    d.set_item("entity", index)?;
    d.set_item("committed", true)?;
    d.set_item("map_brushes_now", m.inner.brush_count())?;
    Ok(d)
}
