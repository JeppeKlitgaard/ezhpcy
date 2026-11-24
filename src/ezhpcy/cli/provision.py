lazy from pathlib import PurePosixPath
lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import (
    APPS_PANEL,
    ConfigArg,
    ConnectionOptions,
    LoadBuiltinManifestsOpt,
    ManifestOpt,
    OptionalProfileArg,
)
lazy from ezhpcy import api
lazy from ezhpcy.cli._console import error_console
lazy from ezhpcy.cli._errors import CliUsageError
lazy from ezhpcy.cli._messages import print_aborted, print_success, rich_text
lazy from ezhpcy.cli._resolve import (
    chosen_apps,
    login_from_cli,
    select_profile,
)
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.cli.utils.ssh import connect_or_exit


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
async def provision_cmd(
    profile: OptionalProfileArg = None,
    /,
    *,
    config: ConfigArg,
    connection_options: ConnectionOptions = ConnectionOptions(),
    manifest: ManifestOpt = None,
    load_builtin_manifests: LoadBuiltinManifestsOpt = None,
    app: Annotated[
        list[str] | None,
        Parameter(
            name="--app",
            # Escaped, as Rich would otherwise read `[@VERSION]` as markup.
            metavar=r"NAME\[@VERSION]",
            help=(
                "An app of the manifests to install on the host too, without "
                "running it: its newest version, or VERSION. Installed in the "
                "profile's apps root unless a shared app root has it, or in "
                "--shared-app-root. May be repeated."
            ),
            group=APPS_PANEL,
        ),
    ] = None,
    all_apps: Annotated[
        bool,
        Parameter(
            name="--all-apps",
            negative=(),
            help=(
                "Install every version of every app of the manifests, as --app "
                "would, such as to set up a shared app root. Links, which have "
                "nothing to install, are passed over."
            ),
            group=APPS_PANEL,
        ),
    ] = False,
    shared_app_root: Annotated[
        PurePosixPath | None,
        Parameter(
            name="--shared-app-root",
            metavar="PATH",
            help=(
                "Install the apps in this shared app root, for others to run them "
                "from (site.shared_app_roots). It must exist, of mode 755, 775, 750 "
                "or 770 (setgid kept), and what is installed takes its group and "
                "mode."
            ),
            group=APPS_PANEL,
        ),
    ] = None,
    yes: Annotated[
        bool,
        Parameter(
            name=["--yes", "-y"],
            help="Proceed with provisioning without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """
    Idempotently provision worker infrastructure on the remote HPC host, and the
    apps given.
    """
    if all_apps and app:
        raise CliUsageError(
            t"It installs every app, and {'--app':name} chooses some.",
            param_hint="--all-apps",
        )
    if shared_app_root is not None and not (app or all_apps):
        raise CliUsageError(
            t"It is where apps are installed, and none is chosen: give "
            t"{'--app':name} or {'--all-apps':name}.",
            param_hint="--shared-app-root",
        )
    if shared_app_root is not None and not shared_app_root.is_absolute():
        raise CliUsageError(
            t"It must be an absolute path on the host.",
            param_hint="--shared-app-root",
            value=str(shared_app_root),
        )
    apps = chosen_apps(
        config,
        manifest or [],
        app or [],
        load_builtins=load_builtin_manifests,
        every=all_apps,
    )
    if not all_apps and (
        links := [chosen for chosen in apps if isinstance(chosen, api.Link)]
    ):
        raise CliUsageError(
            t"{str(links[0]):name} is a link, which has nothing to install.",
            param_hint="--app",
        )
    installed = [chosen for chosen in apps if isinstance(chosen, api.App)]
    select_profile(config, profile)
    login, password = login_from_cli(config, connection_options, profile_name=profile)
    resolved = config.resolve_profile(profile)
    # Asked before connecting (see `ask`).
    target = f"{login.user}@{login.host}"
    names = [str(chosen) for chosen in installed]
    if not installed:
        where = t""
    elif shared_app_root is not None:
        where = t", and {names:name} in the shared app root {str(shared_app_root):path}"
    else:
        where = t", and {names:name} unless a shared app root has them"
    user_accepts = yes or ask(
        Confirm.ask,
        rich_text(
            t"This will provision or repair EzHPCy worker infrastructure on "
            t"{target:name}, under its {'$XDG_CACHE_HOME/ezhpcy':path}{where}. "
            t"Proceed?"
        ),
        console=error_console,
        default=True,
    )
    if not user_accepts:
        print_aborted(t"Provisioning cancelled.")
        raise SystemExit(1)

    async with connect_or_exit(
        login, password, connection_options, profile_name=profile
    ) as host:
        await host.provision()
        for chosen in installed:
            await host.provision_app(
                chosen,
                apps_root=shared_app_root or resolved.apps.root,
                shared_app_roots=(
                    ()
                    if shared_app_root is not None
                    else resolved.site.shared_app_roots
                ),
                shared=shared_app_root is not None,
            )
            print_success(t"App {str(chosen):name} is provisioned.")
    print_success(t"Remote provisioning completed.")
