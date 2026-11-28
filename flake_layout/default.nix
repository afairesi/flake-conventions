{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = false;
  meta.description = "Discover package declarations and check the layout of a flake source tree.";
  postPatch = ''
    substituteInPlace main.py --replace-fail 'GIT = "git"' 'GIT = "${pkgs.lib.getExe pkgs.git}"'
  '';
  propagatedBuildInputs = [ inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.nix_syntax ];
  src = ./.;
}
