{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = false;
  meta.description = "Discover package declarations and check the layout of a flake source tree.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.nix_syntax
    pkgs.git
  ];
  src = ./.;
}
