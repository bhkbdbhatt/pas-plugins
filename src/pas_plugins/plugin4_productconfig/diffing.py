"""Version diffing between two product definitions.

A version bump that nobody can describe is a version bump nobody can review. The
diff is computed structurally by walking both documents, so it cannot miss a
change and cannot invent one.

The distinction that carries weight is `is_material`. A change to a description is
a new version but not a new rate filing; a change to a rate, a cover or a state
list is. Filing obligations attach to the second kind, so the flag is what a
compliance reviewer reads first.

Paths are held as a list of segments rather than a dict. A dict would silently
collapse a path that revisits the same key at different depths - `covers.cv-term-20`
and `covers.cv-term-20.ratingTables` both contain the key `covers`, and a dict
would lose one of them.
"""

from __future__ import annotations

from typing import Any

from pas_plugins.plugin4_productconfig.models import ProductDefinition, VersionDiff, VersionDiffEntry

# Keys that change on every save and carry no meaning in a version diff.
_IGNORED_KEYS = frozenset({"updatedAt", "createdAt"})

# Fields used to match list elements across versions. Reordering a list should not
# read as replacing every element in it.
_ID_FIELDS = ("id", "Id", "ID", "ruleId", "coverId", "tableId", "benefitId", "chargeId", "code")


def diff_definitions(before: ProductDefinition, after: ProductDefinition) -> VersionDiff:
    """Compute the structural difference between two definitions of a product."""
    entries: list[VersionDiffEntry] = []
    _walk([], before.to_dict(), after.to_dict(), entries)
    return VersionDiff(
        product_id=after.product_id,
        from_version=before.version,
        to_version=after.version,
        entries=entries,
    )


def _walk(path: list[str], before: Any, after: Any, entries: list[VersionDiffEntry]) -> None:
    """Recursively compare two JSON-shaped documents."""
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if key in _IGNORED_KEYS:
                continue
            # The version number is bookkeeping, not a change in the product.
            # Including it makes every diff trivially non-empty and buries the
            # entries a reviewer actually needs to read.
            if key == "version":
                continue
            if key not in after:
                entries.append(_entry([*path, key], "removed", before[key], None))
            elif key not in before:
                entries.append(_entry([*path, key], "added", None, after[key]))
            else:
                _walk([*path, key], before[key], after[key], entries)
        return

    if isinstance(before, list) and isinstance(after, list):
        _walk_list(path, before, after, entries)
        return

    if before != after:
        entries.append(_entry(path, "changed", before, after))


def _walk_list(
    path: list[str], before: list[Any], after: list[Any], entries: list[VersionDiffEntry]
) -> None:
    """Compare lists by element identity where possible, by position otherwise."""
    before_index, before_unkeyed = _index_by_id(before)
    after_index, after_unkeyed = _index_by_id(after)

    for key in sorted(set(before_index) | set(after_index)):
        element_path = [*path, key]
        if key not in after_index:
            entries.append(_entry(element_path, "removed", before_index[key], None))
        elif key not in before_index:
            entries.append(_entry(element_path, "added", None, after_index[key]))
        else:
            _walk(element_path, before_index[key], after_index[key], entries)

    for index, value in enumerate(before_unkeyed):
        if index < len(after_unkeyed):
            _walk([*path, f"[{index}]"], value, after_unkeyed[index], entries)
        else:
            entries.append(_entry([*path, f"[{index}]"], "removed", value, None))
    for index in range(len(before_unkeyed), len(after_unkeyed)):
        entries.append(_entry([*path, f"[{index}]"], "added", None, after_unkeyed[index]))


def _index_by_id(values: list[Any]) -> tuple[dict[str, Any], list[Any]]:
    """Split a list into id-keyed elements and unkeyed ones."""
    indexed: dict[str, Any] = {}
    unkeyed: list[Any] = []
    for value in values:
        key = next(
            (
                str(value[f])
                for f in _ID_FIELDS
                if isinstance(value, dict) and f in value and isinstance(value[f], (str, int))
            ),
            None,
        )
        if key is not None:
            indexed[key] = value
        else:
            unkeyed.append(value)
    return indexed, unkeyed


def _entry(path: list[str], change: str, before: Any, after: Any) -> VersionDiffEntry:
    return VersionDiffEntry(path=".".join(path), change=change, before=before, after=after)


__all__ = ["diff_definitions"]