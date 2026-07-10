from ezhpcy.patch import editable


class FakeDistribution:
    def __init__(self, direct_url: str | None) -> None:
        self.direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        if name != "direct_url.json":
            return None

        return self.direct_url


def test_is_editable_install_reads_direct_url(monkeypatch) -> None:
    monkeypatch.setattr(
        editable.metadata,
        "distribution",
        lambda package: FakeDistribution(
            '{"url": "file:///project", "dir_info": {"editable": true}}'
        ),
    )

    assert editable.is_editable_install()


def test_is_editable_install_is_false_for_standard_install(monkeypatch) -> None:
    monkeypatch.setattr(
        editable.metadata,
        "distribution",
        lambda package: FakeDistribution('{"url": "file:///project"}'),
    )

    assert not editable.is_editable_install()
