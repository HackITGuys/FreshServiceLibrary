{ pkgs, lib, config, inputs, ... }:

{
  packages = [
    pkgs.git
  ];

  dotenv.disableHint = true;

  languages.python = {
    enable = true;
    version = "3.14";
    directory = "./";
    venv = {
      enable = true;
      requirements = ''
        click
        requests
        pydantic
        pydantic-settings
        markdown
        bs4
      '';
    };
  };

  git-hooks.hooks = {
    clippy.enable = false;
  };
  
  enterShell = ''
    pip install --upgrade pip
    git --version
    python --version
    pip install -e .
  '';

}
