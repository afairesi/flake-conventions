{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Browse packages, test names, and their changes across Git submodules.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.flake_layout
    pkgs.python3Packages.tree-sitter-language-pack
    pkgs.shfmt
  ];
  src = ./.;
}
