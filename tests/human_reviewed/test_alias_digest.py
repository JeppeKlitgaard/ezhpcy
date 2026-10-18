"""
Every `ResolvedConfig` field is in either `_DIGEST_INCLUDE_FIELDS` or
`_DIGEST_EXCLUDE_FIELDS`.

The digest names anonymous tunnels. Whether a field changes a tunnel's identity,
and whether a secret could leak into the hash, is decided per field.

This test fails on any new field until someone makes that decision, so a field
never ends up in the digest, or out of it, by default.
"""

from ezhpcy.types import ResolvedConfig


def test_anonymous_alias_digest_classifies_every_field() -> None:
    resolved = ResolvedConfig.model_validate(
        {
            "connection": {"user": "alice", "host": "login.example.com"},
            "scheduler": {"type": "LSF", "submission_mode": "batch"},
        }
    )
    participants = resolved.model_dump(
        mode="json", exclude=ResolvedConfig._DIGEST_EXCLUDE_FIELDS
    )

    assert {
        section: frozenset(fields) for section, fields in participants.items()
    } == ResolvedConfig._DIGEST_INCLUDE_FIELDS, (
        "A ResolvedConfig field is in neither ResolvedConfig._DIGEST_INCLUDE_FIELDS "
        "nor ResolvedConfig._DIGEST_EXCLUDE_FIELDS. Ask the maintainer which "
        "list it belongs in (see tests/human_reviewed/README.md)."
    )
