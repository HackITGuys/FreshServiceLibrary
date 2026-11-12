{ pkgs, lib, config, inputs, ... }:

{
  packages = [
    pkgs.git
  ];

  # pydantic-settings reads .env itself; devenv doesn't need to export it.
  dotenv.disableHint = true;

  languages.python = {
    enable = true;
    package = pkgs.python314;
    venv.enable = true;
    uv = {
      enable = true;
      # `uv sync` on shell entry: installs the locked deps, the dev group and
      # this project in editable mode.
      sync.enable = true;
    };
  };

  git-hooks.hooks = {
    ruff.enable = true;
    ruff-format.enable = true;
  };

  enterShell = ''
    python --version
    uv --version
  '';

  enterTest = ''
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src tests
    uv run pytest
  '';
}
