"""
Every field of `Login` and of `JobConfig` is in either its
`_DIGEST_INCLUDE_FIELDS` or its `_DIGEST_EXCLUDE_FIELDS`.

The digest names anonymous tunnels. Whether a field changes a tunnel's identity,
and whether a secret could leak into the hash, is decided per field.

This test fails on any new field until someone makes that decision, so a field
never ends up in the digest, or out of it, by default.
"""

from ezhpcy.types import JobConfig, Login

_REMINDER = (
    "is in neither {model}._DIGEST_INCLUDE_FIELDS nor "
    "{model}._DIGEST_EXCLUDE_FIELDS. Ask the maintainer which list it belongs in "
    "(see tests/human_reviewed/README.md)."
)


def test_anonymous_alias_digest_classifies_every_login_field() -> None:
    login = Login(user="alice", host="login.example.com")
    participants = login.model_dump(
        mode="json", exclude=set(Login._DIGEST_EXCLUDE_FIELDS)
    )

    assert frozenset(participants) == Login._DIGEST_INCLUDE_FIELDS, (
        "A Login field " + _REMINDER.format(model="Login")
    )


def test_anonymous_alias_digest_classifies_every_job_field() -> None:
    job = JobConfig.model_validate(
        {"scheduler": {"type": "LSF", "submission_mode": "batch"}}
    )
    participants = job.model_dump(
        mode="json", exclude=set(JobConfig._DIGEST_EXCLUDE_FIELDS)
    )

    assert {
        section: frozenset(fields) for section, fields in participants.items()
    } == JobConfig._DIGEST_INCLUDE_FIELDS, "A JobConfig field " + _REMINDER.format(
        model="JobConfig"
    )
