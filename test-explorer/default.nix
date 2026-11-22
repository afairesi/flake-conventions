{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Browse package test names and their diffs in a cached, expandable terminal tree.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.project_layout
    pkgs.python3Packages.tree-sitter-language-pack
    pkgs.shfmt
  ];
  src = ./.;
}
