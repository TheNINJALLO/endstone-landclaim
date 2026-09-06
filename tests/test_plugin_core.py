import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from endstone_landclaim import plugin as plugin_module  # noqa: E402
from endstone_landclaim.plugin import AdminLandClaims, Claim, ClaimFlags  # noqa: E402


def make_claim(**overrides):
    values = {
        "name": "Test Claim",
        "dimension": "overworld",
        "x1": 0,
        "y1": -64,
        "z1": 0,
        "x2": 10,
        "y2": 320,
        "z2": 10,
        "allowed_uuids": [],
        "flags": ClaimFlags(),
    }
    values.update(overrides)
    return Claim(**values)


def test_claim_boundaries_floor_negative_coordinates():
    claim = make_claim(x1=-2, z1=-2, x2=2, z2=2)

    assert claim.contains(-1.2, -1.2, "overworld")
    assert not claim.contains(-2.1, -2.1, "overworld")
    assert not claim.contains_3d(-2.1, 0, 0, "overworld")


def test_claim_distance_uses_nearest_edges_not_centers():
    large = make_claim(x1=0, z1=0, x2=1000, z2=1000)
    nearby = make_claim(name="Nearby", x1=1010, z1=400, x2=1020, z2=410)

    assert large.distance_to_claim(nearby) == 10


def test_child_claim_must_fit_inside_parent_on_all_axes():
    parent = make_claim(x2=100, z2=100)
    inside = make_claim(name="Inside", x1=10, y1=0, z1=10, x2=20, y2=100, z2=20)
    outside = make_claim(name="Outside", x1=90, z1=90, x2=110, z2=110)

    assert AdminLandClaims._claim_is_inside_parent(inside, parent)
    assert not AdminLandClaims._claim_is_inside_parent(outside, parent)


def test_smallest_nested_claim_wins_without_candidate_allocation():
    parent = make_claim(name="Parent", x2=100, z2=100)
    child = make_claim(name="Child", x1=5, z1=5, x2=8, z2=8, is_child=True)
    claims = {"parent": parent, "child": child}

    found = AdminLandClaims._get_smallest_containing_claim_3d_from(
        claims.values(), 6, 70, 6, "overworld"
    )

    assert found is child


def test_mob_classification_is_exact():
    assert AdminLandClaims._is_hostile_mob(None, "minecraft:piglin_brute")
    assert not AdminLandClaims._is_passive_mob(None, "minecraft:piglin_brute")
    assert AdminLandClaims._is_passive_mob(None, "minecraft:pig")
    assert not AdminLandClaims._is_hostile_mob(None, "minecraft:pig")
    assert not AdminLandClaims._is_hostile_mob(None, "minecraft:custom_zombie_statue")


def test_named_mobs_are_exempt(monkeypatch):
    class FakeMob:
        def __init__(self, mob_type, name_tag=""):
            self.type = mob_type
            self.name_tag = name_tag

    class FakePlayer(FakeMob):
        pass

    monkeypatch.setattr(plugin_module, "Mob", FakeMob)
    monkeypatch.setattr(plugin_module, "Player", FakePlayer)
    claim = make_claim(
        flags=ClaimFlags(kill_hostile_mobs=True, ignore_named_mobs=True)
    )
    harness = SimpleNamespace(
        _actor_has_custom_name=AdminLandClaims._actor_has_custom_name,
        _get_mob_type=AdminLandClaims._get_mob_type,
        _is_hostile_mob=lambda mob_type: AdminLandClaims._is_hostile_mob(None, mob_type),
        _is_passive_mob=lambda mob_type: AdminLandClaims._is_passive_mob(None, mob_type),
    )

    assert AdminLandClaims._should_kill_mob(harness, FakeMob("minecraft:zombie"), claim)
    assert not AdminLandClaims._should_kill_mob(
        harness, FakeMob("minecraft:zombie", "Dinnerbone"), claim
    )
    assert not AdminLandClaims._should_kill_mob(harness, FakePlayer("minecraft:player"), claim)


def test_large_claim_sweep_honors_per_tick_removal_budget(monkeypatch):
    class FakeMob:
        def __init__(self, mob_type="minecraft:zombie", name_tag=""):
            self.type = mob_type
            self.name_tag = name_tag
            self.location = SimpleNamespace(x=100, y=70, z=100)
            self.dimension = SimpleNamespace(name="overworld")
            self.is_valid = True
            self.is_dead = False
            self.removed = False

        def remove(self):
            self.removed = True
            self.is_valid = False

    class FakePlayer(FakeMob):
        pass

    monkeypatch.setattr(plugin_module, "Mob", FakeMob)
    monkeypatch.setattr(plugin_module, "Player", FakePlayer)
    mobs = [FakeMob(), FakeMob(), FakeMob(name_tag="Named"), FakePlayer()]
    claim = make_claim(
        x2=1_000_000,
        z2=1_000_000,
        flags=ClaimFlags(kill_hostile_mobs=True, ignore_named_mobs=True),
    )

    class Harness:
        _periodic_mob_management = AdminLandClaims._periodic_mob_management
        _has_mob_control_claims = AdminLandClaims._has_mob_control_claims
        _begin_mob_sweep = AdminLandClaims._begin_mob_sweep
        _finish_mob_sweep = AdminLandClaims._finish_mob_sweep
        _reset_mob_sweep = AdminLandClaims._reset_mob_sweep
        _remove_actor_if_managed = AdminLandClaims._remove_actor_if_managed
        _claim_containing_actor = AdminLandClaims._claim_containing_actor
        _get_smallest_containing_claim_3d = AdminLandClaims._get_smallest_containing_claim_3d
        _get_smallest_containing_claim_3d_from = staticmethod(
            AdminLandClaims._get_smallest_containing_claim_3d_from
        )
        _should_kill_mob = AdminLandClaims._should_kill_mob
        _actor_has_custom_name = staticmethod(AdminLandClaims._actor_has_custom_name)
        _get_mob_type = staticmethod(AdminLandClaims._get_mob_type)
        _is_hostile_mob = AdminLandClaims._is_hostile_mob
        _is_passive_mob = AdminLandClaims._is_passive_mob

    harness = Harness()
    harness._claims = {"large": claim}
    harness.server = SimpleNamespace(level=SimpleNamespace(actors=mobs))
    harness.logger = SimpleNamespace(
        debug=lambda *_args: None,
        error=lambda *_args: None,
        warning=lambda *_args: None,
    )
    harness._mob_cleanup_enabled = True
    harness._mob_cleanup_interval_ticks = 60
    harness._mob_cleanup_actors_per_tick = 100
    harness._mob_cleanup_max_removals_per_tick = 1
    harness._mob_cleanup_max_actor_snapshot = 100
    harness._mob_sweep_actors = None
    harness._mob_sweep_index = 0
    harness._mob_sweep_wait_ticks = 0
    harness._mob_sweep_removed = 0
    harness._mob_snapshot_offset = 0
    harness._mob_snapshot_limit_warned = False
    harness._mob_sweep_claims_by_dimension = {}

    harness._periodic_mob_management()
    assert sum(mob.removed for mob in mobs) == 1

    harness._periodic_mob_management()
    assert sum(mob.removed for mob in mobs) == 2
    assert not mobs[2].removed
    assert not mobs[3].removed
