import pytest

from nx_mcp.bridge import ObjectRegistry
from nx_mcp.contracts import NXToolError


def test_object_reference_resolves_only_in_its_part_session():
    registry = ObjectRegistry()
    nx_object = object()
    reference = registry.register(nx_object, kind="sketch", name="PROFILE", part_id="part-1")

    assert registry.resolve(reference.id, expected_kind="sketch") is nx_object

    registry.invalidate_part("part-1")

    with pytest.raises(NXToolError) as error:
        registry.resolve(reference.id, expected_kind="sketch")
    assert error.value.code == "NX_OBJECT_STALE"


def test_object_reference_rejects_wrong_kind():
    registry = ObjectRegistry()
    reference = registry.register(object(), kind="body", name="BODY(1)", part_id="part-1")

    with pytest.raises(NXToolError) as error:
        registry.resolve(reference.id, expected_kind="sketch")
    assert error.value.code == "NX_OBJECT_TYPE_MISMATCH"


def test_registering_same_live_object_returns_same_session_id():
    registry = ObjectRegistry()
    nx_object = object()

    first = registry.register(nx_object, kind="body", name="BODY(1)", part_id="part-1")
    second = registry.register(nx_object, kind="body", name="BODY(1)", part_id="part-1")

    assert second.id == first.id


def test_object_reference_cannot_be_used_with_another_work_part():
    registry = ObjectRegistry()
    reference = registry.register(object(), kind="sketch", name="PROFILE", part_id="part-1")

    with pytest.raises(NXToolError) as error:
        registry.resolve(reference.id, expected_kind="sketch", part_id="part-2")

    assert error.value.code == "NX_OBJECT_STALE"
