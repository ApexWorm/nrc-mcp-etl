"""The gates themselves: what the protected-path pin covers, how self-dev measures change, the
differential harness's bookkeeping, the seam lint's reach, and the shared severity clamp.

Every test here is a way the audit got a gate to pass when it should not have. They are negative
tests on purpose: a gate that only ever says yes is indistinguishable from no gate.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def load_tool(name: str):
    path = REPO / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_gates_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def protected():
    return load_tool("protected")


@pytest.fixture(scope="module")
def selfdev():
    return load_tool("selfdev")


yaml = pytest.importorskip("yaml")


def _profile_names() -> list[str]:
    return sorted(p.name for p in (REPO / "profiles").glob("*.yaml"))


def _load_real(name: str) -> dict:
    return yaml.safe_load((REPO / "profiles" / name).read_text(encoding="utf-8"))


def _digest_of(protected, tmp: Path, data: dict, name: str = "p.yaml") -> str:
    d = tmp / "profiles"
    d.mkdir(exist_ok=True)
    for f in d.glob("*.yaml"):
        f.unlink()
    (d / name).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return protected._verified_rule_digest(tmp)[name]


def _first_verified_leaf(node, verified: bool, path: tuple = ()):
    """The path to the first scalar (not a `confidence` key) whose effective confidence is
    verified, or None."""
    if isinstance(node, dict):
        if "confidence" in node:
            verified = node["confidence"] != "unverified"
        for k, v in node.items():
            if k == "confidence" or str(k).endswith("_confidence"):
                continue
            hit = _first_verified_leaf(v, verified, (*path, k))
            if hit is not None:
                return hit
        return None
    if isinstance(node, list):
        for i, v in enumerate(node):
            hit = _first_verified_leaf(v, verified, (*path, i))
            if hit is not None:
                return hit
        return None
    return path if verified else None


def _set(data, path: tuple, value) -> None:
    for p in path[:-1]:
        data = data[p]
    data[path[-1]] = value


# ---------------------------------------------------------------------------
# G1 — every verified entry is pinned, wherever it sits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _profile_names())
def test_every_verified_section_is_pinned(protected, tmp_path: Path, name: str):
    """Mutate one verified leaf of each top-level section in turn: the digest must move.

    This is the audit's G1 reproduction run over the real profiles, so a section that gains
    `confidence: verified` later is covered without editing this test.
    """
    data = _load_real(name)
    base = _digest_of(protected, tmp_path, data)
    checked = []
    for section in data:
        leaf = _first_verified_leaf(data[section], False, (section,))
        if leaf is None:
            continue
        changed = copy.deepcopy(data)
        _set(changed, leaf, "mutated-by-test")
        assert _digest_of(protected, tmp_path, changed) != base, f"{name}: {leaf} is not pinned"
        checked.append(section)
    # The sections the audit named as unpinned must be among those checked, where they exist.
    for named in ("mapscript", "engine", "packaging", "units", "bots", "objectives"):
        if named in data:
            assert named in checked, f"{name}: no verified leaf found in {named}"


def test_an_emptied_verified_list_is_a_change(protected, tmp_path: Path):
    """`events: []` used to switch off unknown-event errors without touching the pin."""
    data = {"mapscript": {"confidence": "verified", "events": ["spawn", "death"]}}
    before = _digest_of(protected, tmp_path, data)
    data["mapscript"]["events"] = []
    assert _digest_of(protected, tmp_path, data) != before


def test_verified_subsection_of_an_unmarked_section_is_pinned(protected, tmp_path: Path):
    data = {"bots": {"tuning": {"confidence": "verified", "speed": 1.0}, "notes": "free"}}
    before = _digest_of(protected, tmp_path, data)
    data["bots"]["notes"] = "edited freely"
    assert _digest_of(protected, tmp_path, data) == before, "unverified text must stay free"
    data["bots"]["tuning"]["speed"] = 2.0
    assert _digest_of(protected, tmp_path, data) != before


def test_unverified_entries_inside_a_verified_section_stay_free(protected, tmp_path: Path):
    data = {
        "objectives": {
            "confidence": "verified",
            "kinds": [{"id": "a", "x": 1}, {"id": "b", "confidence": "unverified", "x": 1}],
        },
        "rules": [{"id": "R1", "confidence": "verified", "check": "count_exact"}],
    }
    before = _digest_of(protected, tmp_path, data)
    data["objectives"]["kinds"][1]["x"] = 2
    data["objectives"]["kinds"].append({"id": "c", "confidence": "unverified"})
    data["rules"].append({"id": "R2", "confidence": "unverified", "check": "count_min"})
    assert _digest_of(protected, tmp_path, data) == before
    # Demoting a verified entry to get at it is itself a change.
    data["objectives"]["kinds"][0]["confidence"] = "unverified"
    assert _digest_of(protected, tmp_path, data) != before


def test_movement_and_engine_limits_stay_pinned_whole(protected, tmp_path: Path):
    data = {"movement": {"note": "no confidence here"}, "engine_limits": {"max": {"value": 1}}}
    before = _digest_of(protected, tmp_path, data)
    data["movement"]["note"] = "changed"
    assert _digest_of(protected, tmp_path, data) != before


def test_an_unparseable_profile_is_not_dropped_from_the_pin(protected, tmp_path: Path):
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "p.yaml").write_text("rules: [unclosed\n", encoding="utf-8")
    assert "p.yaml" in protected._verified_rule_digest(tmp_path)


# ---------------------------------------------------------------------------
# G2 — one spelling of verified
# ---------------------------------------------------------------------------


def test_a_capitalised_verified_is_pinned_and_refused(protected, tmp_path: Path, monkeypatch):
    from nrc_mcp import profiles, rules

    data = {"rules": [{"id": "X", "confidence": "Verified", "severity": "error", "check": "a"}]}
    before = _digest_of(protected, tmp_path, data)
    data["rules"][0]["check"] = "b"
    assert _digest_of(protected, tmp_path, data) != before, "`Verified` must not escape the pin"

    monkeypatch.setattr(profiles, "profiles_dir", lambda: tmp_path / "profiles")
    profiles.load.cache_clear()
    try:
        with pytest.raises(profiles.ProfileError, match="Verified"):
            profiles.load("p")
    finally:
        profiles.load.cache_clear()
    # And code reading such a rule anyway never lets it fail a build.
    assert rules.clamp_severity("error", "Verified") == "info"
    assert rules._effective_severity({"severity": "error", "confidence": "VERIFIED"}) == "info"


@pytest.mark.parametrize("name", _profile_names())
def test_shipped_profiles_use_only_the_two_confidences(name: str):
    from nrc_mcp import profiles

    assert profiles.bad_confidences(_load_real(name)) == []


# ---------------------------------------------------------------------------
# G3 / G5 / G6 — self-dev measures change from its base commit
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)}
    p = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return p.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    (root / "docs").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "docs" / "conventions.md").write_text("a\n")
    (root / "tools" / "bench.py").write_text("ruler\n")
    (root / ".gitignore").write_text("mise.local.toml\n/bench/results/\n/bench/selfdev/\n")
    (root / "mise.local.toml").write_text("[env]\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def test_a_committed_change_is_still_a_change(selfdev, repo: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tools" / "bench.py").write_text("tampered ruler\n")
    _git(repo, "commit", "-qam", "hide it in a commit")
    assert _git(repo, "status", "--porcelain") == "", "the old check saw nothing here"
    assert "tools/bench.py" in [p for _, p in selfdev.changed_paths(repo, base)]


def test_a_rename_is_checked_under_both_names(selfdev, repo: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "mv", "tools/bench.py", "docs/bench.py")
    paths = [p for _, p in selfdev.changed_paths(repo, base)]
    assert "tools/bench.py" in paths and "docs/bench.py" in paths
    assert not all(selfdev.path_allowed(p)[0] for p in paths)


def test_untracked_files_count_and_attempt_records_do_not(selfdev, repo: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tools" / "new.py").write_text("x\n")
    (repo / "bench" / "selfdev").mkdir(parents=True)
    (repo / "bench" / "selfdev" / "a.json").write_text("{}\n")
    paths = [p for _, p in selfdev.changed_paths(repo, base)]
    assert "tools/new.py" in paths
    assert not any(p.startswith("bench/selfdev/") for p in paths)


def test_an_unknown_base_fails_closed(selfdev, repo: Path):
    with pytest.raises(selfdev.SelfDevError):
        selfdev.changed_paths(repo, "")
    with pytest.raises(selfdev.SelfDevError):
        selfdev.changed_paths(repo, "0" * 40)


def test_ignored_inputs_are_fingerprinted(selfdev, repo: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    before = selfdev.ignored_inputs(repo, base)
    assert "mise.local.toml" in before
    (repo / "mise.local.toml").write_text("[env]\nNRC_X = '1'\n")
    (repo / "bench" / "results").mkdir(parents=True)
    (repo / "bench" / "results" / f"{base[:7]}.json").write_text("{}\n")
    changes = selfdev.ignored_input_changes(before, selfdev.ignored_inputs(repo, base))
    assert "mise.local.toml: changed" in changes
    assert f"bench/results/{base[:7]}.json: appeared" in changes


def test_run_rejects_a_committed_disallowed_change(selfdev, repo: Path, monkeypatch):
    monkeypatch.setenv("NRC_SELFDEV", "1")
    rec = {
        "id": "t1",
        "state": "proposed",
        "base_sha": _git(repo, "rev-parse", "HEAD").strip(),
        "ignored_inputs": {},
        "started_epoch": 0,
    }
    rec["ignored_inputs"] = selfdev.ignored_inputs(repo, rec["base_sha"])
    d = selfdev.attempts_dir(repo)
    (d / "t1.json").write_text(json.dumps(rec))
    (repo / "tools" / "bench.py").write_text("tampered\n")
    _git(repo, "commit", "-qam", "tamper")
    out = selfdev.cmd_run(repo, "t1", force=True)
    assert out["state"] == "rejected"
    assert out["reason"] == "modified a disallowed path"


def test_run_rejects_a_changed_ignored_input(selfdev, repo: Path, monkeypatch):
    monkeypatch.setenv("NRC_SELFDEV", "1")
    base = _git(repo, "rev-parse", "HEAD").strip()
    rec = {"id": "t2", "base_sha": base, "ignored_inputs": selfdev.ignored_inputs(repo, base)}
    (selfdev.attempts_dir(repo) / "t2.json").write_text(json.dumps(rec))
    (repo / "mise.local.toml").write_text("[env]\nQ3MAP2 = '/elsewhere'\n")
    out = selfdev.cmd_run(repo, "t2", force=True)
    assert out["state"] == "rejected"
    assert any("mise.local.toml" in v for v in out["violations"])


def test_revert_restores_from_the_base_commit(selfdev, repo: Path, monkeypatch):
    monkeypatch.setenv("NRC_SELFDEV", "1")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (selfdev.attempts_dir(repo) / "t3.json").write_text(json.dumps({"id": "t3", "base_sha": base}))
    (repo / "docs" / "conventions.md").write_text("edited\n")
    (repo / "profiles").mkdir()
    (repo / "profiles" / "added.yaml").write_text("new: 1\n")
    (repo / "docs" / "human.md").write_text("not the attempt's to revert\n")
    _git(repo, "add", "profiles/added.yaml", "docs/human.md")
    _git(repo, "commit", "-qm", "attempt")
    out = selfdev.cmd_revert(repo, "t3")
    assert (repo / "docs" / "conventions.md").read_text() == "a\n"
    assert not (repo / "profiles" / "added.yaml").exists()
    assert (repo / "docs" / "human.md").exists(), "outside the allowed paths: left alone"
    assert set(out["reverted_paths"]) == {"docs/conventions.md", "profiles/added.yaml"}
    assert (repo / "tools" / "bench.py").read_text() == "ruler\n"


def test_bench_result_is_the_one_this_run_wrote(selfdev, tmp_path: Path, monkeypatch):
    """A planted result with a later mtime in bench/results must not be read."""
    root = tmp_path / "r"
    (root / "tools").mkdir(parents=True)
    (root / "bench" / "results").mkdir(parents=True)
    planted = root / "bench" / "results" / "zzzzzzz.json"
    planted.write_text(json.dumps({"planted": True}))
    os.utime(planted, (4_000_000_000, 4_000_000_000))
    (root / "tools" / "bench.py").write_text(
        "import json, sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "(out / 'abc.json').write_text(json.dumps({'planted': False}))\n"
    )
    assert selfdev.run_bench(root)["planted"] is False


def test_the_merge_rule_ignores_timing_noise_and_counts_a_lost_signal(selfdev):
    def run(**scores) -> dict:
        return {"gate_passed": True, "signals": [{"id": k, "score": v} for k, v in scores.items()]}

    # F6 a few percent faster is noise, not an improvement.
    v = selfdev.compare(run(F5=0.8, F6=10.0), run(F5=0.8, F6=9.7))
    assert v["accept"] is False and not v["improved"]
    # And a few percent slower does not block a real improvement.
    v = selfdev.compare(run(F5=0.8, F6=10.0), run(F5=0.9, F6=10.4))
    assert v["accept"] is True
    # A signal that became skipped is a regression.
    before = run(F4=0.5, F5=0.8)
    after = {
        "gate_passed": True,
        "signals": [{"id": "F4", "skipped": "no compiler"}, {"id": "F5", "score": 0.9}],
    }
    v = selfdev.compare(before, after)
    assert v["accept"] is False
    assert any(r.startswith("F4") for r in v["regressed"])


def test_selfdev_records_are_ignored_by_git():
    p = subprocess.run(
        ["git", "check-ignore", "-q", "bench/selfdev/20260101-000000.json"], cwd=REPO, check=False
    )
    assert p.returncode == 0, "bench/selfdev/ must be git-ignored, or every attempt is rejected"


def test_selfdev_is_itself_protected(protected):
    assert "tools/selfdev.py" in protected.PROTECTED_FILES


# ---------------------------------------------------------------------------
# G9 / G10 — encoding, and the tree hash
# ---------------------------------------------------------------------------


def test_the_tree_hash_keeps_paths_apart_from_content(protected, tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "a").write_bytes(b"bX")
    (b / "ab").write_bytes(b"X")
    assert protected._tree_digest(a) != protected._tree_digest(b)


def test_pins_never_read_in_the_locale_encoding(protected, tmp_path: Path, monkeypatch):
    """A profile with non-ASCII text digests the same whatever the locale's encoding."""
    import io

    real = io.text_encoding
    data = {"movement": {"confidence": "verified", "note": "é — ü"}}
    want = _digest_of(protected, tmp_path, data)
    # What a Windows machine's locale does to a read that names no encoding.
    monkeypatch.setattr(io, "text_encoding", lambda e, *a: "cp1252" if e is None else real(e))
    assert protected._verified_rule_digest(tmp_path)["p.yaml"] == want


# ---------------------------------------------------------------------------
# G4 — difftest keeps every map
# ---------------------------------------------------------------------------


def _fake_nrc(tmp: Path, drop: str | None = None) -> Path:
    script = tmp / "nrc"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"drop = {drop!r}\n"
        "files = [a for a in sys.argv[2:] if not a.startswith('--')]\n"
        "print(json.dumps({'results': [\n"
        "    {'file': f, 'ok': 'bad' not in f, 'first_difference': {'line': 1}}\n"
        "    for f in files if f != drop]}))\n"
    )
    script.chmod(0o755)
    return script


def test_difftest_keeps_two_maps_with_the_same_name(tmp_path: Path):
    difftest = load_tool("difftest")
    (tmp_path / "good").mkdir()
    (tmp_path / "bad").mkdir()
    maps = [tmp_path / "bad" / "x.map", tmp_path / "good" / "x.map"]
    for m in maps:
        m.write_text("{\n}\n")
    out = difftest.run_syntactic(_fake_nrc(tmp_path), maps)
    assert len(out) == 2
    assert out[str(maps[0])].syntactic_ok is False, "the failing twin must not be overwritten"


def test_difftest_reports_a_map_the_kernel_skipped(tmp_path: Path):
    difftest = load_tool("difftest")
    maps = [tmp_path / "a.map", tmp_path / "b.map"]
    for m in maps:
        m.write_text("{\n}\n")
    out = difftest.run_syntactic(_fake_nrc(tmp_path, drop=str(maps[1])), maps)
    assert out[str(maps[1])].syntactic_ok is False
    assert "no result" in out[str(maps[1])].detail


# ---------------------------------------------------------------------------
# G7 — the seam lint's escapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("tools/leak.py", 'x = "ut4" + "_casa"\n'),  # concatenation
        ("tools/leak.py", 'x = ("ut4" "_casa")\n'),  # implicit concatenation
        ("tools/leak.py", 'x = "SPECIAL_ENTITY"\n'),  # case
        ("tools/leak.py", 'f(\n    *["special_entity"]\n)\n'),  # `*` line in Python
        ("tools/data.json", '{"cls": "special_entity"}\n'),  # data beside code
        ("tools/docs/leak.py", 'x = "special_entity"\n'),  # a nested folder named docs
        ("python/src/pkg/target/leak.py", 'x = "special_entity"\n'),
    ],
)
def test_seam_lint_escapes_are_closed(tmp_path: Path, rel: str, text: str):
    seam = load_tool("seam_lint")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "g.yaml").write_text("entities:\n  - classname: special_entity\n")
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    vocab = seam.load_profile_vocabulary(tmp_path)
    assert seam.scan(tmp_path, vocab, verbose=False), f"{rel}: {text!r} slipped through"


def test_seam_lint_still_skips_rust_block_comments(tmp_path: Path):
    seam = load_tool("seam_lint")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "g.yaml").write_text("entities:\n  - classname: special_entity\n")
    (tmp_path / "crates").mkdir()
    (tmp_path / "crates" / "a.rs").write_text("/*\n * from special_entity maps\n */\nfn f() {}\n")
    assert seam.scan(tmp_path, seam.load_profile_vocabulary(tmp_path), verbose=False) == []


def test_mapscript_names_come_from_the_profile():
    from nrc_mcp import mapscript, profiles

    with pytest.raises(profiles.ProfileError, match="block_key"):
        mapscript.block_key({})
    with pytest.raises(profiles.ProfileError, match="file"):
        mapscript.script_path({}, "/x/m.map")
    assert mapscript.script_path({"file": "{map}.s"}, "/x/m.map").name == "m.s"


# ---------------------------------------------------------------------------
# G8 — one clamp for every checker
# ---------------------------------------------------------------------------


def test_an_unknown_severity_is_counted(monkeypatch):
    from nrc_mcp import mapscript, rules

    assert mapscript._severity("fatal", "verified") == "warning"
    assert rules.clamp_severity("ERROR", "verified") == "error"
    assert rules.clamp_severity("error", "unverified") == "info"


def test_ship_shadow_finding_carries_the_profiles_confidence(tmp_path: Path, monkeypatch):
    from nrc_mcp import pack, profiles

    pk3 = tmp_path / "m.pk3"
    with zipfile.ZipFile(pk3, "w") as z:
        for n in ("maps/m.bsp", "textures/reservedname/wall.tga", "scripts/m.shader"):
            z.writestr(n, b"x")
    monkeypatch.setattr(
        profiles,
        "load",
        lambda _pid: {
            "packaging": {"confidence": "unverified", "reserved_shader_prefixes": ["reservedname"]}
        },
    )
    r = pack.ship_check(tmp_path / "m.bsp", "test", pk3=pk3)
    f = next(x for x in r["findings"] if x["code"] == "SHIP_SHADOWS_BASEGAME")
    assert f["confidence"] == "unverified"
    assert f["severity"] == "info"


# ---------------------------------------------------------------------------
# G11 — tape placeholders
# ---------------------------------------------------------------------------


def test_tape_placeholders_leave_other_braces_alone():
    tapes = load_tool("tapes")
    subs = {"work": "/w/m.map", "workspace": "/w", "repo": "/r"}
    assert tapes._expand("set { a b } {work}", subs) == "set { a b } /w/m.map"
    assert tapes._expand("{{literal}}", subs) == "{{literal}}"
    assert tapes._expand(["{repo}/x", {"k": "{}"}], subs) == ["/r/x", {"k": "{}"}]
    with pytest.raises(tapes.TapeError, match="clip_player"):
        tapes._expand("{clip_player}", subs)
