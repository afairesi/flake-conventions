{ inputs, pkgs, ... }:
inputs.self.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Browse packages, test names, and their changes across Git submodules.";
  postPatch = ''
    substituteInPlace suite_readers.py --replace-fail 'SHFMT = "shfmt"' 'SHFMT = "${pkgs.lib.getExe pkgs.shfmt}"'
  '';
  propagatedBuildInputs = [
    inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.flake_layout
    pkgs.python3Packages.tree-sitter-language-pack
  ];
  src = ./.;
}
