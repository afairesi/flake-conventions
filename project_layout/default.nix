{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = false;
  meta.description = "Discover project packages and validate their source layout.";
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.nix_syntax
    pkgs.git
  ];
  src = ./.;
}
