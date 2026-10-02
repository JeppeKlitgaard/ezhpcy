from cyclopts import App

from ezhpcy.cli.config.edit import edit_cmd
from ezhpcy.cli.config.load import load_cmd

config_app = App(name="config", help="Manage the EzHPCy configuration file.")

config_app.command(
    edit_cmd, name="edit", help="Open the configuration file in an editor."
)
config_app.command(load_cmd, name="load", help="Load a packaged configuration preset.")
